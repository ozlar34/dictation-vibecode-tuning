#!/usr/bin/env python3
"""voiceink-model-ab.py — controlled A/B of enhancement models at the Ollama
layer. Replicates the "Vibe Coding" enhancement (live prompt pulled from
VoiceInk's config) over a fixed set of real raw-STT rows, measuring cold-load vs
warm latency + tokens/sec per model and dumping side-by-side outputs for
accuracy judging.

Read-only on the VoiceInk store. Does NOT touch VoiceInk's live config — it runs
entirely against Ollama's HTTP API, so it's safe to run while VoiceInk is in use.

Methodology:
  - system = the "Vibe Coding" promptText, live from `defaults export` (so the
    only variable across models is the model itself — no per-call window-OCR
    context, no prompt drift).
  - temp 0, think:false (gemma4 are thinking models; without think:false they
    stall in the thought channel and return empty content).
  - Per model: `ollama stop` first, so case 1 carries the COLD model load
    (reported via Ollama's own load_duration); cases 2..N run warm.

Usage:  voiceink-model-ab.py [model1 model2 ...]   (default: gemma4:e4b gemma4:e2b)

Config via env (all optional):
  VOICEINK_STORE      path to default.store
  VOICEINK_CASE_PKS   comma-separated ZTRANSCRIPTION Z_PK row ids to test.
                      Replace the default set with your own rows. If unset and
                      no default applies to your store, the run will be empty.
  VOICEINK_AB_OUT     output directory (default ./voiceink-ab-<today>)
  VOICEINK_PROMPT_SCOPE  prompt title to pull   (default "Vibe Coding")
"""
import datetime
import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://localhost:11434"
STORE = os.path.expanduser(
    os.environ.get(
        "VOICEINK_STORE",
        "~/Library/Application Support/com.prakashjoshipax.VoiceInk/default.store",
    )
)
PROMPT_SCOPE = os.environ.get("VOICEINK_PROMPT_SCOPE", "Vibe Coding")
# Reference set of 20 real raw-STT rows by SwiftData Z_PK. These are specific to
# the store they were captured from — override with your own via VOICEINK_CASE_PKS.
_DEFAULT_PKS = [39, 88, 92, 100, 103, 104, 106, 111, 128, 131,
                133, 135, 137, 142, 156, 173, 176, 179, 180, 182]
CASE_PKS = [
    int(x) for x in os.environ["VOICEINK_CASE_PKS"].split(",")
] if os.environ.get("VOICEINK_CASE_PKS") else _DEFAULT_PKS
MODELS = sys.argv[1:] or ["gemma4:e4b", "gemma4:e2b"]
WARM = "10m"  # keep model resident across the case batch


def vibe_prompt():
    xml = subprocess.run(
        ["defaults", "export", "com.prakashjoshipax.VoiceInk", "-"],
        capture_output=True).stdout
    cp = plistlib.loads(xml)["customPrompts"]
    if isinstance(cp, bytes):
        cp = cp.decode("utf-8")
    prompts = json.loads(cp)
    return next(p["promptText"] for p in prompts if p.get("title") == PROMPT_SCOPE)


def cases():
    import sqlite3
    conn = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)
    q = ("SELECT Z_PK, ZTEXT FROM ZTRANSCRIPTION WHERE Z_PK IN (%s)"
         % ",".join("?" * len(CASE_PKS)))
    rows = {pk: txt for pk, txt in conn.execute(q, CASE_PKS)}
    conn.close()
    return [(pk, rows[pk]) for pk in CASE_PKS if pk in rows]


def chat(model, system, user):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False, "think": False, "keep_alive": WARM,
        "options": {"temperature": 0},
    }).encode("utf-8")
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t = time.perf_counter()
    r = json.loads(urllib.request.urlopen(req, timeout=300).read())
    wall = time.perf_counter() - t
    return r, wall


def unload(model):
    subprocess.run(["ollama", "stop", model], capture_output=True)
    time.sleep(1)


def run_model(model, system, all_cases):
    print(f"  unloading {model} for cold-start measurement...", file=sys.stderr)
    unload(model)
    out = []
    for i, (pk, raw) in enumerate(all_cases):
        r, wall = chat(model, system, raw)
        load_ms = r.get("load_duration", 0) / 1e6
        eval_n = r.get("eval_count", 0)
        eval_ms = r.get("eval_duration", 1) / 1e6
        toks = eval_n / (eval_ms / 1000) if eval_ms else 0
        out.append({
            "pk": pk, "raw": raw, "text": r["message"]["content"].strip(),
            "wall": wall, "load_ms": load_ms, "eval_n": eval_n,
            "eval_ms": eval_ms, "toks": toks, "cold": i == 0,
        })
        tag = "COLD" if i == 0 else "warm"
        print(f"    PK {pk:>3} [{tag}] wall={wall:5.2f}s "
              f"load={load_ms/1000:5.2f}s gen={eval_ms/1000:4.2f}s "
              f"{toks:5.1f} tok/s", file=sys.stderr)
    return out


def main():
    system = vibe_prompt()
    all_cases = cases()
    if not all_cases:
        sys.exit("No matching rows. Set VOICEINK_CASE_PKS to Z_PK ids that exist "
                 "in your store (see ZTRANSCRIPTION).")
    print(f"Prompt: {PROMPT_SCOPE} ({len(system)} chars) · {len(all_cases)} cases "
          f"· models: {', '.join(MODELS)}", file=sys.stderr)
    results = {m: run_model(m, system, all_cases) for m in MODELS}

    stamp = datetime.date.today().isoformat()
    outdir = os.path.expanduser(
        os.environ.get("VOICEINK_AB_OUT", f"./voiceink-ab-{stamp}"))
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "report.md")

    lines = [f"# VoiceInk model A/B — {' vs '.join(MODELS)}", "",
             f"Prompt: **{PROMPT_SCOPE}** (live from defaults) · temp 0 · "
             f"think:false · {len(all_cases)} cases · {stamp}", ""]
    # per-case side by side
    by_pk = {m: {r["pk"]: r for r in results[m]} for m in MODELS}
    for pk, _ in all_cases:
        lines.append(f"## Case PK {pk}")
        lines.append("")
        lines.append("**RAW:**")
        lines.append("    " + by_pk[MODELS[0]][pk]["raw"].replace("\n", "\n    "))
        lines.append("")
        for m in MODELS:
            r = by_pk[m][pk]
            tag = " COLD-load" if r["cold"] else ""
            lines.append(f"**{m}**  ({r['eval_ms']/1000:.2f}s gen, "
                         f"{r['toks']:.0f} tok/s{tag}):")
            lines.append("    " + r["text"].replace("\n", "\n    "))
            lines.append("")
    # latency table
    lines.append("## Latency")
    lines.append("")
    lines.append("| model | cold-load (1st) | warm gen avg | warm tok/s avg |")
    lines.append("|---|---|---|---|")
    for m in MODELS:
        rs = results[m]
        cold = rs[0]["load_ms"] / 1000
        warm = [r for r in rs if not r["cold"]]
        gen_avg = sum(r["eval_ms"] for r in warm) / len(warm) / 1000
        tok_avg = sum(r["toks"] for r in warm) / len(warm)
        lines.append(f"| {m} | {cold:.2f}s | {gen_avg:.2f}s | {tok_avg:.0f} |")
    lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    print(f"\nReport: {path}")
    # also drop raw json for downstream scoring
    with open(os.path.join(outdir, "results.json"), "w") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
