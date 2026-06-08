#!/usr/bin/env python3
"""voiceink-review.py — read-only reader of VoiceInk's SwiftData store for a
prompt-refinement loop. Pulls the RAW transcript (ZTEXT) and the ENHANCED output
(ZENHANCEDTEXT) side by side so you can separate a transcription miss (the STT
model heard it wrong) from an enhancement-prompt divergence (the LLM rewrote it).

Never writes the store. The only state it owns is a small sentinel JSON.

Subcommands:
  nudge        one-line nudge when unreviewed pairs exist; silent otherwise.
               After the window closes, one closeout line, then silent.
  show         dump all unreviewed pairs (RAW vs ENHANCED) for judging.
  status       counts: in-window / reviewed / unreviewed / days left.
  ack [TS]     mark reviewed up to TS (core-data float). No arg = latest in window.

Config via env (all optional — defaults reflect the reference experiment):
  VOICEINK_STORE           path to default.store
  VOICEINK_REVIEW_STATE    path to the sentinel JSON
  VOICEINK_PROMPT_SCOPE    ZPROMPTNAME to filter on        (default "Vibe Coding")
  VOICEINK_WINDOW_START    YYYY-MM-DD review-window start
  VOICEINK_WINDOW_END      YYYY-MM-DD review-window end (inclusive)
  VOICEINK_MODEL           label for the live enhancement model
  VOICEINK_PROMPT_VERSION  label for the prompt version

NOTE ON MODEL LABEL: VoiceInk's store column ZAIENHANCEMENTMODELNAME can be STALE
— it records the capital-O plist key while the actual Ollama call uses the
lowercase key. Don't trust the store's model column; the env VOICEINK_MODEL is
the label of record. Ground truth for what ran is ~/.ollama/logs/server.log.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

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
CORE_DATA_EPOCH = 978307200  # seconds between 1970-01-01 and 2001-01-01

# --- review-window defaults (the reference refinement week) ----------------
WINDOW_START = os.environ.get("VOICEINK_WINDOW_START", "2026-06-07")
WINDOW_END = os.environ.get("VOICEINK_WINDOW_END", "2026-06-14")  # inclusive
LIVE_MODEL = os.environ.get("VOICEINK_MODEL", "gemma4:e4b")
PROMPT_VERSION = os.environ.get("VOICEINK_PROMPT_VERSION", "v3.5")


def _day_bounds(date_str):
    """Local-midnight unix bounds for a YYYY-MM-DD date."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    start = d.timestamp()
    end = (d + timedelta(days=1)).timestamp()
    return start, end


def window_core_bounds():
    """Window start/end as core-data timestamps (what ZTIMESTAMP stores)."""
    start_unix, _ = _day_bounds(WINDOW_START)
    _, end_unix = _day_bounds(WINDOW_END)
    return start_unix - CORE_DATA_EPOCH, end_unix - CORE_DATA_EPOCH


def load_sentinel():
    if os.path.exists(SENTINEL):
        try:
            with open(SENTINEL) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "model": LIVE_MODEL,
        "prompt_version": PROMPT_VERSION,
        "last_ack_ts": 0.0,
        "last_ack_iso": None,
        "closeout_nudged": False,
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
        sys.exit(0)  # never break a hook
    return sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)


def to_local(core_ts):
    return datetime.fromtimestamp(core_ts + CORE_DATA_EPOCH)


def query_pairs(since_ts):
    """Enhancement pairs inside the window, newer than since_ts."""
    start_core, end_core = window_core_bounds()
    conn = connect_ro()
    try:
        rows = conn.execute(
            """
            SELECT ZTIMESTAMP, ZTEXT, ZENHANCEDTEXT, ZAIENHANCEMENTMODELNAME,
                   ZPOWERMODENAME
            FROM ZTRANSCRIPTION
            WHERE ZPROMPTNAME = ?
              AND ZENHANCEDTEXT IS NOT NULL
              AND ZTEXT IS NOT NULL
              AND ZTIMESTAMP >= ? AND ZTIMESTAMP < ?
              AND ZTIMESTAMP > ?
            ORDER BY ZTIMESTAMP ASC
            """,
            (PROMPT_SCOPE, start_core, end_core, since_ts),
        ).fetchall()
    finally:
        conn.close()
    return rows


def days_left():
    _, end_unix = _day_bounds(WINDOW_END)
    return max(0, int((end_unix - datetime.now().timestamp()) // 86400))


def past_window():
    _, end_unix = _day_bounds(WINDOW_END)
    return datetime.now().timestamp() >= end_unix


def cmd_nudge():
    s = load_sentinel()
    if past_window():
        if not s.get("closeout_nudged"):
            print(
                "🎙️ VoiceInk review window is over — run a final review pass, "
                "then tear down the loop."
            )
            s["closeout_nudged"] = True
            save_sentinel(s)
        return
    pairs = query_pairs(s.get("last_ack_ts", 0.0))
    if pairs:
        print(
            f"🎙️ VoiceInk: {len(pairs)} unreviewed dictation(s) on {LIVE_MODEL} "
            f"+ {PROMPT_VERSION} ({days_left()}d left in the review window)."
        )


def cmd_show():
    s = load_sentinel()
    pairs = query_pairs(s.get("last_ack_ts", 0.0))
    if not pairs:
        print(f"No unreviewed {PROMPT_SCOPE} dictations in the window.")
        return
    print(
        f"# Unreviewed VoiceInk pairs — {len(pairs)} (live model: {LIVE_MODEL} "
        f"+ {PROMPT_VERSION}; store model column may be STALE, ignore it)\n"
    )
    for i, (ts, raw, enh, store_model, pmode) in enumerate(pairs, 1):
        when = to_local(ts).strftime("%Y-%m-%d %H:%M")
        print(f"## Pair {i} — {when}  (ts={ts:.6f}, mode={pmode or '-'})")
        print(f"RAW:      {raw.strip()}")
        print(f"ENHANCED: {enh.strip()}")
        print()
    print(f"(to mark reviewed: voiceink-review.py ack {pairs[-1][0]:.6f})")


def cmd_status():
    s = load_sentinel()
    all_pairs = query_pairs(0.0)
    unreviewed = query_pairs(s.get("last_ack_ts", 0.0))
    print(f"window:     {WINDOW_START} → {WINDOW_END}  ({days_left()}d left)")
    print(f"live model: {LIVE_MODEL} + {PROMPT_VERSION}")
    print(f"in window:  {len(all_pairs)} pairs")
    print(f"reviewed:   {len(all_pairs) - len(unreviewed)}")
    print(f"unreviewed: {len(unreviewed)}")
    if s.get("last_ack_iso"):
        print(f"last ack:   {s['last_ack_iso']}")


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
    save_sentinel(s)
    print(f"Acked through {s['last_ack_iso']} (ts={ts:.6f}).")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "nudge"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    {
        "nudge": cmd_nudge,
        "show": cmd_show,
        "status": cmd_status,
        "ack": lambda: cmd_ack(arg),
    }.get(cmd, cmd_nudge)()


if __name__ == "__main__":
    main()
