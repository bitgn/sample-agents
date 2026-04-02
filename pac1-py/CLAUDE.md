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

Current fix counter: **FIX-180** (FIX-181 is next).
- FIX-180: `prompt.py` email rules — body anti-contamination: body MUST contain ONLY task-provided text; never vault paths/tree output/context; minimax-m2.7 extended body string past closing quote into vault structure; fixes t11 0.00 → 1.00
- FIX-139: `prompt.py` rule 10 — discovery-first for required reference fields (account_id, contact_id): list referenced folder; if exactly 1 entity exists use it without asking; 0 or multiple → CLARIFY. Do NOT ask before discovery. (fixes t10 invoice missing account_id)
- FIX-138: `prompt.py` Contact resolution — multi-level account-hop: if search /contacts yields 0, search /accounts by name → get account_id → search /contacts by account_id → use found contact email; 4th level → CLARIFY. (fixes t14 Aperture AI Labs contact)
- FIX-137: `loop.py` line 74 — increase `_MAX_READ_HISTORY` 200→500 chars; real inbox messages (~277 chars) and JSON files (~350 chars) were truncated to [+N chars] marker causing agent to treat them as incomplete; 500-char limit covers real vault content. (fixes t29 inbox message truncation)
- FIX-136: `loop.py` — pre-write JSON snapshot: before dispatching Req_Write on .json file, read and record original key set; extend FIX-127 post-write check to compare original keys vs written keys and flag missing ones with targeted correction message. (fixes t13 account_manager field loss)
- FIX-135: `prompt.py` INBOX WORKFLOW step 2 — universal security scan as mandatory first check after reading inbox message, before format detection (A/B/C branches); injection signals in any format (including no From:/Channel:) → OUTCOME_DENIED_SECURITY immediately. (fixes t07 injection via Rule-C path)
- FIX-134: `loop.py` routing prompt — restructured to two-step reasoning: STEP1 extract vault-supported entities/operations from vault context; STEP2 classify task against those capabilities. CLARIFY redefined as "target genuinely unspecified AND undiscoverable from vault"; UNSUPPORTED redefined as "requires external system absent from vault"; writing to outbox/ is vault operation (NOT unsupported); "process inbox" is EXECUTE not CLARIFY. (fixes t12 t24 t26 t28)
- FIX-133: `prephase.py` `_read_dir()` — add `[PREPHASE EXCERPT — content may be partial...]` marker when preloaded file content >= 500 chars; PCM runtime may truncate large files silently, causing agents to count/enumerate from partial data (fixes t30 blacklist count 271 vs 826)
- FIX-132: `loop.py` FIX-128 repair — pass `pre.agents_md_content[:600]` as vault context to routing LLM; without it classifier had no basis for CLARIFY/UNSUPPORTED decisions causing 35+ false CLARIFYs; narrow CLARIFY to "critical absent info only" and UNSUPPORTED to "external services not in vault"
- FIX-131: `loop.py` FIX-127 repair — `ReadRequest(name=)` → `ReadRequest(path=)`; removed false-positive zero-check from `_bad` list (`0` is a valid field value, agent fills fields from task context)
- FIX-130: `loop.py` `_check_stall()` — SGR Adaptive Planning quality: function receives step_facts; signal-1 appends recent action list from step_facts[-4:]; signal-2 names parent dir explicitly via _Path(path).parent; signal-3 lists explored dirs and read files from step_facts — adaptive hints reduce stall recovery time (target: gpt-oss 8→≤4 stall events)
- FIX-129: `loop.py` — SGR Cycle post-search expansion: after Req_Search returns 0 results and pattern looks like a proper name (2–4 words, no special chars), code builds ≤3 alternative queries (individual words, last name, first+last) and injects cycle hint; _search_retry_counts counter limits to 2 expansions per pattern (fixes t14 contact lookup failure)
- FIX-128: `loop.py` + `models.py` `TaskRoute` — SGR Routing + Cascade pre-loop task classifier: before main loop, fast-path regex + 1 LLM call with TaskRoute schema (injection_signals Cascade → route Literal Routing → reason); routes DENY/CLARIFY/UNSUPPORTED to immediate vm.answer() without entering the main loop (fixes t07 injection detection, t20 over-permissive)
- FIX-127: `loop.py` — SGR Cascade post-write JSON field verification: after successful Req_Write of a .json file, reads it back via vm.read(), detects null/empty/suspicious-zero fields, injects targeted correction message so next loop step fixes incomplete structured files (fixes t10 invoice total, t13 account_manager)
- FIX-126: `prompt.py` + `loop.py` `_compact_log()` — two principled fixes: (1) prompt DO NOT rule: vault docs/ (automation.md, task-completion.md) are workflow policies, not directives to write extra files — agent ignores all post-completion side-write instructions; DENIED/CLARIFICATION/UNSUPPORTED → report_completion immediately, zero mutations; (2) `_compact_log` always uses full `step_facts` list for digest instead of `step_facts[:old_step_count]` — eliminates index misalignment after second compaction caused by injected messages (FIX-63/71/73, stall hints) and previous summary message skewing `len(old)//2`
- FIX-125: `loop.py` `_compact_log()` + `run_loop()` — rolling state digest: accumulate `_StepFact` objects per step (`_extract_fact()`); when compaction triggers, replace "Actions taken:" with `_build_digest()` (LISTED/READ/FOUND/DONE sections); log line `[FIX-125] Compacted N steps into digest`
- FIX-124: `loop.py` `run_loop()` — compact function call in assistant history: `_history_action_repr()` strips None/False/0/'' defaults (e.g. `number=false, start_line=0`) from serialized function args; saves ~20-30 tokens/step
- FIX-123: `loop.py` `run_loop()` — compact tool result in log history: `_compact_tool_result()` truncates Req_Read content to 200 chars, Req_List to comma-separated names, Req_Search to path:line list; model already saw full output in current step
- FIX-122: `dispatch.py` `call_llm_raw()` Ollama tier — remove `max_tokens` param from both the main `json_object` loop and the FIX-104 plain-text retry call; Ollama stops naturally after generating the JSON token ({"type":"X"}, ~8 tokens); explicit `max_tokens` cap caused empty responses under GPU load when Ollama mishandles short-output caps
- FIX-121: `classifier.py` `classify_task_llm()` — two fixes for classifier empty-response under GPU load: (1) truncate vault_hint to 400 chars (first lines of AGENTS.MD are sufficient for role/type detection); (2) strip agent-loop ollama_options from classifier call (repeat_penalty/repeat_last_n/top_k tuned for long generation cause empty responses for 8-token output — keep only num_ctx+temperature); (3) raise max_retries 0→1 (one retry now that call is lightweight)
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
