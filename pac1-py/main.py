import os
import textwrap
import time

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import EndTrialRequest, EvalPolicy, GetBenchmarkRequest, StartPlaygroundRequest, StatusRequest
from connectrpc.errors import ConnectError

from agent import run_agent

BITGN_URL = os.getenv("BENCHMARK_HOST") or "https://api.bitgn.com"
BENCHMARK_ID = os.getenv("BENCHMARK_ID") or "bitgn/pac1-dev"
MODEL_ID = os.getenv("MODEL_ID") or "anthropic/claude-haiku-4.5"

MODEL_CONFIGS: dict[str, dict] = {
    # Claude models — use Anthropic SDK directly
    "anthropic/claude-haiku-4.5": {},
    "anthropic/claude-sonnet-4.6": {},
    # Ollama local fallback models
    "qwen/qwen3.5-9b": {"max_completion_tokens": 4000, "ollama_model": "qwen3.5:9b"},
    "qwen2.5:7b": {"max_completion_tokens": 4000},
}

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"


def main() -> None:
    task_filter = os.sys.argv[1:]

    scores = []
    run_start = time.time()
    try:
        client = HarnessServiceClientSync(BITGN_URL)
        print("Connecting to BitGN", client.status(StatusRequest()))
        res = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCHMARK_ID))
        print(
            f"{EvalPolicy.Name(res.policy)} benchmark: {res.benchmark_id} "
            f"with {len(res.tasks)} tasks.\n{CLI_GREEN}{res.description}{CLI_CLR}"
        )

        for task in res.tasks:
            if task_filter and task.task_id not in task_filter:
                continue

            print(f"{'=' * 30} Starting task: {task.task_id} {'=' * 30}")
            task_start = time.time()
            trial = client.start_playground(
                StartPlaygroundRequest(
                    benchmark_id=BENCHMARK_ID,
                    task_id=task.task_id,
                )
            )

            print(f"{CLI_BLUE}{trial.instruction}{CLI_CLR}\n{'-' * 80}")

            try:
                run_agent(MODEL_ID, trial.harness_url, trial.instruction,
                          model_config=MODEL_CONFIGS.get(MODEL_ID))
            except Exception as exc:
                print(exc)

            task_elapsed = time.time() - task_start
            result = client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
            if result.score >= 0:
                scores.append((task.task_id, result.score, list(result.score_detail), task_elapsed))
                style = CLI_GREEN if result.score == 1 else CLI_RED
                explain = textwrap.indent("\n".join(result.score_detail), "  ")
                print(f"\n{style}Score: {result.score:0.2f}\n{explain}\n{CLI_CLR}")

    except ConnectError as exc:
        print(f"{exc.code}: {exc.message}")
    except KeyboardInterrupt:
        print(f"{CLI_RED}Interrupted{CLI_CLR}")

    if scores:
        for task_id, score, *_ in scores:
            style = CLI_GREEN if score == 1 else CLI_RED
            print(f"{task_id}: {style}{score:0.2f}{CLI_CLR}")

        total = sum(score for _, score, *_ in scores) / len(scores) * 100.0
        total_elapsed = time.time() - run_start
        print(f"FINAL: {total:0.2f}%")

        # Summary table for log (no color codes)
        sep = "=" * 80
        print(f"\n{sep}")
        print(f"{'ИТОГОВАЯ СТАТИСТИКА':^80}")
        print(sep)
        print(f"{'Задание':<10} {'Оценка':>7} {'Время':>8}  Проблемы")
        print("-" * 80)
        for task_id, score, detail, elapsed in scores:
            issues = "; ".join(detail) if score < 1.0 else "—"
            print(f"{task_id:<10} {score:>7.2f} {elapsed:>7.1f}s  {issues}")
        print(sep)
        print(f"{'ИТОГО':<10} {total:>6.2f}% {total_elapsed:>7.1f}s")
        print(sep)


if __name__ == "__main__":
    main()
