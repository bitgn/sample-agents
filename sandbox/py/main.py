import os
import textwrap

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import StatusRequest, GetBenchmarkRequest, StartPlaygroundRequest, EvalPolicy, EndTrialRequest
from connectrpc.errors import ConnectError

from agent import run_agent

BITGN_URL = os.getenv("BENCHMARK_HOST") or "https://api.bitgn.com"

MODEL_ID = "gpt-4.1-2025-04-14"

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"


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
            print(f"{'='*30} Starting task: {t.task_id} {'='*30}")

            trial = client.start_playground(StartPlaygroundRequest(
                benchmark_id="bitgn/sandbox",
                task_id=t.task_id,
            ))

            print(f"{CLI_BLUE}{trial.instruction}{CLI_CLR}\n{'-'*80}")

            try:
                run_agent(MODEL_ID,trial.harness_url, trial.instruction)
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
