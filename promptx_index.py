"""Build a compact structural map of a project so the expander knows what the
code actually contains, not just what the files are called.

The problem this solves: given only filenames, a good model correctly refuses
tasks like "update all documentation" — it cannot know what the docs currently
say. Given full file contents it could, but a real project is hundreds of
thousands of tokens and will not fit.

So we send structure instead of source. Per file: imports, class and function
signatures, the first line of each docstring; for Markdown, the heading
outline. That is roughly 50 tokens per file rather than 4,000, so a whole
project lands around 6K tokens — enough for the model to reason about what
exists and how it fits together.

The index is cached as JSON and refreshed incrementally: a file whose size and
mtime are unchanged is never re-parsed. Re-scanning a large project after
editing two files costs two parses.

Standard library only, like the rest of promptx.
"""

import ast
import hashlib
import json
import os
import pathlib
import re
import subprocess
import time

# Directories that are never worth indexing.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", ".next", "target", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".eggs", "site-packages", ".idea", ".vscode",
    "coverage", ".nyc_output", "vendor", ".terraform", ".gradle",
}

# Files we can say something structural about.
CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go",
            ".rs", ".rb", ".java", ".kt", ".swift", ".c", ".h", ".cpp",
            ".hpp", ".cs", ".php", ".sh", ".bash", ".zsh"}
DOC_EXT = {".md", ".markdown", ".rst", ".txt", ".adoc"}
CONF_EXT = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env"}

# Guardrails. A single enormous file should not eat the whole budget, and the
# finished index has to stay comfortably inside a prompt.
MAX_PARSE_BYTES = 400_000      # skip parsing anything larger
MAX_ENTRIES_PER_FILE = 30      # signatures/headings kept per file
MAX_FILES = 400                # files recorded in the index
MAX_RENDER_CHARS = 60_000      # ~15K tokens once rendered into the prompt


# --------------------------------------------------------------------------
# per-language extraction
# --------------------------------------------------------------------------

def _first_line(text):
    """First non-empty line of a docstring, trimmed."""
    if not text:
        return ""
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            return line[:160]
    return ""


def _python_outline(src):
    """Parse with ast so the signatures are real, not regex guesses."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return None

    imports, defs = [], []
    doc = _first_line(ast.get_docstring(tree))

    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or ".")

    def sig(fn):
        args = [a.arg for a in fn.args.args]
        if fn.args.vararg:
            args.append("*" + fn.args.vararg.arg)
        if fn.args.kwarg:
            args.append("**" + fn.args.kwarg.arg)
        return f"{fn.name}({', '.join(args)})"

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            d = _first_line(ast.get_docstring(node))
            defs.append(f"def {sig(node)}" + (f"  — {d}" if d else ""))
        elif isinstance(node, ast.ClassDef):
            d = _first_line(ast.get_docstring(node))
            defs.append(f"class {node.name}" + (f"  — {d}" if d else ""))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defs.append(f"    .{sig(sub)}")
        elif isinstance(node, ast.Assign):
            # module-level constants are often the real configuration surface
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    defs.append(f"{t.id} = ...")
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            # sys.path manipulation changes what every import in the file
            # RESOLVES TO. Without it, a test importing `swarm.foo` while the
            # file lives at src/swarm/foo.py looks like a missing module, and
            # the expander will confidently tell the agent to create a file
            # that already exists. Seen in the wild; worth the four lines.
            fn = node.value.func
            if (isinstance(fn, ast.Attribute)
                    and fn.attr in ("insert", "append")
                    and isinstance(fn.value, ast.Attribute)
                    and fn.value.attr == "path"):
                arg = node.value.args[-1] if node.value.args else None
                shown = ast.unparse(arg)[:90] if arg is not None else "?"
                defs.append(f"sys.path.{fn.attr}({shown})  "
                            f"— imports below resolve relative to this")

    return {"imports": sorted(set(imports))[:20],
            "defs": defs[:MAX_ENTRIES_PER_FILE],
            "doc": doc}


_JS_PATTERNS = [
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+\s*\([^)]{0,120}\))", re.M),
    re.compile(r"^\s*(?:export\s+)?class\s+(\w+)", re.M),
    re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\([^)]{0,120}\)\s*=>", re.M),
]
_JS_IMPORT = re.compile(r"""^\s*(?:import[^'"]*|.*require\()['"]([^'"]+)['"]""", re.M)


def _js_outline(src):
    defs = []
    for pat in _JS_PATTERNS:
        for m in pat.finditer(src):
            defs.append(m.group(1).strip())
    return {"imports": sorted(set(_JS_IMPORT.findall(src)))[:20],
            "defs": defs[:MAX_ENTRIES_PER_FILE],
            "doc": ""}


_GENERIC_DEF = re.compile(
    r"^\s*(?:pub\s+|public\s+|private\s+|static\s+|export\s+)*"
    r"(?:func|fn|def|class|struct|interface|type|impl)\s+(\w[\w<>, ]{0,80})", re.M)


def _generic_outline(src):
    return {"imports": [],
            "defs": [m.group(1).strip() for m in _GENERIC_DEF.finditer(src)][:MAX_ENTRIES_PER_FILE],
            "doc": ""}


_HEADING = re.compile(r"^(#{1,4})\s+(.+)$", re.M)


def _markdown_outline(src):
    """Heading outline — the table of contents the model needs to know what a
    doc already covers."""
    heads = [f"{'  ' * (len(m.group(1)) - 1)}{m.group(1)} {m.group(2).strip()}"
             for m in _HEADING.finditer(src)]
    body = _HEADING.sub("", src).strip()
    return {"imports": [], "defs": heads[:MAX_ENTRIES_PER_FILE],
            "doc": _first_line(body), "words": len(body.split())}


def _conf_outline(path, src):
    """Top-level keys only. Never values — they hold secrets."""
    ext = path.suffix.lower()
    keys = []
    try:
        if ext == ".json":
            data = json.loads(src)
            if isinstance(data, dict):
                keys = list(data.keys())
        else:
            keys = re.findall(r"^([A-Za-z_][\w.-]*)\s*[:=]", src, re.M)
    except (json.JSONDecodeError, ValueError):
        pass
    return {"imports": [], "defs": sorted(set(keys))[:MAX_ENTRIES_PER_FILE], "doc": ""}


def outline_file(path):
    """Structural summary of one file, or None if there is nothing to say."""
    ext = path.suffix.lower()
    try:
        if path.stat().st_size > MAX_PARSE_BYTES:
            return {"imports": [], "defs": [], "doc": "(file too large to parse)"}
        src = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None

    if ext == ".py":
        return _python_outline(src) or _generic_outline(src)
    if ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return _js_outline(src)
    if ext in DOC_EXT:
        return _markdown_outline(src)
    if ext in CONF_EXT:
        return _conf_outline(path, src)
    if ext in CODE_EXT:
        return _generic_outline(src)
    return None


# --------------------------------------------------------------------------
# scanning and caching
# --------------------------------------------------------------------------

def cache_path(root, cache_dir):
    """One cache file per indexed folder, named by a hash of its path."""
    h = hashlib.sha1(str(pathlib.Path(root).resolve()).encode()).hexdigest()[:16]
    return pathlib.Path(cache_dir) / f"{h}.json"


def load_index(root, cache_dir):
    p = cache_path(root, cache_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def scan(root, cache_dir, force=False):
    """Walk the folder and build (or refresh) its index.

    Incremental by default: a file whose size and mtime match the cached entry
    is reused as-is. Only changed files are re-parsed, so refreshing a large
    project after a couple of edits is nearly free. force=True re-parses
    everything.
    """
    root = pathlib.Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")

    old = {} if force else ((load_index(root, cache_dir) or {}).get("files") or {})
    files, reused, parsed = {}, 0, 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in sorted(dirnames)
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = pathlib.Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                continue
            rel = str(full.relative_to(root))

            prev = old.get(rel)
            if prev and prev.get("size") == st.st_size and prev.get("mtime") == int(st.st_mtime):
                files[rel] = prev
                reused += 1
            else:
                entry = {"size": st.st_size, "mtime": int(st.st_mtime)}
                o = outline_file(full)
                if o:
                    entry.update(o)
                    parsed += 1
                files[rel] = entry

            if len(files) >= MAX_FILES:
                break
        if len(files) >= MAX_FILES:
            break

    index = {
        "root": str(root),
        "scanned_at": int(time.time()),
        "file_count": len(files),
        "parsed": parsed,
        "reused": reused,
        "truncated": len(files) >= MAX_FILES,
        # .git is in SKIP_DIRS so it never appears in files — record it here,
        # because the verification block needs to know whether `git diff` works.
        "vcs": "git" if (root / ".git").exists() else None,
        "files": files,
    }

    try:
        p = cache_path(root, cache_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(index), encoding="utf-8")
    except OSError:
        pass  # an unwritable cache is not worth failing the request over
    return index


def list_indexed(cache_dir):
    """Every folder that has been scanned — powers the picker."""
    out = []
    d = pathlib.Path(cache_dir)
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            out.append({"root": data.get("root", "?"),
                        "file_count": data.get("file_count", 0),
                        "scanned_at": data.get("scanned_at", 0),
                        # pushed from another machine — this host cannot rescan it
                        "remote": bool(data.get("remote"))})
        except (json.JSONDecodeError, OSError):
            continue
    return sorted(out, key=lambda r: -r["scanned_at"])


# --------------------------------------------------------------------------
# snapshot and check — observing reality instead of trusting a report
# --------------------------------------------------------------------------
#
# Every earlier attempt at this tried to make the agent honest: demand raw
# output, forbid summarizing, insist on the whole suite. That is unwinnable —
# a claim cannot be verified by asking the claimant more firmly.
#
# So don't. promptx runs on the same machine as the files. Record the state
# before, look again after, and run the tests here. The agent's report stops
# being evidence and becomes a claim to check against what actually happened.

SNAP_MAX_HASH_BYTES = 2_000_000   # above this, fall back to size+mtime


def snap_path(root, cache_dir):
    h = hashlib.sha1(str(pathlib.Path(root).resolve()).encode()).hexdigest()[:16]
    return pathlib.Path(cache_dir) / f"{h}-snap.json"


def _file_sha(path):
    """Content hash. mtime alone is too weak — `touch` fakes a change, and an
    agent that rewrites a file byte-identically has not changed anything."""
    try:
        if path.stat().st_size > SNAP_MAX_HASH_BYTES:
            return None
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:32]
    except OSError:
        return None


def _walk_state(root):
    """{relpath: {size, mtime, sha}} for everything worth watching."""
    root = pathlib.Path(root).resolve()
    state = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            full = pathlib.Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                continue
            state[str(full.relative_to(root))] = {
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "sha": _file_sha(full),
            }
    return state


def run_tests(root, cmd, timeout=900):
    """Run the project's own test command HERE, and keep what it printed.

    This is the whole point: the result comes from execution, not from a
    paragraph an agent wrote about execution.
    """
    if not cmd:
        return None
    try:
        p = subprocess.run(cmd, shell=True, cwd=str(root), timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
        out = p.stdout or ""
        rc = p.returncode
    except subprocess.TimeoutExpired:
        return {"cmd": cmd, "summary": f"TIMED OUT after {timeout}s",
                "returncode": None, "output": ""}
    except OSError as exc:
        return {"cmd": cmd, "summary": f"could not run: {exc}",
                "returncode": None, "output": ""}

    lines = [l for l in out.splitlines() if l.strip()]
    return {"cmd": cmd, "returncode": rc,
            "summary": lines[-1][:200] if lines else "(no output)",
            "output": out[-4000:]}


def spec_paths(text, known=()):
    """Repo-relative paths a work order actually names.

    Backticked tokens are the reliable signal — the system prompt requires full
    relative paths, and models comply. Anything already in the index counts too,
    which catches paths mentioned in prose.
    """
    if not text:
        return []
    found = set()
    for m in re.finditer(r"`([^`\n]{2,120})`", text):
        tok = m.group(1).strip().strip(",.;:")
        if " " in tok and "/" not in tok:
            continue
        if "/" in tok or re.search(r"\.\w{1,5}$", tok):
            found.add(tok.lstrip("./"))
    for k in known:
        if k in text:
            found.add(k)
    return sorted(found)


def snapshot(root, cache_dir, spec=None, with_tests=True, index=None):
    """Record the baseline: file state, the spec, and the current test result."""
    root = pathlib.Path(root).resolve()
    idx = index or load_index(root, cache_dir) or {}
    cmd = test_command(idx) if idx else None

    snap = {
        "root": str(root),
        "taken_at": int(time.time()),
        "vcs": idx.get("vcs"),
        "files": _walk_state(root),
        "spec": spec,
        "spec_files": spec_paths(spec, (idx.get("files") or {}).keys()),
        "tests": run_tests(root, cmd) if (with_tests and cmd) else None,
    }
    try:
        p = snap_path(root, cache_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(snap), encoding="utf-8")
    except OSError:
        pass
    return snap


def load_snapshot(root, cache_dir):
    p = snap_path(root, cache_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _changed(before, after):
    """(modified, added, deleted) by content where hashes exist, else metadata."""
    modified, added = [], []
    for rel, now in after.items():
        was = before.get(rel)
        if was is None:
            added.append(rel)
        elif was.get("sha") and now.get("sha"):
            if was["sha"] != now["sha"]:
                modified.append(rel)
        elif was["size"] != now["size"] or was["mtime"] != now["mtime"]:
            modified.append(rel)
    deleted = [r for r in before if r not in after]
    return sorted(modified), sorted(added), sorted(deleted)


def compare(root, cache_dir, with_tests=True):
    """What actually happened, versus what the work order asked for."""
    root = pathlib.Path(root).resolve()
    before = load_snapshot(root, cache_dir)
    if not before:
        return {"error": "no snapshot for this folder — run --snap first"}

    idx = load_index(root, cache_dir) or {}
    after_files = _walk_state(root)
    modified, added, deleted = _changed(before["files"], after_files)
    touched = set(modified) | set(added) | set(deleted)

    named = set(before.get("spec_files") or [])
    # A named path that does not exist yet is still legitimately "named".
    done = sorted(p for p in named if p in touched)
    untouched = sorted(p for p in named if p not in touched)
    unspecified = sorted(p for p in touched if p not in named)

    cmd = test_command(idx) if idx else None
    tests_after = run_tests(root, cmd) if (with_tests and cmd) else None

    return {
        "root": str(root),
        "taken_at": before["taken_at"],
        "elapsed_s": int(time.time()) - before["taken_at"],
        "had_spec": bool(before.get("spec")),
        "modified": modified, "added": added, "deleted": deleted,
        "spec_named": sorted(named),
        "spec_done": done,
        "spec_untouched": untouched,
        "unspecified": unspecified,
        "tests_before": before.get("tests"),
        "tests_after": tests_after,
    }


def format_report(rep):
    """Human-readable, and honest about what it cannot know."""
    if rep.get("error"):
        return f"promptx: {rep['error']}"

    L = []
    total = len(rep["modified"]) + len(rep["added"]) + len(rep["deleted"])
    L.append(f"Comparing against snapshot from {rep['elapsed_s']}s ago")
    L.append("")

    if rep["had_spec"]:
        L.append(f"SPEC NAMED {len(rep['spec_named'])} PATH(S)")
        for p in rep["spec_done"]:
            L.append(f"  [changed]   {p}")
        for p in rep["spec_untouched"]:
            L.append(f"  [UNTOUCHED] {p}   <- named in the spec, never changed")
        if not rep["spec_named"]:
            L.append("  (no paths parsed out of the work order)")
        L.append("")
    else:
        L.append("No spec was recorded with this snapshot "
                 "(use --snap together with a request).")
        L.append("")

    L.append(f"ACTUAL CHANGES ({total})")
    for p in rep["added"]:
        L.append(f"  [added]     {p}")
    for p in rep["modified"]:
        L.append(f"  [modified]  {p}")
    for p in rep["deleted"]:
        L.append(f"  [deleted]   {p}")
    if not total:
        L.append("  nothing changed on disk")
    L.append("")

    if rep["had_spec"] and rep["unspecified"]:
        L.append(f"UNSPECIFIED ({len(rep['unspecified'])}) "
                 f"- changed but never named in the spec")
        for p in rep["unspecified"]:
            L.append(f"  ! {p}")
        L.append("")

    tb, ta = rep.get("tests_before"), rep.get("tests_after")
    indiscriminate = False
    if ta:
        L.append("TESTS  (run here, not reported by the agent)")
        L.append(f"  command: {ta['cmd']}")
        if tb:
            L.append(f"  before:  {tb['summary']}")
        L.append(f"  after:   {ta['summary']}")
        if tb and tb.get("returncode") is not None and ta.get("returncode") is not None:
            if tb["returncode"] != 0 and ta["returncode"] == 0:
                L.append("  -> was failing, now passing")
            elif tb["returncode"] == 0 and ta["returncode"] != 0:
                L.append("  -> WAS PASSING, NOW FAILING")
            elif (tb["returncode"] == 0 and ta["returncode"] == 0
                  and tb["summary"] == ta["summary"]):
                # Green before, green after, same counts: this suite cannot
                # tell work from no-work. The diff says the files changed;
                # nothing observable says the change DOES anything. An agent
                # that appended a comment to every named file passes both
                # checks. Only a discriminating test closes this.
                indiscriminate = True
                L.append("  -> INDISCRIMINATE: green before and after with "
                         "identical counts. The suite cannot tell whether the "
                         "work was done — only that nothing broke. For build "
                         "tasks, demand a test that fails before and passes "
                         "after.")
        L.append("")

    problems = []
    if rep["spec_untouched"]:
        problems.append(f"{len(rep['spec_untouched'])} specified path(s) never changed")
    if rep["unspecified"]:
        problems.append(f"{len(rep['unspecified'])} unspecified change(s)")
    if ta and ta.get("returncode") not in (0, None):
        problems.append("tests are failing")
    if problems:
        L.append("FAIL: " + "; ".join(problems))
    elif indiscriminate:
        # Not a FAIL — docs-only tasks legitimately leave the suite untouched.
        # But OK would overclaim, so say exactly what was and wasn't proven.
        L.append("OK (unproven): files changed and nothing broke, but no test "
                 "discriminates this work from a no-op.")
    else:
        L.append("OK")
    return "\n".join(L)

# Marker for "this build produces a runnable test command", detected from the
# index rather than guessed. Order matters: first match wins per language.
_TEST_RUNNERS = [
    ("package.json",   "npm test"),
    ("Cargo.toml",     "cargo test"),
    ("go.mod",         "go test ./..."),
    ("Gemfile",        "bundle exec rspec"),
    ("pom.xml",        "mvn -q test"),
    ("build.gradle",   "./gradlew test"),
]


def test_command(index):
    """The command that proves this project still works, or None.

    Derived from files that are actually present. Guessing here is worse than
    saying nothing: a verification step that cannot run teaches the agent to
    skip verification.
    """
    files = index.get("files") or {}
    names = set(files)

    # Python: only claim pytest if there is something for it to collect.
    has_py_tests = any(
        n.startswith(("tests/", "test/")) or pathlib.Path(n).name.startswith("test_")
        for n in names if n.endswith(".py"))
    if has_py_tests:
        return "python3 -m pytest -q"

    for marker, cmd in _TEST_RUNNERS:
        if marker in names:
            return cmd
    return None


def verification_block(index):
    """A verification section the agent cannot satisfy by describing it.

    Built in code, not by a model. Whether a verification gate exists at all is
    not a judgement call — it is the one part of the work order that must be
    present every time, so it must not depend on sampling.
    """
    lines = ["## VERIFICATION — paste raw output, do not summarize", ""]
    cmds = []

    cmd = test_command(index)
    if cmd:
        cmds.append(cmd)
    if (index or {}).get("vcs") == "git":
        cmds.append("git diff --stat")
        cmds.append("git status --porcelain")

    if cmds:
        lines.append("```bash")
        lines.extend(cmds)
        lines.append("```")
        lines.append("")
    lines.append(
        "Paste the output of every command above VERBATIM — the exact final "
        "summary line included. Do not count, total, round, or characterize "
        "the results, and do not report a subset as if it were the whole run. "
        "If anything fails, say so plainly and stop rather than continuing.")
    if not cmds:
        lines.append("")
        lines.append(
            "No test runner was detected in this project, so state exactly "
            "which files you changed and how you confirmed each one.")
    return "\n".join(lines)


def has_verification(text):
    """Did the model already produce a gate that demands evidence?

    Requires both a verification heading and an explicit verbatim demand — a
    heading alone is what produced 'all 6 tests pass' from a 2-test run.
    """
    low = (text or "").lower()
    return ("verif" in low
            and ("verbatim" in low or "exactly as printed" in low
                 or "do not summarize" in low))


# --------------------------------------------------------------------------
# plan lint — mechanical coherence checks on the generated work order
# --------------------------------------------------------------------------
#
# Most of "is this plan sound?" turns out to be derivable, not judgement:
# whether a file exists is an index lookup, and whether step 4 uses something
# step 7 creates is arithmetic on step numbers. A model audit can catch the
# rest, but these four never need one — and unlike a model, they cannot be
# talked out of it by a plausible-sounding step.

_CREATE_RE = re.compile(r"\bcreate\b[^`\n]{0,80}`([^`\n]{2,120})`", re.I)
_STEP_RE = re.compile(r"^\s{0,3}(?:step\s+)?(\d{1,2})[.):]\s", re.I | re.M)


def _spec_steps(text):
    """[(step_number, step_text)] — resilient to formatting variation."""
    marks = list(_STEP_RE.finditer(text or ""))
    steps = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        steps.append((int(m.group(1)), text[m.start():end]))
    return steps


def lint_plan(text, index):
    """Findings about the work order that are provably wrong against the index.

    Returns a list of one-line strings; empty means nothing provable is wrong
    (which is NOT the same as the plan being good).
    """
    if not text or not index or not index.get("files"):
        return []
    known = set(index["files"])
    findings = []

    steps = _spec_steps(text)
    created = {}          # path -> step number that creates it
    for num, body in steps:
        for m in _CREATE_RE.finditer(body):
            path = m.group(1).strip().lstrip("./")
            created.setdefault(path, num)
            # check 1: told to create a file that already exists.
            # This exact failure shipped once — see FIELD-NOTES bug 8.
            if path in known:
                findings.append(
                    f"step {num} says to CREATE `{path}` — it already exists "
                    f"({index['files'][path].get('size', '?')} bytes). "
                    f"If the map missed why (e.g. sys.path), verify before overwriting.")

    # check 2: a step uses a path before the step that creates it
    for num, body in steps:
        for m in re.finditer(r"`([^`\n]{2,120})`", body):
            path = m.group(1).strip().lstrip("./")
            if path in created and created[path] > num and path not in known:
                findings.append(
                    f"step {num} references `{path}` but it is not created "
                    f"until step {created[path]} — order is inverted.")

    # check 3: paths that neither exist nor are created by any step
    mentioned = set()
    for m in re.finditer(r"`([^`\n]{2,120})`", text):
        tok = m.group(1).strip().lstrip("./")
        if "/" in tok and re.search(r"\.\w{1,5}$", tok):
            mentioned.add(tok)
    for path in sorted(mentioned):
        if path not in known and path not in created:
            findings.append(
                f"`{path}` is referenced but does not exist and no step "
                f"creates it — possible typo or invented path.")

    # check 4: verification gate that cannot run
    low = text.lower()
    if "pytest" in low and not any(
            n.startswith(("tests/", "test/")) or pathlib.Path(n).name.startswith("test_")
            for n in known if n.endswith(".py")):
        if not any("test" in p for p in created):
            findings.append(
                "verification runs pytest, but the project has no test files "
                "and no step creates any — the gate can never pass.")

    return findings


def lint_block(findings):
    """Render findings as a section appended to the work order."""
    if not findings:
        return ""
    L = ["", "## PLAN LINT — provable inconsistencies, check before executing", ""]
    for f in findings:
        L.append(f"- {f}")
    return "\n".join(L)


# --------------------------------------------------------------------------
# rendering into the prompt
# --------------------------------------------------------------------------

def render(index, max_chars=MAX_RENDER_CHARS):
    """Turn the index into the text block the expander actually reads.

    Docs first, then code: when a request is about documentation, the doc
    outlines are what matter, and they must survive the truncation cap.
    """
    files = index.get("files", {})
    docs, code, other = [], [], []
    for rel, e in sorted(files.items()):
        ext = pathlib.Path(rel).suffix.lower()
        (docs if ext in DOC_EXT else code if ext in CODE_EXT else other).append((rel, e))

    lines, used = [], 0
    truncated = False

    def emit(rel, e):
        nonlocal used, truncated
        if truncated:
            return
        block = [rel]
        if e.get("doc"):
            block.append(f"    “{e['doc']}”")
        if e.get("imports"):
            block.append("    imports: " + ", ".join(e["imports"]))
        for d in e.get("defs", []):
            block.append("    " + d)
        if not e.get("defs") and not e.get("doc"):
            block[0] = f"{rel}  ({e.get('size', 0)} bytes)"
        text = "\n".join(block)
        if used + len(text) > max_chars:
            truncated = True
            return
        lines.append(text)
        used += len(text) + 1

    if docs:
        lines.append("== documentation ==")
        for rel, e in docs:
            emit(rel, e)
    if code:
        lines.append("\n== code ==")
        for rel, e in code:
            emit(rel, e)
    if other:
        lines.append("\n== other files ==")
        names = [rel for rel, _ in other]
        lines.append("    " + ", ".join(names[:120]))

    header = (f"Project: {index.get('root')}  "
              f"({index.get('file_count', 0)} files, "
              f"indexed {time.strftime('%Y-%m-%d %H:%M', time.localtime(index.get('scanned_at', 0)))})")
    if truncated or index.get("truncated"):
        header += "\nNOTE: index truncated — some files are not shown."
    return header + "\n\n" + "\n".join(lines)
