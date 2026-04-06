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
from pathlib import Path

import mlflow

_DATA_DIR = Path(__file__).parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

MLFLOW_DB = _DATA_DIR / "mlflow.db"
MLFLOW_TRACKING_URI = f"sqlite:///{MLFLOW_DB}"

_initialized = False


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
