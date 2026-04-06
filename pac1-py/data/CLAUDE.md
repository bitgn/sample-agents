# Task Snapshot Data

Snapshots are stored under `tasks/<task_id>/<hash>.json`. Multiple `.json` files per task = different environment variants captured across runs (entropy).

## JSON structure

```
{
  "task_id":               string,
  "benchmark_description": string,          // same across all tasks
  "instruction":           string,          // the task prompt given to the agent
  "captured_at":           ISO-8601 string,
  "environment": {
    "context":             object,          // simulated time, persona (small)
    "root_listing":        array,           // flat list of root dir entries (small)
    "tree":                object,          // full recursive file tree (can be large)
    "instruction_files":   object           // map of path → file content (can be large)
  }
}
```

`tree` and `instruction_files` are the large fields. Avoid loading them unless needed.

## Efficient jq queries

List all task IDs that have snapshots:
```bash
ls tasks/
```

List snapshot files for a task:
```bash
ls tasks/t01/
```

Read just the instruction (small):
```bash
jq '.instruction' tasks/t01/<hash>.json
```

Read context and root listing only (skips large fields):
```bash
jq '{instruction, context: .environment.context, root_listing: .environment.root_listing}' tasks/t01/<hash>.json
```

List which instruction files were captured (paths only, no content):
```bash
jq '.environment.instruction_files | keys' tasks/t01/<hash>.json
```

Read a specific instruction file:
```bash
jq '.environment.instruction_files["AGENTS.md"]' tasks/t01/<hash>.json
jq '.environment.instruction_files["90_memory/soul.md"]' tasks/t01/<hash>.json
```

Read the file tree (structure only, no file contents):
```bash
jq '.environment.tree' tasks/t01/<hash>.json
```

Compare instructions across all snapshots of a task to spot entropy:
```bash
jq -r '.instruction' tasks/t01/*.json | sort -u
```

Count unique variants per task:
```bash
for d in tasks/*/; do echo "$(ls "$d" | wc -l | tr -d ' ') $(basename $d)"; done | sort -rn
```
