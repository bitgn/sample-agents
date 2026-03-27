import os
import textwrap
import time

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import EndTrialRequest, EvalPolicy, GetBenchmarkRequest, StartPlaygroundRequest, StatusRequest
from connectrpc.errors import ConnectError

from agent import run_agent
from agent.classifier import ModelRouter

BITGN_URL = os.getenv("BENCHMARK_HOST") or "https://api.bitgn.com"
BENCHMARK_ID = os.getenv("BENCHMARK_ID") or "bitgn/pac1-dev"
MODEL_ID = os.getenv("MODEL_ID") or "qwen3.5:cloud"

MODEL_CONFIGS: dict[str, dict] = {
    # Anthropic Claude models (primary: Anthropic SDK; fallback: OpenRouter)
    # response_format_hint used when falling back to OpenRouter tier
    "anthropic/claude-haiku-4.5":  {"max_completion_tokens": 16384, "thinking_budget": 2000, "response_format_hint": "json_object"},
    "anthropic/claude-sonnet-4.6": {"max_completion_tokens": 16384, "thinking_budget": 4000, "response_format_hint": "json_object"},
    "anthropic/claude-opus-4.6":   {"max_completion_tokens": 16384, "thinking_budget": 8000, "response_format_hint": "json_object"},
    # Open models via OpenRouter
    "qwen/qwen3.5-9b":             {"max_completion_tokens": 4000,  "response_format_hint": "json_object"},
    "meta-llama/llama-3.3-70b-instruct": {"max_completion_tokens": 4000, "response_format_hint": "json_object"},
    # Ollama local fallback models
    "qwen3.5:9b":    {"max_completion_tokens": 4000, "ollama_think": True},
    "qwen3.5:4b":    {"max_completion_tokens": 4000, "ollama_think": False},
    "qwen3.5:2b":    {"max_completion_tokens": 4000, "ollama_think": False},
    "qwen3.5:0.8b":  {"max_completion_tokens": 4000, "ollama_think": False},
    # Ollama cloud models
    "qwen3.5:cloud": {"max_completion_tokens": 4000, "ollama_think": True},
    "qwen3.5:397b-cloud": {"max_completion_tokens": 4000, "ollama_think": True},
    # FIX-85: cloud-hosted Ollama-format models (name:tag routing, served via OLLAMA_BASE_URL)
    "deepseek-v3.1:671b-cloud":  {"max_completion_tokens": 4000, "ollama_think": False},
    "deepseek-r1:671b-cloud":    {"max_completion_tokens": 4000, "ollama_think": True},
    "deepseek-v3:685b-cloud":    {"max_completion_tokens": 4000, "ollama_think": False},
}

# Multi-model routing: MODEL_DEFAULT/THINK/TOOL/LONG_CONTEXT override MODEL_ID
_model_default  = os.getenv("MODEL_DEFAULT")     or MODEL_ID
_model_think    = os.getenv("MODEL_THINK")        or MODEL_ID
_model_tool     = os.getenv("MODEL_TOOL")         or MODEL_ID
_model_long_ctx = os.getenv("MODEL_LONG_CONTEXT") or MODEL_ID
_model_classifier = os.getenv("MODEL_CLASSIFIER") or ""  # FIX-86: optional lightweight model for task classification

# FIX-88: always use ModelRouter — classification runs for every task,
# logs always show [MODEL_ROUTER] lines, stats always show Тип/Модель columns.
EFFECTIVE_MODEL: ModelRouter = ModelRouter(
    default=_model_default,
    think=_model_think,
    tool=_model_tool,
    long_context=_model_long_ctx,
    classifier=_model_classifier,
    configs=MODEL_CONFIGS,
)
_is_multi = any(v != MODEL_ID for v in [_model_default, _model_think, _model_tool, _model_long_ctx])
print(
    f"[MODEL_ROUTER] {'Multi' if _is_multi else 'Single'}-model mode: "
    f"default={_model_default}, think={_model_think}, tool={_model_tool}, longContext={_model_long_ctx}"
    + (f", classifier={_model_classifier}" if _model_classifier else "")
)

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

            token_stats: dict = {"input_tokens": 0, "output_tokens": 0}
            try:
                token_stats = run_agent(EFFECTIVE_MODEL, trial.harness_url, trial.instruction,
                                        model_config=MODEL_CONFIGS.get(MODEL_ID))
            except Exception as exc:
                print(exc)

            task_elapsed = time.time() - task_start
            result = client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
            if result.score >= 0:
                scores.append((task.task_id, result.score, list(result.score_detail), task_elapsed, token_stats))
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

        total_in = total_out = 0
        for *_, ts in scores:
            total_in += ts.get("input_tokens", 0)
            total_out += ts.get("output_tokens", 0)

        # Summary table for log (no color codes)
        W = 140
        sep = "=" * W
        print(f"\n{sep}")
        _title = "ИТОГОВАЯ СТАТИСТИКА (multi-model)" if _is_multi else "ИТОГОВАЯ СТАТИСТИКА"
        print(f"{_title:^{W}}")
        print(sep)
        print(f"{'Задание':<10} {'Оценка':>7} {'Время':>8}  {'Вход(tok)':>10} {'Выход(tok)':>10}  {'Тип':<11} {'Модель':<34}  Проблемы")
        print("-" * W)
        model_totals: dict[str, dict] = {}
        for task_id, score, detail, elapsed, ts in scores:
            issues = "; ".join(detail) if score < 1.0 else "—"
            in_t = ts.get("input_tokens", 0)
            out_t = ts.get("output_tokens", 0)
            m = ts.get("model_used", MODEL_ID)
            m_short = m.split("/")[-1] if "/" in m else m
            t_type = ts.get("task_type", "—")
            print(f"{task_id:<10} {score:>7.2f} {elapsed:>7.1f}s  {in_t:>10,} {out_t:>10,}  {t_type:<11} {m_short:<34}  {issues}")
            if m not in model_totals:
                model_totals[m] = {"in": 0, "out": 0, "count": 0}
            model_totals[m]["in"] += in_t
            model_totals[m]["out"] += out_t
            model_totals[m]["elapsed"] = model_totals[m].get("elapsed", 0) + elapsed
            model_totals[m]["count"] += 1
        n = len(scores)
        avg_elapsed = total_elapsed / n if n else 0
        avg_in = total_in // n if n else 0
        avg_out = total_out // n if n else 0
        print(sep)
        print(f"{'ИТОГО':<10} {total:>6.2f}% {total_elapsed:>7.1f}s  {total_in:>10,} {total_out:>10,}  {'':11} {'':34}")
        print(f"{'СРЕДНЕЕ':<10} {'':>7} {avg_elapsed:>7.1f}s  {avg_in:>10,} {avg_out:>10,}  {'':11} {'':34}")
        print(sep)
        if len(model_totals) > 1:
            print(f"\n{'─' * 75}")
            print("По моделям:")
            print(f"{'─' * 75}")
            print(f"  {'Модель':<35} {'Задач':>5}  {'Вх.всего':>10}  {'Вх.ср.':>10}  {'Вых.ср.':>9}  {'с/задачу':>9}")
            print(f"  {'─' * 73}")
            for m, mt in sorted(model_totals.items()):
                m_short = m.split("/")[-1] if "/" in m else m
                cnt = mt["count"]
                avg_i = mt["in"] // cnt if cnt else 0
                avg_o = mt["out"] // cnt if cnt else 0
                avg_e = mt.get("elapsed", 0) / cnt if cnt else 0
                print(f"  {m_short:<35} {cnt:>5}  {mt['in']:>10,}  {avg_i:>10,}  {avg_o:>9,}  {avg_e:>8.1f}s")


if __name__ == "__main__":
    main()
