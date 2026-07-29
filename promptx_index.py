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
# the verification gate
# --------------------------------------------------------------------------

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
