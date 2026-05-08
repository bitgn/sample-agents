# BitGN ECOM Python Sample

Runnable Python sample for the `bitgn/ecom1-dev` benchmark.

ECOM is an ecommerce operations runtime. It exposes a file-shaped workspace plus runtime tools such as `/bin/sql` over the `bitgn.vm.ecom` API.

You will need to provide your own `BITGN_API_KEY` and `OPENAI_API_KEY`, or swap the OpenAI client for a provider of your choice.

## Setup

1. Export `BITGN_API_KEY`
2. Export `OPENAI_API_KEY`
3. Optionally export `BENCH_ID`, `BENCHMARK_ID`, or `MODEL_ID`
4. Run `make sync`
5. Run `make run`

## Commands

- Run the full ECOM benchmark: `uv run python main.py`
- Run a single task: `uv run python main.py t01`
- Run a subset of tasks: `uv run python main.py t01 t04`
- Install or update the local environment: `make sync`
- Run the full benchmark via Make: `make run`
- Run selected tasks via Make: `make task TASKS="t01 t04"`

Useful environment overrides:

- `BITGN_API_KEY` is required for official ECOM benchmark runs
- `BENCH_ID` or `BENCHMARK_ID` defaults to `bitgn/ecom1-dev`
- `MODEL_ID` defaults to `gpt-4.1-2025-04-14`
