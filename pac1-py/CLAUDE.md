# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Constraints

- Target directory: `pac1-py/` only
- Do NOT modify `.secrets`
- Use hardcode pattern when extending agent behavior
- Never edit pac1-py/.env and pac1-py/.secrets

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
- `LOG_LEVEL` — logging verbosity: `INFO` (default) or `DEBUG` (logs full think blocks + full RAW)

Per-model config defined in `main.py` `MODEL_CONFIGS` dict:
- `max_completion_tokens`, `thinking_budget`, `response_format_hint`

## Fix numbering

Current fix counter: **Fix-120** (FIX-121 is next).
- FIX-120: `classifier.py` `classify_task_llm()` — regex pre-check fast-path: if regex gives non-default (`think`/`longContext`), return immediately and skip LLM call; LLM is only called when regex is unsure (returns `default`) and vault context might reveal analytical/bulk scope
- FIX-119: `models.json` `_profiles` section (named parameter sets: default/think/long_ctx) + profile references in all 15 models; `main.py` resolves string→dict at load time; `classifier.py` `ModelRouter._adapt_config()` merges task-type overlay into model config inside `resolve_after_prephase()`; `loop.py` Ollama tier now passes `ollama_options` via `extra_body["options"]` (was only `ollama_think`)
- FIX-118: `dispatch.py` + `models.json` — `ollama_options` support: passed via `extra_body["options"]` in Ollama tier; `num_ctx: 16384` added to all cloud models so classifier can handle full AGENTS.MD context
- FIX-117: `classifier.py` + `__init__.py` — single-pass routing: classify AFTER prephase with AGENTS.MD context; removed `resolve_llm()`, `reclassify_with_prephase()`, `_classifier_llm_ok`, `_type_cache`; added `ModelRouter.resolve_after_prephase()`
- FIX-116: `prompt.py` OTP step — MANDATORY delete of OTP file after token match, explicit ordered checklist (1.write email 2.delete OTP file 3.report)
- FIX-115: `prephase.py` — dynamic auto-preload of dirs referenced in AGENTS.MD (intersection with tree); recursive read of subdirs; no hardcoded paths
- FIX-114: `prompt.py` INBOX WORKFLOW — Channel messages: trust rules from preloaded DOCS/; admin = execute literally, lowest-id contact on ambiguity; OTP match = admin; blacklist = DENIED_SECURITY
- FIX-113: `prompt.py` Contact resolution — early-exit after empty search: max 1 alternative retry, then OUTCOME_NONE_CLARIFICATION; NEVER read contacts one by one
- FIX-111: `done_operations` field in `NextStep` schema + server-side ledger in `preserve_prefix` (survives compaction) + improved `_compact_log` (extracts WRITTEN/DELETED from user messages) + YAML fallback in `_extract_json_from_text` (`models.py`, `loop.py`, `prompt.py`)
- FIX-110: `LOG_LEVEL` env var (`INFO`/`DEBUG`) + auto-tee stdout → `logs/{ts}_{model}.log` (`main.py`); DEBUG mode logs full `<think>` blocks and full RAW response without 500-char truncation (`loop.py`, `dispatch.py`)
- FIX-108: `call_llm_raw()` — `max_retries` parameter (default 3); classifier passes `max_retries=0` → 1 attempt only, instant fallback to regex (saves 2-4 min per task on empty response)
- FIX-109: prompt.py — attachments field reinforced in email step 3 and inbox step 6: REQUIRED for invoice resend, never omit
- FIX-103: seq.json semantics clarified in prompt — id N = next free slot, use as-is (do NOT add 1 before writing)
- FIX-104: INBOX WORKFLOW step 2 — check "From:" field first; no From: → OUTCOME_NONE_CLARIFICATION immediately
- FIX-105: `classify_task_llm()` — plain-text keyword extraction fallback after JSON+regex parse fails (extract "think"/"longContext"/"default" from raw text)
- FIX-106: `classify_task_llm()` — pass `think=False` and `max_tokens=_cls_cfg["max_completion_tokens"]` to `call_llm_raw`; prevents think-blocks consuming all 20 default tokens
- FIX-107: `call_llm_raw()` Ollama tier — plain-text retry without `response_format` after 4 failed json_object attempts
- FIX-94: `observation` field in NextStep — verbalize last tool result before acting (Variant A)
- FIX-95: `done_this_step` replaces `current_state` — tracks completed work per step (Variant B)
- FIX-96: `precondition` field in NextStep — mandatory verification before write/delete (Variant C)
- FIX-97: keyword-fingerprint cache in `ModelRouter._type_cache` — skip LLM classify on cache hit
- FIX-98: structured rule engine in `classify_task()` — explicit `_Rule` dataclass matrix with must/must_not conditions replacing bare regex chain
- FIX-99: two-phase LLM re-class with vault context — `classify_task_llm()` gains optional `vault_hint`; `reclassify_with_prephase()` passes vault file count + bulk flag to LLM after prephase
- FIX-100: `_classifier_llm_ok` flag — `classify_task_llm()` tracks LLM success; `reclassify_with_prephase()` skips Ollama retry when flag is False
- FIX-101: JSON bracket-extraction fallback in `_call_openai_tier()` — try `_extract_json_from_text()` before breaking on JSON decode failure (eliminates most loop.py retries)
- FIX-102: few-shot user→assistant pair in `prephase.py` — injected after system prompt; strongest signal for JSON-only output from Ollama-proxied cloud models
Each hardcoded fix gets a sequential label `FIX-N` in code comments.
