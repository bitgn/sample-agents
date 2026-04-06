# BitGN PAC1 Python Sample

Runnable Python sample for the `bitgn/pac1-dev` benchmark.

This sample follows the same control-plane flow as the sandbox demo, but it targets the PCM runtime (`bitgn.vm.pcm`) instead of the mini sandbox VM.

You will need to provide your own `OPENAI_API_KEY` (or swap the OpenAI client for a provider of your choice).

Quick start:

1. Export `OPENAI_API_KEY`
2. Optionally export `BENCHMARK_HOST`, `BENCHMARK_ID`, or `MODEL_ID`
3. Run `make sync`
4. Run `make run`

Run it with:

```bash
uv run python main.py
```

Useful environment overrides:

- `BENCHMARK_HOST` defaults to `https://api.bitgn.com`
- `BENCHMARK_ID` defaults to `bitgn/pac1-dev`
- `MODEL_ID` defaults to `gpt-4.1-2025-04-14`

MLflow cost tracing:

- Token usage and USD cost are logged into each task run.
- Rates are loaded from `model_prices.json`.
- Snapshot model IDs such as `gpt-4.1-2025-04-14` fall back to the base alias `gpt-4.1`.
- If you use other models, add them to that JSON file with `input_cost_per_million`, `cached_input_cost_per_million`, and `output_cost_per_million`.
