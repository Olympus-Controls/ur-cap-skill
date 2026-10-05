#!/bin/bash
# Make this checkout's urcapgen and its skill available everywhere, live (edits here apply at once):
#   ~/.local/bin/urcapgen        runs `python3 -m urcapgen` from this checkout
#   ~/.claude/skills/urcap       → urcapgen/template/.claude/skills/urcap (Claude Code, any directory)
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$HOME/.local/bin" "$HOME/.claude/skills"
cat > "$HOME/.local/bin/urcapgen" <<WRAP
#!/bin/sh
# installed by $here/scripts/install-local.sh
PYTHONPATH="$here\${PYTHONPATH:+:\$PYTHONPATH}" PYTHONDONTWRITEBYTECODE=1 exec python3 -m urcapgen "\$@"
WRAP
chmod 755 "$HOME/.local/bin/urcapgen"
ln -sfn "$here/urcapgen/template/.claude/skills/urcap" "$HOME/.claude/skills/urcap"
echo "urcapgen → $HOME/.local/bin/urcapgen"
echo "skill    → $HOME/.claude/skills/urcap"
