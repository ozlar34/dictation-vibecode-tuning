#!/usr/bin/env python3
"""wispr-ab-test.py — VoiceInk vs WisprFlow head-to-head on 12 hard dictations.

Commands:
  init      Pull VoiceInk store data and write wispr-test-cases.json.
  sheet     Print the numbered speak-this prompts to use during recording.
  entry N   Read WisprFlow output for case N from stdin (paste, then Ctrl-D).
  analyze   Diff all outputs, classify failures, write wispr-report.md.

Workflow:
  1.  python3 wispr-ab-test.py init
  2.  python3 wispr-ab-test.py sheet     ← what to dictate, one by one
  3.  For each case N (1–12):
        dictate into WisprFlow → copy output → run:
        python3 wispr-ab-test.py entry N
        paste, Ctrl-D
  4.  python3 wispr-ab-test.py analyze
"""

import difflib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

STORE = os.path.expanduser(
    "~/Library/Application Support/com.prakashjoshipax.VoiceInk/default.store"
)
HERE = Path(__file__).parent.parent  # dictation-vibecode-tuning/
RESULTS_FILE = HERE / "wispr-test-cases.json"
REPORT_FILE = HERE / "wispr-report.md"

# ---------------------------------------------------------------------------
# Test case definitions — speak_this is the intended ground-truth dictation.
# pk: SwiftData Z_PK in ZTRANSCRIPTION.
# failure_category: STT | ENHANCEMENT | BOTH | NONE
# jargon_terms: tokens to track explicitly in the per-case verdict.
# ---------------------------------------------------------------------------
CASE_DEFS = [
    {
        "id": 1, "pk": 434,
        "label": "Hermes name + trailing word",
        "speak_this": (
            "Guide me through how to set things up on Hermes agent, "
            "whether it's through Telegram, through the desktop app, "
            "through the CLI, and so on, but mostly through the desktop app."
        ),
        "failure_category": "STT",
        "failure_detail": (
            "Parakeet heard 'Hermit's agent' for 'Hermes agent' and "
            "'desktop ad' for 'desktop app' at the tail. "
            "Enhancement passed both through unchanged."
        ),
        "jargon_terms": ["Hermes", "Telegram", "CLI", "desktop app"],
    },
    {
        "id": 2, "pk": 351,
        "label": "MCP acronym + commands→comments",
        "speak_this": (
            "I currently have too many skills, MCP servers, plugins and commands, "
            "and quite a few of them are not really used."
        ),
        "failure_category": "STT",
        "failure_detail": (
            "Parakeet spelled out 'M C P' instead of 'MCP' and heard "
            "'comments' for 'commands'. Enhancement fixed MCP but left 'comments'."
        ),
        "jargon_terms": ["MCP", "commands"],
    },
    {
        "id": 3, "pk": 352,
        "label": "bloat → bload (STT miss + enhancement miss)",
        "speak_this": (
            "I want to make sure that my files that get loaded in every session don't bloat."
        ),
        "failure_category": "STT",
        "failure_detail": (
            "Parakeet produced 'bload'; enhancement also kept 'bload' — "
            "a clean word substitution that neither layer caught."
        ),
        "jargon_terms": ["bloat"],
    },
    {
        "id": 4, "pk": 287,
        "label": "Gemma 4B model name garbled",
        "speak_this": (
            "It's a little slower than what I would like, and I also want to see "
            "what's available in the market. I'm especially interested in "
            "Gemma 4B modified versions of that one."
        ),
        "failure_category": "STT",
        "failure_detail": (
            "Parakeet produced 'Gemma for colon E4B'. "
            "Enhancement recovered to 'gemma 4b' — STT miss, enhancement save."
        ),
        "jargon_terms": ["Gemma", "4B"],
    },
    {
        "id": 5, "pk": 316,
        "label": "Long run-on → enhancement answered the prompt",
        "speak_this": (
            "Help me prepare for my MOS HR interview. I want to brainstorm ideas — "
            "if they ask me my top three skills or something along these lines, "
            "give me five to seven skills based on what you know about my profile "
            "and what's required by the MOS JD, and give me an example line I can "
            "use in the interview for each of them."
        ),
        "failure_category": "ENHANCEMENT",
        "failure_detail": (
            "Raw transcript was accurate. Enhancement completely rewrote the text "
            "as an actual HR answer (six bullet-point skills) instead of cleaning the dictation."
        ),
        "jargon_terms": ["MOS", "JD"],
    },
    {
        "id": 6, "pk": 170,
        "label": "Enhancement hallucinated phantom steps",
        "speak_this": (
            "Let's go with your suggestions. Plus I want to mention that I'm a very "
            "fast learner and I love learning and I spend a lot of my free time "
            "learning too, so I feel like I can get up to speed with the parts "
            "that I'm missing very quickly."
        ),
        "failure_category": "ENHANCEMENT",
        "failure_detail": (
            "Raw was accurate. Enhancement prepended '1. Add error handling\\n2. Write the tests' "
            "— hallucinated content from a different conversation context."
        ),
        "jargon_terms": [],
    },
    {
        "id": 7, "pk": 188,
        "label": "Model name garbled + enhancement truncated",
        "speak_this": (
            "I uninstalled Google Eloquent, but the two local models that it downloaded — "
            "Gemma 4 2B and Gemma 4 14B — are still in my system. "
            "Can you find them and delete them? They're taking up about eight or nine gigs of space."
        ),
        "failure_category": "BOTH",
        "failure_detail": (
            "Parakeet produced 'GEMA414B' for 'Gemma 4 14B'. "
            "Enhancement kept the bad token and also truncated the opening context sentence."
        ),
        "jargon_terms": ["Gemma", "4 14B", "Eloquent"],
    },
    {
        "id": 8, "pk": 236,
        "label": "Enhancement truncated opening sentence",
        "speak_this": (
            "I'm currently using a generic profile banner on my LinkedIn and I want "
            "to use something striking that will interest people going into my profile. "
            "So let's brainstorm what this could look like and then I will ask Gemini "
            "to generate it, so we need to come up with a prompt."
        ),
        "failure_category": "ENHANCEMENT",
        "failure_detail": (
            "Raw was accurate. Enhancement silently dropped the first sentence "
            "('I'm currently using a generic profile banner on my LinkedIn…'), "
            "losing the context needed for the AI to understand the task."
        ),
        "jargon_terms": ["LinkedIn", "Gemini"],
    },
    {
        "id": 9, "pk": 100,
        "label": "File path + camelCase verbalized",
        "speak_this": (
            "Open src slash components slash userProfile dot tsx "
            "and import useAuth from hooks."
        ),
        "failure_category": "STT",
        "failure_detail": (
            "Parakeet heard 'to use auth' instead of 'useAuth' and kept "
            "'user profile' (two words) instead of merging to 'userProfile'. "
            "Enhancement fixed useAuth but left the space in 'user profile'."
        ),
        "jargon_terms": ["userProfile", "useAuth"],
    },
    {
        "id": 10, "pk": 111,
        "label": "CLI flags verbalized (slash / dash dash)",
        "speak_this": (
            "Can you run slash clear, then show me the git status? "
            "Show me what dash dash help prints for the build command."
        ),
        "failure_category": "STT",
        "failure_detail": (
            "Raw verbalization was faithful. Enhancement converted 'dash dash help' "
            "to `--help` (good) but dropped 'slash' from 'slash clear', producing "
            "backtick-wrapped `clear` instead of `/clear`."
        ),
        "jargon_terms": ["/clear", "--help", "git status"],
    },
    {
        "id": 11, "pk": 332,
        "label": "Slash command + compound flag (REFERENCE — both layers worked)",
        "speak_this": (
            "What's the purpose of slash compact and how does it differ "
            "from slash session dash handoff?"
        ),
        "failure_category": "NONE",
        "failure_detail": (
            "Reference success case. Enhancement correctly produced "
            "'/compact' and '/session --handoff' from verbalized input."
        ),
        "jargon_terms": ["/compact", "/session", "--handoff"],
    },
    {
        "id": 12, "pk": 373,
        "label": "Disfluency cleanup (REFERENCE — enhancement worked)",
        "speak_this": (
            "My goal is for my system to auto-suggest improvements or edits "
            "on Obsidian Vault at the correct locations whenever some change happens "
            "or some mention in the Claude Code chat happens about a specific thing "
            "that would fit in the Obsidian Vault."
        ),
        "failure_category": "NONE",
        "failure_detail": (
            "Reference success case. Raw had 'some some' and 'or w some' (disfluencies). "
            "Enhancement cleaned both perfectly."
        ),
        "jargon_terms": ["Obsidian", "Claude Code"],
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_results():
    if not RESULTS_FILE.exists():
        sys.exit(f"Run 'init' first to create {RESULTS_FILE.name}")
    with open(RESULTS_FILE) as f:
        return json.load(f)


def save_results(data):
    with open(RESULTS_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Saved → {RESULTS_FILE}")


def token_accuracy(output: str, targets: list[str]) -> dict:
    """Check which jargon_terms appear correctly (case-insensitive) in output."""
    out_lower = output.lower()
    found, missing = [], []
    for term in targets:
        if term.lower() in out_lower:
            found.append(term)
        else:
            missing.append(term)
    return {"found": found, "missing": missing}


def word_diff(a: str, b: str) -> str:
    """Inline word-level diff: {+added} {-removed}."""
    a_words = a.split()
    b_words = b.split()
    matcher = difflib.SequenceMatcher(None, a_words, b_words)
    parts = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            parts.extend(a_words[i1:i2])
        elif tag == "replace":
            parts.extend(f"{{-{w}}}" for w in a_words[i1:i2])
            parts.extend(f"{{+{w}}}" for w in b_words[j1:j2])
        elif tag == "delete":
            parts.extend(f"{{-{w}}}" for w in a_words[i1:i2])
        elif tag == "insert":
            parts.extend(f"{{+{w}}}" for w in b_words[j1:j2])
    return " ".join(parts)


def wer(ref: str, hyp: str) -> float:
    """Simple word error rate (substitutions+deletions+insertions / ref words)."""
    ref_w = ref.lower().split()
    hyp_w = hyp.lower().split()
    if not ref_w:
        return 0.0
    m = difflib.SequenceMatcher(None, ref_w, hyp_w)
    edits = sum(
        max(i2 - i1, j2 - j1) if tag != "equal" else 0
        for tag, i1, i2, j1, j2 in m.get_opcodes()
    )
    return edits / len(ref_w)


def verdict(vi_raw_wer, vi_enh_wer, wispr_wer, category):
    """Single-line outcome for a test case."""
    vi_best = min(vi_raw_wer, vi_enh_wer)
    if wispr_wer < vi_best - 0.05:
        return "WISPR_WIN"
    if vi_best < wispr_wer - 0.05:
        return "VOICEINK_WIN"
    return "TIE"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init():
    conn = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)
    pks = [c["pk"] for c in CASE_DEFS]
    q = ("SELECT Z_PK, ZTEXT, ZENHANCEDTEXT FROM ZTRANSCRIPTION "
         f"WHERE Z_PK IN ({','.join('?'*len(pks))})")
    store_rows = {pk: (raw, enh) for pk, raw, enh in conn.execute(q, pks)}
    conn.close()

    cases = []
    for defn in CASE_DEFS:
        pk = defn["pk"]
        raw, enh = store_rows.get(pk, ("", ""))
        cases.append({
            "id": defn["id"],
            "pk": pk,
            "label": defn["label"],
            "speak_this": defn["speak_this"],
            "failure_category": defn["failure_category"],
            "failure_detail": defn["failure_detail"],
            "jargon_terms": defn["jargon_terms"],
            "voiceink_raw": raw or "",
            "voiceink_enhanced": enh or "",
            "wispr_output": "",
        })
    save_results(cases)
    print(f"Initialized {len(cases)} test cases.")
    print(f"Next: python3 wispr-ab-test.py sheet")


def cmd_sheet():
    data = load_results()
    print()
    print("=" * 70)
    print("  VOICEINK vs WISPRFLOW — TEST SHEET")
    print("  Dictate each prompt into WisprFlow, then run:")
    print("  python3 wispr-ab-test.py entry N   (paste output, Ctrl-D)")
    print("=" * 70)
    for case in data:
        done = "✓" if case["wispr_output"].strip() else " "
        print()
        print(f"[{done}] Case {case['id']:>2}  ({case['failure_category']})  {case['label']}")
        print(f"     Say: {case['speak_this']}")
    print()
    total = sum(1 for c in data if c["wispr_output"].strip())
    print(f"Progress: {total}/{len(data)} entries recorded.")
    print()


def cmd_entry(n_str):
    try:
        n = int(n_str)
    except ValueError:
        sys.exit("Usage: entry N  (N is the case id, 1–12)")
    data = load_results()
    case = next((c for c in data if c["id"] == n), None)
    if case is None:
        sys.exit(f"No case with id={n}")
    print(f"Case {n}: {case['label']}")
    print(f"Paste WisprFlow output, then Ctrl-D:")
    text = sys.stdin.read().strip()
    if not text:
        sys.exit("Empty — nothing saved.")
    case["wispr_output"] = text
    save_results(data)
    print(f"Saved WisprFlow output for case {n}.")


def cmd_analyze():
    data = load_results()
    missing = [c["id"] for c in data if not c["wispr_output"].strip()]
    if missing:
        print(f"Warning: cases {missing} have no WisprFlow output yet — they will be skipped.")

    lines = [
        "# VoiceInk vs WisprFlow — Head-to-Head Analysis",
        "",
        "| Layer failure | Count |",
        "|---|---|",
    ]
    # Count category distribution
    cats = {}
    for c in data:
        cats[c["failure_category"]] = cats.get(c["failure_category"], 0) + 1
    for cat, cnt in sorted(cats.items()):
        lines.append(f"| {cat} | {cnt} |")
    lines += ["", "---", ""]

    scoreboard = {"WISPR_WIN": 0, "VOICEINK_WIN": 0, "TIE": 0, "SKIP": 0}
    case_verdicts = []

    for case in data:
        speak = case["speak_this"]
        vi_raw = case["voiceink_raw"]
        vi_enh = case["voiceink_enhanced"]
        wispr = case["wispr_output"].strip()

        lines.append(f"## Case {case['id']}: {case['label']}")
        lines.append("")
        lines.append(f"**Failure category:** {case['failure_category']}  ")
        lines.append(f"**Detail:** {case['failure_detail']}")
        lines.append("")
        lines.append(f"**Speak this:** {speak}")
        lines.append("")

        # VoiceInk raw
        vi_raw_wer = wer(speak, vi_raw)
        lines.append(f"**VoiceInk RAW** (WER: {vi_raw_wer:.0%}):")
        lines.append(f"    {vi_raw}")
        if vi_raw != speak:
            lines.append(f"    diff: {word_diff(speak, vi_raw)}")
        lines.append("")

        # VoiceInk enhanced
        vi_enh_wer = wer(speak, vi_enh)
        lines.append(f"**VoiceInk ENHANCED** (WER: {vi_enh_wer:.0%}):")
        lines.append(f"    {vi_enh}")
        if vi_enh != speak:
            lines.append(f"    diff: {word_diff(speak, vi_enh)}")
        lines.append("")

        # WisprFlow
        if not wispr:
            lines.append("**WisprFlow:** *(not recorded)*")
            lines.append("")
            scoreboard["SKIP"] += 1
            case_verdicts.append(("SKIP", case))
            lines.append("---")
            lines.append("")
            continue

        wispr_wer = wer(speak, wispr)
        lines.append(f"**WisprFlow** (WER: {wispr_wer:.0%}):")
        lines.append(f"    {wispr}")
        if wispr != speak:
            lines.append(f"    diff: {word_diff(speak, wispr)}")
        lines.append("")

        # Jargon check
        if case["jargon_terms"]:
            vi_raw_jargon = token_accuracy(vi_raw, case["jargon_terms"])
            vi_enh_jargon = token_accuracy(vi_enh, case["jargon_terms"])
            wispr_jargon = token_accuracy(wispr, case["jargon_terms"])
            lines.append("**Jargon accuracy:**")
            lines.append(f"  VoiceInk raw:      found={vi_raw_jargon['found']} missing={vi_raw_jargon['missing']}")
            lines.append(f"  VoiceInk enhanced: found={vi_enh_jargon['found']} missing={vi_enh_jargon['missing']}")
            lines.append(f"  WisprFlow:         found={wispr_jargon['found']} missing={wispr_jargon['missing']}")
            lines.append("")

        # Verdict
        v = verdict(vi_raw_wer, vi_enh_wer, wispr_wer, case["failure_category"])
        scoreboard[v] += 1
        case_verdicts.append((v, case))
        emoji = {"WISPR_WIN": "W", "VOICEINK_WIN": "V", "TIE": "="}[v]
        lines.append(f"**Verdict: {v}** [{emoji}]")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Summary
    lines.insert(0, "")
    lines.insert(0, f"WisprFlow wins: **{scoreboard['WISPR_WIN']}**  |  "
                     f"VoiceInk wins: **{scoreboard['VOICEINK_WIN']}**  |  "
                     f"Ties: **{scoreboard['TIE']}**  |  "
                     f"Skipped: **{scoreboard['SKIP']}**")
    lines.insert(0, "")

    # Lever recommendation
    stt_wins = sum(1 for v, c in case_verdicts
                   if v == "WISPR_WIN" and c["failure_category"] in ("STT", "BOTH"))
    enh_wins = sum(1 for v, c in case_verdicts
                   if v == "WISPR_WIN" and c["failure_category"] == "ENHANCEMENT")
    stt_cases = sum(1 for c in data if c["failure_category"] in ("STT", "BOTH"))
    enh_cases = sum(1 for c in data if c["failure_category"] == "ENHANCEMENT")

    lines.append("## Lever recommendation")
    lines.append("")
    lines.append(f"STT failure cases: {stt_cases}  →  WisprFlow won {stt_wins}")
    lines.append(f"Enhancement failure cases: {enh_cases}  →  WisprFlow won {enh_wins}")
    lines.append("")
    if stt_wins > enh_wins and stt_wins >= 3:
        lines.append(
            "**Primary gap is in the STT layer (Parakeet).** "
            "Recommended lever: add jargon terms to VoiceInk's custom vocabulary "
            "(free, on-device, immediate). DeepSeek is largely irrelevant here."
        )
    elif enh_wins > stt_wins and enh_wins >= 2:
        lines.append(
            "**Primary gap is in the enhancement layer (Gemma).** "
            "Run `voiceink-model-ab.py` to A/B Gemma vs alternative models. "
            "Note: switching to DeepSeek ships your dictations off-device to a "
            "Chinese API — verify this trade-off is acceptable before proceeding."
        )
    elif stt_wins == 0 and enh_wins == 0:
        lines.append(
            "**VoiceInk matches or beats WisprFlow.** No compelling reason to switch. "
            "Focus on vocabulary tuning and prompt refinement within the existing stack."
        )
    else:
        lines.append(
            "**Mixed signal.** Review per-case verdicts for patterns. "
            "Check if WisprFlow wins cluster on specific jargon domains "
            "or enhancement failure modes."
        )

    report = "\n".join(lines)
    with open(REPORT_FILE, "w") as f:
        f.write(report)
    print(report)
    print(f"\nReport saved → {REPORT_FILE}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    cmd = args[0]
    if cmd == "init":
        cmd_init()
    elif cmd == "sheet":
        cmd_sheet()
    elif cmd == "entry":
        if len(args) < 2:
            sys.exit("Usage: entry N")
        cmd_entry(args[1])
    elif cmd == "analyze":
        cmd_analyze()
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: init | sheet | entry N | analyze")
        sys.exit(1)


if __name__ == "__main__":
    main()
