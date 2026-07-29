#!/bin/bash
# One-time root setup for promptx on the NAS. Does two things:
#
#   1. Makes the promptx web UI survive a reboot (it is running under nohup
#      right now, so it would silently vanish on the next restart).
#   2. Adds the promptx tile to the dashboard at :8888.
#
# Run:  sudo bash /volume1/Projects/promptx/nas-setup.sh
#
# Both steps are idempotent — safe to re-run.

set -uo pipefail

if [ "$(id -u)" != "0" ]; then
    echo "Needs root. Run:  sudo bash $0"
    exit 1
fi

DIR=/volume1/Projects/promptx
SERVER="$DIR/promptx-server.py"
[ -f "$SERVER" ] || SERVER="$DIR/server.py"

echo "############################################################"
echo "# 1/2  reboot persistence"
echo "############################################################"

if [ ! -f "$SERVER" ]; then
    echo "  SKIP: no server script found in $DIR"
else
    echo "  server script: $SERVER"

    # Move the API key out of the working dir into a root-owned file
    if [ -f "$DIR/.env" ] && [ ! -f /etc/promptx.env ]; then
        cp "$DIR/.env" /etc/promptx.env
        chmod 600 /etc/promptx.env
        echo "  copied .env -> /etc/promptx.env (chmod 600)"
    fi

    cat > /etc/systemd/system/promptx.service <<UNIT
[Unit]
Description=promptx — prompt expander web UI
Documentation=https://github.com/NathanMaine/promptx
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=Natron
Group=users
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $SERVER --port 7331 --host 0.0.0.0
EnvironmentFile=-/etc/promptx.env
Environment=PROMPTX_ENV=$DIR/.env
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5
StartLimitIntervalSec=300
StartLimitBurst=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=promptx
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
    echo "  wrote /etc/systemd/system/promptx.service"

    # Stop the nohup instance so systemd owns port 7331
    OLD=$(pgrep -f "python3 .*promptx-server.py" | head -5)
    if [ -n "${OLD:-}" ]; then
        echo "  stopping the nohup instance (PIDs: $(echo "$OLD" | tr '\n' ' '))"
        for p in $OLD; do kill "$p" 2>/dev/null; done
        sleep 2
    fi

    systemctl daemon-reload
    systemctl enable promptx >/dev/null 2>&1
    systemctl restart promptx
    sleep 3

    if systemctl is-active --quiet promptx; then
        echo "  RUNNING under systemd — will now survive reboots"
    else
        echo "  FAILED to start. Last log lines:"
        journalctl -u promptx -n 15 --no-pager | sed 's/^/    /'
    fi
fi

echo ""
echo "############################################################"
echo "# 2/2  dashboard tile at :8888"
echo "############################################################"

CID=$(docker ps --format '{{.ID}} {{.Ports}}' | grep -F ':8888->' | awk '{print $1}' | head -1)
if [ -z "${CID:-}" ]; then
    echo "  SKIP: no running container publishes 8888. Running containers:"
    docker ps --format '    {{.Names}}  {{.Ports}}'
else
    NAME=$(docker inspect -f '{{.Name}}' "$CID" | sed 's|^/||')
    echo "  container: $NAME ($CID)"

    SRC=$(docker exec "$CID" sh -c \
        "grep -rl 'NAS Services' /usr/share/nginx/html /var/www /app /srv /html 2>/dev/null | head -1")
    if [ -z "${SRC:-}" ]; then
        echo "  SKIP: could not find the page inside the container."
        echo "  Explore with:  sudo docker exec -it $CID sh"
    else
        echo "  page: $SRC"
        WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
        docker cp "$CID:$SRC" "$WORK/page.html" >/dev/null

        python3 - "$WORK/page.html" <<'PYEOF'
import sys
path = sys.argv[1]
html = open(path, encoding="utf-8").read()
if "7331" in html:
    print("  tile already present — nothing to do")
    sys.exit(0)
CARD = (
    '\n  <div class="card"><div class="chead"><span class="emoji">\U0001F3AF</span>'
    '<div><p class="cname">promptx</p><span class="port">:7331</span></div></div>\n'
    '    <p class="desc">Prompt expander — turns a vague request into an explicit, '
    'step-by-step work order for your coding agent. A cheap fast model writes the spec '
    'so your local model executes instead of guessing.</p>\n'
    '    <a class="open" href="http://10.0.4.88:7331/" target="_blank" rel="noopener">'
    'Open ↗</a></div>\n'
)
anchor = html.find('10.0.4.88:8899')
if anchor == -1:
    anchor = html.find('Automation')
close = html.find('</div></div>', anchor)
if anchor == -1 or close == -1:
    print("  ERROR: could not locate the Automation & AI section")
    sys.exit(1)
open(path, "w", encoding="utf-8").write(html[:close] + CARD + html[close:])
print("  tile inserted")
PYEOF

        if grep -q 7331 "$WORK/page.html"; then
            docker exec "$CID" sh -c "cp '$SRC' '$SRC.bak' 2>/dev/null" || true
            docker cp "$WORK/page.html" "$CID:$SRC" >/dev/null
            docker exec "$CID" sh -c 'nginx -s reload 2>/dev/null' || true
            cp "$WORK/page.html" /volume1/@home/Natron/nas-home.html 2>/dev/null || true
            chown Natron:users /volume1/@home/Natron/nas-home.html 2>/dev/null || true
            sleep 1
            if curl -s -m 10 http://127.0.0.1:8888/ | grep -q 7331; then
                echo "  LIVE at http://10.0.4.88:8888/  (hard-refresh: Cmd-Shift-R)"
            else
                echo "  written, but :8888 still serves the old page — try:"
                echo "     sudo docker restart $CID"
            fi
        fi
    fi
fi

echo ""
echo "############################################################"
echo "Optional: manage containers without sudo next time"
echo "  sudo usermod -aG docker Natron    # then log out and back in"
echo "Note: the docker group is effectively root on this host."
echo "############################################################"
