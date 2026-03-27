# BitGN PAC1 Python Sample

Runnable Python implementation for the `bitgn/pac1-dev` benchmark, using the PCM runtime instead of a sandbox VM environment.

## Setup

Supply API keys in `.secrets`:

```
OPENROUTER_API_KEY=sk-or-...   # cloud models via OpenRouter
ANTHROPIC_API_KEY=sk-ant-...   # Claude models directly (optional)
```

For local Ollama — no key needed. Set `OLLAMA_BASE_URL` if not on `localhost:11434`.

## Quick Start

```bash
make sync
make run
```

## Model Configuration

### Normal mode — single model

```bash
MODEL_ID=anthropic/claude-sonnet-4.6 uv run python main.py
```

**Model name formats:**

| Format | Routing | Examples |
|--------|---------|---------|
| `name/model` | Anthropic SDK → OpenRouter | `anthropic/claude-sonnet-4.6`, `qwen/qwen3.5-9b` |
| `name:tag` | Ollama (local or cloud) | `qwen3.5:9b`, `deepseek-v3.1:671b-cloud` |

For Ollama cloud models, set `OLLAMA_BASE_URL` to point to the cloud endpoint:

```bash
OLLAMA_BASE_URL=https://your-ollama-cloud/v1 MODEL_ID=deepseek-v3.1:671b-cloud uv run python main.py
```

### Multi-model mode — different models per task type

Override specific task types while keeping a default:

```bash
MODEL_DEFAULT=deepseek-v3.1:671b-cloud \
MODEL_THINK=deepseek-r1:671b-cloud \
MODEL_TOOL=qwen3.5:9b \
uv run python main.py
```

| Env var | Task type | Triggers on |
|---------|-----------|------------|
| `MODEL_DEFAULT` | everything else | standard read/write/create tasks |
| `MODEL_THINK` | reasoning | analyze, distill, compare, evaluate |
| `MODEL_TOOL` | file ops | delete, move, rename, copy |
| `MODEL_LONG_CONTEXT` | bulk ops | all files, batch, 3+ explicit paths |

All four default to `MODEL_ID` when not set.

### Classifier model

LLM-based task classification runs on `MODEL_DEFAULT` by default. To use a lighter model:

```bash
MODEL_CLASSIFIER=qwen3.5:4b MODEL_DEFAULT=deepseek-v3.1:671b-cloud uv run python main.py
```

Falls back to regex classification if LLM classification fails.

## Other Variables

| Env var | Default | Description |
|---------|---------|-------------|
| `TASK_TIMEOUT_S` | `180` | Per-task timeout in seconds |
| `BENCHMARK_HOST` | `https://api.bitgn.com` | API endpoint |
| `BENCHMARK_ID` | `bitgn/pac1-dev` | Benchmark to run |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama endpoint |
| `OLLAMA_MODEL` | _(MODEL_ID)_ | Override Ollama model name |

## Run Examples

```bash
# Single task, custom timeout
TASK_TIMEOUT_S=600 uv run python main.py t01

# Multi-model run with log capture
TZ=Europe/Moscow ts=$(date +"%Y%m%d_%H%M%S") \
MODEL_DEFAULT=deepseek-v3.1:671b-cloud \
MODEL_THINK=deepseek-r1:671b-cloud \
TASK_TIMEOUT_S=900 uv run python main.py 2>&1 | tee >(sed 's/\x1B\[[0-9;]*[A-Za-z]//g' > "../tmp/${ts}_run.log")
```
