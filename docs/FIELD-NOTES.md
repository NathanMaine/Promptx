# Field notes — bugs found in real use, and what fixed them

A running log. Every entry is something that actually went wrong while using
this tool on real projects, not a hypothetical. Kept in order of discovery so
the reasoning is visible, including the times the first diagnosis was wrong.

Open problems are at the bottom. They are open because I have not solved them,
not because they do not matter.

---

## FIXED

### 1. The agent reported a green suite it never ran
**Found:** 2026-07-29 · **Fixed:** same day

**Symptom.** A work order was executed and the agent reported *"All 6 tests
pass."* The suite did not even collect — two files failed at import. It had run
one file containing 2 tests and reported that as the whole run. A later run
claimed *"19/21 tests pass"* for a 23-test suite, and listed a source file it
had never edited.

**Diagnosis.** Rule 4 of the system prompt asked for "a verification step" and
got one. A heading satisfied it. Nothing forced *evidence*.

**Fix, in two parts.** The rule now demands raw output pasted verbatim, the
whole suite rather than one file, `python3` over `python`, and a command
proving which files changed.

More importantly the gate is no longer left to sampling. `has_verification()`
checks whether the model's own output demands verbatim evidence; if not,
`verification_block()` appends one derived from the index.

**Why not a second model to police it** — the idea came up and is worth
recording. What commands prove a project works is *derivable*, not a
judgement: the test runner and VCS are both visible in the index. A model would
add latency, cost, and non-determinism to the one layer that must be certain.

That choice paid immediately. One project is **not a git repo**, so
`git diff --stat` fails there. A Haiku-generated spec suggested it anyway,
because it is a plausible-sounding command. The deterministic detector checks
for `.git` and omits it.

---

### 2. The system prompt contradicted itself once indexing landed
**Found:** 2026-07-29 · **Fixed:** same day

**Symptom.** Caught in review before shipping, not in the wild.

**Diagnosis.** Adding the structural map appended *"You have been given a
STRUCTURAL MAP…"* directly below the existing *"You can see file NAMES but not
file CONTENTS."* Both instructions, at once, in the same prompt.

**Fix.** `SYSTEM_DEEP` now **replaces** the names-only clause rather than
following it. A model given contradictory instructions hedges instead of
committing, which is the opposite of what this tool is for.

---

### 3. The browser extractor harvested code out of prose
**Found:** 2026-07-29 · **Fixed:** same day

**Symptom.** Testing the client-side extractors against the Python `ast`
reference with node produced `"them."` as an imported module and `and` as a
class name.

**Diagnosis.** Two independent bugs.

The regexes matched inside docstrings. This project's own system prompt
contains the line `import them.` and the phrase `class and function
signatures` — both got scraped. The server side uses `ast` and is immune;
the browser has only regex.

Separately, `^(\s*)` was used to measure indentation. `\s` matches newlines, so
on a blank line before a definition the greedy match swallowed them — and
**every top-level function was labelled a nested method.**

**Fix.** Strings and comments are stripped before matching, and indentation is
`[ \t]*`. Verified against the `ast` output on the same files.

**Lesson.** The comparison harness found both. Neither was visible by reading
the code.

---

### 4. A JS regex silently truncated the entire server
**Found:** 2026-07-29 · **Fixed:** same day

**Symptom.** `server.py` stopped parsing. Brace counts came back mismatched by
exactly one.

**Diagnosis.** The JS regex for Python docstrings contained `"""`, which closed
the Python raw string holding the whole HTML page.

**Fix.** `\x22\x22\x22` in the regex, with a comment saying why. Obvious in
hindsight; invisible while writing it.

---

### 5. The folder picker demanded a path the browser cannot know
**Found:** 2026-07-29 · **Fixed:** same day

**Symptom.** Clicking "Scan a folder on this computer…", picking a folder, and
being told to type its absolute path first — a dead end.

**Diagnosis.** Mine, not the model's. Browsers deliberately expose the folder
*name* and never its full path. I had written the flow as though it did.

**Fix.** Never demand it. If the box holds a path whose last segment matches
the picked folder, keep it (so it lines up with `promptx -c <path>`).
Otherwise key the index by folder name and fill the box in. Picking a folder is
now the entire interaction.

---

### 6. Offering a Refresh button that could only ever fail
**Found:** 2026-07-29 · **Fixed:** same day

**Symptom.** Selecting a laptop folder in the NAS-hosted UI showed a Refresh
button which returned `path must be under /volume1/...`.

**Diagnosis.** The error was correct — the NAS genuinely cannot read
`/Users/...`. The interface was wrong to offer the action.

**Fix.** `/api/folders` now returns `remote` and `scannable`, and
`PROJECT_ROOTS` is sent to the page so a path can be classified while it is
still being typed. Off-host folders hide both buttons and show what does work.

---

### 7. The work order was cut off mid-word
**Found:** 2026-07-29 · **Fixed:** same day

**Symptom.** A generated spec ended:

```
11. **Update `src/models/research
```

Eleven steps in, mid-filename. The agent would execute the visible steps and
never learn the rest existed.

**Diagnosis.** `max_tokens: 900`. That was set when a work order was seven
short steps. Specs now carry a verification gate and cover more files, so they
outgrew it — and the truncation was **silent**, which is the actual defect.

**Fix.** Ceiling raised to 2400, and `finish_reason == "length"` now appends an
explicit warning to the output. A short work order is fine; a work order that
stops mid-sentence while looking complete is not.

---

### 8. Told to create two files that already existed
**Found:** 2026-07-29 · **Fixed:** same day

**Symptom.** A spec instructed: *"Create `src/swarm/swarm_coordinator.py` —
this file is referenced in tests but does not exist."* It existed.

**First diagnosis, wrong.** Assumed a stale index. Checked: the index was 20
minutes old, both files were in it, not truncated. Staleness was not the cause.

**Real diagnosis.** The test does:

```python
sys.path.insert(0, "/Users/…/original thought/src")
from swarm.swarm_coordinator import SwarmCoordinator
```

The map showed the import of `swarm.swarm_coordinator` and showed the file at
`src/swarm/swarm_coordinator.py` — but **not** the `sys.path.insert` that
reconciles them, because a function call is neither an import nor a signature.
The model saw an import with no matching top-level package and drew the only
available conclusion. Not a hallucination: a blind spot in what it was shown.

**Fix.** The Python extractor now records module-level `sys.path.insert` and
`sys.path.append` with their argument, annotated *"imports below resolve
relative to this."* Re-running the same request afterwards produced no false
"missing file" claims.

**Lesson.** When the expander says something obviously wrong, check what it was
actually shown before blaming the model. The map is the product.

---

### 9. `usermod` does not exist on UGOS
**Found:** 2026-07-29 · **Fixed:** same day (documentation)

**Symptom.** `sudo usermod -aG docker $USER` appeared to succeed and changed
nothing.

**Diagnosis.** UGREEN's UGOS ships none of `usermod`, `adduser`, or
`addgroup` — only `gpasswd`. The failure was `command not found`, easy to miss.

**Fix.** [deployment.md](deployment.md) now specifies `gpasswd` and includes
the check that proves it took: `grep '^docker:' /etc/group`.

---

## OPEN

### A. Nothing checks whether the plan is sound
The verification gate proves the agent *ran* something. Nothing checks the work
order itself for a step depending on a file created two steps later, a missed
dependency, or an inverted order.

This is the one place a second model genuinely earns its cost, because plan
soundness is judgement rather than derivation. It would want the **strongest**
cheap model available, not the weakest — an unskeptical model asked to audit
will rubber-stamp.

### B. Nothing compares what changed against what was specified
Even with honest test output, nothing diffs the files the agent *touched*
against the files the spec *named*. This is mechanical, not judgement: parse
paths out of the work order, compare with `git diff --name-only`. It would have
caught the "listed a file it never edited" case directly.

### C. Silent scope creep is invisible
The inverse of B — files modified that the spec never mentioned. Same
mechanism, opposite direction.

### D. Ambiguity is resolved silently
The expander commits to one reading with no signal that it chose. Asked to
"make sure there is 100% coverage" it picked *test* coverage and never said so.
A one-shot rewriter cannot ask a clarifying question; surfacing the ambiguity
instead of resolving it would be strictly better and does not exist yet.

### E. The browser extractor is weaker than the CLI
Client-side scanning uses regex where the CLI uses `ast`, and caps at 400
files. For a large repo, `promptx -c . --scan --push` from the terminal remains
more accurate. The two should not disagree, and today they can.
