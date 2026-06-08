#!/usr/bin/env bash
# extract-prompt.sh — refresh prompt/vibe-coding.md from VoiceInk's live config.
#
# The "Vibe Coding" prompt lives in VoiceInk's SwiftData store and is only
# editable in the GUI. After you tweak it there, run this to pull the current
# text back into the repo so the snapshot stays honest.
#
# Usage:  ./extract-prompt.sh [PROMPT_TITLE]   (default: "Vibe Coding")
set -euo pipefail

TITLE="${1:-Vibe Coding}"
DOMAIN="com.prakashjoshipax.VoiceInk"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/vibe-coding.md"

BODY="$(
python3 - "$DOMAIN" "$TITLE" <<'PY'
import json, plistlib, subprocess, sys
domain, title = sys.argv[1], sys.argv[2]
xml = subprocess.run(["defaults", "export", domain, "-"], capture_output=True).stdout
cp = plistlib.loads(xml)["customPrompts"]
if isinstance(cp, (bytes, bytearray)):
    cp = json.loads(cp.decode("utf-8"))
match = next((p for p in cp if p.get("title") == title), None)
if match is None:
    sys.exit(f"prompt {title!r} not found in {domain}")
print(match["promptText"].rstrip())
PY
)"

# Replace only the fenced code block, preserve the prose header above it.
python3 - "$OUT" "$BODY" <<'PY'
import re, sys
out, body = sys.argv[1], sys.argv[2]
text = open(out).read()
new_block = "```text\n" + body + "\n```\n"
text = re.sub(r"```text\n.*?\n```\n", new_block, text, count=1, flags=re.S)
open(out, "w").write(text)
print(f"refreshed {out}")
PY
