#!/usr/bin/env python3
"""voiceink-prompt-ab.py — controlled A/B of two enhancement PROMPTS at a fixed
model, against the live llama-server. Companion to voiceink-model-ab.py (which
holds the prompt fixed and varies the model); this holds the model fixed and
varies the prompt.

Read-only on the VoiceInk store. Talks to the OpenAI-compatible
/v1/chat/completions endpoint on the dedicated llama-server (:11435 by
default) — does not touch VoiceInk's live config, safe to run while VoiceInk
is in use.

Methodology:
  - Same fixed model both sides (the llama-server only has one loaded), temp 0.
  - Only the system prompt varies between "a" and "b".
  - Same fixed set of real raw-STT rows pulled from the SwiftData store.

Usage:  voiceink-prompt-ab.py PROMPT_A.txt PROMPT_B.txt

Config via env (all optional):
  VOICEINK_STORE      path to default.store
  VOICEINK_CASE_PKS   comma-separated ZTRANSCRIPTION Z_PK row ids to test
  VOICEINK_AB_OUT     output directory (default ./voiceink-prompt-ab-<today>)
  VOICEINK_LLM_URL    base URL of the enhancement server (default
                      http://127.0.0.1:11435)
"""
import datetime
import json
import os
import sys
import time
import urllib.request

LLM_URL = os.environ.get("VOICEINK_LLM_URL", "http://127.0.0.1:11435")
STORE = os.path.expanduser(
    os.environ.get(
        "VOICEINK_STORE",
        "~/Library/Application Support/com.prakashjoshipax.VoiceInk/default.store",
    )
)
# Reference set of 20 real raw-STT rows chosen to exercise: trailing questions,
# "like" as filler vs. meaningful, slash/dash/dot punctuation, filename casing
# (CLAUDE.md), model names, and word-repetition stumbles.
_DEFAULT_PKS = [128, 238, 322, 332, 438, 500, 627, 787, 895, 1073,
                1113, 1186, 1189, 1196, 1198, 1209, 1225, 1241, 1250, 1252]
CASE_PKS = [
    int(x) for x in os.environ["VOICEINK_CASE_PKS"].split(",")
] if os.environ.get("VOICEINK_CASE_PKS") else _DEFAULT_PKS


def cases():
    import sqlite3
    conn = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)
    q = ("SELECT Z_PK, ZTEXT FROM ZTRANSCRIPTION WHERE Z_PK IN (%s)"
         % ",".join("?" * len(CASE_PKS)))
    rows = {pk: txt for pk, txt in conn.execute(q, CASE_PKS)}
    conn.close()
    return [(pk, rows[pk]) for pk in CASE_PKS if pk in rows and rows[pk]]


def model_name():
    req = urllib.request.Request(f"{LLM_URL}/v1/models")
    data = json.loads(urllib.request.urlopen(req, timeout=30).read())
    return data["data"][0]["id"]


def chat(model, system, user):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{LLM_URL}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    t = time.perf_counter()
    r = json.loads(urllib.request.urlopen(req, timeout=300).read())
    wall = time.perf_counter() - t
    return r, wall


def run_variant(label, system, model, all_cases):
    out = []
    for pk, raw in all_cases:
        r, wall = chat(model, system, raw)
        text = r["choices"][0]["message"]["content"].strip()
        usage = r.get("usage", {})
        out.append({"pk": pk, "raw": raw, "text": text, "wall": wall,
                    "completion_tokens": usage.get("completion_tokens", 0)})
        print(f"  [{label}] PK {pk:>4}  {wall:5.2f}s", file=sys.stderr)
    return out


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    path_a, path_b = sys.argv[1], sys.argv[2]
    prompt_a = open(path_a).read().rstrip("\n")
    prompt_b = open(path_b).read().rstrip("\n")

    model = model_name()
    all_cases = cases()
    if not all_cases:
        sys.exit("No matching rows. Set VOICEINK_CASE_PKS to Z_PK ids that "
                 "exist in your store (see ZTRANSCRIPTION).")

    print(f"Model: {model} · {len(all_cases)} cases", file=sys.stderr)
    print(f"A: {path_a} ({len(prompt_a)} chars)", file=sys.stderr)
    print(f"B: {path_b} ({len(prompt_b)} chars)", file=sys.stderr)

    results_a = run_variant("A", prompt_a, model, all_cases)
    results_b = run_variant("B", prompt_b, model, all_cases)

    stamp = datetime.date.today().isoformat()
    outdir = os.path.expanduser(
        os.environ.get("VOICEINK_AB_OUT", f"./voiceink-prompt-ab-{stamp}"))
    os.makedirs(outdir, exist_ok=True)
    report_path = os.path.join(outdir, "report.md")

    lines = [f"# VoiceInk prompt A/B — {os.path.basename(path_a)} vs "
             f"{os.path.basename(path_b)}", "",
             f"Model: **{model}** (fixed) · temp 0 · {len(all_cases)} cases · "
             f"{stamp}", ""]
    by_pk_a = {r["pk"]: r for r in results_a}
    by_pk_b = {r["pk"]: r for r in results_b}
    for pk, raw in all_cases:
        lines.append(f"## Case PK {pk}")
        lines.append("")
        lines.append("**RAW:**")
        lines.append("    " + raw.replace("\n", "\n    "))
        lines.append("")
        lines.append(f"**A** ({by_pk_a[pk]['wall']:.2f}s):")
        lines.append("    " + by_pk_a[pk]["text"].replace("\n", "\n    "))
        lines.append("")
        lines.append(f"**B** ({by_pk_b[pk]['wall']:.2f}s):")
        lines.append("    " + by_pk_b[pk]["text"].replace("\n", "\n    "))
        lines.append("")
        if by_pk_a[pk]["text"] == by_pk_b[pk]["text"]:
            lines.append("_(identical output)_")
            lines.append("")
    with open(report_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\nReport: {report_path}")
    with open(os.path.join(outdir, "results.json"), "w") as f:
        json.dump({"a": results_a, "b": results_b, "model": model}, f, indent=2)


if __name__ == "__main__":
    main()
