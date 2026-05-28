import os
import textwrap

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    EvalPolicy,
    GetBenchmarkRequest,
    StartRunRequest,
    StartTrialRequest,
    StatusRequest,
    SubmitRunRequest,
    TRIAL_STATE_DONE,
)
from connectrpc.errors import ConnectError

from agent import run_agent

BITGN_URL = os.getenv("BITGN_HOST") or "https://api.bitgn.com"
BITGN_API_KEY = os.getenv("BITGN_API_KEY") or ""
BENCH_ID = os.getenv("BENCH_ID") or os.getenv("BENCHMARK_ID") or "bitgn/pac1-prod"
MODEL_ID = os.getenv("MODEL_ID") or "gpt-4.1-2025-04-14"

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"


def main() -> None:
    task_filter = os.sys.argv[1:]

    try:
        client = HarnessServiceClientSync(BITGN_URL)

        print("Connecting to BitGN", client.status(StatusRequest()))
        res = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCH_ID))
        print(
            f"{EvalPolicy.Name(res.policy)} benchmark: {res.benchmark_id} "
            f"with {len(res.tasks)} tasks.\n{CLI_GREEN}{res.description}{CLI_CLR}"
        )

        run = client.start_run(StartRunRequest(
            name="SGR NextStep Sample",
            benchmark_id=BENCH_ID,
            api_key=BITGN_API_KEY))

        try:
            for trial_id in run.trial_ids:
                t = client.start_trial(
                    StartTrialRequest(trial_id=trial_id),
                )

                if task_filter and t.task_id not in task_filter:
                    continue

                print(f"{'=' * 30} Starting task: {t.task_id} {'=' * 30}")

                print(f"{CLI_BLUE}{t.instruction}{CLI_CLR}\n{'-' * 80}")

                try:
                    run_agent(MODEL_ID, t.harness_url, t.instruction)
                except Exception as exc:
                    print(exc)

                # complete run to get scores
                client.end_trial(EndTrialRequest(trial_id=t.trial_id))

        finally:
            print(f"\n{CLI_GREEN}>>>> Submitting run... <<<<{CLI_CLR}")
            result = client.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))

            if result.score_available:
                print(f"FINAL SCORE: {result.score:0.2f}")
                incomplete = 0
                for t in result.trials:
                    if t.state != TRIAL_STATE_DONE:
                        incomplete += 1
                        continue  # skip filtered tasks

                    style = CLI_GREEN if t.score == 1 else CLI_RED
                    explain = "\n" + textwrap.indent("\n".join(t.score_detail), "  ") + "\n"
                    print(
                        f"- {t.task_id}: {style}Score: {t.score:0.2f}{CLI_CLR}{explain}".strip(
                            "\n "
                        )
                    )

                if incomplete > 0:
                    print(f"{CLI_RED}incomplete trials: {incomplete}{CLI_CLR}")
            else:
                print(
                    f"\n{CLI_RED}Score is not available. Results are sealed and will be revealed later{CLI_CLR}\n"
                )

    except ConnectError as exc:
        print(f"{exc.code}: {exc.message}")
    except KeyboardInterrupt:
        print(f"{CLI_RED}Interrupted{CLI_CLR}")


if __name__ == "__main__":
    main()
