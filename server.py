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
import urllib.error
import urllib.request

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SPARK_URL = os.getenv("PROMPTX_SPARK_URL", "http://10.0.4.93:11434/v1/chat/completions")
SPARK_MODEL = os.getenv("PROMPTX_SPARK_MODEL", "qwen3.6-uncensored:latest")
ENV_FILE = pathlib.Path(os.getenv("PROMPTX_ENV", "/volume1/Projects/promptx/.env"))
PROJECT_ROOTS = ["/volume1/Projects", "/volume1/@home/Natron"]

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
4. End with an explicit verification step — a command to run that proves it worked.
5. Add a short "Do NOT" section naming the likeliest wrong turn for this task.
6. Under 250 words. Dense and specific, not padded.
7. Output ONLY the work order. No preamble, no reasoning, no commentary about \
what you did. Begin directly with the first step.

IMPORTANT: You can see file NAMES but not file CONTENTS. If the task requires \
reading code to answer (an audit, a review, "what does X do"), do NOT invent \
findings. Instead instruct the agent to read the relevant files first and \
report what it actually finds.

If the request is already specific, tighten it rather than inflating it."""


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


def expand(request, model, ctx_dir):
    ctx = None
    if ctx_dir and ctx_dir.strip():
        ctx, err = repo_context(ctx_dir.strip())
        if err:
            return None, err

    user = request
    if ctx:
        user = (f"Project structure:\n```\n{ctx}\n```\n\nRequest: {request}\n\n"
                f"Use the ACTUAL paths above. If something the request depends on "
                f"is missing from the tree, say so explicitly and instruct the "
                f"agent to create it first.")

    local = model == "__local__"
    payload = {"model": SPARK_MODEL if local else model,
               "messages": [{"role": "system", "content": SYSTEM},
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
    return text.strip(), None


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
    <input type="text" id="dir" placeholder="Project folder (optional) — e.g. /volume1/Projects/my-app">
    <select id="model"></select>
    <button id="go">Expand</button>
  </div>
  <div class="hint">⌘↵ / Ctrl↵ to submit · adding a folder lets it name real file paths instead of guessing</div>
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
const tb=$('tb');
tb.onclick=()=>{const c=document.documentElement.getAttribute('data-theme');
  const n=c==='light'?'dark':'light';document.documentElement.setAttribute('data-theme',n);
  localStorage.setItem('px_theme',n)};
const st=localStorage.getItem('px_theme');if(st)document.documentElement.setAttribute('data-theme',st);

MODELS.forEach(m=>{const o=document.createElement('option');o.value=m[0];
  o.textContent=`${m[1]} · ${m[2]}`;$('model').appendChild(o)});
$('dir').value=localStorage.getItem('px_dir')||'';
$('model').value=localStorage.getItem('px_model')||MODELS[0][0];

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
        if self.path.split("?")[0] not in ("/", "/index.html"):
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
            p = json.loads(self.rfile.read(n))
        except (ValueError, json.JSONDecodeError):
            self._j({"error": "bad request"})
            return
        text, err = expand(p.get("request", ""), p.get("model", MODELS[0][0]), p.get("dir", ""))
        self._j({"error": err} if err else {"text": text})

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
