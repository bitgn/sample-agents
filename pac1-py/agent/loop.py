import json
import os
import time

from google.protobuf.json_format import MessageToDict
from connectrpc.errors import ConnectError
from pydantic import ValidationError

from pathlib import Path as _Path

from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import AnswerRequest, ListRequest, Outcome

from .dispatch import (
    CLI_RED, CLI_GREEN, CLI_CLR, CLI_YELLOW, CLI_BLUE,
    anthropic_client, ollama_client,
    is_claude_model, get_anthropic_model_id,
    dispatch,
)
from .models import NextStep, ReportTaskCompletion, Req_Delete, Req_List
from .prephase import PrephaseResult


TASK_TIMEOUT_S = 180  # 3 minutes per task

_TRANSIENT_KWS = ("503", "502", "NoneType", "overloaded", "unavailable", "server error")


# ---------------------------------------------------------------------------
# Compact tree rendering (avoids huge JSON in tool messages)
# ---------------------------------------------------------------------------

def _render_tree(node: dict, indent: int = 0) -> str:
    prefix = "  " * indent
    name = node.get("name", "?")
    is_dir = node.get("isDir", False)
    children = node.get("children", [])
    line = f"{prefix}{name}/" if is_dir else f"{prefix}{name}"
    if children:
        return line + "\n" + "\n".join(_render_tree(c, indent + 1) for c in children)
    return line


def _format_result(result, txt: str) -> str:
    """Render tree results compactly; return raw JSON for others."""
    if result is None:
        return "{}"
    d = MessageToDict(result)
    if "root" in d and isinstance(d["root"], dict):
        return "VAULT STRUCTURE:\n" + _render_tree(d["root"])
    return txt


# ---------------------------------------------------------------------------
# Log compaction (sliding window)
# ---------------------------------------------------------------------------

def _compact_log(log: list, max_tool_pairs: int = 7, preserve_prefix: list | None = None) -> list:
    """Keep preserved prefix + last N assistant/tool message pairs.
    Older pairs are replaced with a single summary message."""
    prefix_len = len(preserve_prefix) if preserve_prefix else 0
    tail = log[prefix_len:]
    max_msgs = max_tool_pairs * 2

    if len(tail) <= max_msgs:
        return log

    old = tail[:-max_msgs]
    kept = tail[-max_msgs:]

    summary_parts = []
    for msg in old:
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if content:
                summary_parts.append(f"- {content}")
    summary = "Previous steps summary:\n" + "\n".join(summary_parts[-5:])

    base = preserve_prefix if preserve_prefix is not None else log[:prefix_len]
    return list(base) + [{"role": "user", "content": summary}] + kept


# ---------------------------------------------------------------------------
# Anthropic message format conversion
# ---------------------------------------------------------------------------

def _to_anthropic_messages(log: list) -> tuple[str, list]:
    """Convert OpenAI-format log to (system_prompt, messages) for Anthropic API.
    Merges consecutive same-role messages (Anthropic requires strict alternation)."""
    system = ""
    messages = []

    for msg in log:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            system = content
            continue

        if role not in ("user", "assistant"):
            continue

        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n\n" + content
        else:
            messages.append({"role": role, "content": content})

    # Anthropic requires starting with user
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": "(start)"})

    return system, messages


# ---------------------------------------------------------------------------
# LLM call: Anthropic primary, Ollama fallback
# ---------------------------------------------------------------------------

def _call_llm(log: list, model: str, max_tokens: int, cfg: dict) -> tuple[NextStep | None, int]:
    """Call LLM: tries Anthropic SDK for Claude models, falls back to Ollama."""

    # --- Anthropic SDK ---
    if is_claude_model(model) and anthropic_client is not None:
        ant_model = get_anthropic_model_id(model)
        for attempt in range(4):
            try:
                started = time.time()
                system, messages = _to_anthropic_messages(log)
                response = anthropic_client.messages.create(
                    model=ant_model,
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                )
                elapsed_ms = int((time.time() - started) * 1000)
                raw = response.content[0].text if response.content else ""
                try:
                    return NextStep.model_validate_json(raw), elapsed_ms
                except (ValidationError, ValueError) as e:
                    raise RuntimeError(f"JSON parse failed: {e}") from e
            except Exception as e:
                err_str = str(e)
                is_transient = any(kw.lower() in err_str.lower() for kw in _TRANSIENT_KWS)
                if is_transient and attempt < 3:
                    print(f"{CLI_YELLOW}[FIX-27][Anthropic] Transient error (attempt {attempt + 1}): {e} — retrying in 4s{CLI_CLR}")
                    time.sleep(4)
                    continue
                print(f"{CLI_RED}[Anthropic] Error: {e}{CLI_CLR}")
                break

        print(f"{CLI_YELLOW}[Anthropic] Falling back to Ollama{CLI_CLR}")

    # --- Ollama fallback (OpenAI-compatible) ---
    ollama_model = cfg.get("ollama_model") or os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
    ollama_max_tokens = cfg.get("max_completion_tokens", max_tokens)

    for attempt in range(4):
        try:
            started = time.time()
            resp = ollama_client.chat.completions.create(
                model=ollama_model,
                response_format={"type": "json_object"},
                messages=log,
                max_completion_tokens=ollama_max_tokens,
            )
            elapsed_ms = int((time.time() - started) * 1000)
            raw = resp.choices[0].message.content or ""
            try:
                return NextStep.model_validate_json(raw), elapsed_ms
            except (ValidationError, ValueError) as e:
                raise RuntimeError(f"JSON parse failed: {e}") from e
        except Exception as e:
            err_str = str(e)
            is_transient = any(kw.lower() in err_str.lower() for kw in _TRANSIENT_KWS)
            if is_transient and attempt < 3:
                print(f"{CLI_YELLOW}[FIX-27][Ollama] Transient error (attempt {attempt + 1}): {e} — retrying in 4s{CLI_CLR}")
                time.sleep(4)
                continue
            print(f"{CLI_RED}[Ollama] Error: {e}{CLI_CLR}")
            break

    return None, 0


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------

def run_loop(vm: PcmRuntimeClientSync, model: str, _task_text: str,
             pre: PrephaseResult, cfg: dict) -> None:
    log = pre.log
    preserve_prefix = pre.preserve_prefix

    max_tokens = cfg.get("max_completion_tokens", 16384)
    max_steps = 30

    task_start = time.time()
    listed_dirs: set[str] = set()

    for i in range(max_steps):
        # --- Task timeout check ---
        elapsed_task = time.time() - task_start
        if elapsed_task > TASK_TIMEOUT_S:
            print(f"{CLI_RED}[TIMEOUT] Task exceeded {TASK_TIMEOUT_S}s ({elapsed_task:.0f}s elapsed), stopping{CLI_CLR}")
            try:
                vm.answer(AnswerRequest(
                    message=f"Agent timeout: task exceeded {TASK_TIMEOUT_S}s time limit",
                    outcome=Outcome.OUTCOME_ERR_INTERNAL,
                    refs=[],
                ))
            except Exception:
                pass
            break

        step = f"step_{i + 1}"
        print(f"\n{CLI_BLUE}--- {step} ---{CLI_CLR} ", end="")

        # Compact log to prevent token overflow
        log = _compact_log(log, max_tool_pairs=5, preserve_prefix=preserve_prefix)

        # --- LLM call ---
        job, elapsed_ms = _call_llm(log, model, max_tokens, cfg)

        # JSON parse retry hint (for Ollama json_object mode)
        if job is None and not is_claude_model(model):
            print(f"{CLI_YELLOW}[retry] Adding JSON correction hint{CLI_CLR}")
            log.append({"role": "user", "content": "Your previous response was invalid JSON or missing required fields. Respond with a single valid JSON object containing: current_state, plan_remaining_steps, task_completed, function."})
            job, elapsed_ms = _call_llm(log, model, max_tokens, cfg)
            log.pop()

        if job is None:
            print(f"{CLI_RED}No valid response, stopping{CLI_CLR}")
            try:
                vm.answer(AnswerRequest(
                    message="Agent failed: unable to get valid LLM response",
                    outcome=Outcome.OUTCOME_ERR_INTERNAL,
                    refs=[],
                ))
            except Exception:
                pass
            break

        step_summary = job.plan_remaining_steps[0] if job.plan_remaining_steps else "(no steps)"
        print(f"{step_summary} ({elapsed_ms} ms)\n  {job.function}")

        # Record what the agent decided to do
        action_name = job.function.__class__.__name__
        action_args = job.function.model_dump_json()
        log.append({
            "role": "assistant",
            "content": f"{step_summary}\nAction: {action_name}({action_args})",
        })

        # FIX-63: auto-list parent dir before first delete from it
        if isinstance(job.function, Req_Delete):
            parent = str(_Path(job.function.path).parent)
            if parent not in listed_dirs:
                print(f"{CLI_YELLOW}[FIX-63] Auto-listing {parent} before delete{CLI_CLR}")
                try:
                    _lr = vm.list(ListRequest(name=parent))
                    _lr_raw = json.dumps(MessageToDict(_lr), indent=2) if _lr else "{}"
                    listed_dirs.add(parent)
                    log.append({"role": "user", "content": f"[FIX-63] Directory listing of {parent} (auto):\nResult of Req_List: {_lr_raw}"})
                except Exception as _le:
                    print(f"{CLI_RED}[FIX-63] Auto-list failed: {_le}{CLI_CLR}")

        # Track listed dirs
        if isinstance(job.function, Req_List):
            listed_dirs.add(job.function.path)

        try:
            result = dispatch(vm, job.function)
            raw = json.dumps(MessageToDict(result), indent=2) if result else "{}"
            txt = _format_result(result, raw)
            from .models import Req_Write, Req_MkDir, Req_Move
            if isinstance(job.function, Req_Delete) and not txt.startswith("ERROR"):
                txt = f"DELETED: {job.function.path}"
            elif isinstance(job.function, Req_Write) and not txt.startswith("ERROR"):
                txt = f"WRITTEN: {job.function.path}"
            elif isinstance(job.function, Req_MkDir) and not txt.startswith("ERROR"):
                txt = f"CREATED DIR: {job.function.path}"
            print(f"{CLI_GREEN}OUT{CLI_CLR}: {txt[:300]}{'...' if len(txt) > 300 else ''}")
        except ConnectError as exc:
            txt = f"ERROR {exc.code}: {exc.message}"
            print(f"{CLI_RED}ERR {exc.code}: {exc.message}{CLI_CLR}")
            # FIX-73: after NOT_FOUND on read, auto-relist parent — path may have been garbled
            from .models import Req_Read
            if isinstance(job.function, Req_Read) and exc.code.name == "NOT_FOUND":
                parent = str(_Path(job.function.path.strip()).parent)
                print(f"{CLI_YELLOW}[FIX-73] Auto-relisting {parent} after read NOT_FOUND (path may be garbled){CLI_CLR}")
                try:
                    _lr = vm.list(ListRequest(name=parent))
                    _lr_raw = json.dumps(MessageToDict(_lr), indent=2) if _lr else "{}"
                    txt += f"\n[FIX-73] Check path '{job.function.path}' — verify it is correct. Listing of {parent}:\n{_lr_raw}"
                except Exception as _le:
                    print(f"{CLI_RED}[FIX-73] Auto-relist failed: {_le}{CLI_CLR}")
            # FIX-71: after NOT_FOUND on delete, auto-relist parent so model sees remaining files
            if isinstance(job.function, Req_Delete) and exc.code.name == "NOT_FOUND":
                parent = str(_Path(job.function.path).parent)
                print(f"{CLI_YELLOW}[FIX-71] Auto-relisting {parent} after NOT_FOUND{CLI_CLR}")
                try:
                    _lr = vm.list(ListRequest(name=parent))
                    _lr_raw = json.dumps(MessageToDict(_lr), indent=2) if _lr else "{}"
                    listed_dirs.add(parent)
                    txt += f"\n[FIX-71] Remaining files in {parent}:\n{_lr_raw}"
                except Exception as _le:
                    print(f"{CLI_RED}[FIX-71] Auto-relist failed: {_le}{CLI_CLR}")

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

        # Inject result as a user message
        log.append({"role": "user", "content": f"Result of {action_name}: {txt}"})
