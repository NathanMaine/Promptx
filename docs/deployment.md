# Hosting promptx on a NAS or server

Running `server.py` on an always-on box makes promptx a bookmark from any device
on the LAN — phone, laptop, tablet — with no install anywhere else.

This is written against a UGREEN NAS running UGOS (Debian-based, systemd,
Python 3.11). Adapt paths for your box.

---

## 1. Copy the files over

`scp` fails against some NAS SSH configurations. Piping through `cat` always
works:

```bash
cat server.py | ssh nas 'mkdir -p /volume1/Projects/promptx && cat > /volume1/Projects/promptx/server.py'
ssh nas 'chmod +x /volume1/Projects/promptx/server.py'
```

Check the Python version — 3.9 or newer:

```bash
ssh nas 'python3 -V'
```

No pip install step. That is the point of the standard-library-only constraint:
a NAS Python you do not control is still good enough.

---

## 2. Give it a key

```bash
ssh nas 'cat > /volume1/Projects/promptx/.env' <<'EOF'
OPENROUTER_API_KEY=sk-or-v1-your-key-here
EOF
ssh nas 'chmod 600 /volume1/Projects/promptx/.env'
```

`server.py` looks for `.env` at `PROMPTX_ENV`, defaulting to
`/volume1/Projects/promptx/.env`. `chmod 600` matters — a NAS share is visible
to more people than a laptop home directory.

---

## 3. Point it at your own hardware (optional)

For the free local option in the picker:

```bash
PROMPTX_SPARK_URL=http://10.0.4.93:11434/v1/chat/completions
PROMPTX_SPARK_MODEL=qwen3.6-uncensored:latest
```

Any OpenAI-compatible endpoint works — Ollama, vLLM, LM Studio.

---

## 4. Set `PROJECT_ROOTS`

Near the top of `server.py`:

```python
PROJECT_ROOTS = ["/volume1/Projects", "/volume1/@home/Natron"]
```

The browser's project-folder field is restricted to paths under these. **This is
the only thing preventing an arbitrary directory listing of your NAS through the
API.** Set it to the narrowest set that covers your work.

---

## 5. Make it survive a reboot

Do not skip this. It is easy to start it with `nohup`, confirm it works, and
forget — until the NAS reboots months later and the bookmark 404s with no
obvious cause.

```bash
scp systemd/promptx.service nas:/tmp/
ssh nas '
  sudo cp /tmp/promptx.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now promptx
  systemctl status promptx --no-pager'
```

Check the unit's `User=`, `WorkingDirectory=`, and `ExecStart=` first — the
shipped values assume `Natron` and `/volume1/Projects/promptx`.

Prefer the API key in a root-owned file rather than in the unit:

```bash
ssh nas '
  echo "OPENROUTER_API_KEY=sk-or-v1-..." | sudo tee /etc/promptx.env >/dev/null
  sudo chmod 600 /etc/promptx.env'
```

The unit already reads it via `EnvironmentFile=-/etc/promptx.env` (the `-` means
"do not fail if absent").

Verify it actually comes back:

```bash
ssh nas 'sudo systemctl restart promptx && sleep 2 && systemctl is-active promptx'
ssh nas 'journalctl -u promptx -n 20 --no-pager'
```

### If your NAS is not systemd

Check with `[ -d /run/systemd/system ] && echo systemd`. If it isn't, use the
NAS's own task scheduler with a boot-time trigger, or the Docker app with a
`restart: unless-stopped` policy. `nohup` is not a deployment.

---

## 6. Confirm

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://10.0.4.88:7331/
```

`200` means you are done. Open `http://<nas-ip>:7331/` and bookmark it.

---

## Security

**There is no authentication.** Anyone who can reach the port can spend your
OpenRouter credits and list your project directory names.

- LAN only. Do not port-forward, do not put it behind a naive reverse proxy
  without auth.
- Keep `PROJECT_ROOTS` narrow.
- `chmod 600` the `.env`.
- If you need it off-LAN, use a VPN (Tailscale, WireGuard) rather than exposing
  the port. Adding real auth to `server.py` is a genuine feature request, not a
  five-minute patch.

---

## Adding a dashboard tile

If you keep a homelab dashboard, [`scripts/add-promptx-tile.sh`](../scripts/add-promptx-tile.sh)
adds a promptx card to a containerized nginx dashboard. It is idempotent.

```bash
sudo bash scripts/add-promptx-tile.sh
```

**The trap it exists to solve**, because it cost hours here: a dashboard served
by a Docker container keeps its HTML in a **named volume**, not on the host
where you wrote it. Editing the host-side file appears to work — the file
changes, the backup is made, everything looks right — and the served page never
updates.

Two things compounded it in this case:

1. The volume lives at `/volume2/@docker/volumes/nas_home_data/_data`, which is
   **root-only**. Filesystem searches run as a normal user skip it silently,
   because `find ... 2>/dev/null` hides the permission errors. It genuinely
   looks like no copy exists anywhere.
2. The served file is named `index.html`. The host file was `nas-home.html`.
   Searching for the filename you know finds nothing useful.

Diagnosing it takes one command — write a *new* file into the directory you
believe is the web root, and ask the server for it:

```bash
ssh nas 'echo hi > /path/you/think/is/webroot/probe.html'
curl -o /dev/null -w '%{http_code}\n' http://nas:8888/probe.html
```

`404` means it is not the web root, no matter how many correctly-named files
live there. `200` means it is. This test is worth reaching for early; it is
faster and more conclusive than any amount of searching.

Once you have Docker access, the answer is immediate:

```bash
docker inspect <container> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
```

And the edit itself is `docker cp` out, modify, `docker cp` back:

```bash
docker cp nas-home:/usr/share/nginx/html/index.html /tmp/dash.html
# edit /tmp/dash.html
docker cp /tmp/dash.html nas-home:/usr/share/nginx/html/index.html
docker exec nas-home chown 1000:wheel /usr/share/nginx/html/index.html
```

Restore the ownership after copying in — `docker cp` writes as root, and nginx
will still serve it, but the file no longer matches its neighbours.

The script finds the container publishing the port, locates the HTML inside it,
injects the card, and copies it back — which requires docker, which requires
root or membership in the `docker` group.

To manage containers without `sudo` every time:

```bash
sudo gpasswd -a "$USER" docker   # then log out and back in
```

**Use `gpasswd`, not `usermod`.** UGOS does not ship `usermod`, `adduser`, or
`addgroup` — only `gpasswd`. The usual `sudo usermod -aG docker $USER` fails
with "command not found," and because the failure is quiet if you are not
watching, it looks like it worked. Verify it actually took:

```bash
grep '^docker:' /etc/group     # your username should be after the last colon
```

An empty field there means nothing happened, regardless of what the command
appeared to do.

Consider what that grants before running it: the `docker` group is effectively
root on that host, since it can mount the host filesystem into a container.
