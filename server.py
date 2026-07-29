#!/usr/bin/env python3
"""promptx — hosted prompt expander. Turns vague requests into explicit work orders.

Runs on the NAS so it is always available. Reads its OpenRouter key from
/volume1/Projects/promptx/.env (chmod 600).

    python3 promptx-server.py --port 7331
"""

import argparse
import http.server
import json
import os
import pathlib
import socketserver
import time
import urllib.error
import urllib.request

# Optional: without it, promptx still works on filenames alone.
try:
    import promptx_index
except ImportError:
    promptx_index = None

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SPARK_URL = os.getenv("PROMPTX_SPARK_URL", "http://10.0.4.93:11434/v1/chat/completions")
SPARK_MODEL = os.getenv("PROMPTX_SPARK_MODEL", "qwen3.6-uncensored:latest")
ENV_FILE = pathlib.Path(os.getenv("PROMPTX_ENV", "/volume1/Projects/promptx/.env"))
PROJECT_ROOTS = ["/volume1/Projects", "/volume1/@home/Natron"]
INDEX_DIR = pathlib.Path(os.getenv("PROMPTX_INDEX_DIR", "/app/.index"))

# id, label, cost, strength, best-for
MODELS = [
    ("google/gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite", "$0.10/M",
     "Balanced and clean",
     "The default. Fast, never emits reasoning traces, and reliably produces "
     "tidy numbered steps. Best all-round choice for turning a feature request "
     "into a file-by-file plan."),
    ("qwen/qwen3.7-flash", "Qwen3.7 Flash", "$0.03/M",
     "Cheapest paid · 1M context",
     "A third the price of the default with a huge context window. Use it when "
     "you are pasting a very large project tree, or when you are iterating a lot "
     "and want cost near zero. Slightly less polished formatting."),
    ("meta-llama/llama-3.1-8b-instruct", "Llama 3.1 8B", "$0.05/M",
     "Small and literal",
     "Follows the template rigidly and adds little of its own. Good when you "
     "already know exactly what you want and just need it formatted as explicit "
     "steps. Weakest at inferring unstated requirements."),
    ("inclusionai/ling-3.0-flash:free", "Ling 3.0 Flash", "free",
     "No cost · rate limited",
     "Free tier, perfectly adequate for this one-shot rewriting task. Falls over "
     "under heavy use because of rate limits — fine for occasional expansions."),
    ("anthropic/claude-haiku-4.5", "Claude Haiku 4.5", "~$1/M",
     "Best reasoning of the cheap tier",
     "Noticeably better at catching what you did NOT say — missing dependencies, "
     "ordering problems, edge cases. Worth the extra cost for gnarly multi-file "
     "refactors where a bad plan wastes an hour."),
    ("__local__", "Spark (local)", "free",
     "Runs on your own hardware",
     "Uses the DGX Spark's own model. Nothing leaves the building and it costs "
     "nothing, but it emits &lt;think&gt; reasoning traces and is more verbose. "
     "Use it for sensitive work or when you are offline from the cloud."),
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
5. Add a short "Do NOT" section naming the likeliest wrong turn for this task.
6. Under 250 words. Dense and specific, not padded.
7. Output ONLY the work order. No preamble, no reasoning, no commentary about \
what you did. Begin directly with the first step.

IMPORTANT: You can see file NAMES but not file CONTENTS. If the task requires \
reading code to answer (an audit, a review, "what does X do"), do NOT invent \
findings. Instead instruct the agent to read the relevant files first and \
report what it actually finds.

If the request is already specific, tighten it rather than inflating it."""

# Used when the folder has been indexed. The honesty clause above exists
# because filenames alone cannot support an audit; with a structural map that
# constraint genuinely relaxes, so repeating it would make the model refuse
# work it is now equipped to do.
SYSTEM_DEEP = SYSTEM.replace(
    """IMPORTANT: You can see file NAMES but not file CONTENTS. If the task requires \
reading code to answer (an audit, a review, "what does X do"), do NOT invent \
findings. Instead instruct the agent to read the relevant files first and \
report what it actually finds.""",
    """You have been given a STRUCTURAL MAP of the project: for each file, its \
imports, class and function signatures, and docstrings; for each document, its \
heading outline. You can see what exists and what each piece is for.

You do NOT have the full source. So:
- You MAY state which files are relevant, what is missing, what is inconsistent \
between docs and code, and what a change must touch.
- You MAY NOT assert what a function body does beyond what its name, signature, \
and docstring show. Where the task depends on the implementation, instruct the \
agent to read that specific file first — naming it exactly.
- For documentation tasks, compare the heading outlines against the code \
structure and name the concrete gaps: sections that describe code that no \
longer exists, and code with no documentation covering it.""")


def api_key():
    k = os.getenv("OPENROUTER_API_KEY")
    if k:
        return k
    if ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text().splitlines():
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return None


def safe_root(p):
    """Only allow context from known project roots."""
    try:
        rp = pathlib.Path(p).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    for root in PROJECT_ROOTS:
        try:
            rp.relative_to(pathlib.Path(root).resolve())
            return rp
        except ValueError:
            continue
    return None


def repo_context(root, max_files=150):
    rp = safe_root(root)
    if rp is None:
        return None, f"path must be under {' or '.join(PROJECT_ROOTS)}"
    if not rp.is_dir():
        return None, f"not a directory: {rp}"
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
            "build", ".next", "target", ".mypy_cache", ".pytest_cache"}
    out = []
    for p in sorted(rp.rglob("*")):
        if any(s in p.parts for s in skip):
            continue
        if p.is_file():
            try:
                out.append(str(p.relative_to(rp)))
            except ValueError:
                continue
            if len(out) >= max_files:
                out.append("... (truncated)")
                break
    return ("\n".join(out) if out else "(empty)"), None


def deep_context(ctx_dir):
    """Rendered structural map for a folder, if one has been indexed.

    Deliberately does NOT go through safe_root. An index is inert data that was
    already built and stored; rendering it touches no filesystem. That is what
    lets a laptop push an index for /Users/you/project and then expand against
    it from a phone — the NAS never needs to see those files, only the map.

    Returns None when there is no index, and the caller falls back to a plain
    filename listing (which does go through safe_root, because it reads disk).
    """
    if promptx_index is None or not ctx_dir:
        return None
    idx = promptx_index.load_index(ctx_dir, INDEX_DIR)
    if not idx or not idx.get("files"):
        return None
    return promptx_index.render(idx)


def expand(request, model, ctx_dir):
    ctx, deep = None, None
    if ctx_dir and ctx_dir.strip():
        ctx_dir = ctx_dir.strip()
        deep = deep_context(ctx_dir)
        if deep is None:
            ctx, err = repo_context(ctx_dir)
            if err:
                return None, err

    user = request
    if deep:
        user = (f"{deep}\n\nRequest: {request}\n\n"
                f"Use the ACTUAL paths above. Where the request depends on code "
                f"you cannot see the body of, instruct the agent to read that "
                f"specific file first. If something the request depends on is "
                f"missing entirely, say so and instruct the agent to create it.")
    elif ctx:
        user = (f"Project structure:\n```\n{ctx}\n```\n\nRequest: {request}\n\n"
                f"Use the ACTUAL paths above. If something the request depends on "
                f"is missing from the tree, say so explicitly and instruct the "
                f"agent to create it first.")

    local = model == "__local__"
    payload = {"model": SPARK_MODEL if local else model,
               "messages": [{"role": "system", "content": SYSTEM_DEEP if deep else SYSTEM},
                            {"role": "user", "content": user}],
               "temperature": 0.3, "max_tokens": 900}
    headers = {"Content-Type": "application/json"}
    url = SPARK_URL if local else OPENROUTER_URL
    if not local:
        key = api_key()
        if not key:
            return None, "No OpenRouter key configured on the NAS. Pick the Spark model, or add the key to /volume1/Projects/promptx/.env"
        headers["Authorization"] = f"Bearer {key}"
        headers["HTTP-Referer"] = "http://10.0.4.88:8888"
        headers["X-Title"] = "promptx"

    try:
        req = urllib.request.Request(url, json.dumps(payload).encode(), headers)
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            pass
        return None, f"HTTP {e.code}: {msg or 'request rejected'}"
    except (urllib.error.URLError, OSError) as e:
        return None, f"Cannot reach {url}: {e}"

    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return None, f"Unexpected response: {str(data)[:200]}"
    for m in ("</think>", "</thinking>"):
        if m in text:
            text = text.split(m)[-1]
    text = text.strip()

    # Guarantee the evidence gate in code, not by sampling. See
    # promptx_index.verification_block for why this is not a second model.
    if deep and promptx_index is not None:
        idx = promptx_index.load_index(ctx_dir, INDEX_DIR)
        if idx and not promptx_index.has_verification(text):
            text = text + "\n\n" + promptx_index.verification_block(idx)
    return text, None


def do_scan(ctx_dir, force=False):
    """Build or refresh the structural index for one folder."""
    if promptx_index is None:
        return {"error": "promptx_index.py is not installed next to server.py"}
    if not ctx_dir or not ctx_dir.strip():
        return {"error": "no folder given"}
    rp = safe_root(ctx_dir.strip())
    if rp is None:
        return {"error": f"path must be under {' or '.join(PROJECT_ROOTS)}"}
    if not rp.is_dir():
        return {"error": f"not a directory: {rp}"}

    started = time.time()
    try:
        idx = promptx_index.scan(rp, INDEX_DIR, force=force)
    except (OSError, ValueError) as e:
        return {"error": f"scan failed: {e}"}

    return {"ok": True, "root": str(rp),
            "file_count": idx["file_count"], "parsed": idx["parsed"],
            "reused": idx["reused"], "truncated": idx["truncated"],
            "scanned_at": idx["scanned_at"],
            "seconds": round(time.time() - started, 1)}


def store_index(payload):
    """Accept an index built on another machine and cache it here.

    This is what makes the hosted UI useful for projects that live on a laptop
    rather than on this box: `promptx --push <folder>` scans locally, where the
    files actually are, and uploads only the structural map. Expansion then
    works from any device, including ones that cannot see those files at all.

    Only the map is transmitted — signatures, docstrings, and headings. No file
    bodies, and no config values (the indexer records key names only).
    """
    if promptx_index is None:
        return {"error": "promptx_index.py is not installed next to server.py"}
    root = (payload.get("root") or "").strip()
    files = payload.get("files")
    if not root or not isinstance(files, dict):
        return {"error": "need root and files"}
    if len(json.dumps(files)) > 4_000_000:
        return {"error": "index too large (>4MB)"}

    index = {"root": root,
             "scanned_at": int(payload.get("scanned_at") or time.time()),
             "file_count": len(files),
             "parsed": int(payload.get("parsed") or 0),
             "reused": int(payload.get("reused") or 0),
             "truncated": bool(payload.get("truncated")),
             "remote": True,
             "files": files}
    try:
        p = promptx_index.cache_path(root, INDEX_DIR)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(index), encoding="utf-8")
    except OSError as e:
        return {"error": f"could not store index: {e}"}
    return {"ok": True, "root": root, "file_count": len(files)}


def list_folders():
    """Folders you can pick, each tagged with whether it has been indexed.

    Combines the immediate subdirectories of every project root with anything
    already in the index cache, so switching between projects is a dropdown
    rather than remembering absolute paths.
    """
    indexed = {}
    if promptx_index is not None:
        for row in promptx_index.list_indexed(INDEX_DIR):
            indexed[row["root"]] = row

    found = []
    for root in PROJECT_ROOTS:
        rp = pathlib.Path(root)
        if not rp.is_dir():
            continue
        try:
            for child in sorted(rp.iterdir()):
                if child.is_dir() and not child.name.startswith((".", "@", "#")):
                    found.append(str(child))
        except OSError:
            continue

    for path in indexed:
        if path not in found:
            found.append(path)

    out = []
    for path in sorted(set(found)):
        row = indexed.get(path)
        out.append({"path": path,
                    "indexed": bool(row),
                    "file_count": row["file_count"] if row else 0,
                    "scanned_at": row["scanned_at"] if row else 0,
                    # A pushed index describes files this host cannot see, so
                    # it can never be rescanned here — only re-pushed from the
                    # machine that owns them.
                    "remote": bool(row and row.get("remote")),
                    "scannable": bool(safe_root(path))})
    # indexed folders first — those are the ones being actively worked on
    return sorted(out, key=lambda r: (not r["indexed"], -r["scanned_at"], r["path"]))


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>promptx — Prompt Expander</title><style>
:root{--bg:#0f1420;--panel:#171d2b;--ink:#e8edf5;--muted:#94a0b4;--line:#26303f;
      --accent:#6ea8fe;--chip:#1e2636;--sh:0 2px 14px rgba(0,0,0,.35)}
@media(prefers-color-scheme:light){:root{--bg:#eef1f6;--panel:#fff;--ink:#1a2230;
  --muted:#5a6474;--line:#e2e7ef;--accent:#2563eb;--chip:#eef2f8;--sh:0 2px 12px rgba(20,30,60,.08)}}
:root[data-theme=light]{--bg:#eef1f6;--panel:#fff;--ink:#1a2230;--muted:#5a6474;
  --line:#e2e7ef;--accent:#2563eb;--chip:#eef2f8;--sh:0 2px 12px rgba(20,30,60,.08)}
:root[data-theme=dark]{--bg:#0f1420;--panel:#171d2b;--ink:#e8edf5;--muted:#94a0b4;
  --line:#26303f;--accent:#6ea8fe;--chip:#1e2636;--sh:0 2px 14px rgba(0,0,0,.35)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:34px 20px 70px}
header{display:flex;align-items:center;gap:14px;margin-bottom:6px}
.logo{font-size:34px}h1{margin:0;font-size:26px;letter-spacing:-.02em}
.sub{color:var(--muted);margin:2px 0 0;font-size:14px}
.themebtn{margin-left:auto;background:var(--panel);border:1px solid var(--line);
  border-radius:10px;padding:8px 12px;cursor:pointer;color:var(--muted)}
.back{color:var(--muted);text-decoration:none;font-size:13px}.back:hover{color:var(--accent)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
  padding:18px;box-shadow:var(--sh);margin-top:22px}
textarea{width:100%;min-height:92px;resize:vertical;border:1px solid var(--line);
  border-radius:10px;padding:12px;font:inherit;background:var(--bg);color:var(--ink)}
textarea:focus,input:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px}
.row{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap;align-items:center}
input[type=text],select{flex:1;min-width:200px;border:1px solid var(--line);
  border-radius:10px;padding:10px 12px;font:inherit;background:var(--bg);color:var(--ink)}
button{border:0;border-radius:10px;padding:10px 18px;font:inherit;font-weight:650;
  cursor:pointer;background:var(--accent);color:#fff;font-size:14px}
button:disabled{opacity:.5;cursor:default}
button.ghost{background:transparent;color:var(--muted);border:1px solid var(--line);font-weight:500}
.hint{color:var(--muted);font-size:12.5px;margin-top:10px}
.ixrow{display:flex;align-items:center;gap:8px;margin-top:10px;flex-wrap:wrap}
.prog{margin-top:10px}
.ptrack{height:6px;border-radius:99px;background:var(--chip,rgba(127,127,127,.18));overflow:hidden}
.pbar{height:100%;width:0;border-radius:99px;background:var(--accent,#2563eb);
  transition:width .12s linear}
.ptxt{font-size:12px;color:var(--muted);margin-top:6px;font-variant-numeric:tabular-nums}
.ixstat{font-size:12.5px;color:var(--muted);line-height:1.7}
.ixstat code{background:var(--chip,rgba(127,127,127,.14));padding:2px 6px;
  border-radius:5px;font-size:12px;user-select:all}
.ixstat.on{color:#16a34a}
.ixstat.err{color:#dc2626}
.mbox{background:var(--chip);border:1px solid var(--line);border-radius:10px;
  padding:12px 14px;margin-top:12px}
.mhead{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
.mname{font-weight:650;font-size:14.5px}
.mcost{color:var(--accent);font-size:12.5px;font-variant-numeric:tabular-nums;font-weight:600}
.mtag{color:var(--muted);font-size:12.5px}
.mdesc{color:var(--muted);font-size:13px;margin-top:6px;line-height:1.5}
.out{margin-top:24px}
.outhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:10px}
.meta{color:var(--muted);font-size:12.5px}
pre{background:var(--chip);border:1px solid var(--line);border-radius:12px;padding:16px;
  white-space:pre-wrap;word-wrap:break-word;margin:0;
  font:13.5px/1.62 ui-monospace,SFMono-Regular,Menlo,monospace}
.err{border-color:#e0684f;color:#e0684f}
.spin{display:inline-block;width:13px;height:13px;border:2px solid var(--line);
  border-top-color:#fff;border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px}
@keyframes s{to{transform:rotate(360deg)}}
.hist{margin-top:30px}.hist h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;
  color:var(--muted);margin:0 0 10px;font-weight:700}
.hitem{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:11px 13px;margin-bottom:8px;cursor:pointer}
.hitem:hover{border-color:var(--accent)}
.hq{font-size:13.5px}.hm{color:var(--muted);font-size:12px;margin-top:2px}
footer{color:var(--muted);font-size:12.5px;margin-top:34px;border-top:1px solid var(--line);padding-top:14px}
</style></head><body><div class="wrap">
<header><span class="logo">✍️</span><div><h1>promptx</h1>
<p class="sub">Turns a vague request into an explicit work order · paste the result into your coding agent</p></div>
<button class="themebtn" id="tb">◑ Theme</button></header>
<a class="back" href="http://10.0.4.88:8888/">← NAS Services</a>

<div class="card">
  <textarea id="q" placeholder="What do you want done?&#10;e.g. build out the gtm adapters package" autofocus></textarea>
  <div class="row">
    <input type="text" id="dir" list="folders" placeholder="Project folder (optional) — e.g. /volume1/Projects/my-app">
    <datalist id="folders"></datalist>
    <select id="model"></select>
    <button id="go">Expand</button>
  </div>
  <div class="ixrow">
    <button class="ghost" id="scan">Scan folder</button>
    <button class="ghost" id="refresh" hidden>Refresh</button>
    <button class="ghost" id="pick">Scan a folder on this computer…</button>
    <input type="file" id="picker" webkitdirectory directory multiple hidden>
    <span class="ixstat" id="ixstat"></span>
  </div>
  <div class="prog" id="prog" hidden>
    <div class="ptrack"><div class="pbar" id="pbar"></div></div>
    <div class="ptxt" id="ptxt"></div>
  </div>
  <div class="hint">⌘↵ / Ctrl↵ to submit · a folder lets it name real paths · <b>Scan</b> reads the code structure so it can answer questions about what is already there</div>
  <div class="mbox" id="mbox"></div>
</div>

<div class="out" id="out" hidden>
  <div class="outhead"><span class="meta" id="meta"></span>
    <span><button class="ghost" id="again">Try again</button>
    <button id="copy">Copy</button></span></div>
  <pre id="res"></pre>
</div>

<div class="hist" id="hist" hidden><h2>Earlier</h2><div id="hlist"></div></div>

<footer><b>When to use it:</b> building or changing files — anywhere the agent needs a precise spec.
<b>When not to:</b> audits and code review, since promptx sees file names but not contents — ask your agent directly for those.
Always read the expansion before running it; it commits confidently to one reading of an ambiguous request.</footer>

<script>
const $=i=>document.getElementById(i);
const MODELS=__MODELS__;
const ROOTS=__ROOTS__;
const tb=$('tb');
tb.onclick=()=>{const c=document.documentElement.getAttribute('data-theme');
  const n=c==='light'?'dark':'light';document.documentElement.setAttribute('data-theme',n);
  localStorage.setItem('px_theme',n)};
const st=localStorage.getItem('px_theme');if(st)document.documentElement.setAttribute('data-theme',st);

MODELS.forEach(m=>{const o=document.createElement('option');o.value=m[0];
  o.textContent=`${m[1]} · ${m[2]}`;$('model').appendChild(o)});
$('dir').value=localStorage.getItem('px_dir')||'';
$('model').value=localStorage.getItem('px_model')||MODELS[0][0];

/* ---- project folders and the structural index ---- */
let FOLDERS=[];
function fmtAge(ts){if(!ts)return'';const s=Math.floor(Date.now()/1000)-ts;
  if(s<90)return'just now';if(s<5400)return Math.round(s/60)+'m ago';
  if(s<172800)return Math.round(s/3600)+'h ago';return Math.round(s/86400)+'d ago'}
function curFolder(){return FOLDERS.find(f=>f.path===$('dir').value.trim())}
function esc(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML}
/* Can THIS host read that path? Anything outside the project roots lives on
   another machine, so scanning has to happen there and be pushed here. */
function onThisHost(p){return ROOTS.some(r=>p===r||p.startsWith(r.replace(/\/+$/,'')+'/'))}
function pushHint(d){
  /* Only suggest the CLI when the key is a real absolute path. A folder-name
     key came from the browser picker, and `promptx -c promptx` would be wrong. */
  const cli=d.startsWith('/')
    ? ', or run <code>promptx -c '+esc(d)+' --scan --push</code> on that machine' : '';
  return 'click <b>Scan a folder on this computer…</b> and pick it'+cli}
function drawIx(){
  const d=$('dir').value.trim(),f=curFolder(),s=$('ixstat');
  const scan=$('scan'),ref=$('refresh');
  if(!d){s.textContent='';s.className='ixstat';scan.hidden=true;ref.hidden=true;return}
  const here=onThisHost(d);
  if(f&&f.indexed){
    if(here){
      s.textContent='indexed · '+f.file_count+' files · '+fmtAge(f.scanned_at);
      s.className='ixstat on';scan.hidden=true;ref.hidden=false}
    else{
      /* A pushed index. This host cannot rescan it — offering Refresh would
         just produce a path error. Show the command that actually works. */
      s.innerHTML='indexed from another machine · '+f.file_count+' files · '+
        fmtAge(f.scanned_at)+'<br>to update: '+pushHint(d);
      s.className='ixstat on';scan.hidden=true;ref.hidden=true}}
  else{
    if(here){
      s.textContent='not indexed — it can see file names only';
      s.className='ixstat';scan.hidden=false;ref.hidden=true}
    else{
      s.innerHTML='not on this NAS — '+pushHint(d);
      s.className='ixstat';scan.hidden=true;ref.hidden=true}}}
async function loadFolders(){
  try{const r=await fetch('/api/folders');FOLDERS=(await r.json()).folders||[]}
  catch(e){FOLDERS=[]}
  const dl=$('folders');dl.innerHTML='';
  FOLDERS.forEach(f=>{const o=document.createElement('option');o.value=f.path;
    if(f.indexed)o.label='indexed · '+f.file_count+' files';dl.appendChild(o)});
  drawIx()}
async function doScan(force){
  const d=$('dir').value.trim();
  if(!d){$('ixstat').textContent='pick a project folder first';
    $('ixstat').className='ixstat err';return}
  const b=force?$('refresh'):$('scan'),label=b.textContent;
  b.disabled=true;b.textContent='Reading…';
  $('ixstat').textContent='reading file structure…';$('ixstat').className='ixstat';
  try{
    const r=await fetch('/api/scan',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({dir:d,force:!!force})});
    const j=await r.json();
    if(j.error){$('ixstat').textContent=j.error;$('ixstat').className='ixstat err'}
    else{await loadFolders();
      $('ixstat').textContent='indexed '+j.file_count+' files in '+j.seconds+'s'+
        (j.reused?' ('+j.parsed+' read, '+j.reused+' unchanged)':'')+
        (j.truncated?' · truncated':'');
      $('ixstat').className='ixstat on'}}
  catch(e){$('ixstat').textContent='scan failed';$('ixstat').className='ixstat err'}
  b.disabled=false;b.textContent=label}
/* ---- scanning a folder on THIS computer ----
   The NAS cannot read your laptop, but the browser can. <input webkitdirectory>
   works on plain HTTP (unlike showDirectoryPicker, which needs a secure
   context), so the whole scan runs here and only the structural map is sent. */
const SKIP_DIRS=new Set(['.git','.hg','.svn','node_modules','__pycache__','.venv',
  'venv','env','dist','build','.next','target','.mypy_cache','.pytest_cache',
  '.ruff_cache','.tox','.eggs','site-packages','.idea','.vscode','coverage',
  '.nyc_output','vendor','.terraform','.gradle','Pods','DerivedData']);
const CODE_EXT=new Set(['.py','.js','.jsx','.ts','.tsx','.mjs','.cjs','.go','.rs',
  '.rb','.java','.kt','.swift','.c','.h','.cpp','.hpp','.cs','.php','.sh','.bash','.zsh']);
const DOC_EXT=new Set(['.md','.markdown','.rst','.txt','.adoc']);
const CONF_EXT=new Set(['.json','.yaml','.yml','.toml','.ini','.cfg']);
const MAX_FILES=400,MAX_BYTES=400000,MAX_ENTRIES=30;

function extOf(p){const i=p.lastIndexOf('.');return i<0?'':p.slice(i).toLowerCase()}
function relOf(f){const p=f.webkitRelativePath||f.name;const i=p.indexOf('/');
  return i<0?p:p.slice(i+1)}
function skipRel(rel){return rel.split('/').some(s=>SKIP_DIRS.has(s)||s.startsWith('.'))}
function firstLine(t){for(const l of (t||'').split('\n')){const x=l.trim();
  if(x)return x.slice(0,160)}return ''}
function uniq(a,n){return [...new Set(a)].slice(0,n)}

/* The server side uses Python's ast, so it is immune to prose. Here we only
   have regex, and without this stripping we harvest "imports" and "classes"
   out of docstrings — a prompt containing the line "import them." really did
   show up as a dependency. Strip strings and comments before matching.
   \x22 is a double quote; literal ones would close the Python raw string. */
function stripPy(src){
  return src.replace(/[rubf]{0,2}\x22\x22\x22[\s\S]*?\x22\x22\x22/g,'""')
            .replace(/[rubf]{0,2}'''[\s\S]*?'''/g,"''")
            .replace(/#[^\n]*/g,'')}
function stripJs(src){
  return src.replace(/\/\*[\s\S]*?\*\//g,'')
            .replace(/^[ \t]*\/\/[^\n]*/gm,'')}

function pyOutline(src){
  const d=src.match(/^\s*(?:\x22\x22\x22|''')([\s\S]{0,400}?)(?:\x22\x22\x22|''')/);
  const doc=d?firstLine(d[1]):'';
  const code=stripPy(src);
  const imports=[],defs=[];
  /* [ \t]* not \s* — \s matches newlines, so a greedy ^(\s*) can start at an
     earlier blank line and swallow them, making every top-level def look
     indented (and therefore like a method). */
  for(const m of code.matchAll(/^[ \t]*import\s+([\w.]+)/gm))
    imports.push(m[1].replace(/\.+$/,''));
  for(const m of code.matchAll(/^[ \t]*from\s+([\w.]+)\s+import\s/gm))imports.push(m[1]);
  for(const m of code.matchAll(/^([ \t]*)(?:async\s+)?def\s+(\w+\s*\([^)]{0,140}\))/gm))
    defs.push((m[1].length?'    .':'def ')+m[2].replace(/\s+/g,' '));
  for(const m of code.matchAll(/^[ \t]*class\s+(\w+)/gm))defs.push('class '+m[1]);
  for(const m of code.matchAll(/^([A-Z][A-Z0-9_]{2,})\s*=/gm))defs.push(m[1]+' = ...');
  return {imports:uniq(imports,20),defs:defs.slice(0,MAX_ENTRIES),doc:doc}}

function jsOutline(src){
  const code=stripJs(src);
  const imports=[],defs=[];
  for(const m of code.matchAll(/(?:from|require\()\s*['"]([^'"]+)['"]/g))imports.push(m[1]);
  for(const m of code.matchAll(/^[ \t]*(?:export\s+)?(?:async\s+)?function\s+(\w+\s*\([^)]{0,140}\))/gm))
    defs.push('function '+m[1].replace(/\s+/g,' '));
  for(const m of code.matchAll(/^[ \t]*(?:export\s+)?class\s+(\w+)/gm))defs.push('class '+m[1]);
  for(const m of code.matchAll(/^[ \t]*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\(/gm))
    defs.push('const '+m[1]+'(...)');
  return {imports:uniq(imports,20),defs:defs.slice(0,MAX_ENTRIES),doc:''}}

function mdOutline(src){
  const heads=[];
  for(const m of src.matchAll(/^(#{1,4})\s+(.+)$/gm))
    heads.push('  '.repeat(m[1].length-1)+m[1]+' '+m[2].trim());
  const body=src.replace(/^#{1,4}\s+.+$/gm,'').trim();
  return {imports:[],defs:heads.slice(0,MAX_ENTRIES),doc:firstLine(body),
          words:body.split(/\s+/).length}}

function confOutline(src,ext){
  let keys=[];
  if(ext==='.json'){try{const o=JSON.parse(src);
    if(o&&typeof o==='object'&&!Array.isArray(o))keys=Object.keys(o)}catch(e){}}
  else{for(const m of src.matchAll(/^([A-Za-z_][\w.-]*)\s*[:=]/gm))keys.push(m[1])}
  /* key NAMES only — values are where secrets live */
  return {imports:[],defs:uniq(keys,MAX_ENTRIES),doc:''}}

function outlineFor(rel,src){
  const e=extOf(rel);
  if(e==='.py')return pyOutline(src);
  if(['.js','.jsx','.ts','.tsx','.mjs','.cjs'].includes(e))return jsOutline(src);
  if(DOC_EXT.has(e))return mdOutline(src);
  if(CONF_EXT.has(e))return confOutline(src,e);
  if(CODE_EXT.has(e)){const defs=[];
    for(const m of src.matchAll(/^\s*(?:pub\s+|public\s+|static\s+|export\s+)*(?:func|fn|class|struct|interface|type|impl)\s+(\w[\w<>, ]{0,80})/gm))
      defs.push(m[1].trim());
    return {imports:[],defs:defs.slice(0,MAX_ENTRIES),doc:''}}
  return null}

function showProg(done,total,label){
  $('prog').hidden=false;
  const pct=total?Math.round(done/total*100):0;
  $('pbar').style.width=pct+'%';
  $('ptxt').textContent=label||(done+' / '+total+' files  ·  '+pct+'%')}

async function scanLocal(fileList){
  const all=[...fileList];
  if(!all.length)return;

  /* The browser gives the folder NAME but never its absolute path — that is a
     deliberate privacy boundary, not something to work around. So never demand
     the full path: if the box already holds one whose last segment matches what
     was picked, keep it (it will line up with `promptx -c <path>` on the CLI).
     Otherwise just key the index by folder name and fill the box in, so picking
     a folder is all it ever takes. */
  const picked=all[0].webkitRelativePath.split('/')[0];
  const typed=$('dir').value.trim();
  const typedBase=typed.replace(/\/+$/,'').split('/').pop();
  const root=(typed && typedBase===picked) ? typed : picked;
  if(root!==typed)$('dir').value=root;

  const keep=[];
  for(const f of all){
    const rel=relOf(f);
    if(!rel||skipRel(rel))continue;
    keep.push({f,rel});
    if(keep.length>=MAX_FILES)break}

  showProg(0,keep.length,'reading '+keep.length+' files (of '+all.length+' in the folder)…');
  const files={};let parsed=0;
  for(let i=0;i<keep.length;i++){
    const {f,rel}=keep[i];
    const e={size:f.size,mtime:Math.floor(f.lastModified/1000)};
    const ext=extOf(rel);
    if(f.size<=MAX_BYTES&&(CODE_EXT.has(ext)||DOC_EXT.has(ext)||CONF_EXT.has(ext))){
      try{const o=outlineFor(rel,await f.text());
        if(o){Object.assign(e,o);parsed++}}
      catch(err){}}
    files[rel]=e;
    if(i%8===0||i===keep.length-1){showProg(i+1,keep.length);
      await new Promise(r=>setTimeout(r,0))}}

  showProg(keep.length,keep.length,'uploading map…');
  const payload={root:root,scanned_at:Math.floor(Date.now()/1000),
    file_count:Object.keys(files).length,parsed:parsed,reused:0,
    truncated:keep.length>=MAX_FILES,files:files};
  try{
    const r=await fetch('/api/index',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const j=await r.json();
    if(j.error){$('ixstat').textContent=j.error;$('ixstat').className='ixstat err'}
    else{await loadFolders();
      $('ixstat').textContent='indexed '+j.file_count+' files ('+parsed+
        ' read for structure) — ready to use';
      $('ixstat').className='ixstat on'}}
  catch(err){$('ixstat').textContent='upload failed: '+err;
    $('ixstat').className='ixstat err'}
  setTimeout(()=>{$('prog').hidden=true},1200)}

$('pick').onclick=()=>$('picker').click();
$('picker').onchange=e=>{scanLocal(e.target.files);e.target.value=''};

$('scan').onclick=()=>doScan(false);
$('refresh').onclick=()=>doScan(false);
$('dir').addEventListener('input',drawIx);
$('dir').addEventListener('change',drawIx);
loadFolders();

function drawModel(){
  const m=MODELS.find(x=>x[0]===$('model').value)||MODELS[0];
  $('mbox').innerHTML=`<div class="mhead"><span class="mname"></span>
    <span class="mcost"></span><span class="mtag"></span></div><div class="mdesc"></div>`;
  $('mbox').querySelector('.mname').textContent=m[1];
  $('mbox').querySelector('.mcost').textContent=m[2];
  $('mbox').querySelector('.mtag').textContent='· '+m[3];
  $('mbox').querySelector('.mdesc').innerHTML=m[4];
}
$('model').onchange=()=>{localStorage.setItem('px_model',$('model').value);drawModel()};
drawModel();

let hist=JSON.parse(localStorage.getItem('px_hist')||'[]'),last='';
function drawHist(){if(!hist.length){$('hist').hidden=true;return}
  $('hist').hidden=false;$('hlist').innerHTML='';
  hist.slice(0,6).forEach(h=>{const d=document.createElement('div');d.className='hitem';
    d.innerHTML='<div class="hq"></div><div class="hm"></div>';
    d.children[0].textContent=h.q;d.children[1].textContent=h.model;
    d.onclick=()=>{show(h.text,h.model);$('q').value=h.q};$('hlist').appendChild(d)})}
function show(t,m,isErr){last=t;$('out').hidden=false;$('res').textContent=t;
  $('res').className=isErr?'err':'';$('meta').textContent=isErr?'error':m;
  $('copy').style.display=isErr?'none':'';$('res').scrollIntoView({behavior:'smooth',block:'nearest'})}
async function run(){const q=$('q').value.trim();if(!q)return;
  const model=$('model').value,dir=$('dir').value.trim();
  localStorage.setItem('px_dir',dir);localStorage.setItem('px_model',model);
  $('go').disabled=true;$('go').innerHTML='<span class="spin"></span>';
  $('out').hidden=false;$('res').textContent='Thinking…';$('res').className='';
  $('meta').textContent=model;$('copy').style.display='none';
  try{const r=await fetch('/api/expand',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({request:q,model,dir})});const d=await r.json();
    if(d.error){show(d.error,model,true)}else{show(d.text,model);
      hist.unshift({q,model,text:d.text});hist=hist.slice(0,20);
      localStorage.setItem('px_hist',JSON.stringify(hist));drawHist()}}
  catch(e){show(String(e),model,true)}
  $('go').disabled=false;$('go').textContent='Expand'}
$('go').onclick=run;$('again').onclick=run;
$('q').addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter')run()});
$('copy').onclick=async()=>{try{await navigator.clipboard.writeText(last)}
  catch(e){const t=document.createElement('textarea');t.value=last;document.body.appendChild(t);
    t.select();document.execCommand('copy');t.remove()}
  $('copy').textContent='Copied ✓';setTimeout(()=>$('copy').textContent='Copy',1400)};
drawHist();
</script></div></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        route = self.path.split("?")[0]

        if route == "/api/folders":
            self._j({"folders": list_folders()})
            return

        if route not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = (PAGE.replace("__MODELS__", json.dumps(MODELS))
            .replace("__ROOTS__", json.dumps(PROJECT_ROOTS))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            p = json.loads(self.rfile.read(n))
        except (ValueError, json.JSONDecodeError):
            self._j({"error": "bad request"})
            return

        if self.path == "/api/expand":
            text, err = expand(p.get("request", ""),
                               p.get("model", MODELS[0][0]),
                               p.get("dir", ""))
            self._j({"error": err} if err else {"text": text})
            return

        if self.path == "/api/scan":
            self._j(do_scan(p.get("dir", ""), bool(p.get("force"))))
            return

        if self.path == "/api/index":
            self._j(store_index(p))
            return

        self.send_error(404)

    def _j(self, o):
        b = json.dumps(o).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7331)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    print(f"promptx serving on http://{a.host}:{a.port}  key={'yes' if api_key() else 'NO'}")
    with S((a.host, a.port), H) as s:
        s.serve_forever()
