# Changelog

Notable changes. Bug detail — symptom, diagnosis, fix — lives in
[docs/FIELD-NOTES.md](docs/FIELD-NOTES.md); this is the summary.

## 0.3.0 — 2026-08-05

### Added
- Two local engines in the hosted picker, so you can choose per request:
  **Spark (local · vLLM)** — the quicker path, one model live at a time —
  and **Spark (local · Ollama)**, the always-loaded fallback. Each has its
  own endpoint/model env vars (`PROMPTX_SPARK_VLLM_URL` /
  `PROMPTX_SPARK_VLLM_MODEL` alongside the existing `PROMPTX_SPARK_*`).
  Both stay free and on-LAN.
- Docker deployment: `Dockerfile` + `docker-compose.yml`. The image is tagged
  with the VERSION file, so updates and rollbacks are tag swaps — no
  hand-copied files, no NAS-local drift to merge. Compose runs the container
  read-only with no-new-privileges, read-only project mounts, keys via
  `env_file`, the index on a volume, and a healthcheck on `/api/version`.
- Upstream of the DeepSeek direct integration that shipped NAS-only in 0.2.0:
  `deepseek-v4-flash` in the hosted picker, `DEEPSEEK_API_KEY` (env or
  `.env`), dedicated `api.deepseek.com` endpoint, and a 6,000-token budget so
  the reasoning model has room to think *and* answer. Now documented in
  `models.md`, `documentation.md`, and `.env.example` so a redeploy from the
  repo alone carries it.

## 0.2.0 — 2026-08-05

First numbered release — everything under the old "Unreleased" heading ships
with it, plus the below.

### Added
- `qwen/qwen3.8-max` as an expander option in all three UIs — flagship Qwen
  with a 1M context window, for huge scanned maps where the cheap tier drops
  structure ($2/M in, $6/M out).
- Versioning. `VERSION` file + `promptx_version.py`: shown by
  `promptx --version`, in both web UI footers, in the hosted server's startup
  log, and at `GET /api/version` — so you can tell which release a NAS
  container is running without opening it. `install.sh` ships both files.
- `docs/deployment.md` — "Updating a running install": the Docker
  bind-mount-at-/app flow, version check, rollback.

### Changed
- File limits raised for large projects: index default 400 → **2,000 files**,
  rendered map 60K → **240K characters** (~60K tokens). Both are now env
  overrides — `PROMPTX_MAX_FILES`, `PROMPTX_MAX_RENDER_CHARS` — so huge repos
  paired with a 1M-context expander can go further still.
- Name-only trees (`-c` without `--scan`) 120/150 → **600 paths** in all
  three apps.
- Browser-side scanner cap 400 → 2,000, matching the CLI default; pushed-index
  upload limit 4 MB → 8 MB.

### Fixed
- Scanner parity: the browser scanner skipped `.env` config outlines the CLI
  recorded, and the CLI lacked the browser's `Pods`/`DerivedData` skips. Both
  sets now match.
- Stale docs: the request-parameters table still said `max_tokens: 900`; it
  has been 2400 since the truncation fix.

## Unreleased (shipped as 0.2.0)

### Added
- Structural indexing (`promptx_index.py`). Sends signatures, docstrings, and
  heading outlines instead of source — 19x compression measured on this repo —
  so the expander can answer questions about existing code rather than refusing.
- `--scan`, `--push`, `--folders`. `--push` uploads a map built on the machine
  that has the files, so a hosted instance can serve projects it cannot see.
- Browser-side folder scanning in the hosted UI, with real per-file progress.
- Deterministic verification gate. Derived from the index, not from a model.
- Pre-commit secret scanner (`scripts/install-hooks.sh`).
- `--snap` / `--check`: verification by observation. Records content hashes and
  the test result before the agent works; afterwards reports specified paths
  never touched, unspecified changes, and the before/after suite result from an
  independent run. Exit 1 on mismatch. Closes #2 and #3.
- Plan lint: four mechanical coherence checks on every generated work order —
  create-existing, use-before-create, phantom paths, unreachable verification.
  Catches the FIELD-NOTES bug-8 spec verbatim. Mostly closes #1.
- `READ AS:` line — ambiguous requests now declare the chosen reading on line
  one instead of resolving it silently. Closes #4.
- Indiscriminate-suite detection in `--check`: green-before/green-after with
  identical counts now yields `OK (unproven)` instead of a bare OK, and build
  specs must include a test that fails before and passes after.
- Three defects caught by adversarial self-review the same day they shipped:
  indiscriminate detection was dead code (summary strings embed wall-clock
  timing, so equality never matched a real suite), the phantom-path lint
  false-positived on every Do-NOT section, and the create-existing check
  matched only the literal verb "create". All fixed and retested in both
  directions — see FIELD-NOTES bug 10.

### Fixed
- Work orders silently truncated at `max_tokens` (900 -> 2400, plus an explicit
  warning when `finish_reason == "length"`).
- `sys.path` manipulation was invisible to the map, so bare imports looked like
  missing modules and the expander told agents to create files that existed.
- `SYSTEM_DEEP` contradicted `SYSTEM` when indexing landed.
- Browser extractors matched inside docstrings; `^(\s*)` mislabelled every
  top-level function as a nested method.
- The folder picker demanded an absolute path browsers do not expose.
- A Refresh button was offered for folders the server cannot reach.

### Documentation
- `docs/FIELD-NOTES.md` — running log of every bug found in real use.
- `docs/deployment.md` — UGOS ships `gpasswd`, not `usermod`.
