# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains sample AI agents for the **BitGN sandbox benchmark** — a platform for evaluating autonomous agents on structured tasks within an Obsidian vault-like filesystem environment. The primary implementation is a Python agent using Schema-Guided Reasoning (SGR).

## Commands

All commands run from `sandbox/py/`:

```bash
# Run full benchmark (all tasks)
uv run python main.py

# Run specific tasks by ID
uv run python main.py t01 t02 t03

# Install/sync dependencies
uv sync
```

Environment setup via Nix:
```bash
nix develop  # Enter dev shell with Go, protobuf, Python 3.14, uv
```

API keys go in `sandbox/py/.secrets` (one `KEY=value` per line, not tracked by git).

## Architecture

### Entry Point Flow

```
main.py → HarnessServiceClientSync (api.bitgn.com)
  → for each task: start_playground → run_agent() → end_trial
```

`main.py` fetches benchmark tasks, runs the agent loop per task, and reports aggregate scores.

### Core Agent (`sandbox/py/agent.py`)

The agent uses **Pydantic-structured LLM outputs** (OpenAI SDK `response_format=`) with 4 action types:

| Action | Subtype | Maps to VM method |
|--------|---------|------------------|
| `Navigate` | `tree` | `vm.outline(path)` |
| `Navigate` | `list` | `vm.list(path)` |
| `Inspect` | `read` | `vm.read(path)` |
| `Inspect` | `search` | `vm.search(path, pattern)` |
| `Modify` | `write` | `vm.write(path, content)` |
| `Modify` | `delete` | `vm.delete(path)` |
| `Finish` | — | `vm.answer(answer, refs)` |

Each LLM step produces a `MicroStep` with fields: `think` (one-sentence COT), `prev_result_ok`, `prev_result_problem`, `action`.

### VM Client (`sandbox/py/bitgn/vm/mini_connect.py`)

Connect-RPC client (via `connect-python`) to the sandbox harness. Provides the 7 VM methods listed above. Uses locally generated protobuf (`bitgn/vm/mini_pb2.py`, `bitgn/harness_pb2.py`) — do not regenerate unless the `.proto` files change.

### Model Configuration

Defined in `main.py` as `MODEL_CONFIGS` dict. Current default: `qwen3.5:9b` (local Ollama). Alternative: `anthropic/claude-sonnet-4.6` via OpenRouter. Switch by changing `MODEL_ID` at top of `main.py`.

### Key Files

| File | Purpose |
|------|---------|
| `sandbox/py/main.py` | Benchmark runner and task loop |
| `sandbox/py/agent.py` | Agent loop with U1–U7 enhancements |
| `sandbox/py/bitgn/vm/mini_connect.py` | VM Connect-RPC client |
| `sandbox/py/AGENTS.MD` | Task conventions read by the agent at runtime |
| `flake.nix` | Nix dev environment |

## Important Conventions

- `AGENTS.MD` (inside the sandbox vault) is a runtime instruction file that the agent reads on every run — it defines naming patterns and task rules for the benchmark.
- The agent log is compacted using a sliding window to stay within token limits; the system prompt + first two messages are always preserved.
