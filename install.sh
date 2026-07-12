#!/usr/bin/env bash
# Install the Claude Code usage status line.
#
#   - Copies statusline.py into your Claude config dir (~/.claude by default,
#     or $CLAUDE_CONFIG_DIR if you set it).
#   - Sets the `statusLine` key in settings.json — and ONLY that key. Your
#     model, hooks, permissions, and everything else are left untouched.
#   - Backs settings.json up first.
#
# Safe to re-run (this is how you update). Requires: python3.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SETTINGS="$CLAUDE_DIR/settings.json"

command -v python3 >/dev/null 2>&1 || { echo "✗ python3 not found (required)."; exit 1; }

mkdir -p "$CLAUDE_DIR"

echo "→ Installing statusline.py into $CLAUDE_DIR"
cp "$REPO_DIR/statusline.py" "$CLAUDE_DIR/statusline.py"
chmod +x "$CLAUDE_DIR/statusline.py"

# Ensure settings.json exists (start from an empty object if not).
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"

# Back it up before touching it.
BACKUP="$SETTINGS.bak.$(date +%Y%m%d%H%M%S)"
cp "$SETTINGS" "$BACKUP"
echo "→ Backed up settings.json to $BACKUP"

echo "→ Setting the statusLine key in settings.json"
CLAUDE_DIR="$CLAUDE_DIR" python3 - "$SETTINGS" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    with open(path) as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError
except Exception:
    print("  ! settings.json wasn't valid JSON; leaving it alone.")
    print("    Add the block manually (see README).")
    sys.exit(1)

if cfg.get("statusLine"):
    print(f"  (replacing an existing statusLine — old value saved in the backup)")

# $HOME expands at runtime, so this one line works on macOS and Linux alike.
cfg["statusLine"] = {
    "type": "command",
    "command": "python3 $HOME/.claude/statusline.py",
    "padding": 0,
}
# If they use a custom config dir, point at the real path.
cdir = os.environ.get("CLAUDE_DIR", "")
if cdir and os.path.abspath(cdir) != os.path.abspath(os.path.expanduser("~/.claude")):
    cfg["statusLine"]["command"] = f"python3 {os.path.join(cdir, 'statusline.py')}"

with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print("  statusLine set.")
PY

echo "✓ Done. Start a new Claude Code session to see the status line."
echo "  Requirements: python3 + curl, and you must be logged into Claude Code."
