"""
tracing.py – MLflow observability for PAC1 agent runs.

Stores traces in a local SQLite database at data/mlflow.db via MLflow's
SQL-based tracking. OpenAI calls are auto-traced by MLflow's built-in
OpenAI autologging.

Each new invocation creates a fresh timestamped experiment so traces stay
grouped by run session. Full agent runs use names like
"2026-04-06-14-32-full-run". Debug / single-task runs use names like
"2026-04-06-14-32-debug-run".
"""

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

import mlflow

_DATA_DIR = Path(__file__).parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

MLFLOW_DB = _DATA_DIR / "mlflow.db"
MLFLOW_TRACKING_URI = f"sqlite:///{MLFLOW_DB}"
MODEL_PRICES_PATH = Path(__file__).parent / "model_prices.json"

_initialized = False
_model_prices_cache: dict[str, dict[str, float]] | None = None
_model_price_warnings: set[str] = set()


def _experiment_name(*, debug: bool) -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M")
    run_kind = "debug-run" if debug else "full-run"
    return f"{timestamp}-{run_kind}"


def init_tracing(*, debug: bool = False) -> None:
    """
    Configure MLflow with a SQLite tracking store and enable OpenAI autologging.

    Args:
        debug: If True, traces go to a timestamped debug-run experiment;
               otherwise they go to a timestamped full-run experiment.
    """
    global _initialized
    if _initialized:
        return

    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    mlflow.set_experiment(_experiment_name(debug=debug))

    mlflow.openai.autolog()

    _initialized = True


def _load_model_prices() -> dict[str, dict[str, float]]:
    global _model_prices_cache
    if _model_prices_cache is not None:
        return _model_prices_cache

    if not MODEL_PRICES_PATH.exists():
        _model_prices_cache = {}
        return _model_prices_cache

    with MODEL_PRICES_PATH.open() as f:
        raw = json.load(f)

    _model_prices_cache = {
        model_id: {
            key: float(value)
            for key, value in pricing.items()
            if value is not None
        }
        for model_id, pricing in raw.items()
    }
    return _model_prices_cache


def _model_price_candidates(model_id: str) -> list[str]:
    candidates = [model_id]
    snapshot_match = re.match(r"^(.+)-\d{4}-\d{2}-\d{2}$", model_id)
    if snapshot_match:
        candidates.append(snapshot_match.group(1))
    return candidates


def _resolve_model_pricing_entry(model_id: str) -> tuple[str, dict[str, float]] | None:
    prices = _load_model_prices()
    for candidate in _model_price_candidates(model_id):
        pricing = prices.get(candidate)
        if pricing:
            return candidate, pricing
    return None


def _resolve_model_pricing(model_id: str) -> dict[str, float] | None:
    resolved = _resolve_model_pricing_entry(model_id)
    if resolved is None:
        return None
    _, pricing = resolved
    return pricing


def _log_missing_model_pricing(model_id: str) -> None:
    if model_id in _model_price_warnings:
        return
    print(
        "MLflow cost tracing skipped:"
        f" no entry for model '{model_id}' in {MODEL_PRICES_PATH.name}"
    )
    _model_price_warnings.add(model_id)


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _usage_totals(usage: Any) -> dict[str, int]:
    prompt_tokens = _as_int(getattr(usage, "prompt_tokens", None))
    completion_tokens = _as_int(getattr(usage, "completion_tokens", None))
    total_tokens = _as_int(getattr(usage, "total_tokens", None)) or (prompt_tokens + completion_tokens)

    prompt_details = getattr(usage, "prompt_tokens_details", None)
    cached_prompt_tokens = _as_int(getattr(prompt_details, "cached_tokens", None))
    cached_prompt_tokens = min(cached_prompt_tokens, prompt_tokens)

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
    }


def record_llm_usage(model_id: str, usage: Any, *, step: int) -> dict[str, float]:
    """
    Log token usage and optional USD cost for one LLM call.

    Cost calculation uses rates from model_prices.json keyed by model alias or
    exact snapshot, e.g. "gpt-4.1" or "gpt-4.1-2025-04-14".
    """
    totals = _usage_totals(usage)
    mlflow.log_metric("llm_prompt_tokens", totals["prompt_tokens"], step=step)
    mlflow.log_metric("llm_completion_tokens", totals["completion_tokens"], step=step)
    mlflow.log_metric("llm_total_tokens", totals["total_tokens"], step=step)
    mlflow.log_metric("llm_cached_prompt_tokens", totals["cached_prompt_tokens"], step=step)

    summary: dict[str, float] = {
        "prompt_tokens": float(totals["prompt_tokens"]),
        "completion_tokens": float(totals["completion_tokens"]),
        "total_tokens": float(totals["total_tokens"]),
        "cached_prompt_tokens": float(totals["cached_prompt_tokens"]),
        "input_cost_usd": 0.0,
        "output_cost_usd": 0.0,
        "cost_usd": 0.0,
    }

    pricing = _resolve_model_pricing(model_id)
    if not pricing:
        _log_missing_model_pricing(model_id)
        return summary

    uncached_prompt_tokens = max(totals["prompt_tokens"] - totals["cached_prompt_tokens"], 0)
    input_rate = pricing["input_cost_per_million"]
    output_rate = pricing["output_cost_per_million"]
    cached_input_rate = pricing.get("cached_input_cost_per_million", input_rate)

    input_cost_usd = (
        uncached_prompt_tokens * input_rate
        + totals["cached_prompt_tokens"] * cached_input_rate
    ) / 1_000_000
    output_cost_usd = totals["completion_tokens"] * output_rate / 1_000_000
    total_cost_usd = input_cost_usd + output_cost_usd

    mlflow.log_metric("llm_input_cost_usd", input_cost_usd, step=step)
    mlflow.log_metric("llm_output_cost_usd", output_cost_usd, step=step)
    mlflow.log_metric("llm_cost_usd", total_cost_usd, step=step)

    summary["input_cost_usd"] = input_cost_usd
    summary["output_cost_usd"] = output_cost_usd
    summary["cost_usd"] = total_cost_usd
    return summary


def record_llm_totals(totals: dict[str, float]) -> None:
    """Log cumulative LLM token/cost totals for the current task run."""
    for metric_name, value in (
        ("llm_prompt_tokens_total", totals["prompt_tokens"]),
        ("llm_completion_tokens_total", totals["completion_tokens"]),
        ("llm_total_tokens_total", totals["total_tokens"]),
        ("llm_cached_prompt_tokens_total", totals["cached_prompt_tokens"]),
        ("llm_input_cost_usd_total", totals["input_cost_usd"]),
        ("llm_output_cost_usd_total", totals["output_cost_usd"]),
        ("llm_cost_usd_total", totals["cost_usd"]),
    ):
        mlflow.log_metric(metric_name, value)


def record_llm_metadata(model_id: str, totals: dict[str, float]) -> None:
    """Store cumulative LLM usage/cost state as run metadata via MLflow tags."""
    resolved = _resolve_model_pricing_entry(model_id)
    priced_model = resolved[0] if resolved is not None else ""
    mlflow.set_tags(
        {
            "llm_model_id": model_id,
            "llm_pricing_model": priced_model,
            "llm_pricing_source": MODEL_PRICES_PATH.name,
            "llm_cost_traced": str(bool(priced_model)).lower(),
            "llm_prompt_tokens_total": str(int(totals["prompt_tokens"])),
            "llm_completion_tokens_total": str(int(totals["completion_tokens"])),
            "llm_total_tokens_total": str(int(totals["total_tokens"])),
            "llm_cached_prompt_tokens_total": str(int(totals["cached_prompt_tokens"])),
            "llm_input_cost_usd_total": f"{totals['input_cost_usd']:.10f}",
            "llm_output_cost_usd_total": f"{totals['output_cost_usd']:.10f}",
            "llm_cost_usd_total": f"{totals['cost_usd']:.10f}",
        }
    )


def record_run_llm_totals(model_id: str, totals: dict[str, float]) -> None:
    """Log full-run cumulative LLM totals on the parent run."""
    for metric_name, value in (
        ("run_llm_prompt_tokens_total", totals["prompt_tokens"]),
        ("run_llm_completion_tokens_total", totals["completion_tokens"]),
        ("run_llm_total_tokens_total", totals["total_tokens"]),
        ("run_llm_cached_prompt_tokens_total", totals["cached_prompt_tokens"]),
        ("run_llm_input_cost_usd_total", totals["input_cost_usd"]),
        ("run_llm_output_cost_usd_total", totals["output_cost_usd"]),
        ("run_llm_cost_usd_total", totals["cost_usd"]),
    ):
        mlflow.log_metric(metric_name, value)

    resolved = _resolve_model_pricing_entry(model_id)
    priced_model = resolved[0] if resolved is not None else ""
    mlflow.set_tags(
        {
            "run_llm_model_id": model_id,
            "run_llm_pricing_model": priced_model,
            "run_llm_pricing_source": MODEL_PRICES_PATH.name,
            "run_llm_cost_traced": str(bool(priced_model)).lower(),
            "run_llm_prompt_tokens_total": str(int(totals["prompt_tokens"])),
            "run_llm_completion_tokens_total": str(int(totals["completion_tokens"])),
            "run_llm_total_tokens_total": str(int(totals["total_tokens"])),
            "run_llm_cached_prompt_tokens_total": str(int(totals["cached_prompt_tokens"])),
            "run_llm_input_cost_usd_total": f"{totals['input_cost_usd']:.10f}",
            "run_llm_output_cost_usd_total": f"{totals['output_cost_usd']:.10f}",
            "run_llm_cost_usd_total": f"{totals['cost_usd']:.10f}",
        }
    )


@contextmanager
def trace_run(*, model_id: str, benchmark_id: str, task_count: int, debug: bool):
    """Top-level MLflow run wrapping an entire agent run."""
    with mlflow.start_run(run_name="agent-run") as run:
        mlflow.log_params(
            {
                "model_id": model_id,
                "benchmark_id": benchmark_id,
                "task_count": task_count,
                "debug": debug,
            }
        )
        yield run


@contextmanager
def trace_task(task_id: str, instruction: str):
    """Nested MLflow run wrapping a single task. OpenAI calls become children."""
    with mlflow.start_run(run_name=f"task:{task_id}", nested=True) as run:
        mlflow.log_params(
            {
                "task_id": task_id,
                "instruction": instruction[:250],
            }
        )
        yield run


def record_task_score(span, task_id: str, score: float, score_detail: list[str]) -> None:
    """Log scoring results as metrics/params on the current task run."""
    mlflow.log_metric("score", score)
    mlflow.log_param("score_detail", "\n".join(score_detail)[:250])
