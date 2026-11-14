#!/usr/bin/env bash
# Link the skills in this repo into ~/.claude/skills so Claude Code can use them everywhere.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)/.claude/skills"
DEST="$HOME/.claude/skills"
mkdir -p "$DEST"
for skill in "$SRC"/*/; do
  name="$(basename "$skill")"
  if [ -e "$DEST/$name" ] && [ ! -L "$DEST/$name" ]; then
    echo "skip $name: $DEST/$name exists and is not a symlink"; continue
  fi
  ln -sfn "$skill" "$DEST/$name"
  echo "linked $name -> $DEST/$name"
done
