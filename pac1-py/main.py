import datetime
import json
import os
import re
import sys
import textwrap
import time
import zoneinfo
from pathlib import Path


# ---------------------------------------------------------------------------
# FIX-110: LOG_LEVEL env + auto-tee stdout → logs/{ts}_{model}.log
# Must be set up before agent/dispatch imports (they print at import time).
# ---------------------------------------------------------------------------

def _setup_log_tee() -> None:
    """Tee stdout to logs/{ts}_{model}.log. ANSI codes are stripped in file."""
    # Read MODEL_DEFAULT and LOG_LEVEL from env or .env file (no import side-effects yet)
    _env_path = Path(__file__).parent / ".env"
    _dotenv: dict[str, str] = {}
    try:
        for _line in _env_path.read_text().splitlines():
            _s = _line.strip()
            if _s and not _s.startswith("#") and "=" in _s:
                _k, _, _v = _s.partition("=")
                _dotenv[_k.strip()] = _v.strip()
    except Exception:
        pass

    model = os.getenv("MODEL_DEFAULT") or _dotenv.get("MODEL_DEFAULT") or "unknown"
    log_level = (os.getenv("LOG_LEVEL") or _dotenv.get("LOG_LEVEL") or "INFO").upper()

    logs_dir = Path(__file__).parent / "logs"
    logs_dir.mkdir(exist_ok=True)

    _tz_name = os.environ.get("TZ", "")
    try:
        _tz = zoneinfo.ZoneInfo(_tz_name) if _tz_name else None
    except Exception:
        _tz = None
    _now = datetime.datetime.now(tz=_tz) if _tz else datetime.datetime.now()
    _safe = model.replace("/", "-").replace(":", "-")
    log_path = logs_dir / f"{_now.strftime('%Y%m%d_%H%M%S')}_{_safe}.log"

    _fh = open(log_path, "w", buffering=1, encoding="utf-8")
    _ansi = re.compile(r"\x1B\[[0-9;]*[A-Za-z]")
    _orig = sys.stdout

    class _Tee:
        def write(self, data: str) -> None:
            _orig.write(data)
            _fh.write(_ansi.sub("", data))

        def flush(self) -> None:
            _orig.flush()
            _fh.flush()

        def isatty(self) -> bool:
            return _orig.isatty()

        @property
        def encoding(self) -> str:
            return _orig.encoding

    sys.stdout = _Tee()
    print(f"[LOG] {log_path}  (LOG_LEVEL={log_level})")


LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()  # re-exported for external use
_setup_log_tee()


from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import EndTrialRequest, EvalPolicy, GetBenchmarkRequest, StartPlaygroundRequest, StatusRequest
from connectrpc.errors import ConnectError

from agent import run_agent
from agent.classifier import ModelRouter

BITGN_URL = os.getenv("BENCHMARK_HOST") or "https://api.bitgn.com"
BENCHMARK_ID = os.getenv("BENCHMARK_ID") or "bitgn/pac1-dev"

_MODELS_JSON = Path(__file__).parent / "models.json"
_raw = json.loads(_MODELS_JSON.read_text())
_profiles: dict[str, dict] = _raw.get("_profiles", {})  # FIX-119: named parameter profiles
MODEL_CONFIGS: dict[str, dict] = {k: v for k, v in _raw.items() if not k.startswith("_")}
# FIX-119: resolve profile name references in ollama_options fields (string → dict)
for _cfg in MODEL_CONFIGS.values():
    for _fname in ("ollama_options", "ollama_options_think", "ollama_options_longContext", "ollama_options_classifier", "ollama_options_coder"):
        if isinstance(_cfg.get(_fname), str):
            _cfg[_fname] = _profiles.get(_cfg[_fname], {})

# FIX-91: все типы задаются явно — MODEL_ID как fallback упразднён.
# Каждая переменная обязательна; если не задана — ValueError при старте.
def _require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise ValueError(f"Env var {name} is required but not set. Check .env or environment.")
    return v

_model_classifier = _require_env("MODEL_CLASSIFIER")
_model_default    = _require_env("MODEL_DEFAULT")
_model_think      = _require_env("MODEL_THINK")
_model_long_ctx   = _require_env("MODEL_LONG_CONTEXT")

# Unit 8: optional per-type overrides (fall back to default/think if not set)
_model_email   = os.getenv("MODEL_EMAIL")   or _model_default
_model_lookup  = os.getenv("MODEL_LOOKUP")  or _model_default
_model_inbox   = os.getenv("MODEL_INBOX")   or _model_think
_model_coder   = os.getenv("MODEL_CODER")   or _model_default

# FIX-88: always use ModelRouter — classification runs for every task,
# logs always show [MODEL_ROUTER] lines, stats always show Тип/Модель columns.
EFFECTIVE_MODEL: ModelRouter = ModelRouter(
    default=_model_default,
    think=_model_think,
    long_context=_model_long_ctx,
    classifier=_model_classifier,
    email=_model_email,
    lookup=_model_lookup,
    inbox=_model_inbox,
    coder=_model_coder,
    configs=MODEL_CONFIGS,
)
print(
    f"[MODEL_ROUTER] Multi-model mode:\n"
    f"  classifier  = {_model_classifier}\n"
    f"  default     = {_model_default}\n"
    f"  think       = {_model_think}\n"
    f"  longContext = {_model_long_ctx}\n"
    f"  email       = {_model_email}\n"
    f"  lookup      = {_model_lookup}\n"
    f"  inbox       = {_model_inbox}\n"
    f"  coder       = {_model_coder}"
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
                token_stats = run_agent(EFFECTIVE_MODEL, trial.harness_url, trial.instruction)
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
        W = 166
        sep = "=" * W
        print(f"\n{sep}")
        _title = "ИТОГОВАЯ СТАТИСТИКА"
        print(f"{_title:^{W}}")
        print(sep)
        print(f"{'Задание':<10} {'Оценка':>7} {'Время':>8}  {'Шаги':>5} {'Запр':>5}  {'Вход(tok)':>10} {'Выход(tok)':>10} {'ток/с':>7}  {'Тип':<11} {'Модель':<34}  Проблемы")
        print("-" * W)
        model_totals: dict[str, dict] = {}
        total_llm_ms = 0
        total_steps = 0
        total_calls = 0
        for task_id, score, detail, elapsed, ts in scores:
            issues = "; ".join(detail) if score < 1.0 else "—"
            in_t = ts.get("input_tokens", 0)
            out_t = ts.get("output_tokens", 0)
            llm_ms = ts.get("llm_elapsed_ms", 0)
            ev_c   = ts.get("ollama_eval_count", 0)
            ev_ms  = ts.get("ollama_eval_ms", 0)
            steps  = ts.get("step_count", 0)
            calls  = ts.get("llm_call_count", 0)
            # Prefer Ollama-native gen metrics (accurate); fall back to wall-clock
            if ev_c > 0 and ev_ms > 0:
                tps = ev_c / (ev_ms / 1000.0)
            elif llm_ms > 0:
                tps = out_t / (llm_ms / 1000.0)
            else:
                tps = 0.0
            total_llm_ms += llm_ms
            total_steps += steps
            total_calls += calls
            m = ts.get("model_used", "—")
            m_short = m.split("/")[-1] if "/" in m else m
            t_type = ts.get("task_type", "—")
            print(f"{task_id:<10} {score:>7.2f} {elapsed:>7.1f}s  {steps:>5} {calls:>5}  {in_t:>10,} {out_t:>10,} {tps:>6.0f}  {t_type:<11} {m_short:<34}  {issues}")
            if m not in model_totals:
                model_totals[m] = {"in": 0, "out": 0, "llm_ms": 0, "ev_c": 0, "ev_ms": 0, "count": 0}
            model_totals[m]["in"] += in_t
            model_totals[m]["out"] += out_t
            model_totals[m]["llm_ms"] = model_totals[m].get("llm_ms", 0) + llm_ms
            model_totals[m]["ev_c"]  = model_totals[m].get("ev_c", 0) + ev_c
            model_totals[m]["ev_ms"] = model_totals[m].get("ev_ms", 0) + ev_ms
            model_totals[m]["elapsed"] = model_totals[m].get("elapsed", 0) + elapsed
            model_totals[m]["count"] += 1
        n = len(scores)
        avg_elapsed = total_elapsed / n if n else 0
        avg_in = total_in // n if n else 0
        avg_out = total_out // n if n else 0
        avg_steps = total_steps // n if n else 0
        avg_calls = total_calls // n if n else 0
        total_ev_c  = sum(ts.get("ollama_eval_count", 0) for *_, ts in scores)
        total_ev_ms = sum(ts.get("ollama_eval_ms", 0)    for *_, ts in scores)
        if total_ev_c > 0 and total_ev_ms > 0:
            total_tps = total_ev_c / (total_ev_ms / 1000.0)
        elif total_llm_ms > 0:
            total_tps = total_out / (total_llm_ms / 1000.0)
        else:
            total_tps = 0.0
        print(sep)
        print(f"{'ИТОГО':<10} {total:>6.2f}% {total_elapsed:>7.1f}s  {total_steps:>5} {total_calls:>5}  {total_in:>10,} {total_out:>10,} {total_tps:>6.0f}  {'':11} {'':34}")
        print(f"{'СРЕДНЕЕ':<10} {'':>7} {avg_elapsed:>7.1f}s  {avg_steps:>5} {avg_calls:>5}  {avg_in:>10,} {avg_out:>10,} {'':>6}  {'':11} {'':34}")
        print(sep)
        if len(model_totals) > 1:
            print(f"\n{'─' * 84}")
            print("По моделям:")
            print(f"{'─' * 84}")
            print(f"  {'Модель':<35} {'Задач':>5}  {'Вх.всего':>10}  {'Вх.ср.':>10}  {'Вых.ср.':>9}  {'с/задачу':>9}  {'ток/с':>7}")
            print(f"  {'─' * 82}")
            for m, mt in sorted(model_totals.items()):
                m_short = m.split("/")[-1] if "/" in m else m
                cnt = mt["count"]
                avg_i = mt["in"] // cnt if cnt else 0
                avg_o = mt["out"] // cnt if cnt else 0
                avg_e = mt.get("elapsed", 0) / cnt if cnt else 0
                m_ev_c  = mt.get("ev_c", 0)
                m_ev_ms = mt.get("ev_ms", 0)
                m_llm_ms = mt.get("llm_ms", 0)
                if m_ev_c > 0 and m_ev_ms > 0:
                    m_tps = m_ev_c / (m_ev_ms / 1000.0)
                elif m_llm_ms > 0:
                    m_tps = mt["out"] / (m_llm_ms / 1000.0)
                else:
                    m_tps = 0.0
                print(f"  {m_short:<35} {cnt:>5}  {mt['in']:>10,}  {avg_i:>10,}  {avg_o:>9,}  {avg_e:>8.1f}s  {m_tps:>6.0f}")


if __name__ == "__main__":
    main()
