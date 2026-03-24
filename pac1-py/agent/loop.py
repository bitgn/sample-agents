import json
import time

from google.protobuf.json_format import MessageToDict
from connectrpc.errors import ConnectError
from pydantic import ValidationError

from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import AnswerRequest, Outcome

from .dispatch import CLI_RED, CLI_GREEN, CLI_CLR, CLI_YELLOW, CLI_BLUE, client, dispatch
from .models import NextStep, ReportTaskCompletion
from .prephase import PrephaseResult


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
# Main agent loop
# ---------------------------------------------------------------------------

def run_loop(vm: PcmRuntimeClientSync, model: str, _task_text: str,
             pre: PrephaseResult, cfg: dict) -> None:
    log = pre.log
    preserve_prefix = pre.preserve_prefix

    max_tokens = cfg.get("max_completion_tokens", 16384)
    max_steps = 30
    _transient_kws = ("503", "502", "NoneType", "overloaded", "unavailable", "server error")

    for i in range(max_steps):
        step = f"step_{i + 1}"
        print(f"\n{CLI_BLUE}--- {step} ---{CLI_CLR} ", end="")

        # Compact log to prevent token overflow
        log = _compact_log(log, max_tool_pairs=5, preserve_prefix=preserve_prefix)

        # --- LLM call with retry (FIX-27) ---
        job = None
        elapsed_ms = 0

        use_json_object = cfg.get("use_json_object", False)

        for _attempt in range(4):
            try:
                started = time.time()
                extra_body = cfg.get("extra_body", {})

                if use_json_object:
                    # For models that generate overly verbose structured output,
                    # use json_object mode and parse manually (FIX-qwen)
                    resp = client.chat.completions.create(
                        model=model,
                        response_format={"type": "json_object"},
                        messages=log,
                        max_completion_tokens=max_tokens,
                        extra_body=extra_body if extra_body else None,
                    )
                    elapsed_ms = int((time.time() - started) * 1000)
                    raw = resp.choices[0].message.content or ""
                    try:
                        job = NextStep.model_validate_json(raw)
                    except (ValidationError, ValueError) as parse_err:
                        raise RuntimeError(f"JSON parse failed: {parse_err}") from parse_err
                else:
                    resp = client.beta.chat.completions.parse(
                        model=model,
                        response_format=NextStep,
                        messages=log,
                        max_completion_tokens=max_tokens,
                        extra_body=extra_body if extra_body else None,
                    )
                    elapsed_ms = int((time.time() - started) * 1000)
                    job = resp.choices[0].message.parsed
                break
            except Exception as e:
                _err_str = str(e)
                _is_transient = any(kw.lower() in _err_str.lower() for kw in _transient_kws)
                if _is_transient and _attempt < 3:
                    print(f"{CLI_YELLOW}[FIX-27] Transient error (attempt {_attempt + 1}): {e} — retrying in 4s{CLI_CLR}")
                    time.sleep(4)
                    continue
                print(f"{CLI_RED}LLM call error: {e}{CLI_CLR}")
                break

        if job is None and use_json_object:
            # Retry once with explicit correction hint for JSON parse failures
            print(f"{CLI_YELLOW}[retry] Adding JSON correction hint{CLI_CLR}")
            log.append({"role": "user", "content": "Your previous response was invalid JSON or missing required fields. Respond with a single valid JSON object containing: current_state, plan_remaining_steps, task_completed, function."})
            try:
                resp2 = client.chat.completions.create(
                    model=model,
                    response_format={"type": "json_object"},
                    messages=log,
                    max_completion_tokens=max_tokens,
                )
                raw2 = resp2.choices[0].message.content or ""
                job = NextStep.model_validate_json(raw2)
                elapsed_ms = 0
                log.pop()  # remove the correction hint
            except Exception:
                log.pop()  # remove the correction hint even on failure

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

        # Record what the agent decided to do (plain assistant message — avoids tool_calls
        # format which confuses some models when routing via OpenRouter)
        action_name = job.function.__class__.__name__
        action_args = job.function.model_dump_json()
        log.append({
            "role": "assistant",
            "content": f"{step_summary}\nAction: {action_name}({action_args})",
        })

        try:
            result = dispatch(vm, job.function)
            raw = json.dumps(MessageToDict(result), indent=2) if result else "{}"
            txt = _format_result(result, raw)
            # For delete/write/mkdir operations, make feedback explicit about the path
            from .models import Req_Delete, Req_Write, Req_MkDir, Req_Move
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

        # Inject result as a user message (plain format, avoids tool role issues)
        log.append({"role": "user", "content": f"Result of {action_name}: {txt}"})
