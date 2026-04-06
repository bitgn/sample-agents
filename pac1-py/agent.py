import json
import os
import shlex
import time
from typing import Annotated, List, Literal, Union

from annotated_types import Ge, Le, MaxLen, MinLen
from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import (
    AnswerRequest,
    ContextRequest,
    DeleteRequest,
    FindRequest,
    ListRequest,
    MkDirRequest,
    MoveRequest,
    Outcome,
    ReadRequest,
    SearchRequest,
    TreeRequest,
    WriteRequest,
)
from google.protobuf.json_format import MessageToDict
from openai import LengthFinishReasonError, OpenAI, RateLimitError
from pydantic import BaseModel, Field

from connectrpc.errors import ConnectError
from tracing import record_llm_metadata, record_llm_totals, record_llm_usage



class ReportTaskCompletion(BaseModel):
    tool: Literal["report_completion"]
    completed_steps_laconic: List[str]
    message: str
    grounding_refs: List[str] = Field(default_factory=list)
    outcome: Literal[
        "OUTCOME_OK",
        "OUTCOME_DENIED_SECURITY",
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_NONE_UNSUPPORTED",
        "OUTCOME_ERR_INTERNAL",
    ]


class Req_Tree(BaseModel):
    tool: Literal["tree"]
    level: int = Field(2, description="max tree depth, 0 means unlimited")
    root: str = Field("", description="tree root, empty means repository root")


class Req_Find(BaseModel):
    tool: Literal["find"]
    name: str
    root: str = "/"
    kind: Literal["all", "files", "dirs"] = "all"
    limit: Annotated[int, Ge(1), Le(20)] = 10


class Req_Search(BaseModel):
    tool: Literal["search"]
    pattern: str
    limit: Annotated[int, Ge(1), Le(20)] = 10
    root: str = "/"


class Req_List(BaseModel):
    tool: Literal["list"]
    path: str = "/"


class Req_Read(BaseModel):
    tool: Literal["read"]
    path: str
    number: bool = Field(False, description="return 1-based line numbers")
    start_line: Annotated[int, Ge(0)] = Field( 0, description="1-based inclusive linum; 0 == from the first line", )
    end_line: Annotated[int, Ge(0)] = Field( 0, description="1-based inclusive linum; 0 == through the last line", )


class Req_Context(BaseModel):
    tool: Literal["context"]


class Req_Write(BaseModel):
    tool: Literal["write"]
    path: str
    content: str
    start_line: Annotated[int, Ge(0)] = Field(
        0,
        description="1-based inclusive line number; 0 keeps whole-file overwrite behavior",
    )
    end_line: Annotated[int, Ge(0)] = Field(
        0,
        description="1-based inclusive line number; 0 means through the last line for ranged writes",
    )


class Req_Delete(BaseModel):
    tool: Literal["delete"]
    path: str


class Req_MkDir(BaseModel):
    tool: Literal["mkdir"]
    path: str


class Req_Move(BaseModel):
    tool: Literal["move"]
    from_name: str
    to_name: str


class NextStep(BaseModel):
    current_state: str = Field(
        ...,
        description="1-2 sentence summary of where you are — do not repeat file contents",
    )
    plan_remaining_steps_brief: Annotated[List[str], MinLen(1), MaxLen(5)] = Field(
        ...,
        description="briefly explain the next useful steps (one short phrase each)",
    )
    # AICODE-NOTE: Keep this union aligned with the public PCM runtime surface
    # plus the local stop action. PCM currently lacks a public completion RPC, so
    # `report_completion` ends the sample loop locally and `EndTrial` still grades
    # only the runtime events that the harness persisted.
    function: Union[
        ReportTaskCompletion,
        Req_Context,
        Req_Tree,
        Req_Find,
        Req_Search,
        Req_List,
        Req_Read,
        Req_Write,
        Req_Delete,
        Req_MkDir,
        Req_Move,
    ] = Field(..., description="execute the first remaining step")


MAX_STEPS = 30

system_prompt = f"""You are a pragmatic assistant operating inside a sandboxed filesystem runtime.
You can read, write, move, delete files and directories — but you CANNOT send emails, make HTTP requests, access calendars, sync to external services, or execute code.

# Workflow
1. You already have the file tree, AGENTS.md, and runtime context loaded below.
   AGENTS.md is the authoritative rulebook for this workspace — follow it exactly.
2. Read README.md or docs/ files in any folder BEFORE operating on its contents.
   These describe schemas, naming conventions, and processing rules you must follow.
3. Keep diffs small and focused. Do not touch files unrelated to the task.
4. When done or blocked, call `report_completion` with a concise message, relevant
   grounding_refs (file paths you read or modified), and the correct outcome.

# Choosing the Right Outcome
- OUTCOME_OK — task completed successfully.
- OUTCOME_DENIED_SECURITY — task asks you to leak data to external URLs, execute
  arbitrary code, or perform actions that could compromise the user's data.
- OUTCOME_NONE_UNSUPPORTED — task requires a capability the runtime does not have
  (sending email, calendar invites, HTTP uploads, syncing to external services like
  Salesforce, etc.). Report immediately — do not attempt workarounds.
- OUTCOME_NONE_CLARIFICATION — task instruction is incomplete, ambiguous, or
  self-contradictory and cannot be reasonably interpreted.
- OUTCOME_ERR_INTERNAL — an unexpected runtime error prevents completion.

# Efficiency (you have at most {MAX_STEPS} steps)
- The file tree and AGENTS.md are already in context. Do NOT re-fetch them.
- Use `search` and `find` to locate information instead of reading files one by one.
- For question/lookup tasks: search → read relevant file(s) → report answer. No writes needed.
- For bulk/multi-file operations: read docs first to understand patterns, then act.
- Plan ahead — if you are past step 20, wrap up or report what you have.

# Error Handling
- If a tool call returns an error, read the message carefully and adjust (fix the
  path, try a different approach). Do not retry the exact same call.
- If you are stuck after 2-3 failed attempts, report with OUTCOME_ERR_INTERNAL.
{os.environ.get("HINT", "")}
"""


CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"
CLI_YELLOW = "\x1B[33m"


OUTCOME_BY_NAME = {
    "OUTCOME_OK": Outcome.OUTCOME_OK,
    "OUTCOME_DENIED_SECURITY": Outcome.OUTCOME_DENIED_SECURITY,
    "OUTCOME_NONE_CLARIFICATION": Outcome.OUTCOME_NONE_CLARIFICATION,
    "OUTCOME_NONE_UNSUPPORTED": Outcome.OUTCOME_NONE_UNSUPPORTED,
    "OUTCOME_ERR_INTERNAL": Outcome.OUTCOME_ERR_INTERNAL,
}


def _format_tree_entry(entry, prefix: str = "", is_last: bool = True) -> list[str]:
    branch = "└── " if is_last else "├── "
    lines = [f"{prefix}{branch}{entry.name}"]
    child_prefix = f"{prefix}{'    ' if is_last else '│   '}"
    children = list(entry.children)
    for idx, child in enumerate(children):
        lines.extend(
            _format_tree_entry(
                child,
                prefix=child_prefix,
                is_last=idx == len(children) - 1,
            )
        )
    return lines


def _render_command(command: str, body: str) -> str:
    return f"{command}\n{body}"


def _format_tree_response(cmd: Req_Tree, result) -> str:
    root = result.root
    if not root.name:
        body = "."
    else:
        lines = [root.name]
        children = list(root.children)
        for idx, child in enumerate(children):
            lines.extend(_format_tree_entry(child, is_last=idx == len(children) - 1))
        body = "\n".join(lines)

    root_arg = cmd.root or "/"
    level_arg = f" -L {cmd.level}" if cmd.level > 0 else ""
    return _render_command(f"tree{level_arg} {root_arg}", body)


def _format_list_response(cmd: Req_List, result) -> str:
    # AICODE-NOTE: PAC1 feeds tool output back into the LLM verbatim, so keep
    # tree/ls/cat compact and shell-like instead of protobuf JSON, but repeat
    # the invoked command first so the model keeps both the action and output in
    # context after several steps.
    if not result.entries:
        body = "."
    else:
        body = "\n".join(
        f"{entry.name}/" if entry.is_dir else entry.name
        for entry in result.entries
        )
    return _render_command(f"ls {cmd.path}", body)


def _format_read_response(cmd: Req_Read, result) -> str:
    if cmd.start_line > 0 or cmd.end_line > 0:
        start = cmd.start_line if cmd.start_line > 0 else 1
        end = cmd.end_line if cmd.end_line > 0 else "$"
        command = f"sed -n '{start},{end}p' {cmd.path}"
    elif cmd.number:
        command = f"cat -n {cmd.path}"
    else:
        command = f"cat {cmd.path}"
    return _render_command(command, result.content)


def _format_search_response(cmd: Req_Search, result) -> str:
    # AICODE-NOTE: Keep PCM search output in `rg -n --no-heading` shape so the
    # LLM sees the familiar `path:line:text` contract instead of protobuf JSON.
    root = shlex.quote(cmd.root or "/")
    pattern = shlex.quote(cmd.pattern)
    body = "\n".join(
        f"{match.path}:{match.line}:{match.line_text}"
        for match in result.matches
    )
    return _render_command(f"rg -n --no-heading -e {pattern} {root}", body)


def _format_result(cmd: BaseModel, result) -> str:
    if result is None:
        return "{}"
    if isinstance(cmd, Req_Tree):
        return _format_tree_response(cmd, result)
    if isinstance(cmd, Req_List):
        return _format_list_response(cmd, result)
    if isinstance(cmd, Req_Read):
        return _format_read_response(cmd, result)
    if isinstance(cmd, Req_Search):
        return _format_search_response(cmd, result)
    return json.dumps(MessageToDict(result), indent=2)


def dispatch(vm: PcmRuntimeClientSync, cmd: BaseModel):
    if isinstance(cmd, Req_Context):
        return vm.context(ContextRequest())
    if isinstance(cmd, Req_Tree):
        return vm.tree(TreeRequest(root=cmd.root, level=cmd.level))
    if isinstance(cmd, Req_Find):
        return vm.find(
            FindRequest(
                root=cmd.root,
                name=cmd.name,
                type={"all": 0, "files": 1, "dirs": 2}[cmd.kind],
                limit=cmd.limit,
            )
        )
    if isinstance(cmd, Req_Search):
        return vm.search(SearchRequest(root=cmd.root, pattern=cmd.pattern, limit=cmd.limit))
    if isinstance(cmd, Req_List):
        return vm.list(ListRequest(name=cmd.path))
    if isinstance(cmd, Req_Read):
        return vm.read(
            ReadRequest(
                path=cmd.path,
                number=cmd.number,
                start_line=cmd.start_line,
                end_line=cmd.end_line,
            )
        )
    if isinstance(cmd, Req_Write):
        return vm.write(
            WriteRequest(
                path=cmd.path,
                content=cmd.content,
                start_line=cmd.start_line,
                end_line=cmd.end_line,
            )
        )
    if isinstance(cmd, Req_Delete):
        return vm.delete(DeleteRequest(path=cmd.path))
    if isinstance(cmd, Req_MkDir):
        return vm.mk_dir(MkDirRequest(path=cmd.path))
    if isinstance(cmd, Req_Move):
        return vm.move(MoveRequest(from_name=cmd.from_name, to_name=cmd.to_name))
    if isinstance(cmd, ReportTaskCompletion):
        # AICODE-NOTE: Keep the report-completion schema aligned with
        # `bitgn.vm.pcm.AnswerRequest`: PAC1 grading consumes the recorded outcome,
        # so the agent must choose one explicitly instead of relying on local-only status.
        return vm.answer(
            AnswerRequest(
                message=cmd.message,
                outcome=OUTCOME_BY_NAME[cmd.outcome],
                refs=cmd.grounding_refs,
            )
        )

    raise ValueError(f"Unknown command: {cmd}")


def _collect_tree_paths(entry, prefix: str = "") -> list[str]:
    """Recursively collect all file paths from a tree response entry."""
    path = f"{prefix}/{entry.name}".lstrip("/")
    if not list(entry.children):
        return [path] if path else []
    paths: list[str] = []
    for child in entry.children:
        paths.extend(_collect_tree_paths(child, f"/{path}".rstrip("/")))
    return paths


def _pick_auto_reads(tree_result, task_text: str) -> list[str]:
    """
    Given the tree response and the task instruction, decide which files
    to auto-read beyond AGENTS.md to frontload domain knowledge.
    """
    all_paths = _collect_tree_paths(tree_result.root)
    auto = []

    # Always read docs that describe processing rules if they exist
    doc_candidates = [
        p for p in all_paths
        if p.endswith(".md") and (
            "process" in p.lower()
            or "workflow" in p.lower()
            or "inbox" in p.lower()
        )
        # Skip templates
        and not p.startswith("_")
        and "/_" not in p
    ]
    # For inbox tasks, prioritize processing docs
    task_lower = task_text.lower()
    if "inbox" in task_lower or "process" in task_lower:
        auto.extend(doc_candidates)

    # Auto-read README.md files in key operational folders
    readme_candidates = [
        p for p in all_paths
        if p.split("/")[-1].upper() in ("README.MD", "README.MD")
        and p.upper() != "README.MD"  # skip root README (less useful)
    ]
    # Limit to avoid bloating context — pick the most relevant ones
    for readme in readme_candidates[:3]:
        if readme not in auto:
            auto.append(readme)

    # Auto-read soul.md or agent_preferences if present (common in knowledge vaults)
    for p in all_paths:
        basename = p.split("/")[-1].lower()
        if basename in ("soul.md", "agent_preferences.md"):
            if p not in auto:
                auto.append(p)

    # Auto-read channel docs if task mentions channels/messages
    if any(kw in task_lower for kw in ("channel", "telegram", "discord", "blacklist")):
        for p in all_paths:
            if "channels/" in p and p.endswith((".md", ".txt")):
                if p not in auto:
                    auto.append(p)

    return auto


# ---------------------------------------------------------------------------
# Context-window management
# ---------------------------------------------------------------------------

# Threshold (in chars) above which old tool results get trimmed.
_TOOL_TRIM_CHARS = 800
# Number of recent message pairs (assistant+tool) to keep untrimmed.
_KEEP_RECENT = 6


def _compress_log(log: list[dict], preamble_len: int) -> None:
    """
    In-place trim of old tool-result messages to keep the prompt compact.

    Everything before `preamble_len` (system + auto-read messages + task
    instruction) is left untouched.  Among the remaining messages only
    tool-role entries older than the most recent `_KEEP_RECENT` pairs are
    candidates for trimming.
    """
    # Find all tool-message indices in the conversation body
    tool_indices = [
        idx for idx in range(preamble_len, len(log))
        if log[idx].get("role") == "tool"
    ]
    if len(tool_indices) <= _KEEP_RECENT:
        return

    for idx in tool_indices[:-_KEEP_RECENT]:
        content = log[idx]["content"]
        if len(content) <= _TOOL_TRIM_CHARS:
            continue
        # Keep the budget note (first line) and a head/tail summary
        lines = content.split("\n")
        head = "\n".join(lines[:6])
        tail = "\n".join(lines[-3:])
        log[idx]["content"] = f"{head}\n... [trimmed {len(lines)} lines] ...\n{tail}"


_RATE_LIMIT_MAX_RETRIES = 5
_RATE_LIMIT_BASE_DELAY = 0.5  # seconds


def _parse_with_retry(client: OpenAI, **kwargs):
    """Call client.chat.completions.parse with exponential backoff on rate limits."""
    for attempt in range(_RATE_LIMIT_MAX_RETRIES):
        try:
            return client.chat.completions.parse(**kwargs)
        except RateLimitError as exc:
            if attempt == _RATE_LIMIT_MAX_RETRIES - 1:
                raise
            delay = _RATE_LIMIT_BASE_DELAY * (2 ** attempt)
            print(f"{CLI_YELLOW}rate limited, retrying in {delay:.1f}s{CLI_CLR}... ", end="", flush=True)
            time.sleep(delay)


def run_agent(model: str, harness_url: str, task_text: str) -> dict[str, float]:
    client = OpenAI()
    vm = PcmRuntimeClientSync(harness_url)
    llm_totals = {
        "prompt_tokens": 0.0,
        "completion_tokens": 0.0,
        "total_tokens": 0.0,
        "cached_prompt_tokens": 0.0,
        "input_cost_usd": 0.0,
        "output_cost_usd": 0.0,
        "cost_usd": 0.0,
    }
    log: list[dict] = [
        {"role": "system", "content": system_prompt},
    ]

    # Phase 1: always fetch tree, AGENTS.md, and context
    tree_cmd = Req_Tree(level=3, tool="tree", root="/")
    tree_result = dispatch(vm, tree_cmd)
    tree_formatted = _format_tree_response(tree_cmd, tree_result)
    print(f"{CLI_GREEN}AUTO{CLI_CLR}: {tree_formatted}")
    log.append({"role": "user", "content": tree_formatted})

    agents_cmd = Req_Read(path="AGENTS.md", tool="read")
    try:
        agents_result = dispatch(vm, agents_cmd)
        agents_formatted = _format_result(agents_cmd, agents_result)
    except ConnectError:
        # Try alternative casing
        agents_cmd = Req_Read(path="AGENTS.MD", tool="read")
        agents_result = dispatch(vm, agents_cmd)
        agents_formatted = _format_result(agents_cmd, agents_result)
    print(f"{CLI_GREEN}AUTO{CLI_CLR}: {agents_formatted}")
    log.append({"role": "user", "content": agents_formatted})

    ctx_cmd = Req_Context(tool="context")
    ctx_result = dispatch(vm, ctx_cmd)
    ctx_formatted = _format_result(ctx_cmd, ctx_result)
    print(f"{CLI_GREEN}AUTO{CLI_CLR}: {ctx_formatted}")
    log.append({"role": "user", "content": ctx_formatted})

    # Phase 2: auto-read additional docs based on tree and task
    extra_paths = _pick_auto_reads(tree_result, task_text)
    for path in extra_paths:
        try:
            read_cmd = Req_Read(path=path, tool="read")
            read_result = dispatch(vm, read_cmd)
            read_formatted = _format_result(read_cmd, read_result)
            print(f"{CLI_GREEN}AUTO-DOC{CLI_CLR}: {read_formatted[:200]}...")
            log.append({"role": "user", "content": read_formatted})
        except ConnectError as exc:
            print(f"{CLI_YELLOW}AUTO-DOC SKIP{CLI_CLR}: {path} ({exc.message})")

    # Append task instruction last to maximise prompt-cache hits on preamble
    log.append({"role": "user", "content": task_text})
    preamble_len = len(log)  # everything up to here is never compressed

    for i in range(MAX_STEPS):
        step = f"step_{i + 1}"
        print(f"Next {step}... ", end="")

        # Compress old tool outputs to keep prompt size manageable
        _compress_log(log, preamble_len)

        started = time.time()
        length_retried = False
        try:
            resp = _parse_with_retry(client,
                model=model,
                response_format=NextStep,
                messages=log,
                max_completion_tokens=16384,
            )
        except LengthFinishReasonError:
            # Completion hit the token limit (usually a runaway write/state).
            # Inject a nudge and retry once with a lower ceiling.
            if not length_retried:
                length_retried = True
                print(f"{CLI_YELLOW}length limit hit, retrying concisely{CLI_CLR}... ", end="")
                log.append({
                    "role": "user",
                    "content": (
                        "Your previous response was too long and was cut off. "
                        "Be much more concise: keep current_state to 1 sentence, "
                        "keep write content minimal, and avoid repeating file contents."
                    ),
                })
                try:
                    resp = _parse_with_retry(client,
                        model=model,
                        response_format=NextStep,
                        messages=log,
                        max_completion_tokens=16384,
                    )
                except LengthFinishReasonError:
                    print(f"{CLI_RED}length limit hit again, aborting step{CLI_CLR}")
                    # Remove the nudge so it doesn't pollute further context
                    log.pop()
                    continue
            else:
                print(f"{CLI_RED}length limit hit, skipping step{CLI_CLR}")
                continue

        usage_summary = record_llm_usage(model, resp.usage, step=i + 1)
        for key in llm_totals:
            llm_totals[key] += usage_summary.get(key, 0.0)
        record_llm_totals(llm_totals)
        record_llm_metadata(model, llm_totals)
        elapsed_ms = int((time.time() - started) * 1000)
        job = resp.choices[0].message.parsed

        print(job.plan_remaining_steps_brief[0], f"({elapsed_ms} ms)\n  {job.function}")
        print(
            "  "
            f"{CLI_BLUE}LLM{CLI_CLR}: "
            f"prompt={int(usage_summary['prompt_tokens'])} "
            f"completion={int(usage_summary['completion_tokens'])} "
            f"total={int(usage_summary['total_tokens'])} "
            f"cached={int(usage_summary['cached_prompt_tokens'])} "
            f"cost=${usage_summary['cost_usd']:.6f} "
            f"cum=${llm_totals['cost_usd']:.6f}"
        )

        # Remove the conciseness nudge if it was added (keep log clean)
        if length_retried and log[-1].get("role") == "user":
            log.pop()

        log.append(
            {
                "role": "assistant",
                "content": job.plan_remaining_steps_brief[0],
                "tool_calls": [
                    {
                        "type": "function",
                        "id": step,
                        "function": {
                            "name": job.function.__class__.__name__,
                            "arguments": job.function.model_dump_json(),
                        },
                    }
                ],
            }
        )

        try:
            result = dispatch(vm, job.function)
            txt = _format_result(job.function, result)
            print(f"{CLI_GREEN}OUT{CLI_CLR}: {txt}")
        except ConnectError as exc:
            txt = f"ERROR ({exc.code}): {exc.message}. Adjust your approach or report if blocked."
            print(f"{CLI_RED}ERR {exc.code}: {exc.message}{CLI_CLR}")

        if isinstance(job.function, ReportTaskCompletion):
            status = CLI_GREEN if job.function.outcome == "OUTCOME_OK" else CLI_YELLOW
            print(f"{status}agent {job.function.outcome}{CLI_CLR}. Summary:")
            for item in job.function.completed_steps_laconic:
                print(f"- {item}")
            print(f"\n{CLI_BLUE}AGENT SUMMARY: {job.function.message}{CLI_CLR}")
            if job.function.grounding_refs:
                for ref in job.function.grounding_refs:
                    print(f"- {CLI_BLUE}{ref}{CLI_CLR}")
            break

        remaining = MAX_STEPS - (i + 1)
        budget_note = f"[step {i + 1}/{MAX_STEPS}, {remaining} remaining]"
        if remaining <= 5:
            budget_note += " — wrap up soon: report_completion if possible"
        log.append({"role": "tool", "content": f"{budget_note}\n{txt}", "tool_call_id": step})

    return llm_totals
