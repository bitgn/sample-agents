# AICODE-NOTE: Keep these wrappers aligned with the README commands so the sample
# stays trivial to run from a fresh checkout without inventing parallel workflows.

.PHONY: sync run task collect collect-runs collect-debug

sync:
	uv sync

run:
	uv run python main.py

task:
	@if [ -z "$(TASKS)" ]; then echo "usage: make task TASKS='t01 t03'"; exit 1; fi
	uv run python main.py $(TASKS)

# Collect one snapshot pass over all tasks (or a subset via TASKS='t01 t02')
collect:
	uv run python collect_tasks.py $(TASKS)

# Collect multiple passes to capture entropy variants; defaults to 10
# usage: make collect-runs RUNS=20
collect-runs:
	uv run python collect_tasks.py --runs $(or $(RUNS),10) $(TASKS)

# Quick debug: one pass over only the first N tasks (default 2)
# usage: make collect-debug N=3
collect-debug:
	uv run python collect_tasks.py --limit $(or $(N),2)
