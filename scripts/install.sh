#!/usr/bin/env bash
# Install gslider for Claude Code: Python deps, the /gslider skill, and (optionally) the base deck.
# Usage: bash scripts/install.sh [--init-base]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SKILLS="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"

# A failed dependency install is reported at the end (non-zero exit); the skill link still runs.
DEPS_OK=1
echo "1/4 Python dependencies"
if ! PIP_OUT="$(python3 -m pip install --user --quiet -e "$REPO" 2>&1)"; then
  if grep -q "externally-managed-environment" <<<"$PIP_OUT"; then
    # Homebrew and some Linux Pythons refuse pip installs; --user keeps this in your home folder.
    echo "   this Python is externally managed; retrying with --break-system-packages (user install only)"
    if ! PIP_OUT="$(python3 -m pip install --user --quiet --break-system-packages -e "$REPO" 2>&1)"; then
      echo "$PIP_OUT" >&2
      DEPS_OK=0
    fi
  else
    echo "$PIP_OUT" >&2
    DEPS_OK=0
  fi
fi
[ "$DEPS_OK" = 1 ] && echo "   ok"

echo "2/4 /gslider skill -> $SKILLS/gslider"
mkdir -p "$SKILLS"
if [ -e "$SKILLS/gslider" ] && [ ! -L "$SKILLS/gslider" ]; then
  echo "   $SKILLS/gslider exists and is not a link; leaving it alone" >&2
else
  ln -sfn "$REPO/skills/gslider" "$SKILLS/gslider"
fi

echo "3/4 gws CLI"
if ! command -v gws >/dev/null; then
  echo "   gws not found: install the Google Workspace CLI and run 'gws auth login'" >&2
elif ! gws drive files list --params '{"pageSize":1}' >/dev/null 2>&1; then
  echo "   gws is not signed in: run 'gws auth login'" >&2
else
  echo "   ok"
fi

echo "4/4 base deck"
if [ -f "$HOME/.config/gslider/config.json" ] && grep -q base_id "$HOME/.config/gslider/config.json"; then
  echo "   already configured"
elif [ "${1:-}" = "--init-base" ] && [ "$DEPS_OK" = 0 ]; then
  echo "   skipped: the Python dependencies did not install" >&2
elif [ "${1:-}" = "--init-base" ]; then
  (cd "$REPO" && python3 -m gslider.cli init-base)
else
  echo "   not yet: run 'bash scripts/install.sh --init-base' (creates one blank deck in your Drive)"
fi

if [ "$DEPS_OK" = 0 ]; then
  echo "FAILED: the Python dependencies did not install (pip output above). The /gslider skill is linked;" >&2
  echo "fix the pip error, then run this script again." >&2
  exit 1
fi
echo "Done. In Claude Code, type /gslider. Set GSLIDER_HOME=$REPO if the repo is not at ~/Projects/HTML_Gslider."
