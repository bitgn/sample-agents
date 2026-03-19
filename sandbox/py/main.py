import os
import textwrap

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import StatusRequest, GetBenchmarkRequest, StartPlaygroundRequest, EvalPolicy, EndTrialRequest
from connectrpc.errors import ConnectError

from agent import run_agent

BITGN_URL = os.getenv("BENCHMARK_HOST") or "https://api.bitgn.com"

# MODEL_ID = "anthropic/claude-sonnet-4.6"
MODEL_ID = "qwen3.5:9b"

# U7: Model-specific configurations
MODEL_CONFIGS = {
    "qwen3.5:9b": {"max_completion_tokens": 512},
    "qwen3.5:14b": {"max_completion_tokens": 512},
}


CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"


def main() -> None:

    # optional task ids could be included as tasks to run, e.g. `python main.py task1 task2`
    task_filter = os.sys.argv[1:]


    scores = []
    try:
        client = HarnessServiceClientSync(BITGN_URL)
        print("Connecting to BitGN", client.status(StatusRequest()))
        res = client.get_benchmark(GetBenchmarkRequest(benchmark_id="bitgn/sandbox"))
        print(f"{EvalPolicy.Name(res.policy)} benchmark: {res.benchmark_id} with {len(res.tasks)} tasks.\n{CLI_GREEN}{res.description}{CLI_CLR}")


        for t in res.tasks:
            if task_filter and t.task_id not in task_filter:
                continue
            print("=" * 40)
            print(f"Starting Task: {t.task_id}")

            trial = client.start_playground(StartPlaygroundRequest(
                benchmark_id="bitgn/sandbox",
                task_id=t.task_id,
            ))

            print("Task:", trial.instruction)

            try:
                run_agent(MODEL_ID, trial.harness_url, trial.instruction,
                          model_config=MODEL_CONFIGS.get(MODEL_ID))
            except Exception as e:
                print(e)

            result = client.end_trial(EndTrialRequest(trial_id=trial.trial_id))


            if result.score >= 0:
                scores.append((t.task_id, result.score))

                style = CLI_GREEN if result.score == 1 else CLI_RED

                explain = textwrap.indent("\n".join(result.score_detail), "  ")
                print(f"\n{style}Score: {result.score:0.2f}\n{explain}\n{CLI_CLR}")

    except ConnectError as e:
        print(f"{e.code}: {e.message}")
    except KeyboardInterrupt:
        print(f"{CLI_RED}Interrupted{CLI_CLR}")

    # print scores as table
    if scores:
        for tid, score in scores:
            style = CLI_GREEN if score == 1 else CLI_RED
            print(f"{tid}: {style}{score:0.2f}{CLI_CLR}")

        # print average
        total = sum([t[1] for t in scores]) / len(scores) * 100.0
        print(f"FINAL: {total:0.2f}%")


if __name__ == "__main__":
    main()
