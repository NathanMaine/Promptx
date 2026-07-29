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

## The lineup

| Model | Cost/M in | Character |
|---|---|---|
| `google/gemini-2.5-flash-lite` | $0.10 | **Default.** Fast, clean numbered steps, never leaks traces. Best all-rounder. |
| `qwen/qwen3.7-flash` | $0.03 | Cheapest paid, 1M context. Formatting slightly less tidy. |
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

**Use `--local` for sensitive work.** Your request and your file tree never
leave the LAN. The tradeoff is verbosity and reasoning traces.

**Use `llama-3.1-8b-instruct` when you already know the answer** and just want
it formatted as steps. It will not editorialize. It also will not save you.

## What the file tree costs you

With `-c`, you are sending up to 120 relative paths — call it 1,500 tokens for a
mid-sized project. At $0.10/M that is $0.00015 per expansion. Roughly seven
thousand expansions per dollar.

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
