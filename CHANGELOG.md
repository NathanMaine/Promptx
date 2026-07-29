# Changelog

Notable changes. Bug detail — symptom, diagnosis, fix — lives in
[docs/FIELD-NOTES.md](docs/FIELD-NOTES.md); this is the summary.

## Unreleased

### Added
- Structural indexing (`promptx_index.py`). Sends signatures, docstrings, and
  heading outlines instead of source — 19x compression measured on this repo —
  so the expander can answer questions about existing code rather than refusing.
- `--scan`, `--push`, `--folders`. `--push` uploads a map built on the machine
  that has the files, so a hosted instance can serve projects it cannot see.
- Browser-side folder scanning in the hosted UI, with real per-file progress.
- Deterministic verification gate. Derived from the index, not from a model.
- Pre-commit secret scanner (`scripts/install-hooks.sh`).

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
