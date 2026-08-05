# Choosing an expander model

## The job is smaller than it looks

Expansion is **one short call**: read a sentence, emit under 250 words of
numbered steps in a fixed shape. There is no problem to solve. The hard thinking
happens afterward, in your coding agent.

That makes the usual instinct — reach for the smartest model — actively wrong.

## Why reasoning models are the wrong tool here

They fail in three ways at once:

1. **They cost more for no gain.** Thinking tokens are billed. On a formatting
   task there is nothing to think about.
2. **They leak traces.** `<think>...</think>` blocks end up in your output, and
   if you paste without reading, into your coding agent as if they were part of
   the spec. promptx strips the formats it recognizes, but it cannot recognize
   all of them.
3. **They elaborate.** Given "add retry logic," a reasoning model considers
   jitter strategies, circuit breakers, and observability. You wanted three
   numbered steps.

Cheap, fast, and literal wins. This is a rewriting task.

**The one sanctioned exception: `deepseek-v4-flash`** in the hosted picker.
The anti-reasoning rule above bends when the model is cheap *and* carries a
1M window: V4 Flash is a 284B mixture-of-experts (13B active) at $0.14/M in.
Because it thinks before writing, `server.py` gives it a 6,000-token budget
instead of the usual 2,400 — at the normal cap a thinking model can spend its
entire budget before emitting anything, and the work order comes back empty.
It uses a dedicated `DEEPSEEK_API_KEY`, deliberately not OpenRouter, so
billing and rate limits stay independent.

## The lineup

| Model | Cost/M in | Character |
|---|---|---|
| `google/gemini-2.5-flash-lite` | $0.10 | **Default.** Fast, clean numbered steps, never leaks traces. Best all-rounder. |
| `qwen/qwen3.7-flash` | $0.03 | Cheapest paid, 1M context. Formatting slightly less tidy. |
| `qwen/qwen3.8-max` | $2 (out: $6) | Flagship Qwen, 1M context. For huge scanned maps where the cheap tier drops structure. |
| `deepseek-v4-flash` | $0.14 (out: $0.28) | DeepSeek direct — 1M-context reasoning model. The sanctioned exception, below. Hosted picker only. |
| `meta-llama/llama-3.1-8b-instruct` | $0.05 | Rigid. Follows the template, adds nothing. Weakest at inferring what you left out. |
| `inclusionai/ling-3.0-flash:free` | free | Fine for the task; rate limits bite under real use. |
| `anthropic/claude-haiku-4.5` | ~$1 | Best at catching what you did *not* say. |
| local (`--local`) | free | Your hardware. Verbose, emits `<think>`, nothing leaves the building. |

## Picking one

**Start with the default.** `gemini-2.5-flash-lite` handles most work and costs
roughly a hundredth of a cent per expansion. At that price, cost is not a real
input to the decision — pick on behavior.

**Switch to `claude-haiku-4.5` when a bad plan is expensive.** Multi-file
refactors, anything touching a package's import graph, anything where you will
not notice the mistake for an hour. It is the one that reliably says *"these
three modules don't exist yet — create them before editing `__init__.py`,"*
which is the exact failure this tool was built for. Ten times the price of the
default is still under a cent.

**Switch to `qwen3.7-flash` for very large trees.** The 1M context window is the
reason, not the price.

**Switch to `qwen/qwen3.8-max` when the map is huge *and* the plan matters.**
Same 1M window as qwen3.7-flash, but flagship-grade planning on top: big
scanned indexes, tangled import graphs, multi-package changes where a
flash-tier model keeps losing track of what depends on what. Twenty times the
price of the default is still cents per expansion — but it is overkill for a
three-file fix. The pairing to know: scan with `PROMPTX_MAX_FILES` raised,
expand with this.

**Use `--local` for sensitive work.** Your request and your file tree never
leave the LAN. The tradeoff is verbosity and reasoning traces.

**Use `llama-3.1-8b-instruct` when you already know the answer** and just want
it formatted as steps. It will not editorialize. It also will not save you.

## What the file tree costs you

With `-c`, you are sending up to 600 relative paths — call it 7,500 tokens for a
large project. At $0.10/M that is $0.00075 per expansion. Roughly thirteen
hundred expansions per dollar.

Do not optimize this. Use `-c` every time.

## Verifying a model id

OpenRouter ids are exact, and the aliases coding agents use internally are often
not real ones. `google/gemini-flash-latest` looks perfectly plausible and does
not exist — it is an OpenCode alias. It cost a debugging cycle here.

```bash
curl -s https://openrouter.ai/api/v1/models \
  | python3 -c 'import json,sys; [print(m["id"]) for m in json.load(sys.stdin)["data"]]' \
  | grep -i gemini
```

Live pricing: [openrouter.ai/models](https://openrouter.ai/models). The numbers
in this file were accurate when written and will drift.

## Adding a model to the picker

Edit the `MODELS` list at the top of `server.py` or `web.py`:

```python
("provider/model-id", "Display Name", "$X/M",
 "one-line strength",
 "A paragraph on what this is actually good for — shown under the picker.")
```

`main.py` has a simpler `SUGGESTED` list of `(id, description)` pairs used only
by `--models`; `-m` accepts any id whether or not it is listed.
