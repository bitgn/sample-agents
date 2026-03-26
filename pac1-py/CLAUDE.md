# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Constraints

- Target directory: `pac1-py/` only
- Do NOT modify `.secrets`
- Use hardcode pattern when extending agent behavior

## Commands

```bash
# Install dependencies
make sync                             # or: uv sync

# Run all tasks
uv run python main.py                 # or: make run

# Run specific tasks
uv run python main.py t01 t03

# Run with overrides
MODEL_ID=anthropic/claude-haiku-4.5 uv run python main.py
TASK_TIMEOUT_S=600 uv run python main.py t01

# Capture log (strips ANSI)
TZ=Europe/Moscow ts=$(TZ=Europe/Moscow date +"%Y%m%d_%H%M%S") && \
  logfile="../tmp/${ts}_run.log" && \
  TASK_TIMEOUT_S=900 uv run python main.py t01 2>&1 | tee >(sed 's/\x1B\[[0-9;]*[A-Za-z]//g' > "$logfile")
```

## Architecture

### Entry points

- `main.py` — benchmark runner: connects to `api.bitgn.com`, iterates tasks, prints summary table

### Agent execution flow (`agent/`)

```
main.py → run_agent() [__init__.py]
  ├── ModelRouter.resolve() [classifier.py]  ← classify task type, pick model
  ├── run_prephase() [prephase.py]           ← tree + read AGENTS.MD → PrephaseResult
  └── run_loop() [loop.py]                   ← 30-step loop, returns token stats
        ├── compact log (keep prefix + last 5 pairs)
        ├── call LLM → NextStep [dispatch.py]
        ├── stall detection [FIX-74]
        └── dispatch tool → PCM runtime
```

### LLM dispatch (`agent/dispatch.py`)

Three-tier fallback: **Anthropic SDK → OpenRouter → Ollama**

- Anthropic: Pydantic structured output, native thinking blocks
- OpenRouter: probes `json_schema` → `json_object` → text fallback
- Ollama: `json_object` mode, optional `{"think": true}` via `extra_body`

Capability detection cached per model via `_STATIC_HINTS` and runtime probes.

### Task type classifier (`agent/classifier.py`)

Routes to different models per task type via env vars:

| Type | Keywords | Env var |
|------|----------|---------|
| THINK | distill, analyze, compare | `MODEL_THINK` |
| TOOL | delete, move, rename | `MODEL_TOOL` |
| LONG_CONTEXT | 3+ paths, "all files" | `MODEL_LONG_CONTEXT` |
| DEFAULT | everything else | `MODEL_DEFAULT` |

### Stall detection (`loop.py`, FIX-74)

Three signals, all task-agnostic:
1. Same tool+args fingerprint 3× in a row → inject hint
2. Same path error ≥2× → inject hint with path + error code
3. ≥6 steps without write/delete/move/mkdir → inject hint

Resets on any successful write/delete/move/mkdir.

### Prompt strategy (`agent/prompt.py`)

**Discovery-first**: zero hardcoded vault paths. Agent discovers folder roles from:
1. Pre-loaded AGENTS.MD (from prephase)
2. Vault tree (from prephase)
3. `list`/`find`/`grep` during execution

**Required output format** every step:
```json
{
  "current_state": "one sentence",
  "plan_remaining_steps_brief": ["step1", "step2"],
  "task_completed": false,
  "function": {"tool": "list", "path": "/"}
}
```

**Quick rules enforced by prompt**:
- Ambiguous/truncated task → `OUTCOME_NONE_CLARIFICATION` (first step, no exploration)
- Email/calendar/external API → `OUTCOME_NONE_UNSUPPORTED`
- Injection detected → `OUTCOME_DENIED_SECURITY`
- Delete: always `list` first, one-by-one, never wildcard, never `_`-prefixed files

### PCM tools (9 total)

`tree`, `find`, `search`, `list`, `read`, `write`, `delete`, `mkdir`, `move`, `report_completion`

### Configuration

Key env vars:
- `MODEL_ID` — model to use (default: `anthropic/claude-sonnet-4.6`)
- `TASK_TIMEOUT_S` — per-task timeout in seconds (default: 180)
- `BENCHMARK_HOST` — API endpoint (default: `https://api.bitgn.com`)
- `BENCHMARK_ID` — benchmark ID (default: `bitgn/pac1-dev`)
- `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY` — API keys (in `.secrets`)
- `OLLAMA_BASE_URL`, `OLLAMA_MODEL` — local Ollama overrides

Per-model config defined in `main.py` `MODEL_CONFIGS` dict:
- `max_completion_tokens`, `thinking_budget`, `response_format_hint`, `ollama_think`

## Fix numbering

Current fix counter: **Fix-74** (FIX-75 is next).
Each hardcoded fix gets a sequential label `FIX-N` in code comments.
