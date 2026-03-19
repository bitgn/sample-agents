import json
import os
import time
from pathlib import Path
from typing import List, Literal, Union
from google.protobuf.json_format import MessageToDict
from openai import OpenAI
from pydantic import BaseModel, Field

from bitgn.vm.mini_connect import MiniRuntimeClientSync
from bitgn.vm.mini_pb2 import (
    AnswerRequest,
    DeleteRequest,
    ListRequest,
    OutlineRequest,
    ReadRequest,
    SearchRequest,
    WriteRequest,
)
from connectrpc.errors import ConnectError


def _load_secrets(path: str = ".secrets") -> None:
    """Load KEY=VALUE pairs from secrets file into os.environ (if not already set)."""
    secrets_file = Path(path)
    if not secrets_file.exists():
        return
    for line in secrets_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_secrets()

_OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")

if _OPENROUTER_KEY:
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=_OPENROUTER_KEY,
        default_headers={
            "HTTP-Referer": "http://localhost",
            "X-Title": "bitgn-agent",
        },
    )
else:
    client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")


class ReportTaskCompletion(BaseModel):
    tool: Literal["report_completion"]
    completed_steps_laconic: List[str]
    answer: str
    grounding_refs: List[str] = Field(default_factory=list)

    code: Literal["completed", "failed"]


class Req_Tree(BaseModel):
    tool: Literal["tree"]
    path: str = Field(..., description="folder path")


class Req_Search(BaseModel):
    tool: Literal["search"]
    pattern: str
    count: int = Field(default=5, description="number of results, 1-10")
    path: str = "/"


class Req_List(BaseModel):
    tool: Literal["list"]
    path: str


class Req_Read(BaseModel):
    tool: Literal["read"]
    path: str


class Req_Write(BaseModel):
    tool: Literal["write"]
    path: str
    content: str


class Req_Delete(BaseModel):
    tool: Literal["delete"]
    path: str


class NextStep(BaseModel):
    current_state: str
    # we'll use only the first step, discarding all the rest.
    plan_remaining_steps_brief: List[str] = Field(
        ...,
        description="1-5 brief steps explaining how to accomplish the task",
    )
    # now let's continue the cascade and check with LLM if the task is done
    task_completed: bool
    # AICODE-NOTE: Keep this union aligned with the MiniRuntime protobuf surface so
    # structured tool calling stays exhaustive as demo VM request types evolve.
    function: Union[
        ReportTaskCompletion,
        Req_Tree,
        Req_Search,
        Req_List,
        Req_Read,
        Req_Write,
        Req_Delete,
    ] = Field(..., description="execute first remaining step")


system_prompt = """
You are a personal business assistant, helpful and precise.

- always start by discovering available information by running root outline.
- always read `AGENTS.md` at the start
- always reference (ground) in final response all files that contributed to the answer
- Clearly report when tasks are done
"""


CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"


def dispatch(vm: MiniRuntimeClientSync, cmd: BaseModel):
    if isinstance(cmd, Req_Tree):
        return vm.outline(OutlineRequest(path=cmd.path))
    if isinstance(cmd, Req_Search):
        return vm.search(SearchRequest(path=cmd.path, pattern=cmd.pattern, count=cmd.count))
    if isinstance(cmd, Req_List):
        return vm.list(ListRequest(path=cmd.path))
    if isinstance(cmd, Req_Read):
        return vm.read(ReadRequest(path=cmd.path))
    if isinstance(cmd, Req_Write):
        return vm.write(WriteRequest(path=cmd.path, content=cmd.content))
    if isinstance(cmd, Req_Delete):
        return vm.delete(DeleteRequest(path=cmd.path))
    if isinstance(cmd, ReportTaskCompletion):
        return vm.answer(AnswerRequest(answer=cmd.answer, refs=cmd.grounding_refs))



    raise ValueError(f"Unknown command: {cmd}")


def run_agent(model: str, harness_url: str, task_text: str):
    vm = MiniRuntimeClientSync(harness_url)

    # log will contain conversation context for the agent within task
    log = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_text},
    ]

    # let's limit number of reasoning steps by 20, just to be safe
    for i in range(30):
        step = f"step_{i + 1}"
        print(f"Next {step}... ", end="")

        started = time.time()

        resp = client.beta.chat.completions.parse(
            model=model,
            response_format=NextStep,
            messages=log,
            max_completion_tokens=16384,
        )

        job = resp.choices[0].message.parsed

        # print next sep for debugging
        print(job.plan_remaining_steps_brief[0], f"\n  {job.function}")

        # Let's add tool request to conversation history as if OpenAI asked for it.
        # a shorter way would be to just append `job.model_dump_json()` entirely
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

        # now execute the tool by dispatching command to our handler
        txt = ""
        try:
            result = dispatch(vm, job.function)
            mappe = MessageToDict(result)
            txt = json.dumps(mappe, indent=2)
            print(f"{CLI_GREEN}OUT{CLI_CLR}: {txt}")
        except ConnectError as e:
            txt = str(e.message)
            # print to console as ascii red
            print(f"{CLI_RED}ERR {e.code}: {e.message}{CLI_CLR}")
        except Exception as e:
            txt = f"error: {e}"
            print(f"{CLI_RED}ERR: {e}{CLI_CLR}")

        # was this the completion?
        if isinstance(job.function, ReportTaskCompletion):
            print(f"{CLI_GREEN}agent {job.function.code}{CLI_CLR}. Summary:")
            for s in job.function.completed_steps_laconic:
                print(f"- {s}")

            # print answer
            print(f"\n{CLI_BLUE}AGENT ANSWER: {job.function.answer}{CLI_CLR}")
            if job.function.grounding_refs:
                for ref in job.function.grounding_refs:
                    print(f"- {CLI_BLUE}{ref}{CLI_CLR}")
            break

        # and now we add results back to the convesation history, so that agent
        # we'll be able to act on the results in the next reasoning step.
        log.append({"role": "tool", "content": txt, "tool_call_id": step})
