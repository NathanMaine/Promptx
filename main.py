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

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.getenv("PROMPTX_MODEL", "google/gemini-2.5-flash-lite")
LOCAL_URL = os.getenv("PROMPTX_LOCAL_URL", "http://10.0.4.93:11434/v1/chat/completions")
LOCAL_MODEL = os.getenv("PROMPTX_LOCAL_MODEL", "qwen3.6-uncensored:latest")

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
4. End with an explicit verification step — a command to run that proves it worked.
5. Add a short "Do NOT" section naming the likeliest wrong turn for this task.
6. Under 250 words. Dense and specific, not padded.
7. Output ONLY the work order. No preamble, no reasoning, no commentary about \
what you did. Do not explain your process. Begin directly with the first step.

If the request is already specific, tighten it rather than inflating it."""


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


def expand(request, model, context=None, local=False, timeout=180):
    user = request
    if context:
        user = (f"Project structure:\n```\n{context}\n```\n\n"
                f"Request: {request}\n\n"
                f"Use the ACTUAL paths above. If something the request depends on "
                f"is missing from the tree, say so explicitly and instruct the "
                f"agent to create it first.")

    payload = {
        "model": LOCAL_MODEL if local else model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
        "temperature": 0.3,
        "max_tokens": 900,
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
    return text.strip()


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
    args = ap.parse_args()

    if args.models:
        print("\nSuggested expander models (one short call — cheap and fast wins):\n")
        for name, why in SUGGESTED:
            print(f"  {name:38} {why}")
        print(f"\n  current default: {DEFAULT_MODEL}")
        print("  override per-call with -m, or set PROMPTX_MODEL\n")
        return 0

    if not args.request:
        ap.error("give me a request, or use --models")

    ctx = repo_context(args.context) if args.context else None
    result = expand(" ".join(args.request), args.model, ctx, args.local)

    if args.raw or args.exec:
        print(result)
    else:
        who = LOCAL_MODEL if args.local else args.model
        print(f"\n\033[2m── expanded via {who} ──\033[0m\n")
        print(result)
        print(f"\n\033[2m{'─' * 44}\033[0m")

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
