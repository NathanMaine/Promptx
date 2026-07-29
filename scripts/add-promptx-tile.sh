#!/bin/bash
# Adds the promptx tile to the NAS dashboard at :8888.
#
# The dashboard HTML lives INSIDE the nginx container, not on the host
# filesystem, so editing /volume1/@home/Natron/nas-home.html has no effect on
# what :8888 serves. This reaches into the container instead.
#
# Run:  sudo bash /volume1/Projects/promptx/add-promptx-tile.sh
#
# Idempotent — re-running it does nothing if the tile is already there.

set -euo pipefail

if [ "$(id -u)" != "0" ]; then
    echo "This needs root (docker access). Run:"
    echo "  sudo bash $0"
    exit 1
fi

echo "==> finding the container publishing port 8888"
CID=$(docker ps --format '{{.ID}} {{.Ports}} {{.Names}}' | grep -F ':8888->' | awk '{print $1}' | head -1)
if [ -z "${CID:-}" ]; then
    echo "ERROR: no running container publishes 8888. Currently running:"
    docker ps --format '  {{.Names}}  {{.Ports}}'
    exit 1
fi
NAME=$(docker inspect -f '{{.Name}}' "$CID" | sed 's|^/||')
echo "    container: $NAME  ($CID)"

echo "==> locating the dashboard HTML inside it"
SRC=$(docker exec "$CID" sh -c \
    "grep -rl 'NAS Services' /usr/share/nginx/html /var/www /app /srv /html 2>/dev/null | head -1" || true)
if [ -z "${SRC:-}" ]; then
    echo "ERROR: could not find the page inside the container."
    echo "Look manually with:  docker exec -it $CID sh"
    exit 1
fi
echo "    file: $SRC"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
docker cp "$CID:$SRC" "$WORK/page.html" >/dev/null

echo "==> injecting the tile"
python3 - "$WORK/page.html" <<'PYEOF'
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    html = fh.read()

if ":7331" in html or "promptx" in html:
    print("    already present — nothing to do")
    sys.exit(0)

CARD = (
    '\n  <div class="card"><div class="chead"><span class="emoji">\U0001F3AF</span>'
    '<div><p class="cname">promptx</p><span class="port">:7331</span></div></div>\n'
    '    <p class="desc">Prompt expander — turns a vague request into an explicit, '
    'step-by-step work order for your coding agent. A cheap fast model writes the spec so '
    'your local model executes instead of guessing.</p>\n'
    '    <a class="open" href="http://10.0.4.88:7331/" target="_blank" rel="noopener">'
    'Open ↗</a></div>\n'
)

# Anchor on the last card of the "Automation & AI" section, then insert
# before that section's closing tags.
anchor = html.find('10.0.4.88:8899')
if anchor == -1:
    anchor = html.find('Automation')
close = html.find('</div></div>', anchor)
if anchor == -1 or close == -1:
    print("    ERROR: could not find the Automation & AI section", file=sys.stderr)
    sys.exit(1)

html = html[:close] + CARD + html[close:]
with open(path, "w", encoding="utf-8") as fh:
    fh.write(html)
print("    tile inserted")
PYEOF

if grep -q "7331" "$WORK/page.html"; then
    echo "==> backing up and writing it back"
    docker exec "$CID" sh -c "cp '$SRC' '$SRC.bak' 2>/dev/null || true"
    docker cp "$WORK/page.html" "$CID:$SRC" >/dev/null
    docker exec "$CID" sh -c "nginx -s reload 2>/dev/null || true"

    # keep the host copy in sync so it isn't misleading later
    cp "$WORK/page.html" /volume1/@home/Natron/nas-home.html 2>/dev/null || true
    chown Natron:users /volume1/@home/Natron/nas-home.html 2>/dev/null || true

    sleep 1
    if curl -s -m 10 http://127.0.0.1:8888/ | grep -q 7331; then
        echo ""
        echo "DONE — the promptx tile is live at http://10.0.4.88:8888/"
        echo "(hard-refresh the page: Cmd-Shift-R)"
    else
        echo ""
        echo "Written, but :8888 is still serving the old page."
        echo "Try:  docker restart $CID"
    fi
else
    echo "Nothing changed."
fi
