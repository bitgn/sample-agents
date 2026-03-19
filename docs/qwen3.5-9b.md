# qwen3.5:9b - Benchmark Results

## Run Info

| Parameter        | Value                          |
|------------------|--------------------------------|
| Model            | qwen3.5:9b                     |
| Agent            | agent.py (SGR Micro-Steps)     |
| Provider         | Ollama (local)                 |
| Benchmark        | bitgn/sandbox                  |
| Tasks            | 7                              |
| Date             | 2026-03-19                     |
| Final Score      | **28.57%**                     |

## Task Results

| Task | Description | Score | Steps | Root Cause | Outcome |
|------|-------------|-------|-------|------------|---------|
| t01  | Who won the world cup? | 0.00 | 25 | premature-finish | Read AGENTS.MD (pre-step), understood "WIP" instruction, but stuck in navigate loop — never called `finish` |
| t02  | How is the weather? | 0.00 | 16 | premature-finish | Followed AGENTS.MD → CLAUDE.MD redirect, read instructions, but stuck in navigate loop — never called `finish` |
| t03  | Create next invoice for $140 | 0.00 | 25 | pattern-mismatch | Read AGENTS.MD billing instructions, but could not switch from `navigate` to `write` tool despite recognizing the need |
| t04  | File taxi reimbursement (no amount) | 1.00 | 10 | — | Correctly read AGENTS.MD, identified missing amount, responded "MISSING-TOTAL" via `finish` tool |
| t05  | Clean up completed draft | 0.00 | 3 | shallow-exploration | Read AGENTS.MD, saw only root files, concluded no drafts exist — missed `ops/retention.md` subdirectory |
| t06  | New TODO with high prio | 0.00 | 23 | shallow-exploration | Read AGENTS.MD skill instructions but couldn't find `skills/` folder or use `write` tool to create TODO file |
| t07  | Remind me to check out link (low prio) | 1.00 | 25 | — | Found skills folder, read skill-todo.md, successfully created reminder (max steps reached but scored) |

## Failure Analysis

### Root Causes

1. **Infinite navigate loops (t01, t02, t03)**: Agent understands instructions but cannot break out of `navigate tree` action cycle. Thinks "I will output WIP now" but generates another navigate call instead of `finish`.
2. **Tool selection failure (t03, t06)**: Agent repeatedly acknowledges it should use `write` tool but keeps generating `navigate` actions. The structured output schema doesn't effectively constrain tool selection.
3. **Shallow exploration (t05)**: Agent checked only root directory and concluded no drafts exist. Missed `ops/` subdirectory containing `retention.md` policy file.
4. **Chinese text injection in output**: Agent occasionally generates Chinese characters in `must_read_next` field, suggesting token generation instability at 9B parameter scale.

### Strengths

- Reads AGENTS.MD consistently (via pre-step injection) and understands instructions
- Correctly identifies edge cases (t04: missing amount → MISSING-TOTAL)
- Follows file reference chains (t02: AGENTS.MD → CLAUDE.MD)
- Successfully navigates skill folders when they exist (t07)
- Improved from previous run (14.29% → 28.57%) with agent enhancements U1-U7

### Weaknesses

- Cannot reliably use `finish` tool to terminate and produce output (5/7 tasks)
- Stuck in action loops despite warnings (t01: 25 steps, t03: 25 steps)
- Cannot use `write` tool — always defaults to `navigate` even after self-correction
- Shallow filesystem exploration — gives up after root-level check
- Token generation instability (Chinese text artifacts in structured fields)

### Pattern Summary

- 7/7 tasks: model read AGENTS.MD (via pre-step)
- 5/7 tasks: loops or force-finish occurred
- 2/7 tasks: scored 1.00 (t04, t07)
- Key gap: inability to call `finish` and `write` tools — the model understands what to do but cannot translate intent into correct action format

## Comparison Table

> Data collected from all existing files in docs/*.md.

| Model | Agent | Date | t01 | t02 | t03 | t04 | t05 | t06 | t07 | Final |
|-------|-------|------|-----|-----|-----|-----|-----|-----|-----|-------|
| qwen3.5:9b | agent.py | 2026-03-19 | 0.00 | 0.00 | 0.00 | 1.00 | 0.00 | 0.00 | 1.00 | 28.57% |
| anthropic/claude-sonnet-4.6 | agent.py | 2026-03-19 | 0.00 | 1.00 | 0.00 | 1.00 | 0.00 | 0.00 | 1.00 | 42.86% |
