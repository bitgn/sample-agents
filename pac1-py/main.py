import os
import textwrap
from dotenv import load_dotenv

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import EndTrialRequest, EvalPolicy, GetBenchmarkRequest, StartPlaygroundRequest, StatusRequest
from connectrpc.errors import ConnectError

from agent import run_agent
from tracing import init_tracing, trace_run, trace_task, record_task_score, record_run_llm_totals

load_dotenv()

BITGN_URL = os.getenv("BENCHMARK_HOST") or "https://api.bitgn.com"
BENCHMARK_ID = os.getenv("BENCHMARK_ID") or "bitgn/pac1-dev"
MODEL_ID = os.getenv("MODEL_ID") or "gpt-4.1-2025-04-14"

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"


def main() -> None:
    task_filter = os.sys.argv[1:]
    is_debug = bool(task_filter)

    init_tracing(debug=is_debug)

    scores = []
    run_llm_totals = {
        "prompt_tokens": 0.0,
        "completion_tokens": 0.0,
        "total_tokens": 0.0,
        "cached_prompt_tokens": 0.0,
        "input_cost_usd": 0.0,
        "output_cost_usd": 0.0,
        "cost_usd": 0.0,
    }
    try:
        client = HarnessServiceClientSync(BITGN_URL)
        print("Connecting to BitGN", client.status(StatusRequest()))
        res = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCHMARK_ID))
        print(
            f"{EvalPolicy.Name(res.policy)} benchmark: {res.benchmark_id} "
            f"with {len(res.tasks)} tasks.\n{CLI_GREEN}{res.description}{CLI_CLR}"
        )

        active_tasks = [t for t in res.tasks if not task_filter or t.task_id in task_filter]

        with trace_run(
            model_id=MODEL_ID,
            benchmark_id=BENCHMARK_ID,
            task_count=len(active_tasks),
            debug=is_debug,
        ):
            for task in active_tasks:
                print(f"{'=' * 30} Starting task: {task.task_id} {'=' * 30}")
                trial = client.start_playground(
                    StartPlaygroundRequest(
                        benchmark_id=BENCHMARK_ID,
                        task_id=task.task_id,
                    )
                )

                print(f"{CLI_BLUE}{trial.instruction}{CLI_CLR}\n{'-' * 80}")

                with trace_task(task.task_id, trial.instruction) as task_run:
                    task_llm_totals = None
                    try:
                        task_llm_totals = run_agent(MODEL_ID, trial.harness_url, trial.instruction)
                    except Exception as exc:
                        import mlflow
                        mlflow.log_param("task_error", str(exc)[:250])
                        print(exc)
                    finally:
                        if task_llm_totals is not None:
                            for key in run_llm_totals:
                                run_llm_totals[key] += task_llm_totals.get(key, 0.0)

                    result = client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
                    if result.score >= 0:
                        scores.append((task.task_id, result.score))
                        record_task_score(task_run, task.task_id, result.score, list(result.score_detail))
                        style = CLI_GREEN if result.score == 1 else CLI_RED
                        explain = textwrap.indent("\n".join(result.score_detail), "  ")
                        print(f"\n{style}Score: {result.score:0.2f}\n{explain}\n{CLI_CLR}")

            record_run_llm_totals(MODEL_ID, run_llm_totals)

    except ConnectError as exc:
        print(f"{exc.code}: {exc.message}")
    except KeyboardInterrupt:
        print(f"{CLI_RED}Interrupted{CLI_CLR}")

    if scores:
        for task_id, score in scores:
            style = CLI_GREEN if score == 1 else CLI_RED
            print(f"{task_id}: {style}{score:0.2f}{CLI_CLR}")

        total = sum(score for _, score in scores) / len(scores) * 100.0
        print(f"FINAL: {total:0.2f}%")

    print(
        "FULL RUN LLM:"
        f" prompt={int(run_llm_totals['prompt_tokens'])}"
        f" completion={int(run_llm_totals['completion_tokens'])}"
        f" total={int(run_llm_totals['total_tokens'])}"
        f" cached={int(run_llm_totals['cached_prompt_tokens'])}"
        f" cost=${run_llm_totals['cost_usd']:.6f}"
    )


if __name__ == "__main__":
    main()
