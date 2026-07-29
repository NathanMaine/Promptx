---
name: Bug report
about: Something produced the wrong work order, or broke
title: ''
labels: bug
assignees: ''
---

## What you asked for

The exact request you typed.

## What you got

Paste the work order verbatim — including any `**WARNING — this work order was
CUT OFF**` line. Do not summarize it; the wording is usually where the bug is.

## What you expected instead

## Was the folder indexed?

```
promptx --folders
```

Paste the output. Whether an index exists — and how old it is — changes the
answer more than anything else. Without one, promptx sees filenames only and
will correctly refuse questions about what the code says.

## Setup

- promptx: CLI / local web UI / hosted `server.py`
- Model: (e.g. `google/gemini-2.5-flash-lite`)
- Python: `python3 -V`
- OS:

## If the work order named a file wrongly

Say whether the file exists, and where. Several past bugs turned out to be gaps
in what the *map* showed rather than model error — for example, a
`sys.path.insert` making a bare import resolve, which the map did not record.
See [docs/FIELD-NOTES.md](../../docs/FIELD-NOTES.md).
