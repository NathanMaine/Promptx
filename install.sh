#!/usr/bin/env bash
# Installs promptx and promptx-web into ~/bin.
#
# No dependencies to install — promptx is standard library only. This just
# copies two files and makes sure ~/bin is on your PATH.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${PROMPTX_BIN:-$HOME/bin}"

echo "==> checking python"
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. promptx needs Python 3.9 or newer."
    exit 1
fi
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "    python3 $PYV"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || {
    echo "ERROR: need Python 3.9+, found $PYV"
    exit 1
}

echo "==> installing to $DEST"
mkdir -p "$DEST"
install -m 755 "$SRC/main.py" "$DEST/promptx"
install -m 755 "$SRC/web.py"  "$DEST/promptx-web"
# Imported by both. Must sit next to them — Python looks in the script's own
# directory first, which is how the CLI finds it without a package install.
install -m 644 "$SRC/promptx_index.py" "$DEST/promptx_index.py"
echo "    promptx"
echo "    promptx-web"
echo "    promptx_index.py  (structural indexing — enables --scan)"

# ~/bin on PATH?
case ":$PATH:" in
    *":$DEST:"*)
        ON_PATH=yes ;;
    *)
        ON_PATH=no ;;
esac

if [ "$ON_PATH" = no ]; then
    case "${SHELL##*/}" in
        zsh)  RC="$HOME/.zshrc"  ;;
        bash) RC="$HOME/.bashrc" ;;
        *)    RC="" ;;
    esac
    LINE='export PATH="$HOME/bin:$PATH"'
    if [ -n "$RC" ] && ! grep -qF "$LINE" "$RC" 2>/dev/null; then
        printf '\n# added by promptx install.sh\n%s\n' "$LINE" >> "$RC"
        echo "==> added ~/bin to PATH in $RC"
        echo "    run:  source $RC"
    else
        echo "==> add this to your shell rc, then reload it:"
        echo "    $LINE"
    fi
fi

echo ""
echo "==> checking for an API key"
if [ -n "${OPENROUTER_API_KEY:-}" ]; then
    echo "    found OPENROUTER_API_KEY in the environment"
elif [ -f "$HOME/.local/share/opencode/auth.json" ] \
     && grep -q openrouter "$HOME/.local/share/opencode/auth.json" 2>/dev/null; then
    echo "    found an OpenRouter key in OpenCode's auth store — promptx will use it"
else
    echo "    none found. Either:"
    echo "      export OPENROUTER_API_KEY=sk-or-v1-...   (get one at openrouter.ai/keys)"
    echo "      or copy .env.example to .env and fill it in"
    echo "      or use  promptx --local  to run against your own hardware"
fi

echo ""
echo "Done. Try it:"
echo "    promptx --models"
echo "    promptx -c . --copy \"add retry logic to the api client\""
echo "    promptx-web"
