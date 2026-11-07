#!/usr/bin/env python3
"""
voiceink-ab-test.py — empirical gate for VoiceInk enhancement-prompt changes.

WHY THIS EXISTS (2026-07-13, the "Claudeflare" incident):
A prompt rule was shipped on reasoning alone: 'cloud' -> 'Claude' near tooling words,
WITH a negative carve-out ("leave cloud alone for cloud server / iCloud / Cloudflare").
The carve-out did nothing. The 4B model globally rewrote cloud->Claude and emitted
"Claudeflare in front of the Claude deployment". Neither the author nor a reviewing
frontier model predicted this — but the local server answers it definitively in ~40s.
So: never ship a prompt rule on belief. Measure it.

WHAT IT DOES
Runs the CURRENT prompt and a PROPOSED prompt against the same cases, at temp 0, N reps,
straight to the llama-server at :11435 (the same backend VoiceInk uses).

  targets  — cases the change is SUPPOSED to fix. needles must APPEAR. (proposed > current)
  controls — cases that already work. needles must SURVIVE. (proposed == current == pass)

A case passes a rep only if EVERY needle in its list is present in the output. This is
deliberate: a single substring cannot detect deletion of text it does not name. On
2026-08-29 the live prompt dropped two of three sentences on ctl-trailing-question and
the gate scored it PASS, because the lone needle happened to sit inside the surviving
sentence. Control needles must therefore span every content clause of the raw dictation.

SHIP RULE: a change ships only if it improves targets AND breaks zero controls.
A control regression is disqualifying no matter how good the target numbers look.

AUTHORING CAUTIONS (learned the hard way)
  - Prefer the DICTIONARY (deterministic literal substitution) over a prompt rule for any
    enumerable substitution. Prompt rules are for genuine contextual judgment only.
  - Any rule with a negative carve-out ("...but leave X alone") is GUILTY UNTIL TESTED.
    Small models follow the imperative and drop the exception. Write controls that
    specifically attack the carve-out — that is the whole point of the controls list.

USAGE
  # A/B a proposed prompt against what's live now:
  voiceink-ab-test.py --proposed new_prompt.txt [--targets targets.json] [--reps 3]

  # Just check the live prompt against the standing regression corpus:
  voiceink-ab-test.py --check-only

targets.json format (same shape as the "targets" key in scripts/regression-cases.json):
  [ {"id": "...", "raw": "<real RAW dictation>", "needles": ["<substring>", ...]} ]
  (a scalar "needle" is still accepted for one-needle cases)
Targets MUST be real RAW dictations pulled from the review window — never invented text.
"""
import argparse, json, plistlib, subprocess, sys, urllib.request
from pathlib import Path

URL = "http://127.0.0.1:11435/v1/chat/completions"
MODEL = "gemma-4-E4B-it-Q4_K_M.gguf"
CORPUS = Path(__file__).with_name("regression-cases.json")
PREFS_DOMAIN = "com.prakashjoshipax.VoiceInk"
PROMPT_TITLE = "Vibe Coding"


def live_prompt() -> str:
    """The prompt VoiceInk is using right now, read from its prefs (same reader as
    prompt/extract-prompt.sh). Read-only."""
    xml = subprocess.run(["defaults", "export", PREFS_DOMAIN, "-"],
                         capture_output=True).stdout
    try:
        cp = plistlib.loads(xml)["customPrompts"]
        if isinstance(cp, (bytes, bytearray)):
            cp = json.loads(cp.decode("utf-8"))
    except (ValueError, KeyError) as e:
        sys.exit(f"could not read VoiceInk prompts from {PREFS_DOMAIN}: {e!r}")
    txt = next((p["promptText"] for p in cp if p.get("title") == PROMPT_TITLE), None)
    if txt is None:
        sys.exit(f"prompt {PROMPT_TITLE!r} not found in {PREFS_DOMAIN}")
    return txt.strip()


def enhance(system_prompt: str, raw: str) -> str:
    body = json.dumps({
        "model": MODEL, "temperature": 0.0,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": raw}],
    }).encode()
    req = urllib.request.Request(URL, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as f:
        return json.load(f)["choices"][0]["message"]["content"].strip()


def needles(c: dict) -> list:
    """Cases carry a list of needles, ALL of which must survive. A single-needle
    case may still use the legacy scalar "needle" key (external --targets files do)."""
    return c["needles"] if "needles" in c else [c["needle"]]


def score(prompt: str, cases: list, reps: int) -> dict:
    out = {}
    for c in cases:
        ns = needles(c)
        outs = [enhance(prompt, c["raw"]) for _ in range(reps)]
        hits = sum(1 for o in outs if all(n.lower() in o.lower() for n in ns))
        missed = sorted({n for o in outs for n in ns if n.lower() not in o.lower()})
        out[c["id"]] = {"hits": hits, "reps": reps, "sample": outs[0], "missed": missed}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proposed", help="file containing the proposed full prompt")
    ap.add_argument("--targets", help="JSON file of target cases the change must fix")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--corpus", default=str(CORPUS),
                    help="regression corpus JSON (default: regression-cases.json beside this script)")
    ap.add_argument("--check-only", action="store_true",
                    help="run the live prompt against the controls; no A/B")
    a = ap.parse_args()

    corpus = json.load(open(a.corpus))
    controls = corpus["controls"]
    targets = json.load(open(a.targets)) if a.targets else corpus.get("targets", [])

    cur = live_prompt()

    if a.check_only:
        res = score(cur, controls, a.reps)
        bad = [k for k, v in res.items() if v["hits"] < v["reps"]]
        print(f"{'CONTROL':28} {'LIVE':>7}")
        for c in controls:
            v = res[c["id"]]
            ok = v["hits"] == v["reps"]
            print(f"{c['id']:28} {v['hits']}/{v['reps']:<5} {'ok' if ok else 'FAIL'}")
            if not ok:
                print(f"{'':28} missing: {v['missed']}")
                print(f"{'':28} out: {v['sample']}")
        sys.exit(1 if bad else 0)

    if not a.proposed:
        sys.exit("--proposed is required (or use --check-only)")
    new = open(a.proposed).read().strip()
    if new == cur:
        sys.exit("proposed prompt is identical to the live prompt — nothing to test")

    tgt_cur, tgt_new = score(cur, targets, a.reps), score(new, targets, a.reps)
    ctl_cur, ctl_new = score(cur, controls, a.reps), score(new, controls, a.reps)

    print(f"\n{'TARGET (must improve)':32} {'current':>9} {'proposed':>9}")
    improved = regressed_t = 0
    for c in targets:
        a_, b_ = tgt_cur[c["id"]], tgt_new[c["id"]]
        mark = "+" if b_["hits"] > a_["hits"] else ("-" if b_["hits"] < a_["hits"] else " ")
        improved += b_["hits"] > a_["hits"]
        regressed_t += b_["hits"] < a_["hits"]
        print(f"{mark} {c['id']:30} {a_['hits']}/{a_['reps']:<7} {b_['hits']}/{b_['reps']}")

    print(f"\n{'CONTROL (must not break)':32} {'current':>9} {'proposed':>9}")
    broken = []
    for c in controls:
        a_, b_ = ctl_cur[c["id"]], ctl_new[c["id"]]
        ok = b_["hits"] >= a_["hits"]
        if not ok:
            broken.append(c)
        print(f"{'  ' if ok else 'X '}{c['id']:30} {a_['hits']}/{a_['reps']:<7} {b_['hits']}/{b_['reps']}")

    if broken:
        print("\n--- BROKEN CONTROL OUTPUT (proposed) ---")
        for c in broken:
            print(f"\n[{c['id']}] {c.get('why','')}")
            print(f"  RAW: {c['raw']}")
            print(f"  OUT: {ctl_new[c['id']]['sample']}")
            print(f"  MISSING NEEDLES: {ctl_new[c['id']]['missed']}")

    print("\n" + "=" * 60)
    if broken:
        print(f"VERDICT: DO NOT SHIP — {len(broken)} control(s) regressed.")
        print("A control regression disqualifies the change regardless of target gains.")
        sys.exit(1)
    if regressed_t:
        print(f"VERDICT: DO NOT SHIP — {regressed_t} target(s) got worse.")
        sys.exit(1)
    if not improved:
        print("VERDICT: NO EFFECT — no target improved. The rule is dead weight; drop it.")
        sys.exit(2)
    print(f"VERDICT: SHIP — {improved} target(s) improved, 0 controls broken.")
    sys.exit(0)


if __name__ == "__main__":
    main()
