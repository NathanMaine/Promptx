# promptx

**Turns a vague request into an explicit work order, so local coding models
execute instead of guessing.**

```
you:      "build out the gtm adapters package"

promptx:  1. Create src/gtm_adapters/gtm_adapter.py — abstract base class
             GTMAdapter with method process_event(event: dict) -> dict
          2. Create src/gtm_adapters/strategy_council.py — class
             StrategyCouncil, method evaluate(genome) -> float
          3. Create src/gtm_adapters/strategy_genome.py — dataclass
             StrategyGenome with fields: id, traits, fitness
          4. ONLY THEN edit src/gtm_adapters/__init__.py to import them
          5. Verify: python -c "import src.gtm_adapters"

          Do NOT rewrite __init__.py to fix an ImportError. The error names
          __init__.py but the cause is the missing sibling modules above.
```

You paste the second thing into your coding agent instead of the first.

---

## Why this exists

A local 80B model was asked to "build out the gtm adapters package." It wrote
`src/gtm_adapters/__init__.py`, whose imports referenced three sibling modules
that did not exist. The import failed. The model saw an error naming
`__init__.py`, so it rewrote `__init__.py`. Same failure. It did this **ten
times** before it got killed.

The model wasn't broken. It was **fixing the file the error named instead of
creating the files that were missing** — a diagnostic step smaller models
frequently get wrong.

The lesson generalizes: local models follow explicit instructions well and
infer badly. Ambiguity is what makes them loop.

So split the job. A cheap fast cloud model (~$0.10 per million tokens) converts
your intent into a precise spec. Your local model — excellent at execution,
weak at inference — runs it. Neither is good at both, and the pairing costs a
fraction of a cent per request.

---

## New in 0.2.0 — built for the big repos

The tool that started with a 400-file ceiling and one cheap model just grew
teeth:

- **Huge projects are in scope now.** `--scan` indexes **2,000 files by
  default** — five times the old cap — and renders ~60K tokens of structure.
  Both limits are env overrides (`PROMPTX_MAX_FILES`,
  `PROMPTX_MAX_RENDER_CHARS`) for when your monorepo laughs at 2,000. Even
  without scanning, the expander now sees **600 paths instead of 120**.
- **Two heavyweights joined the cheap fleet.** `qwen/qwen3.8-max` — flagship
  Qwen with a 1M context window, for maps so big the flash tier loses the
  plot. And `deepseek-v4-flash`, billed direct by DeepSeek: 284B MoE, 1M
  context, $0.14/M, with its own key and a thinking-sized token budget.
- **You can see what's running.** Every surface carries the version:
  `promptx --version`, both web UI footers, the server's startup log, and
  `GET /api/version`. No more wondering which copy the NAS is actually
  serving.
- **Docker deployment where the image IS the version.** Tagged
  `promptx:0.2.0`, hardened by default: read-only rootfs, read-only project
  mounts, no-new-privileges, keys never touching the filesystem. Update or
  roll back with one `docker compose up -d`.

---

## Install

```bash
git clone https://github.com/NathanMaine/promptx.git
cd promptx
./install.sh
```

That puts `promptx` and `promptx-web` in `~/bin` and makes sure `~/bin` is on
your `PATH`.

**API key.** promptx reads `OPENROUTER_API_KEY` from the environment, and falls
back to OpenCode's auth store at `~/.local/share/opencode/auth.json` if you
already use OpenRouter there. Copy `.env.example` to `.env` if you'd rather set
it explicitly. Get a key at [openrouter.ai](https://openrouter.ai).

No dependencies — Python 3.9+ standard library only, on purpose. It should run
on a NAS with no pip.

---

## Use it

### Command line

```bash
promptx -c . --copy "add retry logic to the api client"   # expand, copy to clipboard
promptx -c . -x "add retry logic to the api client"       # expand, then run via opencode
promptx -c . "add retry logic to the api client"          # just print it
promptx --models                                          # list suggested models
promptx --version                                         # which promptx is this?
promptx -m anthropic/claude-haiku-4.5 "..."               # pick a different model
promptx --local "..."                                     # use your own GPU, free
promptx -c . --snap "add retry logic"                     # spec + record baseline
promptx -c . --check                                      # verify what the agent did
```

**The `-c .` matters more than anything else here.** It sends the real file
tree along with your request, so the expander names paths that actually exist
instead of inventing plausible ones. Without it you get confident fiction.

### Browser

```bash
promptx-web          # opens http://localhost:7331
```

A textarea, a project-folder field it remembers between sessions, a model
picker with descriptions of what each one is good at, and the result with a
Copy button and a Try again button. It keeps your recent expansions so you can
compare two phrasings side by side.

### Hosted, so it's on every device

Run `server.py` on a NAS or any always-on box and it's a bookmark from your
phone, your laptop, anywhere on the LAN. See
[docs/deployment.md](docs/deployment.md) — it covers the recommended Docker
deployment (the image carries the version; updates and rollbacks are one
`docker compose up -d`), plus a systemd unit, because `nohup` does not
survive a reboot and you will forget that it was running under `nohup`.

---

## Give it real context: `--scan`

By default promptx sees **file names only**. That is enough to build things, but
a good model will correctly *refuse* anything that depends on what the code
actually says:

> *"I cannot write this work order. 'Update all documentation' requires me to
> read the actual content of existing docs and code to know what is outdated. I
> cannot see file contents — only names."*

That refusal is right, and `--scan` fixes it:

```bash
promptx -c . --scan          # build the map (incremental — only changed files)
promptx -c . "update all documentation for this project"
```

Now the same request produces a file-by-file work order that names real
functions and real gaps.

**It sends structure, not source.** Per file: imports, class and function
signatures, and docstrings. Per document: the heading outline. That is ~50
tokens per file instead of ~4,000, so a whole project fits in a few thousand
tokens. Full contents would blow past any context window and cost real money on
every call.

Since 0.2.0 the map holds **2,000 files by default** and renders up to ~60K
tokens — big enough for most real codebases out of the box. Bigger still is
one export away — `PROMPTX_MAX_FILES=5000 PROMPTX_MAX_RENDER_CHARS=500000
promptx -c . --scan` — paired with a 1M-context expander like `qwen3.8-max`,
which exists precisely for the result.

The map is cached as JSON under `~/.promptx/index/`. Re-running `--scan` after
editing two files re-reads two files — everything else is matched by size and
mtime and reused.

```bash
promptx --folders            # what's indexed, how many files, how long ago
promptx -c . --scan --push   # also upload the map to the hosted instance
```

### `--push`, for projects that live on your laptop

The hosted copy on a NAS cannot see `/Users/you/myproject`. `--push` scans
locally — where the files are — and uploads **only the map**. The hosted UI then
lists that folder and can expand against it from any device, including ones that
have no access to those files at all.

What travels: signatures, docstrings, headings, and config *key names*. Not file
bodies, and not config values.

## Don't trust the agent's report — check it

An agent once told me *"All 6 tests pass"* after running one file with 2 tests
in it. Another claimed 19/21 for a 23-test suite and listed a file it never
edited. The fix is not a sterner prompt — a claim cannot be verified by asking
the claimant more firmly.

`--snap` records the state of your project (content hashes, plus the current
test result) together with the work order. `--check` looks again afterwards:

```
SPEC NAMED 2 PATH(S)
  [changed]   src/calc.py
  [UNTOUCHED] README.md   <- named in the spec, never changed

UNSPECIFIED (1) - changed but never named in the spec
  ! tests/test_calc.py

TESTS  (run here, not reported by the agent)
  before:  1 failed, 1 passed
  after:   3 passed

FAIL: 1 specified path(s) never changed; 1 unspecified change(s)
```

The tests are run by promptx, on your machine — the agent's pasted output is
never consulted. Exit code 1 on any mismatch, so it composes:
`promptx -c . --check && git commit -am "done"`.

## When to use it, and when not to

**Use it for BUILD tasks.** Creating files, adding features, refactoring —
anywhere the agent needs a precise target. This is where the loops happen and
where the payoff is.

**For INVESTIGATE tasks — audits, code review, "what does this module actually
do" — run `--scan` first.** Without an index promptx sees only file names and
will correctly refuse; there is nothing there to reason from. With one it
becomes a good *planner*. Asked to audit this project for security problems it
named `safe_root()` (path traversal), `store_index()` (what it accepts and
writes), and `scan()` (symlink handling) — the right functions out of the
codebase, with what to check in each.

What it still will not do is tell you whether `safe_root()` actually has a bug.
That needs the function body, which the map does not carry, so it instructs the
agent to read that specific file rather than inventing a finding. Investigation
gets a focused plan, not an answer — the right division of labour, since your
coding agent can open the files and promptx cannot.

**Skip it when you're already specific.** "Fix the typo on line 42 of
`utils.py`" does not need expanding, and promptx will pad it if you insist.

---

## Which model to use

Default is `google/gemini-2.5-flash-lite` — about $0.10 per million input
tokens, fast, and it never leaks reasoning traces into the output.

| Model | Cost | What it's good for |
|---|---|---|
| `google/gemini-2.5-flash-lite` | $0.10/M | The default. Balanced and clean. |
| `qwen/qwen3.7-flash` | $0.03/M | Cheapest paid option, 1M context — good for very large project trees. |
| `qwen/qwen3.8-max` | $2/M in · $6/M out | Flagship Qwen, 1M context — for huge scanned maps where the cheap tier drops structure. |
| `deepseek-v4-flash` | $0.14/M in · $0.28/M out | DeepSeek direct (hosted picker): 1M-context reasoning model with its own key and a bigger token budget. |
| `meta-llama/llama-3.1-8b-instruct` | $0.05/M | Rigid and literal. Use when you already know exactly what you want. |
| `inclusionai/ling-3.0-flash:free` | free | Rate limited, but fine for occasional use. |
| `anthropic/claude-haiku-4.5` | ~$1/M | Catches the things you did *not* say. Worth the money on a gnarly refactor. |
| `--local` | free | Your own hardware — the hosted picker has both engines: vLLM (quicker) and Ollama (always loaded). Verbose, `<think>` traces get stripped. |

**Avoid reasoning models.** This is one-shot rewriting, not a problem to solve.
Reasoning models burn tokens thinking about it and leak traces into the output.
Cheap and literal wins. (One sanctioned exception — `deepseek-v4-flash`, cheap
enough and carrying a 1M window — earns a bigger budget instead. The reasoning
behind that exception is below, in the longer comparison.)

Longer comparison in [docs/models.md](docs/models.md).

---

## Read the output before you run it

promptx commits **confidently to one interpretation** of an ambiguous request.
Asked to "review the platform end to end and make sure there is 100% coverage,"
it picked *test* coverage and produced a spec for writing a test file. That's a
defensible reading. It may not have been the intended one.

That is exactly what the Try again button is for. Reword, re-expand, and only
then paste. A bad spec you catch in ten seconds beats a wrong implementation
you catch in an hour.

---

## What's in here

| Path | What it is |
|---|---|
| [main.py](main.py) | The CLI. Installed as `promptx`. |
| [web.py](web.py) | Local browser UI. Installed as `promptx-web`. |
| [server.py](server.py) | The hosted version — multi-user, model descriptions, history. |
| [Dockerfile](Dockerfile) + [docker-compose.yml](docker-compose.yml) | The hosted deployment, containerized — the image tag carries the version. |
| [promptx_index.py](promptx_index.py) | The structural indexer behind `--scan`. |
| [promptx_version.py](promptx_version.py) + [VERSION](VERSION) | Which release this is — shown by `--version`, in the UI footers, and at `GET /api/version`. |
| [install.sh](install.sh) | Installer. |
| [systemd/promptx.service](systemd/promptx.service) | Keeps `server.py` alive across reboots. |
| [scripts/add-promptx-tile.sh](scripts/add-promptx-tile.sh) | Adds a promptx tile to a containerized nginx dashboard. |
| [docs/](docs/) | Full reference, model notes, deployment, prompt design. |

## Docs

- [docs/documentation.md](docs/documentation.md) — complete reference: every flag, every env var, the HTTP API
- [docs/models.md](docs/models.md) — picking an expander model, and why reasoning models are wrong here
- [docs/deployment.md](docs/deployment.md) — hosting it on a NAS or server, with systemd
- [docs/prompt-design.md](docs/prompt-design.md) — why the system prompt says what it says, and what happened when it didn't
- [docs/FIELD-NOTES.md](docs/FIELD-NOTES.md) — running log of bugs found in real use, what fixed them, and what is still open

## License

MIT — see [LICENSE](LICENSE).

## Found a bug?

Open an issue — the [bug template](.github/ISSUE_TEMPLATE/bug_report.md) asks
for the two things that matter most: the work order verbatim, and whether the
folder was indexed.

[docs/FIELD-NOTES.md](docs/FIELD-NOTES.md) lists every bug found so far with its
diagnosis, plus the problems still open. Worth a look before filing — and the
open ones are the best place to contribute.
