#!/usr/bin/env python3
"""
voiceink-server-ab.py — empirical gate for VoiceInk llama-server CONFIG changes.

Sibling of voiceink-prompt-ab.py. That one varies the PROMPT (quality);
this one varies SERVER FLAGS (speed) — decode flags, cache types, and
speculative-decoding settings in launchagents/com.user.voiceink-llm.plist.

WHY THIS EXISTS (2026-07-24, the "ngram-mod" incident):
`--spec-type ngram-mod` measured ~4x faster and was shipped. It was worth 0%.
The benchmark repeated each transcript, and ngram-mod keeps a PERSISTENT
CROSS-REQUEST n-gram store — so re-sending the same text let it memorise its
own previous output. Real dictation is always novel, so the win evaporated.
It was reverted for `ngram-simple` (indexed from the current prompt only),
which measured 2.10x and holds up on first-exposure text.

A second trap surfaced in the same session: ambient GPU load on this machine
drifted ~2x between sequential runs, which aliased onto whichever config ran
first and inverted the rankings outright.

So: never ship a server flag on a single sequential timing run. Three rules,
all enforced below.

THE THREE RULES
  1. SHADOW SERVER. Candidates run on :11436. Production (:11435) is never
     touched, never stopped, never reconfigured. Dictation keeps working.
  2. FIRST EXPOSURE. Each transcript is seen exactly ONCE per config per round.
     This is what kills the persistent-store artifact.
  3. SAME-ROUND BASELINE. Configs are round-robined across rounds and every
     variant is scored RELATIVE TO THE BASELINE MEASURED IN ITS OWN ROUND.
     A slow ambient period hits baseline and candidate equally, so it cancels.

Speed is not the only bar: enhancement mostly copies the transcript, so correct
speculative decoding must be LOSSLESS. At temperature 0 the output must be
byte-identical to the non-speculative baseline. The equivalence table at the
bottom of the report is a hard gate — a fast config that changes output is a
regression, not a win.

SHIP RULE: a config ships only if the median same-round speedup is a real
improvement AND output is 100% byte-identical to baseline at temp 0.

CAUTION — THE CORPUS IS REUSED ACROSS RUNS.
The transcripts below are fixed so results compare across sessions, which is
safe for every prompt-indexed spec type (ngram-simple, ngram-map-*): their
state is rebuilt per request. It is NOT safe for any spec type with a
persistent cross-request store (ngram-mod, and any future draft-model cache
that survives requests). If you test one of those, replace CORPUS with text
the server has never seen, or you will re-run the 2026-07-24 mistake.

USAGE
  # Gate: is the currently-shipped config still beating no-spec?
  voiceink-server-ab.py

  # Sweep candidate flag sets (JSON: {"name": ["--flag","val", ...], ...})
  voiceink-server-ab.py --configs candidates.json --rounds 3

  # A "baseline" key is injected automatically if the file omits it.
"""
import argparse
import json
import plistlib
import signal
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = 11436  # shadow — production llama-server owns 11435
URL = f"http://127.0.0.1:{PORT}/v1/chat/completions"
BIN = Path.home() / ".local/bin/llama-server"
MODEL = Path.home() / "models/gemma-4-E4B-it-Q4_K_M.gguf"
PLIST = Path.home() / "Library/Preferences/com.prakashjoshipax.VoiceInk.plist"
PROD_PORT = 11435

# Server flags common to every config — mirror the production plist EXCEPT the
# flags under test. Keeping these identical is what makes the comparison honest.
BASE_FLAGS = [
    "--host", "127.0.0.1", "--port", str(PORT),
    "-ngl", "99", "--jinja", "--flash-attn", "on",
    "--ctx-size", "8192", "--parallel", "1", "--reasoning", "off",
    "--swa-full",
]

# The shipped config (2026-07-24): prompt-lookup speculative decoding.
# Default run = regression gate for exactly this.
SHIPPED = [
    "--spec-type", "ngram-simple",
    "--spec-ngram-simple-size-n", "3",
    "--spec-ngram-simple-size-m", "32",
]

DEFAULT_CONFIGS = {"baseline": [], "shipped_ngram_simple_n3_m32": SHIPPED}

# 8 realistic dictations, none a repeat of another. Length matters: latency is
# output-token-bound, and these are the ~30-60s utterances where seconds show up.
CORPUS = [
    "okay so I've been looking at the deployment pipeline this morning and there's a few things "
    "that bother me. um the first one is that we're running the integration tests twice, once in "
    "the PR check and once again on merge, which is just wasting like ten minutes every time. the "
    "second thing is the docker build isn't cached properly so every build pulls the base image "
    "again. and uh third, actually the more important one, we don't have any rollback story if a "
    "deploy goes bad, we just have to push a revert commit and wait. so I think we should tackle "
    "the rollback thing first because that's the actual risk.",

    "so the customer call went pretty well overall. they're happy with the the reporting features "
    "but they raised two concerns. first is that the export takes too long for big date ranges, "
    "they said anything over three months basically times out. second thing is they want to be "
    "able to share dashboards with people outside their org which we don't support right now. I "
    "told them we'd look into the export performance first since that seems like a bug more than "
    "a feature request. can you file tickets for both of those.",

    "right so I want to restructure how we're handling the the config loading because right now "
    "it's scattered across like four different files and nobody knows which one wins. um the "
    "proposal is we have a single config module that reads environment variables first, then the "
    "config file, then falls back to defaults, in that order. and everything else imports from "
    "there. the tricky part is the the test suite currently monkeypatches a bunch of these so "
    "we'd need to update maybe thirty test files. I think it's worth it though.",

    "quick update on the hiring process. we've got four candidates in the final round for the "
    "backend role. two of them are really strong on systems stuff, one is more of a product "
    "engineer, and the fourth one, sorry not the fourth, actually the third one has the best "
    "communication skills by far. I'm leaning towards making an offer to the first candidate but "
    "I want to get one more reference check done before we commit. should have a decision by "
    "thursday or friday at the latest.",

    "so the database migration is mostly done but I hit a snag with the foreign key constraints. "
    "the old schema had a nullable user id on the orders table and the new one makes it required, "
    "but there's about twelve thousand legacy rows where it's null. um I think we have three "
    "options, we can backfill them with a placeholder user, we can delete them since they're all "
    "from before twenty twenty three, or we can keep the column nullable and just enforce it in "
    "the application layer. I'd probably go with deleting them honestly.",

    "I've been reading through the the incident report from last week and I think we're drawing "
    "the wrong conclusion. the report says the root cause was the memory leak in the worker "
    "process but actually the leak had been there for months. what changed was that we increased "
    "the batch size which made the leak hit the limit way faster. so the real lesson isn't fix "
    "the leak, it's that we don't have any alerting on memory growth trends. we only alert when "
    "it's already at ninety five percent which is way too late to do anything about it.",

    "okay for the roadmap planning, I think we should focus on three themes next quarter. the "
    "first one is performance, specifically getting the page load under two seconds for the "
    "dashboard. second is the the integrations story, we keep losing deals because we don't have "
    "a salesforce connector. and third is just paying down some tech debt, particularly the "
    "authentication code which is genuinely scary at this point. if we can only do two of those "
    "I'd drop the tech debt one and push it to the quarter after.",

    "so I tried the new model for the summarization task and the results are mixed. it's "
    "definitely faster, maybe twice as fast as what we had before, and the summaries are more "
    "concise which is what we wanted. but it hallucinates numbers, like it'll say revenue grew "
    "fifteen percent when the source document says twelve. that happened in maybe three out of "
    "twenty test cases which is way too high for anything customer facing. so I don't think we "
    "can ship it as is, we'd need some kind of verification step on top.",
]


def load_prompt(title="Vibe Coding"):
    """Read the live enhancement prompt straight from VoiceInk's prefs.

    Reads the plist directly rather than via `defaults` — see Gotcha 2, the
    cfprefsd read-race returns stale values for a few seconds after a write.
    """
    with open(PLIST, "rb") as fh:
        prefs = plistlib.load(fh)
    custom = prefs["customPrompts"]
    if isinstance(custom, (bytes, bytearray)):
        custom = custom.decode("utf-8")
    prompts = {p["title"]: p["promptText"] for p in json.loads(custom)}
    if title not in prompts:
        sys.exit(f"prompt {title!r} not found; have: {sorted(prompts)}")
    return prompts[title]


def wait_ready(proc, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()
            return True
        except Exception:
            time.sleep(1)
    return False


def call(system, user):
    body = json.dumps({
        "model": "m",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.0, "seed": 42, "stream": False,
    }).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=240) as resp:
        data = json.loads(resp.read())
    return ((time.perf_counter() - t0) * 1000,
            data["choices"][0]["message"]["content"],
            data.get("usage", {}).get("completion_tokens", 0))


def run_config(extra, texts, prompt, tag):
    """Boot a shadow server with `extra` flags, run `texts` once each, tear down."""
    cmd = [str(BIN), "--model", str(MODEL)] + BASE_FLAGS + extra
    with open(f"/tmp/vi_server_ab_{tag}.log", "w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        try:
            if not wait_ready(proc):
                sys.exit(f"{tag}: server failed to start — see /tmp/vi_server_ab_{tag}.log")
            call(prompt, "hi")  # warm weights; not measured
            total_ms = total_tok = 0
            outs = []
            for txt in texts:
                dt, out, ntok = call(prompt, txt)
                total_ms += dt
                total_tok += ntok
                outs.append(out)
            return total_tok / (total_ms / 1000), total_ms, outs
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            time.sleep(2)  # let the GPU settle before the next config


def guard_production():
    """Refuse to run if something already owns the shadow port."""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()
    except Exception:
        return
    sys.exit(f"something is already listening on :{PORT} (the shadow port). "
             f"Stop it first — this script must not benchmark against a server "
             f"it did not configure.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--configs", type=Path,
                    help='JSON {"name": ["--flag","val"], ...}; "baseline" auto-added')
    ap.add_argument("--rounds", type=int, default=2,
                    help="round-robin passes (default 2; use 3 when the call is close)")
    ap.add_argument("--prompt", default="Vibe Coding", help="VoiceInk prompt title")
    args = ap.parse_args()

    if not BIN.exists():
        sys.exit(f"llama-server not found at {BIN}")
    if not MODEL.exists():
        sys.exit(f"model not found at {MODEL}")
    guard_production()

    configs = dict(DEFAULT_CONFIGS)
    if args.configs:
        configs = json.loads(args.configs.read_text())
        configs.setdefault("baseline", [])
        configs = {"baseline": configs.pop("baseline"), **configs}

    prompt = load_prompt(args.prompt)

    # Split the corpus into per-round slices so no config sees a transcript
    # twice within a run. With more rounds than slices the corpus wraps — fine
    # for prompt-indexed spec types, NOT for persistent-store ones (see header).
    per_round = max(1, len(CORPUS) // max(1, args.rounds))
    rounds = [CORPUS[i * per_round:(i + 1) * per_round] for i in range(args.rounds)]
    rounds = [r if r else CORPUS[:per_round] for r in rounds]

    print(f"shadow :{PORT} · production :{PROD_PORT} untouched · "
          f"prompt {args.prompt!r} · {args.rounds} rounds × {per_round} transcripts")

    tps = {c: [] for c in configs}
    outs = {c: [] for c in configs}
    for rnd, texts in enumerate(rounds):
        print(f"\n--- round {rnd + 1}/{len(rounds)} ---", flush=True)
        for name, extra in configs.items():
            speed, ms, out = run_config(extra, texts, prompt, f"{name}_r{rnd}")
            tps[name].append(speed)
            outs[name].append(out)
            print(f"   {name:32s} {speed:6.1f} tok/s  ({ms:7.0f} ms)", flush=True)

    print("\n" + "=" * 78)
    print("RELATIVE SPEED vs baseline, normalised WITHIN each round")
    print("=" * 78)
    width = max(len(c) for c in configs)
    speedups = {}
    for name in configs:
        rel = [tps[name][i] / tps["baseline"][i] for i in range(len(rounds))]
        speedups[name] = statistics.median(rel)
        print(f"  {name:{width}s} " + " ".join(f"{x:6.2f}x" for x in rel) +
              f"   median {speedups[name]:6.2f}x")

    spread = max(tps["baseline"]) / min(tps["baseline"]) if len(rounds) > 1 else 1.0
    print(f"\n  baseline absolute tok/s per round: " +
          ", ".join(f"{x:.1f}" for x in tps["baseline"]) +
          f"  (spread {spread:.2f}x)")
    if spread > 1.25:
        print("  NOTE: >1.25x ambient drift between rounds — this is exactly why "
              "raw\n        sequential timings are untrustworthy. Normalised "
              "figures above still hold.")

    print("\n" + "=" * 78)
    print("OUTPUT EQUIVALENCE vs baseline at temp 0 — must be 100%, this is a gate")
    print("=" * 78)
    lossy = []
    for name in configs:
        if name == "baseline":
            continue
        total = same = 0
        for r in range(len(rounds)):
            for i in range(len(rounds[r])):
                total += 1
                same += outs[name][r][i] == outs["baseline"][r][i]
        flag = "OK" if same == total else "LOSSY — DO NOT SHIP"
        if same != total:
            lossy.append(name)
        print(f"  {name:{width}s} {same:3d}/{total:<3d} identical   {flag}")

    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)
    for name in configs:
        if name == "baseline":
            continue
        if name in lossy:
            print(f"  {name:{width}s} REJECT — changes output at temp 0")
        elif speedups[name] >= 1.10:
            print(f"  {name:{width}s} SHIP-ABLE — {speedups[name]:.2f}x, lossless")
        else:
            print(f"  {name:{width}s} no real win — {speedups[name]:.2f}x")
    print("\nA win here is necessary, not sufficient: re-measure after editing the\n"
          "plist, since production flags may differ from BASE_FLAGS.")
    return 1 if lossy else 0


if __name__ == "__main__":
    sys.exit(main())
