#!/usr/bin/env python3
"""
Параллельный запуск бенчмарка PAC1 для набора моделей.
Каждая модель тестируется в отдельном git worktree с отдельной веткой.

Использование:
    python run_parallel_benchmark.py                  # все модели
    python run_parallel_benchmark.py minimax glm      # фильтр по подстроке
    python run_parallel_benchmark.py --cleanup        # удалить все worktrees
"""

import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

MODELS = [
    "minimax-m2.7:cloud",
    "qwen3.5:cloud",
    "qwen3.5:397b-cloud",
    "ministral-3:3b-cloud",
    "ministral-3:8b-cloud",
    "ministral-3:14b-cloud",
    "nemotron-3-super:cloud",
    "glm-5:cloud",
    "kimi-k2.5:cloud",
    "nemotron-3-nano:30b-cloud",
    "gpt-oss:20b-cloud",
    "gpt-oss:120b-cloud",
    "deepseek-v3.1:671b-cloud",
    "kimi-k2-thinking:cloud",
    "rnj-1:8b-cloud"
]

REPO_ROOT = Path(__file__).parent
PAC1_SRC = REPO_ROOT / "pac1-py"
WORKTREES_DIR = REPO_ROOT / "tmp" / "worktrees"
LOGS_DIR = REPO_ROOT / "tmp"

TASK_TIMEOUT_S = os.environ.get("TASK_TIMEOUT_S", "900")
# Built once; each subprocess gets a copy via fork, no per-thread dict expansion
_SUBPROCESS_ENV = {**os.environ, "TASK_TIMEOUT_S": TASK_TIMEOUT_S}


def model_to_branch(model: str) -> str:
    return re.sub(r"[:/.\s]+", "-", model).strip("-")


def run_cmd(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def ensure_worktree(branch: str, model: str) -> Path:
    wt_path = WORKTREES_DIR / branch
    if wt_path.exists():
        run_cmd(["git", "worktree", "remove", "--force", str(wt_path)], cwd=REPO_ROOT)
    run_cmd(["git", "branch", "-D", branch], cwd=REPO_ROOT)
    result = run_cmd(
        ["git", "worktree", "add", "-b", branch, str(wt_path), "HEAD"],
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"[{model}] git worktree add failed:\n{result.stderr}")
    return wt_path


def setup_pac1_env(wt_path: Path, model: str) -> None:
    pac1_wt = wt_path / "pac1-py"
    (pac1_wt / ".env").write_text(
        f"MODEL_CLASSIFIER={model}\n"
        f"MODEL_DEFAULT={model}\n"
        f"MODEL_THINK={model}\n"
        f"MODEL_LONG_CONTEXT={model}\n"
    )
    venv_src = PAC1_SRC / ".venv"
    if not (pac1_wt / ".venv").exists() and venv_src.exists():
        (pac1_wt / ".venv").symlink_to(venv_src)
    secrets_src = PAC1_SRC / ".secrets"
    if not (pac1_wt / ".secrets").exists() and secrets_src.exists():
        (pac1_wt / ".secrets").symlink_to(secrets_src)


def run_test(model: str) -> dict:
    branch = model_to_branch(model)
    result: dict = {"model": model}

    try:
        print(f"[{model}] Создаю worktree (ветка: {branch})...")
        wt_path = ensure_worktree(branch, model)
        setup_pac1_env(wt_path, model)

        pac1_wt = wt_path / "pac1-py"
        ts = time.strftime("%Y%m%d_%H%M%S")
        log_file = LOGS_DIR / f"{ts}_{branch}.log"

        print(f"[{model}] Запускаю тест → {log_file.name}")
        start = time.time()

        with open(log_file, "w", buffering=1) as lf:
            lf.write(
                f"# Модель: {model}\n"
                f"# Ветка:  {branch}\n"
                f"# Старт: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"{'=' * 70}\n\n"
            )
            proc = subprocess.run(
                ["uv", "run", "python", "main.py"],
                cwd=pac1_wt,
                stdout=lf,
                stderr=subprocess.STDOUT,
                text=True,
                env=_SUBPROCESS_ENV,
            )

        elapsed = time.time() - start
        result["elapsed"] = elapsed
        result["returncode"] = proc.returncode
        result["log"] = str(log_file)

        score_line = None
        try:
            for line in reversed(log_file.read_text(errors="replace").splitlines()):
                if line.startswith("FINAL:"):
                    score_line = line.strip()
                    m = re.search(r"([\d.]+)%", score_line)
                    if m:
                        result["score_pct"] = float(m.group(1))
                    break
        except Exception:
            pass

        status = "✓" if proc.returncode == 0 else f"✗ rc={proc.returncode}"
        print(f"[{model}] {status} | {elapsed:.0f}s | {score_line or 'нет оценки'}")

    except Exception as exc:
        result["error"] = str(exc)
        print(f"[{model}] ОШИБКА: {exc}")

    return result


def cleanup_worktrees() -> None:
    print("Удаляю worktrees...")
    if WORKTREES_DIR.exists():
        for wt in WORKTREES_DIR.iterdir():
            if wt.is_dir():
                run_cmd(["git", "worktree", "remove", "--force", str(wt)], cwd=REPO_ROOT)
                run_cmd(["git", "branch", "-D", wt.name], cwd=REPO_ROOT)
                print(f"  Удалён: {wt.name}")
        try:
            WORKTREES_DIR.rmdir()
        except OSError:
            pass
    print("Готово.")


def main() -> None:
    args = sys.argv[1:]

    if "--cleanup" in args:
        cleanup_worktrees()
        return

    models = MODELS
    if args:
        models = [m for m in MODELS if any(f in m for f in args)]
        if not models:
            print(f"Нет моделей, соответствующих фильтру: {args}")
            sys.exit(1)

    # WORKTREES_DIR.mkdir(parents=True) creates LOGS_DIR ("tmp/") as a side effect
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Запуск {len(models)} моделей параллельно")
    print(f"Worktrees: {WORKTREES_DIR}")
    print(f"Логи:      {LOGS_DIR}")
    print(f"Timeout:   {TASK_TIMEOUT_S}s на задачу")
    print("=" * 60)

    run_start = time.time()
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=len(models)) as executor:
        futures = {executor.submit(run_test, m): m for m in models}
        for future in as_completed(futures):
            model = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"model": model, "error": str(exc)})
                print(f"[{model}] Необработанная ошибка: {exc}")

    total_elapsed = time.time() - run_start

    print("\n" + "=" * 70)
    print(f"{'ИТОГИ ПАРАЛЛЕЛЬНОГО БЕНЧМАРКА':^70}")
    print("=" * 70)
    print(f"  {'Модель':<35} {'Оценка':>8}  {'Время':>7}  Статус")
    print("  " + "-" * 66)

    for r in sorted(results, key=lambda r: -r.get("score_pct", -1)):
        model = r["model"]
        if "error" in r:
            print(f"  {model:<35} {'—':>8}  {'—':>7}  ОШИБКА: {r['error']}")
        else:
            score_str = f"{r['score_pct']:.2f}%" if "score_pct" in r else "—"
            rc = r.get("returncode", "?")
            print(f"  {model:<35} {score_str:>8}  {r['elapsed']:.0f}s  {'OK' if rc == 0 else f'rc={rc}'}")

    print("=" * 70)
    completed = [r for r in results if "score_pct" in r]
    if completed:
        avg = sum(r["score_pct"] for r in completed) / len(completed)
        print(f"  Среднее по {len(completed)} моделям: {avg:.2f}%")
    print(f"  Общее время: {total_elapsed:.0f}s")
    print("=" * 70)

    print("\nЛоги:")
    for r in results:
        if "log" in r:
            print(f"  {r['model']}: {r['log']}")


if __name__ == "__main__":
    main()
