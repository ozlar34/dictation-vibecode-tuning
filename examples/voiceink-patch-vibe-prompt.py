#!/usr/bin/env python3
"""
voiceink-patch-vibe-prompt.py — ILLUSTRATIVE EXAMPLE, not part of the core kit.

Shows how to patch VoiceInk's "Vibe Coding" enhancement prompt programmatically
(via `defaults`), rather than clicking through the GUI. The specific edit here
tightened the Lists rule so list items keep their full prose — it fixed the
clause-drop where "1. Can you run slash clear, then show me the git status?"
collapsed to a bare "/clear".

Treat this as a pattern to adapt, not a script to run as-is: the ANCHOR/NEW_BULLET
strings are tied to one exact version of the prompt. It is idempotent (safe to
re-run) and backs up the pre-edit prompt text first.

CAVEAT: VoiceInk MUST be quit first — it rewrites its prefs on exit and would
clobber the change. The script waits up to ~10s for it to quit, then aborts if
it's still running.

Backup goes to ./vibe-prompt-before-<today>.txt unless VOICEINK_PATCH_BACKUP_DIR
is set.
"""
import json
import plistlib
import subprocess
import sys
import time
import os
import datetime

DOMAIN = "com.prakashjoshipax.VoiceInk"
TITLE = "Vibe Coding"
MARKER = "never reduce an item to an embedded command"  # idempotency probe
ANCHOR = "Outside these cases, do not invent list formatting."
NEW_BULLET = (
    "- Each list item keeps its full sentence or question intact (minus fillers) "
    "— never reduce an item to an embedded command, flag, or path. Substitute "
    "spoken punctuation in place but keep the surrounding prose. (\"Can you run "
    "slash clear, then show me the git status?\" becomes \"Can you run /clear, "
    "then show me the git status?\" — never just \"/clear\".)"
)


def running():
    return subprocess.run(["pgrep", "-x", "VoiceInk"],
                          capture_output=True).returncode == 0


def main():
    for _ in range(20):
        if not running():
            break
        time.sleep(0.5)
    if running():
        sys.exit("VoiceInk is still running — quit it first, then re-run.")

    xml = subprocess.run(["defaults", "export", DOMAIN, "-"],
                         capture_output=True).stdout
    cp = plistlib.loads(xml)["customPrompts"]
    if isinstance(cp, bytes):
        cp = cp.decode("utf-8")
    prompts = json.loads(cp)

    target = next((p for p in prompts if p.get("title") == TITLE), None)
    if target is None:
        sys.exit(f"prompt {TITLE!r} not found in {DOMAIN}.")
    txt = target.get("promptText", "")
    if MARKER in txt:
        print("Already patched — no change made.")
        return
    if ANCHOR not in txt:
        sys.exit("Anchor line not found; prompt structure changed — aborting "
                 "rather than guessing where to insert.")

    stamp = datetime.date.today().isoformat()
    bk_dir = os.path.expanduser(
        os.environ.get("VOICEINK_PATCH_BACKUP_DIR", "."))
    bk = os.path.join(bk_dir, f"vibe-prompt-before-{stamp}.txt")
    os.makedirs(os.path.dirname(bk) or ".", exist_ok=True)
    with open(bk, "w") as f:
        f.write(txt)

    target["promptText"] = txt.replace(ANCHOR, NEW_BULLET + "\n" + ANCHOR)
    blob = json.dumps(prompts, ensure_ascii=False).encode("utf-8")
    subprocess.run(
        ["defaults", "write", DOMAIN, "customPrompts", "-data", blob.hex()],
        check=True)

    # verify the write landed
    xml2 = subprocess.run(["defaults", "export", DOMAIN, "-"],
                          capture_output=True).stdout
    cp2 = plistlib.loads(xml2)["customPrompts"]
    if isinstance(cp2, bytes):
        cp2 = cp2.decode("utf-8")
    got = next(p for p in json.loads(cp2) if p["title"] == TITLE)["promptText"]
    print("Patched OK." if MARKER in got else "WARNING: write did not verify!")
    print(f"Backup: {bk}")


if __name__ == "__main__":
    main()
