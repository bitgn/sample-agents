# BitGN PAC1 Python Sample

Runnable Python implementation for the `bitgn/pac1-dev` benchmark, using the PCM runtime instead of a sandbox VM environment.

## Setup

Supply your API key in `.secrets` (same format as `sandbox/py/.secrets`):

```
OPENROUTER_API_KEY=sk-or-...
```

Or set the standard OpenAI key if not using OpenRouter:

```
OPENAI_API_KEY=sk-...
```

## Quick Start

```bash
make sync
make run
```

Or run directly:

```bash
uv run python main.py
```

## Universal Agent

The `agent_universal/` package provides a modular agent implementation with:
- OpenRouter support (same as `sandbox/py/agent_universal`)
- FIX-27 retry logic for transient 503/502 errors
- Log compaction (sliding window)
- Pre-phase exploration (tree + AGENTS.md)

```bash
make run-universal
```

## Configuration

Set environment variables to override defaults:

- `BENCHMARK_HOST`: defaults to `https://api.bitgn.com`
- `BENCHMARK_ID`: defaults to `bitgn/pac1-dev`
- `MODEL_ID`: defaults to `anthropic/claude-sonnet-4.6`

Or edit `MODEL_ID` in `main.py` directly.
