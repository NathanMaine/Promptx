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
| `--scan` | Build/refresh the structural index for `-c DIR`. Incremental. |
| `--push` | Also upload that index to the hosted instance (`PROMPTX_HOSTED_URL`). |
| `--folders` | List indexed folders, with file counts and scan times. |
| `--snap` | Record `-c DIR`'s state and this work order, for `--check` to compare against. |
| `--check` | Compare against the last `--snap`. Exits 1 on mismatch. |
| `--no-tests` | With `--snap`/`--check`, skip running the suite. |

### Which install sees which filesystem

If you run both a local copy and a hosted one, this catches people out: **each
install can only see the filesystem of the machine it runs on.**

| Install | Reached at | Can send a tree for |
|---|---|---|
| Local (`promptx`, `promptx-web`) | your own terminal / `localhost:7331` | your laptop's filesystem |
| Hosted (`server.py`) | `http://<nas-ip>:7331` from any device | paths under `PROJECT_ROOTS` on that host |

The hosted UI's project-folder field is not a file picker for your computer —
it takes an absolute path **on the server**, and rejects anything outside
`PROJECT_ROOTS`. Typing `/Users/you/myproject` into the NAS-hosted UI will not
work, because that path does not exist on the NAS.

So: hosted copy for projects that live on the server, and for reaching promptx
from a phone or a machine with nothing installed. Local copy for whatever you
are actually coding on. Leaving the folder blank works in either, and gets you
generic invented paths — fine for a quick one-liner, wrong for real work.

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

## The structural index

Filenames alone cannot support a question about what the code *says*. A good
model will refuse those outright — correctly. The index removes that limit
without sending source.

### What it stores

Per file, from `promptx_index.py`:

| Kind | Extracted |
|---|---|
| Python | imports, class and function signatures (via `ast`, so they are real), first docstring line, module-level `UPPER_CASE` constants |
| JS / TS | imports and requires, exported functions, classes, arrow consts |
| Markdown / rst / txt | heading outline, first line of body, word count |
| Go, Rust, Java, C, Ruby, … | top-level `func` / `fn` / `class` / `struct` / `type` declarations |
| JSON / YAML / TOML / ini | **top-level key names only — never values**, because those hold secrets |
| anything else | path and size |

Roughly 50 tokens per file against ~4,000 for full source. A 14-file project
renders to about 1,700 tokens. The render is capped at 240,000 characters
(~60K tokens) and 2,000 files by default — both configurable via
`PROMPTX_MAX_RENDER_CHARS` and `PROMPTX_MAX_FILES` for huge repos paired with
a 1M-context expander. Documentation is emitted before code so doc outlines
survive truncation.

### Caching and refresh

One JSON file per indexed folder, named by a hash of its absolute path:

- CLI and local UI: `~/.promptx/index/`
- Hosted server: `/app/.index/` (override with `PROMPTX_INDEX_DIR`)

Refresh is incremental. A file whose size **and** mtime match the cached entry
is reused untouched, so re-scanning after editing two files parses two files.
`force=True` on the API (not exposed as a CLI flag) re-parses everything —
needed only if the extractor itself changed.

### Two prompts, not one

When an index is present, promptx swaps `SYSTEM` for `SYSTEM_DEEP`, which
**replaces** the "you can see file NAMES but not file CONTENTS" clause rather
than appending to it. Both at once is a contradiction, and a model given
contradictory instructions hedges instead of committing.

`SYSTEM_DEEP` keeps a narrower honesty rule: it may say which files are
relevant, what is missing, and what is inconsistent between docs and code — but
it may not assert what a function *body* does beyond what the signature and
docstring show, and must instruct the agent to read that specific file first.

### Server endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/scan` `{dir, force}` | Index a folder **on the server**. Goes through `safe_root`. |
| `POST /api/index` `{root, files, …}` | Store an index built elsewhere. See below. |
| `GET /api/folders` | Pickable folders, each flagged indexed or not. |
| `GET /api/version` | The running promptx version — check it after updating the NAS without opening the UI. |

`deep_context()` deliberately does **not** go through `safe_root`. An index is
inert data that was already built; rendering it touches no filesystem. That is
precisely what lets a laptop push a map for `/Users/you/project` and then expand
against it from a phone — the server never needs to see those files.

`POST /api/index` has the same trust model as the rest of the hosted server:
none. Anyone on the LAN can store an index. Payloads over 8 MB are rejected.
Keep it on the LAN.

## Verifying what the agent actually did

The verification gate makes a false report harder and more conspicuous. It
cannot make one impossible — promptx emits text and never observes execution,
so an agent can fabricate pasted output wholesale.

`--snap` / `--check` sidesteps that entirely. Instead of trying to make the
agent honest, it makes the agent's honesty irrelevant: promptx records the
state before, looks again after, and runs the tests itself.

```bash
promptx -c . --snap "add retry logic to the api client"   # spec + baseline
# ... hand the work order to your agent ...
promptx -c . --check                                      # what really happened
```

```
SPEC NAMED 2 PATH(S)
  [changed]   src/calc.py
  [UNTOUCHED] README.md   <- named in the spec, never changed

ACTUAL CHANGES (2)
  [modified]  src/calc.py
  [modified]  tests/test_calc.py

UNSPECIFIED (1) - changed but never named in the spec
  ! tests/test_calc.py

TESTS  (run here, not reported by the agent)
  before:  1 failed, 1 passed
  after:   3 passed
  -> was failing, now passing

FAIL: 1 specified path(s) never changed; 1 unspecified change(s)
```

Exit code is 1 on any mismatch, so it composes:
`promptx -c . --check && git commit -am "done"`.

**How change is detected.** SHA-256 of file contents, falling back to
size+mtime for files over 2 MB. Content hashing matters: `touch` fakes an mtime
change, and an agent that rewrites a file byte-identically has not changed
anything.

**Which paths the spec "named"** comes from backticked tokens in the work
order, plus anything already in the index that appears in the text. The system
prompt requires full relative paths, so backticks are reliable in practice.

**What it still cannot tell you.** If a suite was already green it stays green
whether the agent worked or slept. Tests prove the code works; the *diff*
proves the agent did something. Read both.

## Configuration

Every setting is an environment variable, and every one has a default.

| Variable | Used by | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | all | falls back to OpenCode's auth store, then `.env` |
| `DEEPSEEK_API_KEY` | `server.py` | unset — only needed for the `deepseek-v4-flash` picker option |
| `PROMPTX_MODEL` | `main.py` | `google/gemini-2.5-flash-lite` |
| `PROMPTX_LOCAL_URL` | `main.py`, `web.py` | `http://10.0.4.93:11434/v1/chat/completions` |
| `PROMPTX_LOCAL_MODEL` | `main.py`, `web.py` | `qwen3.6-uncensored:latest` |
| `PROMPTX_SPARK_URL` | `server.py` | same as above |
| `PROMPTX_SPARK_MODEL` | `server.py` | same as above |
| `PROMPTX_SPARK_VLLM_URL` | `server.py` | `http://10.0.4.93:8000/v1/chat/completions` — the quicker vLLM engine |
| `PROMPTX_SPARK_VLLM_MODEL` | `server.py` | whatever vLLM currently serves — must match, or the call fails |
| `PROMPTX_ENV` | `server.py` | `/volume1/Projects/promptx/.env` |
| `PROMPTX_INDEX_DIR` | all | `~/.promptx/index` locally, `/app/.index` hosted |
| `PROMPTX_MAX_FILES` | indexer | `2000` — files recorded in a `--scan` index |
| `PROMPTX_MAX_RENDER_CHARS` | indexer | `240000` — rendered map size (~60K tokens) |
| `PROMPTX_HOSTED_URL` | `main.py` (`--push`) | `http://10.0.4.88:7331` |

Note the naming split: `server.py` uses `SPARK_*` while the other two use
`LOCAL_*`. Set both if you are configuring a box that runs all three.

### How the key is found

1. `OPENROUTER_API_KEY` in the environment
2. `~/.local/share/opencode/auth.json`, key `openrouter.key` (`main.py`, `web.py`)
3. The `.env` file at `PROMPTX_ENV`, parsed as `KEY=value` lines (`server.py`)

If none of those turn up a key, you get an error naming all three options.
The key is never logged, never echoed, and never included in an error message.

`server.py` knows one more key: `DEEPSEEK_API_KEY` (environment or the same
`.env` file), used only by the `deepseek-v4-flash` option, which calls
`api.deepseek.com` directly instead of routing through OpenRouter. Separate
key, separate billing — deliberately.

---

## Request parameters

Not configurable without editing the source, but worth knowing:

| Parameter | Value | Why |
|---|---|---|
| `temperature` | `0.3` | Low. You want a consistent format, not creative variety. |
| `max_tokens` | `2400` | The system prompt asks for under 250 words; 2400 leaves headroom for deep-map work orders without inviting an essay. A `length` finish reason appends an explicit cut-off warning. Reasoning models (`deepseek-v4-flash`) get 6,000 — a thinking model can spend the whole 2,400 budget before it emits anything. |
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
| Paths in the output do not exist | You forgot `-c`, or the tree got truncated at 600 files — `--scan` goes much deeper. |

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

- **600 files of context** without `--scan` (2,000 with it by default; raise
  `PROMPTX_MAX_FILES` and `PROMPTX_MAX_RENDER_CHARS` for bigger). Large repos
  get truncated alphabetically; point `-c` at a subdirectory.
- **Names, not contents — unless you `--scan`.** Without an index it cannot
  review code it has never read, and the system prompt tells it to say so
  rather than invent findings. With an index it sees signatures and docstrings,
  but still not function bodies, so it will name the file to read instead of
  guessing what the body does.
- **One interpretation, confidently.** Ambiguity in, a decisive guess out. Read
  before you run.
- **No auth on the hosted server.** LAN only.
- **`--copy` is macOS-only.** It shells out to `pbcopy`. On Linux, swap in
  `xclip -selection clipboard` or `wl-copy` in `main.py`.
