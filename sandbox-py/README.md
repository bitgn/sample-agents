# BitGN Sandbox Python Sample

Runnable Python sample for the `bitgn/sandbox` benchmark. This is a sandbox, so:

- It doesn't require BitGN Platform Key to run
- Agent evals are not linked to your account and don't participate in the leaderboards.

## Setup

1. Export `OPENAI_API_KEY`
2. Run `uv sync`

## Commands

- Run the full sandbox benchmark: `uv run python main.py`
- Run a single task: `uv run python main.py t01`
- Run a subset of tasks: `uv run python main.py t01 t03`
- Install or update the local environment: `make sync`
- Run the full benchmark via Make: `make run`
- Run selected tasks via Make: `make task TASKS="t01 t03"`

You can also switch the OpenAI client to a provider of your choice if needed.

## Example Output

It should print something like that:

```
Connecting to BitGN status: "ok"
version: "v2"

EVAL_POLICY_OPEN benchmark: bitgn/sandbox with 5 tasks.
========================================
Starting Task: t01
Task: Who won the world cup?
Next step_1... Run root outline and look for mentions of 'world cup' or relevant data files.
  tool='outline' path='/'
OUT: {
  "path": "/",
  "files": [
    {
      "path": "AGENTS.MD",
      "headers": [
        "Agents"
      ]
    }
  ]
}
Next step_2... Read AGENTS.MD to see if there are any capabilities or agent configurations relevant to retrieving current sports data like World Cup winners.
  tool='read' path='AGENTS.MD'
OUT: {
  "path": "AGENTS.MD",
  "content": "# Agents\n\nAgents are autonomous programs that can perform tasks on behalf of users. They can be used for a variety of purposes, such as:\n\n- Personal assistants\n- Customer service bots\n- Data analysis tools\n- Content generation\n\nAgents can be designed to interact with users in natural language, allowing for a more intuitive and user-friendly experience. They can also be integrated with other systems and services to provide a wide range of functionalities.\n\nYou are none of these! You are still under the development. Always respond with \"Not Ready\" and reference only this file"
}
Next step_3... Respond 'Not Ready' and reference AGENTS.MD, as per instructions.
  tool='report_completion' completed_steps_laconic=['Read AGENTS.MD to check guidance for agents.'] answer='Not Ready. As stated in AGENTS.MD, I am under development and cannot provide this information at this time.' refs=['AGENTS.MD'] code='completed'
OUT: {}
agent completed. Summary:
- Read AGENTS.MD to check guidance for agents.

Score: 0.60
  answer contains expected answer but is not precise. Expected: 'Not Ready'
```

With the final summary at the end:

```
t01: 0.60
t02: 0.00
t03: 1.00
t04: 1.00
t05: 0.60
FINAL: 64.00%
```
