#!/usr/bin/env python3
"""voiceink-apply-prompt.py — ILLUSTRATIVE EXAMPLE, not part of the core kit.

Safely replaces VoiceInk's "Vibe Coding" enhancement prompt with new text read
from stdin (or a file). Useful when a review loop generates a complete new prompt
and you want to apply it without clicking through the GUI.

Why full-replace instead of an anchored insert: the caller supplies the complete
new prompt text, so there is no fragile ANCHOR/NEW_BULLET matching that drifts
out of sync as the prompt evolves. (This is the successor to the older
anchored-patch example, which was tied to one exact prompt version.)

SAFETY CONTRACT:
  - Refuses to run while VoiceInk is open (it rewrites its prefs on exit and would
    clobber the change). Quit VoiceInk first, relaunch after.
  - Backs up the *current* prompt text (timestamped) before writing — restore by
    pasting the backup text back in the GUI, or via
    `defaults write <domain> customPrompts -data <hex of the backed-up blob>`.
  - Verifies the write landed by re-reading the store, and exits non-zero if not.

Usage:
  echo "<new full prompt text>" | voiceink-apply-prompt.py
  voiceink-apply-prompt.py --file /path/to/new-prompt.txt
  voiceink-apply-prompt.py --print        # just print the current prompt, no write

Config via env:
  VOICEINK_PROMPT_TITLE       customPrompts entry to target  (default "Vibe Coding")
  VOICEINK_PROMPT_BACKUP_DIR  where to write the backup       (default ~/voiceink-prompt-backups)
"""
import argparse
import datetime
import json
import os
import plistlib
import subprocess
import sys

DOMAIN = "com.prakashjoshipax.VoiceInk"
TITLE = os.environ.get("VOICEINK_PROMPT_TITLE", "Vibe Coding")
BACKUP_DIR = os.path.expanduser(
    os.environ.get("VOICEINK_PROMPT_BACKUP_DIR", "~/voiceink-prompt-backups")
)


def running():
    return subprocess.run(["pgrep", "-x", "VoiceInk"],
                          capture_output=True).returncode == 0


def load_prompts():
    xml = subprocess.run(["defaults", "export", DOMAIN, "-"],
                         capture_output=True).stdout
    cp = plistlib.loads(xml)["customPrompts"]
    if isinstance(cp, bytes):
        cp = cp.decode("utf-8")
    return json.loads(cp)


def find_target(prompts):
    t = next((p for p in prompts if p.get("title") == TITLE), None)
    if t is None:
        sys.exit(f"prompt {TITLE!r} not found in {DOMAIN}.")
    return t


def main():
    ap = argparse.ArgumentParser(prog="voiceink-apply-prompt.py")
    ap.add_argument("--file", help="read new prompt text from this file")
    ap.add_argument("--print", action="store_true", dest="print_only",
                    help="print current prompt text and exit (no write)")
    args = ap.parse_args()

    prompts = load_prompts()
    target = find_target(prompts)
    current = target.get("promptText", "")

    if args.print_only:
        sys.stdout.write(current)
        return

    if running():
        sys.exit("VoiceInk is still running — quit it first, then re-run.")

    new_text = (open(args.file).read() if args.file else sys.stdin.read()).rstrip("\n")
    if not new_text.strip():
        sys.exit("Refusing to write an empty prompt.")
    if new_text == current:
        print("New prompt is identical to current — no change made.")
        return

    # backup current prompt text
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    bk = os.path.join(BACKUP_DIR, f"vibe-prompt-before-{stamp}.txt")
    with open(bk, "w") as f:
        f.write(current)

    # write
    target["promptText"] = new_text
    blob = json.dumps(prompts, ensure_ascii=False).encode("utf-8")
    subprocess.run(
        ["defaults", "write", DOMAIN, "customPrompts", "-data", blob.hex()],
        check=True)

    # verify
    got = find_target(load_prompts()).get("promptText", "")
    if got == new_text:
        print(f"Patched OK ({len(current)} → {len(new_text)} chars).")
        print(f"Backup: {bk}")
    else:
        sys.exit(f"WARNING: write did not verify! Backup at {bk}")


if __name__ == "__main__":
    main()
