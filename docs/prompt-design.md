# Why the system prompt says what it says

Every rule in `SYSTEM` exists because something went wrong without it. This is
the record.

---

## The failure that started it

A local 80B model was asked to *"build out the gtm adapters package."*

It wrote `src/gtm_adapters/__init__.py`:

```python
from .gtm_adapter import GTMAdapter
from .strategy_council import StrategyCouncil
from .strategy_genome import StrategyGenome
```

None of those three modules existed. The import failed:

```
ImportError: cannot import name 'GTMAdapter' from 'src.gtm_adapters'
  (src/gtm_adapters/__init__.py)
```

The model read the error, saw `__init__.py` named in it, and rewrote
`__init__.py`. Same error. It did this **ten times** before being killed.

The model was not confused about Python. It made a **diagnostic** error: it
fixed the file the traceback named rather than creating the files that were
missing. That distinction — the error points at the importer, the fault is in
the absent importee — is exactly the kind of inference smaller models get
wrong, and it is invisible in benchmarks that measure code generation.

The fix is not a better model. It is a request that never left the ambiguity
open in the first place.

---

## The rules, and the failures behind them

### 1. Name every file, with its full relative path

> *Never say "the appropriate file" or "relevant modules".*

Vague targets are where models substitute convention for fact. Told to update
"the config," a model writes `config.py` because that is what projects usually
have — and yours has `settings/base.py`. Now you have two config systems.

### 2. List each module in a package separately

> *A missing sibling module is the single most common cause of import-error
> loops.*

This is the `__init__.py` failure, generalized. Ask for "a package" and you get
the package's front door with nothing behind it. Ask for each module by name and
the front door is written last, when its imports resolve.

### 3. State the order of operations

> *Create dependencies BEFORE the files that import them.*

Models generate in the order you list things. If the entry point is listed
first, it gets written first, and it is broken from the moment it exists. Every
subsequent step then happens against a codebase that is already failing, which
is how one missing file turns into a debugging session.

### 4. End with an explicit verification step

> *A command to run that proves it worked.*

Without one, "done" means "I stopped generating." With `python -c "import
src.gtm_adapters"` as the last step, the agent has a definition of done it can
actually check — and it catches the exact failure above on the first try instead
of the tenth.

### 5. Add a "Do NOT" section

> *Naming the likeliest wrong turn for this task.*

The single highest-value line in the whole output. For the failure above it is:

> *Do NOT rewrite `__init__.py` to fix an ImportError. The error names
> `__init__.py`, but the cause is the missing sibling modules.*

That sentence, in the prompt, prevents the loop outright. It is cheap to
generate and it targets the specific failure mode of the specific task.

### 6. Under 250 words

Long prompts dilute. Every extra sentence competes with the instructions that
matter, and a spec longer than the code it describes gets skimmed by the model
and by you. Density is the goal, not completeness.

### 7. Output only the work order

> *No preamble, no reasoning, no commentary. Begin directly with the first step.*

The output gets pasted directly into another agent. `"Here's a detailed plan for
your request!"` becomes an instruction to the coding agent, which will dutifully
try to make sense of it. Everything in the output must be executable.

---

## The rule added later: the honesty clause

> *You can see file NAMES but not file CONTENTS. If the task requires reading
> code to answer (an audit, a review, "what does X do"), do NOT invent findings.
> Instead instruct the agent to read the relevant files first and report what it
> actually finds.*

This one came from a real miss. Asked to *"review and analyze the platform end
to end and make sure there is 100% coverage,"* promptx produced a confident,
well-structured spec — for writing a test file. It had chosen *test* coverage,
and it had done so from a list of filenames, having read no code at all.

Two distinct problems, and the clause addresses the more dangerous one:

- **Ambiguity** — "coverage" has several meanings, and it picked one silently.
- **Hallucinated findings** — it was one step from asserting things about code
  it had never seen.

The clause fixes the second. The first is unfixable from inside the prompt: a
one-shot rewriter cannot ask a clarifying question. That is what the **Try
again** button is for, and why the README says read the output before you run it.

**The practical rule: use promptx for BUILD tasks, not INVESTIGATE tasks.** Your
coding agent can open files. promptx cannot. Ask the one that can.

---

## Temperature 0.3

You want the same request to produce the same shape every time. Consistency of
format is the deliverable; creative variety is a defect. Low but not zero —
zero makes the phrasing stilted without making the structure more reliable.

---

## What still does not work

**Ambiguity resolution.** promptx commits to one reading, confidently, with no
signal that it chose. A version that surfaced *"this could mean test coverage or
feature coverage — which?"* would be strictly better, and it does not exist yet.

**Contents.** Everything here is inference from filenames. Sending file contents
would fix the audit case and cost real tokens — the tradeoff has not been made.

**The 120-file cap.** Past it the tree is truncated alphabetically, so the
expander may "notice" a missing file that was simply cut off. On large repos,
point `-c` at a subdirectory.

Improvements welcome — the system prompt is the whole product, and it is
fourteen lines.
