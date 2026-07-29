# promptx — complete reference

Three programs share one idea and one system prompt:

| File | Installed as | What it is |
|---|---|---|
| `main.py` | `promptx` | Command line. Clipboard, piping, `-x` to execute. |
| `web.py` | `promptx-web` | Local browser UI. Opens a tab on localhost. |
| `server.py` | — | Hosted version for a NAS or server. Model descriptions, history, LAN access. |

All three are Python 3.9+ standard library only. No pip, no venv, no lockfile.
That constraint is deliberate: `server.py` has to run on a NAS whose Python you
do not control.

---

## The CLI (`promptx`)

```
promptx [-c DIR] [-x] [-m MODEL] [--local] [--copy] [--raw] [--models] REQUEST...
```

| Flag | Effect |
|---|---|
| `-c DIR`, `--context DIR` | Send `DIR`'s file tree with the request. **Use this.** |
| `-x`, `--exec` | Pipe the expanded prompt straight into `opencode run`. |
| `-m MODEL`, `--model` | Any OpenRouter model id. Default `google/gemini-2.5-flash-lite`. |
| `--local` | Use your own endpoint instead of OpenRouter. Free, more verbose. |
| `--copy` | Copy the result to the clipboard via `pbcopy` (macOS). |
| `--raw` | Print the work order with no header or separators. Good for piping. |
| `--models` | Print the suggested-model table and exit. |

### `-c` is the flag that matters

Without it, the expander is guessing at your project layout, and it guesses
*confidently*. It will produce `src/services/auth_service.py` because that is
what such a project usually looks like — not because you have one.

With `-c .`, it gets the real tree and names real paths. It also gains the
ability to notice that something is missing, which is the whole point:

> *"`strategy_council.py` is imported by `__init__.py` but does not exist in the
> tree. Create it first."*

That single sentence is what breaks the loop described in the README.

### What `-c` actually sends

`repo_context()` walks the directory with `rglob`, sorts the results, and sends
**relative paths only** — no file contents, ever. It caps at 120 files and
appends `... (truncated)` past that.

Skipped directories: `.git`, `node_modules`, `__pycache__`, `.venv`, `venv`,
`dist`, `build`, `.next`, `target`, `.mypy_cache`, `.pytest_cache`,
`.ruff_cache`.

**The 120-file cap is a real limitation on large repos.** Past it, the expander
sees an alphabetical prefix of your tree and nothing else — so it may "notice"
that a file is missing when it simply got truncated away. On a big monorepo,
point `-c` at the subdirectory you are working in rather than the root.

### Examples

```bash
# the normal loop: expand, eyeball, paste
promptx -c . --copy "add retry logic with exponential backoff to the api client"

# hand it straight to opencode without reading it (bolder than it sounds)
promptx -c . -x "add a health check endpoint"

# pipe it somewhere else
promptx -c . --raw "add rate limiting" | pbcopy
promptx -c . --raw "add rate limiting" > /tmp/spec.md

# harder task, better model
promptx -c . -m anthropic/claude-haiku-4.5 "split the monolith service into three modules"

# offline / sensitive
promptx -c . --local "add auth to the internal admin tool"
```

---

## The local web UI (`promptx-web`)

```
promptx-web [--port 7331] [--no-open]
```

Starts a server on `127.0.0.1` and opens a browser tab. `--no-open` suppresses
that if you are on a headless box or already have the tab.

Type, press **Cmd/Ctrl-Enter**, read the result, hit **Copy**. **Try again**
re-runs the same request so you can compare two expansions of the same idea.

---

## The hosted server (`server.py`)

```
python3 server.py [--port 7331] [--host 0.0.0.0]
```

Binds `0.0.0.0` by default so it is reachable across the LAN. Threaded, so more
than one person can use it at once.

Difference from `web.py`: a richer model picker (each option shows cost, a
one-line strength, and a paragraph on what it is actually good for), a
remembered project folder, and expansion history in browser localStorage.

### HTTP API

Two endpoints. That is the whole surface.

**`GET /`** → the HTML page. Any other path returns 404.

**`POST /api/expand`**

```json
{
  "request": "add retry logic to the api client",
  "model":   "google/gemini-2.5-flash-lite",
  "dir":     "/volume1/Projects/myapp"
}
```

Response is either `{"text": "1. Create ..."}` or `{"error": "..."}`. Both come
back as HTTP 200 — check for the `error` key, not the status code.

`model` accepts any id from the `MODELS` table, plus the sentinel `__local__`
which routes to `PROMPTX_SPARK_URL` instead of OpenRouter.

```bash
curl -s http://10.0.4.88:7331/api/expand \
  -H 'Content-Type: application/json' \
  -d '{"request":"add retry logic","model":"google/gemini-2.5-flash-lite","dir":""}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("text","")) '
```

### Security

**There is no authentication.** Anyone who can reach the port can spend your
OpenRouter credits and enumerate your project directory names. Keep it on the
LAN. Do not port-forward it.

`dir` is restricted to paths under `PROJECT_ROOTS` (`/volume1/Projects` and
`/volume1/@home/Natron` by default) — edit that list at the top of `server.py`
for your own box. That check is the only thing standing between the endpoint and
an arbitrary directory listing of your NAS, so keep it.

---

## Configuration

Every setting is an environment variable, and every one has a default.

| Variable | Used by | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | all | falls back to OpenCode's auth store, then `.env` |
| `PROMPTX_MODEL` | `main.py` | `google/gemini-2.5-flash-lite` |
| `PROMPTX_LOCAL_URL` | `main.py`, `web.py` | `http://10.0.4.93:11434/v1/chat/completions` |
| `PROMPTX_LOCAL_MODEL` | `main.py`, `web.py` | `qwen3.6-uncensored:latest` |
| `PROMPTX_SPARK_URL` | `server.py` | same as above |
| `PROMPTX_SPARK_MODEL` | `server.py` | same as above |
| `PROMPTX_ENV` | `server.py` | `/volume1/Projects/promptx/.env` |

Note the naming split: `server.py` uses `SPARK_*` while the other two use
`LOCAL_*`. Set both if you are configuring a box that runs all three.

### How the key is found

1. `OPENROUTER_API_KEY` in the environment
2. `~/.local/share/opencode/auth.json`, key `openrouter.key` (`main.py`, `web.py`)
3. The `.env` file at `PROMPTX_ENV`, parsed as `KEY=value` lines (`server.py`)

If none of those turn up a key, you get an error naming all three options.
The key is never logged, never echoed, and never included in an error message.

---

## Request parameters

Not configurable without editing the source, but worth knowing:

| Parameter | Value | Why |
|---|---|---|
| `temperature` | `0.3` | Low. You want a consistent format, not creative variety. |
| `max_tokens` | `900` | The system prompt asks for under 250 words; 900 leaves headroom without inviting an essay. |
| `timeout` | 180s | Generous — a cold model on a loaded local box can be slow. |

`HTTP-Referer` and `X-Title` headers are sent to OpenRouter for attribution.
They are cosmetic and appear on your OpenRouter activity page.

### Reasoning-trace stripping

Some models emit `<think>...</think>` regardless of instructions. Both clients
split on `</think>` and `</thinking>` and keep only what follows the **last**
marker. This is why the local option works at all — the Qwen model reliably
emits traces, and they would otherwise be pasted into your coding agent as if
they were part of the spec.

---

## Failure modes

| What you see | What happened |
|---|---|
| `no OpenRouter key found` | None of the three key sources had one. Use `--local` or set the env var. |
| `HTTP 401` | Key is wrong or revoked. |
| `HTTP 402` | OpenRouter credits exhausted. |
| `HTTP 429` | Rate limited — usually the `:free` model. Switch models or wait. |
| `HTTP 404` with a model id | That model id does not exist on OpenRouter. Check [openrouter.ai/models](https://openrouter.ai/models); the aliases your coding agent uses are often not real OpenRouter ids. |
| `cannot reach ...` | Network, or `--local` pointed at a box that is down. |
| Output contains `<think>` | A model whose trace format the stripper does not recognize. Switch models. |
| Output is an essay, not steps | A reasoning model. See [models.md](models.md). |
| Paths in the output do not exist | You forgot `-c`, or the tree got truncated at 120 files. |

### On model ids specifically

`google/gemini-flash-latest` looks reasonable and is not a real OpenRouter id —
it is an alias some coding agents define internally. This cost a debugging cycle
during development. When in doubt:

```bash
curl -s https://openrouter.ai/api/v1/models | python3 -c '
import json,sys
for m in json.load(sys.stdin)["data"]:
    print(m["id"])' | grep gemini
```

---

## Limits worth knowing before you rely on it

- **120 files of context.** Large repos get truncated alphabetically.
- **Names, not contents.** It cannot review code it has never read. The system
  prompt tells it to say so rather than invent findings, and it mostly complies
  — but "mostly" is doing work in that sentence.
- **One interpretation, confidently.** Ambiguity in, a decisive guess out. Read
  before you run.
- **No auth on the hosted server.** LAN only.
- **`--copy` is macOS-only.** It shells out to `pbcopy`. On Linux, swap in
  `xclip -selection clipboard` or `wl-copy` in `main.py`.
