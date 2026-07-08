#!/usr/bin/env python3
"""voiceink-review.py — read-only reader of VoiceInk's SwiftData store for the
permanent /voiceink-review loop. Pulls the RAW transcript (ZTEXT) and the
ENHANCED output (ZENHANCEDTEXT) side by side so the /voiceink-review command can
separate a Parakeet transcription miss (STT heard it wrong) from an
enhancement-prompt divergence (the LLM rewrote it).

Never writes the store. The only state it owns is the sentinel JSON below, which
tracks the timestamp of the last reviewed dictation ("since you last ran it").

Subcommands:
  json  [--days N]   unreviewed pairs since last ack, as JSON (what the command reads).
  show  [--days N]   same pairs, human-readable.
  status             counts: total / reviewed / unreviewed, and last-ack time.
  ack   [TS]         mark reviewed up to TS (core-data float). No arg = latest pair.

--days N caps the lookback to the last N days (use on a first run, when there is
no ack yet, to avoid dumping the entire history). When an ack exists, the review
window is always "newer than the last ack" — --days only tightens it further.

NOTE ON MODEL LABEL: VoiceInk's store column ZAIENHANCEMENTMODELNAME can be STALE
— it records the capital-O plist key while the actual Ollama call uses the
lowercase key. Don't trust the store's model column; the env VOICEINK_MODEL is
the label of record. Ground truth for what ran is the llama-server logs at
~/Library/Logs/com.user.voiceink-llm.{out,err} (Ollama retired 2026-06).

Config via env (all optional):
  VOICEINK_STORE           path to default.store
  VOICEINK_REVIEW_STATE    path to the sentinel JSON
  VOICEINK_PROMPT_SCOPE    ZPROMPTNAME to filter on   (default "Vibe Coding")
  VOICEINK_MODEL           label for the live enhancement model
  VOICEINK_PROMPT_VERSION  label for the prompt version
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

STORE = os.path.expanduser(
    os.environ.get(
        "VOICEINK_STORE",
        "~/Library/Application Support/com.prakashjoshipax.VoiceInk/default.store",
    )
)
SENTINEL = os.path.expanduser(
    os.environ.get("VOICEINK_REVIEW_STATE", "~/.voiceink-review.json")
)
PROMPT_SCOPE = os.environ.get("VOICEINK_PROMPT_SCOPE", "Vibe Coding")
LIVE_MODEL = os.environ.get("VOICEINK_MODEL", "gemma4:e4b")
PROMPT_VERSION = os.environ.get("VOICEINK_PROMPT_VERSION", "v3.6")
CORE_DATA_EPOCH = 978307200  # seconds between 1970-01-01 and 2001-01-01


def load_sentinel():
    if os.path.exists(SENTINEL):
        try:
            with open(SENTINEL) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "model": LIVE_MODEL,
        "prompt_version": PROMPT_VERSION,
        "last_ack_ts": 0.0,
        "last_ack_iso": None,
    }


def save_sentinel(d):
    os.makedirs(os.path.dirname(SENTINEL) or ".", exist_ok=True)
    tmp = SENTINEL + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, SENTINEL)


def connect_ro():
    if not os.path.exists(STORE):
        sys.stderr.write(f"voiceink-review: store not found at {STORE}\n")
        sys.exit(0)
    return sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)


def to_local(core_ts):
    return datetime.fromtimestamp(core_ts + CORE_DATA_EPOCH)


def effective_since(sentinel, days):
    """Floor of the review window as a core-data timestamp: newer than the last
    ack, and (if --days given) no older than N days ago."""
    since = sentinel.get("last_ack_ts", 0.0)
    if days is not None:
        cutoff_core = datetime.now().timestamp() - days * 86400 - CORE_DATA_EPOCH
        since = max(since, cutoff_core)
    return since


def query_pairs(since_ts):
    """Vibe-Coding enhancement pairs newer than since_ts (no upper bound)."""
    conn = connect_ro()
    try:
        rows = conn.execute(
            """
            SELECT ZTIMESTAMP, ZTEXT, ZENHANCEDTEXT, ZPOWERMODENAME
            FROM ZTRANSCRIPTION
            WHERE ZPROMPTNAME = ?
              AND ZENHANCEDTEXT IS NOT NULL
              AND ZTEXT IS NOT NULL
              AND ZTIMESTAMP > ?
            ORDER BY ZTIMESTAMP ASC
            """,
            (PROMPT_SCOPE, since_ts),
        ).fetchall()
    finally:
        conn.close()
    return rows


def cmd_json(days):
    s = load_sentinel()
    pairs = query_pairs(effective_since(s, days))
    out = {
        "prompt_scope": PROMPT_SCOPE,
        "model": LIVE_MODEL,
        "prompt_version": PROMPT_VERSION,
        "last_ack_ts": s.get("last_ack_ts", 0.0),
        "last_ack_iso": s.get("last_ack_iso"),
        "count": len(pairs),
        "pairs": [
            {
                "ts": ts,
                "when": to_local(ts).strftime("%Y-%m-%d %H:%M"),
                "raw": raw.strip(),
                "enhanced": enh.strip(),
                "mode": pmode or None,
            }
            for (ts, raw, enh, pmode) in pairs
        ],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_show(days):
    s = load_sentinel()
    pairs = query_pairs(effective_since(s, days))
    if not pairs:
        print(f"No unreviewed {PROMPT_SCOPE} dictations since last ack.")
        return
    print(
        f"# Unreviewed VoiceInk pairs — {len(pairs)} (live model: {LIVE_MODEL} "
        f"+ {PROMPT_VERSION}; store model column may be STALE, ignore it)\n"
    )
    for i, (ts, raw, enh, pmode) in enumerate(pairs, 1):
        when = to_local(ts).strftime("%Y-%m-%d %H:%M")
        print(f"## Pair {i} — {when}  (ts={ts:.6f}, mode={pmode or '-'})")
        print(f"RAW:      {raw.strip()}")
        print(f"ENHANCED: {enh.strip()}")
        print()
    print(f"(to mark reviewed: voiceink-review.py ack {pairs[-1][0]:.6f})")


def cmd_status():
    s = load_sentinel()
    total = len(query_pairs(0.0))
    unreviewed = len(query_pairs(s.get("last_ack_ts", 0.0)))
    print(f"live model: {LIVE_MODEL} + {PROMPT_VERSION}")
    print(f"total:      {total} Vibe Coding pairs in store")
    print(f"reviewed:   {total - unreviewed}")
    print(f"unreviewed: {unreviewed}")
    print(f"last ack:   {s.get('last_ack_iso') or '(never)'}")


def cmd_ack(arg):
    s = load_sentinel()
    if arg:
        ts = float(arg)
    else:
        pairs = query_pairs(s.get("last_ack_ts", 0.0))
        if not pairs:
            print("Nothing to ack.")
            return
        ts = pairs[-1][0]
    s["last_ack_ts"] = ts
    s["last_ack_iso"] = to_local(ts).strftime("%Y-%m-%d %H:%M:%S")
    s["model"] = LIVE_MODEL
    s["prompt_version"] = PROMPT_VERSION
    save_sentinel(s)
    print(f"Acked through {s['last_ack_iso']} (ts={ts:.6f}).")


def main():
    p = argparse.ArgumentParser(prog="voiceink-review.py")
    sub = p.add_subparsers(dest="cmd")
    for name in ("json", "show"):
        sp = sub.add_parser(name)
        sp.add_argument("--days", type=int, default=None)
    sub.add_parser("status")
    sp = sub.add_parser("ack")
    sp.add_argument("ts", nargs="?", default=None)
    args = p.parse_args()

    if args.cmd == "json":
        cmd_json(args.days)
    elif args.cmd == "show":
        cmd_show(args.days)
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "ack":
        cmd_ack(args.ts)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
