#!/usr/bin/env python3
"""promptx-web — browser UI for turning vague requests into explicit work orders.

    promptx-web                     # serve on :7331 and open a browser
    promptx-web --port 8899
    promptx-web --no-open

Runs locally because it needs your OpenRouter key and read access to your
projects. Nothing leaves your machine except the expansion request itself.
"""

import argparse
import http.server
import json
import os
import pathlib
import socketserver
import threading
import urllib.error
import urllib.request
import webbrowser

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LOCAL_URL = os.getenv("PROMPTX_LOCAL_URL", "http://10.0.4.93:11434/v1/chat/completions")
LOCAL_MODEL = os.getenv("PROMPTX_LOCAL_MODEL", "qwen3.6-uncensored:latest")
INDEX_DIR = pathlib.Path(os.getenv("PROMPTX_INDEX_DIR",
                                   str(pathlib.Path.home() / ".promptx/index")))

# Optional; without it the UI works on filenames alone.
try:
    import promptx_index
except ImportError:
    promptx_index = None

MODELS = [
    ("google/gemini-2.5-flash-lite", "Gemini Flash Lite — fast, cheap (default)"),
    ("qwen/qwen3.7-flash", "Qwen3.7 Flash — cheapest"),
    ("meta-llama/llama-3.1-8b-instruct", "Llama 3.1 8B — small, literal"),
    ("inclusionai/ling-3.0-flash:free", "Ling 3.0 Flash — free tier"),
    ("anthropic/claude-haiku-4.5", "Claude Haiku — best instructions"),
    ("__local__", "Spark (local) — free, slower, verbose"),
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
5b. If the request admits more than one reasonable reading (e.g. "coverage" could mean test coverage or feature coverage), the FIRST line of your output must be:  READ AS: <the reading you chose>  — one line, so a wrong choice is caught in one second instead of after an hour of correct work on the wrong goal. If only one reading is sensible, omit the line entirely; do not use it to restate obvious requests.
6. Under 250 words. Dense and specific, not padded.
7. Output ONLY the work order. No preamble, no reasoning, no commentary about \
what you did. Begin directly with the first step (or the READ AS line when present).

IMPORTANT: You can see file NAMES but not file CONTENTS. If the task requires \
reading code to answer (an audit, a review, "what does X do"), do NOT invent \
findings. Instead instruct the agent to read the relevant files first and \
report what it actually finds.

If the request is already specific, tighten it rather than inflating it."""

# Replaces the names-only clause rather than appending to it — the two would
# contradict each other, and a model told both things at once will hedge.
SYSTEM_DEEP = SYSTEM.replace(
    """IMPORTANT: You can see file NAMES but not file CONTENTS. If the task requires \
reading code to answer (an audit, a review, "what does X do"), do NOT invent \
findings. Instead instruct the agent to read the relevant files first and \
report what it actually finds.""",
    """You have been given a STRUCTURAL MAP of the project: per file, its imports, \
class and function signatures, and docstrings; per document, its heading outline. \
You can see what exists and what each piece is for.

You do NOT have the full source. So:
- You MAY state which files are relevant, what is missing, what is inconsistent \
between docs and code, and what a change must touch.
- You MAY NOT assert what a function body does beyond what its name, signature, \
and docstring show. Where the task depends on the implementation, instruct the \
agent to read that specific file first — naming it exactly.
- For documentation tasks, compare the heading outlines against the code \
structure and name concrete gaps: docs describing code that no longer exists, \
and code with no documentation covering it.""")


def api_key():
    k = os.getenv("OPENROUTER_API_KEY")
    if k:
        return k
    auth = pathlib.Path.home() / ".local/share/opencode/auth.json"
    if auth.exists():
        try:
            entry = json.loads(auth.read_text()).get("openrouter") or {}
            if entry.get("key"):
                return entry["key"]
        except (json.JSONDecodeError, OSError):
            pass
    return None


def repo_context(root, max_files=150):
    try:
        root = pathlib.Path(root).expanduser().resolve()
    except (OSError, RuntimeError):
        return None, "bad path"
    if not root.is_dir():
        return None, f"not a directory: {root}"
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
    return ("\n".join(out) if out else "(empty)"), None


def deep_context(root):
    """Structural map for a folder, if `promptx -c DIR --scan` has built one.

    This UI has no Scan button — scanning is a CLI action. If an index exists it
    is used automatically, so the two tools stay in sync.
    """
    if promptx_index is None or not root:
        return None
    idx = promptx_index.load_index(root, INDEX_DIR)
    if not idx or not idx.get("files"):
        return None
    return promptx_index.render(idx)


def expand(request, model, ctx_dir):
    ctx, err, deep = (None, None, None)
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
                f"whose body you cannot see, instruct the agent to read that "
                f"specific file first. If something it depends on is missing "
                f"entirely, say so and instruct the agent to create it.")
    elif ctx:
        user = (f"Project structure:\n```\n{ctx}\n```\n\nRequest: {request}\n\n"
                f"Use the ACTUAL paths above. If something the request depends on "
                f"is missing from the tree, say so explicitly and instruct the "
                f"agent to create it first.")

    local = model == "__local__"
    payload = {
        "model": LOCAL_MODEL if local else model,
        "messages": [{"role": "system", "content": SYSTEM_DEEP if deep else SYSTEM},
                     {"role": "user", "content": user}],
        "temperature": 0.3, "max_tokens": 2400,
    }
    headers = {"Content-Type": "application/json"}
    url = LOCAL_URL if local else OPENROUTER_URL
    if not local:
        key = api_key()
        if not key:
            return None, "No OpenRouter key found. Set OPENROUTER_API_KEY, or pick the Spark model."
        headers["Authorization"] = f"Bearer {key}"
        headers["HTTP-Referer"] = "http://localhost"
        headers["X-Title"] = "promptx-web"

    try:
        req = urllib.request.Request(url, json.dumps(payload).encode(), headers)
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            pass
        return None, f"HTTP {e.code}: {body or 'request rejected'}"
    except (urllib.error.URLError, OSError) as e:
        return None, f"Cannot reach {url}: {e}"

    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return None, f"Unexpected response: {str(data)[:200]}"
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

    # Guarantee the evidence gate in code, not by sampling. See
    # promptx_index.verification_block for why this is not a second model.
    if deep and promptx_index is not None:
        idx = promptx_index.load_index(ctx_dir, INDEX_DIR)
        if idx and not promptx_index.has_verification(text):
            text = text + "\n\n" + promptx_index.verification_block(idx)
        if idx:
            text += promptx_index.lint_block(promptx_index.lint_plan(text, idx))
    return text, None


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>promptx</title>
<style>
  :root{--bg:#fbfbfa;--fg:#1a1a19;--mut:#6b6b68;--line:#e3e3e0;--card:#fff;
        --accent:#2f6f4e;--accentfg:#fff;--code:#f5f5f3;}
  @media (prefers-color-scheme:dark){
    :root{--bg:#161615;--fg:#ececea;--mut:#9a9a96;--line:#2c2c2a;--card:#1e1e1c;
          --accent:#5ea882;--accentfg:#0f1f18;--code:#232321;}}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif;}
  .wrap{max-width:820px;margin:0 auto;padding:32px 20px 80px}
  h1{font-size:19px;margin:0 0 4px;letter-spacing:-.01em}
  .sub{color:var(--mut);font-size:13.5px;margin:0 0 24px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
  textarea{width:100%;min-height:88px;resize:vertical;border:1px solid var(--line);
    border-radius:9px;padding:11px 12px;font:inherit;background:var(--bg);color:var(--fg)}
  textarea:focus,input:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}
  .row{display:flex;gap:9px;margin-top:11px;flex-wrap:wrap;align-items:center}
  input[type=text],select{flex:1;min-width:190px;border:1px solid var(--line);border-radius:9px;
    padding:9px 11px;font:inherit;background:var(--bg);color:var(--fg)}
  button{border:0;border-radius:9px;padding:10px 17px;font:inherit;font-weight:560;
    cursor:pointer;background:var(--accent);color:var(--accentfg)}
  button:disabled{opacity:.5;cursor:default}
  button.ghost{background:transparent;color:var(--mut);border:1px solid var(--line);font-weight:450}
  .hint{color:var(--mut);font-size:12.5px;margin-top:9px}
  .out{margin-top:26px}
  .outhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:9px;gap:10px}
  .outhead .meta{color:var(--mut);font-size:12.5px}
  pre{background:var(--code);border:1px solid var(--line);border-radius:11px;padding:15px;
    white-space:pre-wrap;word-wrap:break-word;margin:0;
    font:13.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}
  .err{border-color:#b4443c;color:#b4443c}
  .spin{display:inline-block;width:13px;height:13px;border:2px solid var(--line);
    border-top-color:var(--accent);border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px}
  @keyframes s{to{transform:rotate(360deg)}}
  .hist{margin-top:34px;border-top:1px solid var(--line);padding-top:18px}
  .hist h2{font-size:13px;color:var(--mut);margin:0 0 11px;font-weight:560;
    text-transform:uppercase;letter-spacing:.05em}
  .hitem{border:1px solid var(--line);border-radius:9px;padding:10px 12px;margin-bottom:8px;cursor:pointer}
  .hitem:hover{border-color:var(--accent)}
  .hitem .q{font-size:13.5px}
  .hitem .m{color:var(--mut);font-size:12px;margin-top:2px}
</style></head><body><div class="wrap">

<h1>promptx</h1>
<p class="sub">Turns a vague request into an explicit work order. Paste the result into your coding agent.</p>

<div class="card">
  <textarea id="q" placeholder="What do you want done? e.g. build out the gtm adapters package" autofocus></textarea>
  <div class="row">
    <input type="text" id="dir" placeholder="Project folder (optional, but recommended)">
    <select id="model"></select>
    <button id="go">Expand</button>
  </div>
  <div class="hint">⌘↵ / Ctrl↵ to submit · the folder lets it name real file paths</div>
</div>

<div class="out" id="out" hidden>
  <div class="outhead">
    <span class="meta" id="meta"></span>
    <span><button class="ghost" id="again">Try again</button>
    <button id="copy">Copy</button></span>
  </div>
  <pre id="res"></pre>
</div>

<div class="hist" id="hist" hidden><h2>Earlier</h2><div id="hlist"></div></div>

<script>
const $=i=>document.getElementById(i);
const MODELS=__MODELS__;
MODELS.forEach(([v,l])=>{const o=document.createElement('option');o.value=v;o.textContent=l;$('model').appendChild(o)});
$('dir').value=localStorage.getItem('px_dir')||'';
$('model').value=localStorage.getItem('px_model')||MODELS[0][0];
let hist=JSON.parse(localStorage.getItem('px_hist')||'[]');
let last='';

function drawHist(){
  if(!hist.length){$('hist').hidden=true;return}
  $('hist').hidden=false;$('hlist').innerHTML='';
  hist.slice(0,6).forEach(h=>{
    const d=document.createElement('div');d.className='hitem';
    d.innerHTML=`<div class="q"></div><div class="m"></div>`;
    d.children[0].textContent=h.q;
    d.children[1].textContent=h.model;
    d.onclick=()=>{show(h.text,h.model,h.q);$('q').value=h.q};
    $('hlist').appendChild(d)});
}
function show(text,model,q,isErr){
  last=text;$('out').hidden=false;
  $('res').textContent=text;
  $('res').className=isErr?'err':'';
  $('meta').textContent=isErr?'error':model;
  $('copy').style.display=isErr?'none':'';
  $('res').scrollIntoView({behavior:'smooth',block:'nearest'});
}
async function run(){
  const q=$('q').value.trim(); if(!q)return;
  const model=$('model').value, dir=$('dir').value.trim();
  localStorage.setItem('px_dir',dir);localStorage.setItem('px_model',model);
  $('go').disabled=true;$('go').innerHTML='<span class="spin"></span>';
  $('out').hidden=false;$('res').textContent='Thinking…';$('res').className='';
  $('meta').textContent=model;$('copy').style.display='none';
  try{
    const r=await fetch('/api/expand',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({request:q,model,dir})});
    const d=await r.json();
    if(d.error){show(d.error,model,q,true)}
    else{show(d.text,model,q);
      hist.unshift({q,model,text:d.text});hist=hist.slice(0,20);
      localStorage.setItem('px_hist',JSON.stringify(hist));drawHist()}
  }catch(e){show(String(e),model,q,true)}
  $('go').disabled=false;$('go').textContent='Expand';
}
$('go').onclick=run;
$('again').onclick=run;
$('q').addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter')run()});
$('copy').onclick=async()=>{
  try{await navigator.clipboard.writeText(last);
    $('copy').textContent='Copied ✓';setTimeout(()=>$('copy').textContent='Copy',1400)}
  catch(e){const t=document.createElement('textarea');t.value=last;document.body.appendChild(t);
    t.select();document.execCommand('copy');t.remove();
    $('copy').textContent='Copied ✓';setTimeout(()=>$('copy').textContent='Copy',1400)}
};
drawHist();
</script></div></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = PAGE.replace("__MODELS__", json.dumps(MODELS)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api/expand":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n))
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "bad request"})
            return
        text, err = expand(payload.get("request", ""),
                           payload.get("model", MODELS[0][0]),
                           payload.get("dir", ""))
        self._json({"error": err} if err else {"text": text})

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=7331)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    url = f"http://localhost:{args.port}"
    if not api_key():
        print("note: no OpenRouter key found — the Spark (local) model will still work")
    with Server(("127.0.0.1", args.port), Handler) as httpd:
        print(f"promptx-web → {url}   (ctrl-c to stop)")
        if not args.no_open:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
