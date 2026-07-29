#!/usr/bin/env bash
# Installs the pre-commit secret scanner into this clone.
#
# Git hooks are not versioned, so every clone has to run this once.
#
#   bash scripts/install-hooks.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS="$(git -C "$ROOT" rev-parse --git-path hooks)"

mkdir -p "$HOOKS"
install -m 755 "$ROOT/scripts/pre-commit" "$HOOKS/pre-commit"

echo "installed: $HOOKS/pre-commit"
echo ""
echo "It blocks commits containing anything shaped like a live API key,"
echo "including keys pasted into tracked source — which .gitignore cannot catch."
echo "Bypass with: git commit --no-verify"
