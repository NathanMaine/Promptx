#!/usr/bin/env python3
"""promptx — turn a vague request into an explicit spec before your coding model sees it.

Small local models follow explicit instructions well and infer badly. Given
"build me an auth system" they guess; given "create these three files with
these classes, then run pytest" they execute reliably. Ambiguity is what makes
them loop.

This runs your request through a fast model that rewrites it as an unambiguous
work order — naming files, ordering operations, and adding a verification step.

    promptx "add user auth"                  # expand using the default model
    promptx -c . "add user auth"             # include the repo tree so paths are real
    promptx -c . -x "add user auth"          # expand, then execute via opencode
    promptx --copy "add user auth"           # expand and copy to clipboard
    promptx -m anthropic/claude-haiku-latest "..."   # pick any OpenRouter model
    promptx --local "..."                    # use the Spark instead of OpenRouter
    promptx --models                         # show suggested models

Expansion is one short call, so a cheap fast model is the right tool. Reasoning
models are a poor fit here: they burn tokens thinking and leak traces into the
output.

Config via env (all optional):
    PROMPTX_MODEL       default google/gemini-flash-latest
    OPENROUTER_API_KEY  falls back to opencode's auth store
    PROMPTX_LOCAL_URL   default http://10.0.4.93:11434/v1/chat/completions
    PROMPTX_LOCAL_MODEL default qwen3.6-uncensored:latest
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

# Sits next to this script in ~/bin. Without it, promptx still works on
# filenames alone — you just lose --scan.
try:
    import promptx_index
except ImportError:
    promptx_index = None

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.getenv("PROMPTX_MODEL", "google/gemini-2.5-flash-lite")
LOCAL_URL = os.getenv("PROMPTX_LOCAL_URL", "http://10.0.4.93:11434/v1/chat/completions")
LOCAL_MODEL = os.getenv("PROMPTX_LOCAL_MODEL", "qwen3.6-uncensored:latest")
INDEX_DIR = pathlib.Path(os.getenv("PROMPTX_INDEX_DIR",
                                   str(pathlib.Path.home() / ".promptx/index")))
HOSTED_URL = os.getenv("PROMPTX_HOSTED_URL", "http://10.0.4.88:7331")

SUGGESTED = [
    ("google/gemini-2.5-flash-lite", "$0.10/M — fast, clean output. Default."),
    ("qwen/qwen3.7-flash", "$0.03/M — cheapest non-free, 1M context"),
    ("meta-llama/llama-3.1-8b-instruct", "$0.05/M — small and literal"),
    ("inclusionai/ling-3.0-flash:free", "free tier — fine for this, rate limited"),
    ("anthropic/claude-haiku-4.5", "pricier, best instruction-following if it matters"),
    ("--local", "the Spark — free, but emits thinking traces"),
]

SYSTEM = """You rewrite vague software requests into explicit work orders for a \
coding agent. You do NOT write the code. You produce instructions.

The agent you are writing for is capable but literal. It follows precise \
instructions well and guesses badly. Ambiguity makes it loop.

Rules for your output:

1. Name EVERY file to create or modify, with its full relative path. Never say \
"the appropriate file" or "relevant modules".
2. If a package needs several modules, list each one separately with what it \
must contain. A missing sibling module is the single most common cause of \
import-error loops.
3. State the order of operations. Create dependencies BEFORE the files that \
import them.
4. End with a VERIFICATION section the agent cannot fake:
   - Give exact commands. Prefer `python3` over `python` — `python` is often absent.
   - Run the WHOLE suite, never a single test file.
   - Demand the raw output pasted VERBATIM — not summarized, not counted by hand.
   - Include a command that proves which files changed (e.g. `git diff --stat`).
   - State: report numbers exactly as printed, including failures. Do not round,
     total, or characterize them. If something fails, say so and stop.
   - For build tasks: include a NEW or EXTENDED test that FAILS before the
     change and PASSES after it. A suite that is green both before and after
     proves nothing about the work — it only proves nothing broke.
5. Add a short "Do NOT" section naming the likeliest wrong turn for this task.
5b. If the request admits more than one reasonable reading (e.g. "coverage" could mean test coverage or feature coverage), the FIRST line of your output must be:  READ AS: <the reading you chose>  — one line, so a wrong choice is caught in one second instead of after an hour of correct work on the wrong goal. If only one reading is sensible, omit the line entirely; do not use it to restate obvious requests.
6. Under 250 words. Dense and specific, not padded.
7. Output ONLY the work order. No preamble, no reasoning, no commentary about \
what you did. Do not explain your process. Begin directly with the first step (or the READ AS line when present).

If the request is already specific, tighten it rather than inflating it."""

# Used when --scan has built a structural map. Without it, the model only sees
# filenames and correctly refuses anything that depends on what the code says.
SYSTEM_DEEP = SYSTEM + """

You have also been given a STRUCTURAL MAP of the project: per file, its imports,
class and function signatures, and docstrings; per document, its heading outline.

You do NOT have the full source. So:
- You MAY state which files are relevant, what is missing, what is inconsistent
  between docs and code, and what a change must touch.
- You MAY NOT assert what a function body does beyond what its name, signature,
  and docstring show. Where the task depends on the implementation, instruct the
  agent to read that specific file first — naming it exactly.
- For documentation tasks, compare the heading outlines against the code
  structure and name concrete gaps: docs describing code that no longer exists,
  and code with no documentation covering it."""


def api_key():
    """OPENROUTER_API_KEY, else opencode's auth store. Never logged."""
    k = os.getenv("OPENROUTER_API_KEY")
    if k:
        return k
    auth = pathlib.Path.home()/".local/share/opencode/auth.json"
    if auth.exists():
        try:
            entry = json.loads(auth.read_text()).get("openrouter") or {}
            if entry.get("key"):
                return entry["key"]
        except (json.JSONDecodeError, OSError):
            pass
    raise SystemExit(
        "promptx: no OpenRouter key found.\n"
        "  set OPENROUTER_API_KEY, or run `opencode` and add the provider,\n"
        "  or use --local to run against the Spark instead.")


def repo_context(root, max_files=120):
    """Compact file tree so the expander names real paths, not invented ones."""
    root = pathlib.Path(root).resolve()
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
            "build", ".next", "target", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
    out = []
    for p in sorted(root.rglob("*")):
        if any(s in p.parts for s in skip):
            continue
        if p.is_file():
            try:
                out.append(str(p.relative_to(root)))
            except ValueError:
                continue
            if len(out) >= max_files:
                out.append("... (truncated)")
                break
    return "\n".join(out) if out else "(empty directory)"


def load_idx(root):
    """The stored index for a folder, or None."""
    if promptx_index is None or not root:
        return None
    idx = promptx_index.load_index(root, INDEX_DIR)
    return idx if (idx and idx.get("files")) else None


def deep_context(root):
    """Rendered structural map for a folder, if it has been scanned."""
    idx = load_idx(root)
    return promptx_index.render(idx) if idx else None


def enforce_verification(text, idx):
    """Guarantee the work order ends with a gate that demands evidence.

    The system prompt asks for one, and usually gets it — but "usually" is the
    wrong standard for the step whose absence let an agent report a green suite
    it never ran. So this is decided in code: if the model's own gate does not
    demand verbatim output, a derived one is appended. No second model, because
    a probabilistic check on the honesty layer defeats the point.
    """
    if not idx or promptx_index is None:
        return text
    if not promptx_index.has_verification(text):
        text = text.rstrip() + "\n\n" + promptx_index.verification_block(idx)
    return text + promptx_index.lint_block(promptx_index.lint_plan(text, idx))


def do_scan_cli(root, push=False):
    """Scan a folder, and optionally publish the map to the hosted instance.

    Scanning happens here, on the machine that can actually see the files. Only
    the resulting map travels — which is what makes a laptop project usable from
    the hosted UI on a phone.
    """
    root = str(pathlib.Path(root).expanduser().resolve())
    print(f"scanning {root} ...")
    try:
        idx = promptx_index.scan(root, INDEX_DIR)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"promptx: {exc}")

    print(f"  {idx['file_count']} files  ({idx['parsed']} read, "
          f"{idx['reused']} unchanged){'  [truncated]' if idx['truncated'] else ''}")
    rendered = promptx_index.render(idx)
    print(f"  map is ~{len(rendered) // 4} tokens")

    if push:
        body = json.dumps(idx).encode()
        url = HOSTED_URL.rstrip("/") + "/api/index"
        req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read())
        except (urllib.error.URLError, OSError) as exc:
            raise SystemExit(f"promptx: could not reach {url}: {exc}")
        if res.get("error"):
            raise SystemExit(f"promptx: push rejected: {res['error']}")
        print(f"  pushed to {HOSTED_URL} — pick this folder there to use it")

    print(f"\nnow:  promptx -c {root} \"your request\"")
    return 0


def expand(request, model, context=None, local=False, timeout=180, deep=None):
    user = request
    if deep:
        user = (f"{deep}\n\nRequest: {request}\n\n"
                f"Use the ACTUAL paths above. Where the request depends on code "
                f"whose body you cannot see, instruct the agent to read that "
                f"specific file first. If something it depends on is missing "
                f"entirely, say so and instruct the agent to create it.")
    elif context:
        user = (f"Project structure:\n```\n{context}\n```\n\n"
                f"Request: {request}\n\n"
                f"Use the ACTUAL paths above. If something the request depends on "
                f"is missing from the tree, say so explicitly and instruct the "
                f"agent to create it first.")

    payload = {
        "model": LOCAL_MODEL if local else model,
        "messages": [{"role": "system", "content": SYSTEM_DEEP if deep else SYSTEM},
                     {"role": "user", "content": user}],
        "temperature": 0.3,
        "max_tokens": 2400,
    }
    headers = {"Content-Type": "application/json"}
    url = LOCAL_URL if local else OPENROUTER_URL
    if not local:
        headers["Authorization"] = f"Bearer {api_key()}"
        headers["HTTP-Referer"] = "https://github.com/NathanMaine"
        headers["X-Title"] = "promptx"

    req = urllib.request.Request(url, json.dumps(payload).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:200] if hasattr(exc, "read") else ""
        raise SystemExit(f"promptx: HTTP {exc.code} from {url}\n  {detail}")
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"promptx: cannot reach {url}: {exc}")

    if "error" in data and "choices" not in data:
        raise SystemExit(f"promptx: API error: {str(data['error'])[:200]}")
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise SystemExit(f"promptx: unexpected response: {str(data)[:200]}")

    # some models leak reasoning; keep only what follows the final marker
    for marker in ("</think>", "</thinking>"):
        if marker in text:
            text = text.split(marker)[-1]
    text = text.strip()

    # A work order that stops mid-sentence is worse than a short one: the agent
    # executes the visible steps and never learns the rest existed. Surface it.
    if (data.get("choices") or [{}])[0].get("finish_reason") == "length":
        text += ("\n\n**WARNING — this work order was CUT OFF at the token limit "
                 "and is incomplete.** Steps after the last one shown are missing. "
                 "Narrow the request, or point -c at a subdirectory.")
    return text


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("request", nargs="*", help="what you want, in plain words")
    ap.add_argument("-c", "--context", metavar="DIR",
                    help="include this project's file tree so paths are real")
    ap.add_argument("-x", "--exec", action="store_true",
                    help="run the expanded prompt through opencode")
    ap.add_argument("-m", "--model", default=DEFAULT_MODEL,
                    help=f"OpenRouter model (default {DEFAULT_MODEL})")
    ap.add_argument("--local", action="store_true",
                    help="use the Spark instead of OpenRouter")
    ap.add_argument("--copy", action="store_true", help="copy result to clipboard")
    ap.add_argument("--raw", action="store_true", help="no header, just the prompt")
    ap.add_argument("--models", action="store_true", help="show suggested models")
    ap.add_argument("--scan", action="store_true",
                    help="index -c DIR so it can answer questions about the code, "
                         "not just name paths (incremental: only changed files)")
    ap.add_argument("--push", action="store_true",
                    help="upload the index to the hosted promptx so other devices "
                         f"can use it (default {HOSTED_URL})")
    ap.add_argument("--folders", action="store_true", help="list indexed folders")
    ap.add_argument("--snap", action="store_true",
                    help="record -c DIR's current state (and this work order) "
                         "so --check can later compare reality against it")
    ap.add_argument("--check", action="store_true",
                    help="compare -c DIR against the last --snap: what really "
                         "changed, what the spec named, and the test result "
                         "run HERE. Exits 1 on mismatch.")
    ap.add_argument("--no-tests", action="store_true",
                    help="with --snap/--check, skip running the test suite")
    args = ap.parse_args()

    if args.check:
        if promptx_index is None:
            raise SystemExit("promptx: promptx_index.py is not installed next to promptx")
        if not args.context:
            raise SystemExit("promptx: --check needs a folder, e.g.  promptx -c . --check")
        rep = promptx_index.compare(args.context, INDEX_DIR,
                                    with_tests=not args.no_tests)
        print(promptx_index.format_report(rep))
        if rep.get("error"):
            return 2
        bad = (rep["spec_untouched"] or rep["unspecified"]
               or (rep.get("tests_after") or {}).get("returncode") not in (0, None))
        return 1 if bad else 0

    if args.folders:
        if promptx_index is None:
            raise SystemExit("promptx: promptx_index.py is not installed next to promptx")
        rows = promptx_index.list_indexed(INDEX_DIR)
        if not rows:
            print("\n  no folders indexed yet — run:  promptx -c . --scan\n")
            return 0
        print("\nIndexed folders:\n")
        for r in rows:
            import time as _t
            when = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(r["scanned_at"]))
            print(f"  {r['file_count']:>5} files  {when}  {r['root']}")
        print()
        return 0

    if args.scan or args.push:
        if promptx_index is None:
            raise SystemExit("promptx: promptx_index.py is not installed next to promptx")
        if not args.context:
            raise SystemExit("promptx: --scan needs a folder, e.g.  promptx -c . --scan")
        return do_scan_cli(args.context, push=args.push)

    if args.snap and not args.request:
        # Web-UI bridge: the spec was generated in the browser and Copied.
        # Read it from the clipboard so snap still knows which paths the work
        # order names — the flow is Copy -> `promptx -c DIR --snap` -> paste
        # into the agent -> `promptx -c DIR --check`.
        if promptx_index is None:
            raise SystemExit("promptx: promptx_index.py is not installed next to promptx")
        if not args.context:
            raise SystemExit("promptx: --snap needs a folder, e.g.  promptx -c . --snap")
        spec = None
        try:
            out = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
            clip = (out.stdout or "").strip()
            # Only treat the clipboard as a work order if it looks like one —
            # otherwise whatever you last copied becomes the "spec" silently.
            if len(clip) > 40 and ("`" in clip or "VERIFICATION" in clip.upper()):
                spec = clip
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
        idx = load_idx(args.context)
        snap = promptx_index.snapshot(args.context, INDEX_DIR, spec=spec,
                                      with_tests=not args.no_tests, index=idx)
        src = "clipboard spec" if spec else "no spec (baseline only)"
        t = snap["tests"]["summary"] if snap["tests"] else "not run"
        print(f"snapshot: {len(snap['files'])} files · {src} · "
              f"{len(snap['spec_files'])} path(s) named · tests: {t}")
        print(f"when the agent is done:  promptx -c {args.context!r} --check")
        return 0

    if args.models:
        print("\nSuggested expander models (one short call — cheap and fast wins):\n")
        for name, why in SUGGESTED:
            print(f"  {name:38} {why}")
        print(f"\n  current default: {DEFAULT_MODEL}")
        print("  override per-call with -m, or set PROMPTX_MODEL\n")
        return 0

    if not args.request:
        ap.error("give me a request, or use --models")

    idx = load_idx(args.context) if args.context else None
    deep = promptx_index.render(idx) if idx else None
    ctx = None if deep else (repo_context(args.context) if args.context else None)
    result = expand(" ".join(args.request), args.model, ctx, args.local, deep=deep)
    result = enforce_verification(result, idx)

    if args.raw or args.exec:
        print(result)
    else:
        who = LOCAL_MODEL if args.local else args.model
        print(f"\n\033[2m── expanded via {who} ──\033[0m\n")
        print(result)
        print(f"\n\033[2m{'─' * 44}\033[0m")

    if args.snap:
        if promptx_index is None:
            raise SystemExit("promptx: promptx_index.py is not installed next to promptx")
        if not args.context:
            raise SystemExit("promptx: --snap needs a folder, e.g.  promptx -c . --snap \"...\"")
        snap = promptx_index.snapshot(args.context, INDEX_DIR, spec=result,
                                      with_tests=not args.no_tests, index=idx)
        n = len(snap["spec_files"])
        print(f"\n\033[2m── snapshot: {len(snap['files'])} files, "
              f"{n} path(s) parsed from this work order"
              f"{'' if snap['tests'] is None else ', tests: ' + snap['tests']['summary']}"
              f"\033[0m", file=sys.stderr)
        print(f"\033[2m   when the agent is done:  promptx -c {args.context} --check\033[0m",
              file=sys.stderr)

    if args.copy:
        try:
            subprocess.run(["pbcopy"], input=result.encode(), check=True)
            print("\033[2m(copied to clipboard)\033[0m", file=sys.stderr)
        except (FileNotFoundError, subprocess.CalledProcessError):
            print("promptx: clipboard copy failed", file=sys.stderr)

    if args.exec:
        print("\n\033[2m── executing via opencode ──\033[0m\n", file=sys.stderr)
        subprocess.run(["opencode", "run", result])

    return 0


if __name__ == "__main__":
    sys.exit(main())
