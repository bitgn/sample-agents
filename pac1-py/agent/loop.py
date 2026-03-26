import json
import os
import re
import time
from collections import Counter, deque

from google.protobuf.json_format import MessageToDict
from connectrpc.errors import ConnectError
from pydantic import ValidationError

from pathlib import Path as _Path

from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import AnswerRequest, ListRequest, Outcome

from .dispatch import (
    CLI_RED, CLI_GREEN, CLI_CLR, CLI_YELLOW, CLI_BLUE,
    anthropic_client, openrouter_client, ollama_client,
    is_claude_model, get_anthropic_model_id,
    dispatch,
    probe_structured_output, get_response_format,
)
from .models import NextStep, ReportTaskCompletion, Req_Delete, Req_List, Req_Read, Req_Write, Req_MkDir, Req_Move
from .prephase import PrephaseResult


TASK_TIMEOUT_S = int(os.environ.get("TASK_TIMEOUT_S", "180"))  # default 3 min, override via env

# FIX-76: copy also defined in dispatch.py for call_llm_raw(); keep both in sync
_TRANSIENT_KWS = ("503", "502", "429", "NoneType", "overloaded", "unavailable", "server error", "rate limit", "rate-limit")


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
# JSON extraction from free-form text (fallback when SO not supported)
# ---------------------------------------------------------------------------

def _extract_json_from_text(text: str) -> dict | None:
    """Extract first valid JSON object from free-form model output (already de-thought).
    Tries: ```json fenced block → bracket-matched first {…}."""
    # Try ```json ... ``` fenced block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    # Bracket-match from the first { to its balanced closing }
    start = text.find("{")
    if start != -1:
        depth = 0
        for idx in range(start, len(text)):
            if text[idx] == "{":
                depth += 1
            elif text[idx] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:idx + 1])
                    except (json.JSONDecodeError, ValueError):
                        break

    return None


# ---------------------------------------------------------------------------
# LLM call: Anthropic primary, OpenRouter/Ollama fallback
# ---------------------------------------------------------------------------

def _call_openai_tier(
    oai_client,
    model: str,
    log: list,
    max_tokens: int,
    label: str,
    extra_body: dict | None = None,
    response_format: dict | None = None,
) -> tuple[NextStep | None, int, int, int, int]:
    """Shared retry loop for OpenAI-compatible tiers (OpenRouter, Ollama).
    response_format=None means model does not support it — use text extraction fallback.
    Returns (result, elapsed_ms, input_tokens, output_tokens, thinking_tokens)."""
    for attempt in range(4):
        raw = ""
        elapsed_ms = 0
        try:
            started = time.time()
            create_kwargs: dict = dict(
                model=model,
                messages=log,
                max_completion_tokens=max_tokens,
            )
            if response_format is not None:
                create_kwargs["response_format"] = response_format
            if extra_body:
                create_kwargs["extra_body"] = extra_body
            resp = oai_client.chat.completions.create(**create_kwargs)
            elapsed_ms = int((time.time() - started) * 1000)
            raw = resp.choices[0].message.content or ""
        except Exception as e:
            err_str = str(e)
            is_transient = any(kw.lower() in err_str.lower() for kw in _TRANSIENT_KWS)
            if is_transient and attempt < 3:
                print(f"{CLI_YELLOW}[FIX-27][{label}] Transient error (attempt {attempt + 1}): {e} — retrying in 4s{CLI_CLR}")
                time.sleep(4)
                continue
            print(f"{CLI_RED}[{label}] Error: {e}{CLI_CLR}")
            break
        else:
            in_tok = getattr(getattr(resp, "usage", None), "prompt_tokens", 0)
            out_tok = getattr(getattr(resp, "usage", None), "completion_tokens", 0)
            think_match = re.search(r"<think>(.*?)</think>", raw, re.DOTALL)
            think_tok = len(think_match.group(1)) // 4 if think_match else 0
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            print(f"{CLI_YELLOW}[{label}] RAW: {raw[:500]}{CLI_CLR}")
            if response_format is not None:
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, ValueError) as e:
                    print(f"{CLI_RED}[{label}] JSON decode failed: {e}{CLI_CLR}")
                    break
            else:
                parsed = _extract_json_from_text(raw)
                if parsed is None:
                    print(f"{CLI_RED}[{label}] JSON extraction from text failed{CLI_CLR}")
                    break
                print(f"{CLI_YELLOW}[{label}] JSON extracted from free-form text{CLI_CLR}")
            # FIX-W1: auto-wrap bare function objects (model returns {"tool":...} without outer NextStep)
            if isinstance(parsed, dict) and "tool" in parsed and "current_state" not in parsed:
                print(f"{CLI_YELLOW}[FIX-W1] Auto-wrapping bare function object{CLI_CLR}")
                parsed = {
                    "current_state": "continuing",
                    "plan_remaining_steps_brief": ["execute action"],
                    "task_completed": False,
                    "function": parsed,
                }
            # FIX-W2: strip thinking-only wrapper (model returns {"reasoning":...} without NextStep fields)
            elif isinstance(parsed, dict) and "reasoning" in parsed and "current_state" not in parsed:
                print(f"{CLI_YELLOW}[FIX-W2] Stripping bare reasoning wrapper, using list action{CLI_CLR}")
                parsed = {
                    "current_state": "reasoning stripped",
                    "plan_remaining_steps_brief": ["explore vault"],
                    "task_completed": False,
                    "function": {"tool": "list", "path": "/"},
                }
            # FIX-W3: truncate plan_remaining_steps_brief to MaxLen(5)
            if isinstance(parsed, dict) and isinstance(parsed.get("plan_remaining_steps_brief"), list):
                steps = [s for s in parsed["plan_remaining_steps_brief"] if s]  # drop empty strings
                if not steps:
                    steps = ["continue"]
                parsed["plan_remaining_steps_brief"] = steps[:5]
            try:
                return NextStep.model_validate(parsed), elapsed_ms, in_tok, out_tok, think_tok
            except ValidationError as e:
                print(f"{CLI_RED}[{label}] JSON parse failed: {e}{CLI_CLR}")
                break
    return None, 0, 0, 0, 0


def _call_llm(log: list, model: str, max_tokens: int, cfg: dict) -> tuple[NextStep | None, int, int, int, int]:
    """Call LLM: Anthropic SDK (tier 1) → OpenRouter (tier 2) → Ollama (tier 3).
    Returns (result, elapsed_ms, input_tokens, output_tokens, thinking_tokens)."""

    # --- Anthropic SDK ---
    if is_claude_model(model) and anthropic_client is not None:
        ant_model = get_anthropic_model_id(model)
        thinking_budget = cfg.get("thinking_budget", 0)
        for attempt in range(4):
            raw = ""
            elapsed_ms = 0
            try:
                started = time.time()
                system, messages = _to_anthropic_messages(log)
                create_kwargs: dict = dict(
                    model=ant_model,
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                )
                if thinking_budget:
                    create_kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
                response = anthropic_client.messages.create(**create_kwargs)
                elapsed_ms = int((time.time() - started) * 1000)
                think_tok = 0
                for block in response.content:
                    if block.type == "thinking":
                        # Estimate thinking tokens (rough: chars / 4)
                        think_tok += len(getattr(block, "thinking", "")) // 4
                    elif block.type == "text":
                        raw = block.text
                in_tok = getattr(getattr(response, "usage", None), "input_tokens", 0)
                out_tok = getattr(getattr(response, "usage", None), "output_tokens", 0)
                print(f"{CLI_YELLOW}[Anthropic] tokens in={in_tok} out={out_tok} think≈{think_tok}{CLI_CLR}")
            except Exception as e:
                err_str = str(e)
                is_transient = any(kw.lower() in err_str.lower() for kw in _TRANSIENT_KWS)
                if is_transient and attempt < 3:
                    print(f"{CLI_YELLOW}[FIX-27][Anthropic] Transient error (attempt {attempt + 1}): {e} — retrying in 4s{CLI_CLR}")
                    time.sleep(4)
                    continue
                print(f"{CLI_RED}[Anthropic] Error: {e}{CLI_CLR}")
                break
            else:
                try:
                    return NextStep.model_validate_json(raw), elapsed_ms, in_tok, out_tok, think_tok
                except (ValidationError, ValueError) as e:
                    print(f"{CLI_RED}[Anthropic] JSON parse failed: {e}{CLI_CLR}")
                    return None, elapsed_ms, in_tok, out_tok, think_tok

        _next = "OpenRouter" if openrouter_client is not None else "Ollama"
        print(f"{CLI_YELLOW}[Anthropic] Falling back to {_next}{CLI_CLR}")

    # --- OpenRouter (cloud, tier 2) ---
    if openrouter_client is not None:
        # Detect structured output capability (static hint → probe → fallback)
        so_hint = cfg.get("response_format_hint")
        so_mode = probe_structured_output(openrouter_client, model, hint=so_hint)
        or_fmt = get_response_format(so_mode)  # None if mode="none"
        if so_mode == "none":
            print(f"{CLI_YELLOW}[OpenRouter] Model {model} does not support response_format — using text extraction{CLI_CLR}")
        result = _call_openai_tier(openrouter_client, model, log, cfg.get("max_completion_tokens", max_tokens), "OpenRouter", response_format=or_fmt)
        if result[0] is not None:
            return result
        print(f"{CLI_YELLOW}[OpenRouter] Falling back to Ollama{CLI_CLR}")

    # --- Ollama fallback (local, tier 3) ---
    ollama_model = cfg.get("ollama_model") or os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
    extra = {"think": cfg["ollama_think"]} if "ollama_think" in cfg else None
    return _call_openai_tier(ollama_client, ollama_model, log, cfg.get("max_completion_tokens", max_tokens), "Ollama", extra_body=extra, response_format=get_response_format("json_schema"))


# ---------------------------------------------------------------------------
# Adaptive stall detection (FIX-74)
# ---------------------------------------------------------------------------

def _check_stall(
    fingerprints: deque,
    steps_since_write: int,
    error_counts: Counter,
) -> str | None:
    """Detect stall patterns and return an adaptive, task-agnostic hint.

    Signals checked (in priority order):
    1. Last 3 action fingerprints are identical → stuck in action loop.
    2. Repeated error (same tool:path:code ≥ 2 times) → path doesn't exist.
    3. ≥ 6 steps without any write/delete/move/mkdir → stuck in exploration.
    Returns None if no stall detected."""
    # Signal 1: repeated identical action
    if len(fingerprints) >= 3 and fingerprints[-1] == fingerprints[-2] == fingerprints[-3]:
        tool_name = fingerprints[-1].split(":")[0]
        return (
            f"You have called {tool_name} with the same arguments 3 times in a row without progress. "
            "Change your approach: try a different tool, a different path, or use search/find. "
            "If the task is complete or cannot be completed, call report_completion."
        )

    # Signal 2: repeated error on same path
    for (tool_name, path, code), count in error_counts.items():
        if count >= 2:
            return (
                f"Error {code} on path '{path}' has occurred {count} times. "
                "This path does not exist or is inaccessible. "
                "List the parent directory to find the correct filename, then retry."
            )

    # Signal 3: long exploration without writing
    if steps_since_write >= 6:
        return (
            f"You have taken {steps_since_write} steps without writing, deleting, moving, or creating anything. "
            "Either take a concrete action (write/delete/move/mkdir) "
            "or call report_completion if the task is done or cannot be completed."
        )

    return None


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------

def run_loop(vm: PcmRuntimeClientSync, model: str, _task_text: str,
             pre: PrephaseResult, cfg: dict) -> dict:
    """Run main agent loop. Returns token usage stats dict."""
    log = pre.log
    preserve_prefix = pre.preserve_prefix

    max_tokens = cfg.get("max_completion_tokens", 16384)
    max_steps = 30

    task_start = time.time()
    listed_dirs: set[str] = set()
    total_in_tok = 0
    total_out_tok = 0
    total_think_tok = 0

    # FIX-74: adaptive stall detection state
    _action_fingerprints: deque = deque(maxlen=6)
    _steps_since_write: int = 0
    _error_counts: Counter = Counter()
    _stall_hint_active: bool = False

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
        job, elapsed_ms, in_tok, out_tok, think_tok = _call_llm(log, model, max_tokens, cfg)
        total_in_tok += in_tok
        total_out_tok += out_tok
        total_think_tok += think_tok

        # JSON parse retry hint (for Ollama json_object mode)
        if job is None and not is_claude_model(model):
            print(f"{CLI_YELLOW}[retry] Adding JSON correction hint{CLI_CLR}")
            log.append({"role": "user", "content": (
                'Your previous response was invalid. Respond with EXACTLY this JSON structure '
                '(all 4 fields required, correct types):\n'
                '{"current_state":"<string>","plan_remaining_steps_brief":["<string>"],'
                '"task_completed":false,"function":{"tool":"list","path":"/"}}\n'
                'RULES: current_state=string, plan_remaining_steps_brief=array of strings, '
                'task_completed=boolean (true/false not string), function=object with "tool" key inside.'
            )})
            job, elapsed_ms, in_tok, out_tok, think_tok = _call_llm(log, model, max_tokens, cfg)
            total_in_tok += in_tok
            total_out_tok += out_tok
            total_think_tok += think_tok
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

        step_summary = job.plan_remaining_steps_brief[0] if job.plan_remaining_steps_brief else "(no steps)"
        print(f"{step_summary} ({elapsed_ms} ms)\n  {job.function}")

        # Serialize once; reuse for fingerprint and log message
        action_name = job.function.__class__.__name__
        action_args = job.function.model_dump_json()

        # FIX-74: update fingerprints and check for stall before logging
        # (hint retry must use a log that doesn't yet contain this step)
        _action_fingerprints.append(f"{action_name}:{action_args}")

        _stall_hint = _check_stall(_action_fingerprints, _steps_since_write, _error_counts)
        if _stall_hint and not _stall_hint_active:
            print(f"{CLI_YELLOW}[FIX-74][STALL] Detected: {_stall_hint[:120]}{CLI_CLR}")
            log.append({"role": "user", "content": f"[STALL HINT] {_stall_hint}"})
            _stall_hint_active = True
            _job2, _, _i2, _o2, _t2 = _call_llm(log, model, max_tokens, cfg)
            log.pop()
            if _job2 is not None:
                job = _job2
                total_in_tok += _i2
                total_out_tok += _o2
                total_think_tok += _t2
                action_name = job.function.__class__.__name__
                action_args = job.function.model_dump_json()
                _action_fingerprints[-1] = f"{action_name}:{action_args}"

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

        # FIX-W4: reject wildcard delete paths early with instructive message
        if isinstance(job.function, Req_Delete) and ("*" in job.function.path):
            wc_parent = job.function.path.rstrip("/*").rstrip("/") or "/"
            print(f"{CLI_YELLOW}[FIX-W4] Wildcard delete rejected: {job.function.path}{CLI_CLR}")
            log.append({
                "role": "user",
                "content": (
                    f"ERROR: Wildcards not supported. You must delete files one by one.\n"
                    f"List '{wc_parent}' first, then delete each file individually by its exact path."
                ),
            })
            _steps_since_write += 1
            continue

        try:
            result = dispatch(vm, job.function)
            raw = json.dumps(MessageToDict(result), indent=2) if result else "{}"
            txt = _format_result(result, raw)
            if isinstance(job.function, Req_Delete) and not txt.startswith("ERROR"):
                txt = f"DELETED: {job.function.path}"
            elif isinstance(job.function, Req_Write) and not txt.startswith("ERROR"):
                txt = f"WRITTEN: {job.function.path}"
            elif isinstance(job.function, Req_MkDir) and not txt.startswith("ERROR"):
                txt = f"CREATED DIR: {job.function.path}"
            print(f"{CLI_GREEN}OUT{CLI_CLR}: {txt[:300]}{'...' if len(txt) > 300 else ''}")
            # FIX-74: reset stall state on meaningful progress
            if isinstance(job.function, (Req_Write, Req_Delete, Req_Move, Req_MkDir)):
                _steps_since_write = 0
                _stall_hint_active = False
                _error_counts.clear()
            else:
                _steps_since_write += 1
        except ConnectError as exc:
            txt = f"ERROR {exc.code}: {exc.message}"
            print(f"{CLI_RED}ERR {exc.code}: {exc.message}{CLI_CLR}")
            # FIX-74: record repeated errors for stall detection
            _err_path = getattr(job.function, "path", getattr(job.function, "from_name", "?"))
            _error_counts[(action_name, _err_path, exc.code.name)] += 1
            _stall_hint_active = False  # allow stall hint on next iteration if error repeats
            _steps_since_write += 1
            # FIX-73: after NOT_FOUND on read, auto-relist parent — path may have been garbled
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

    return {"input_tokens": total_in_tok, "output_tokens": total_out_tok, "thinking_tokens": total_think_tok}
