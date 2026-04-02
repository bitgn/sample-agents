import os
import re
import time
from pathlib import Path

import anthropic
from openai import OpenAI
from pydantic import BaseModel

from google.protobuf.json_format import MessageToDict

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

from .models import (
    ReportTaskCompletion,
    Req_CodeEval,
    Req_Context,
    Req_Delete,
    Req_Find,
    Req_List,
    Req_MkDir,
    Req_Move,
    Req_Read,
    Req_Search,
    Req_Tree,
    Req_Write,
)


# ---------------------------------------------------------------------------
# code_eval sandbox
# ---------------------------------------------------------------------------

_SAFE_BUILTINS = {
    k: (
        __builtins__[k]
        if isinstance(__builtins__, dict)
        else getattr(__builtins__, k, None)
    )
    for k in (
        "len", "sorted", "reversed", "max", "min", "sum", "abs", "round",
        "filter", "map", "zip", "enumerate", "range",
        "list", "dict", "set", "tuple", "str", "int", "float", "bool",
        "isinstance", "hasattr", "print", "repr", "type",
    )
    if (
        __builtins__[k]
        if isinstance(__builtins__, dict)
        else getattr(__builtins__, k, None)
    ) is not None
}


def _execute_code_safe(code: str, context_vars: dict, timeout_s: int = 5) -> str:
    """Run model-generated Python 3 code in a restricted sandbox.

    Allowed modules: datetime, json, re, math.
    Allowed builtins: see _SAFE_BUILTINS (no os, sys, subprocess, open).
    Timeout: SIGALRM (5 s default). Returns stdout output or error string.
    """
    import signal
    import io
    import datetime as _dt
    import json as _json
    import re as _re
    import math as _math
    import sys as _sys

    safe_globals: dict = {
        "__builtins__": _SAFE_BUILTINS,
        "datetime": _dt,
        "json": _json,
        "re": _re,
        "math": _math,
    }
    safe_globals.update(context_vars)
    buf = io.StringIO()

    def _alarm(_sig, _frame):
        raise TimeoutError("code_eval timeout")

    old_handler = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(timeout_s)
    old_stdout = _sys.stdout
    try:
        _sys.stdout = buf
        exec(compile(code, "<code_eval>", "exec"), safe_globals)
        return buf.getvalue().strip() or "(ok, no output)"
    except TimeoutError as e:
        return f"[error] {e}"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"
    finally:
        _sys.stdout = old_stdout
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


# ---------------------------------------------------------------------------
# FIX-163: Coder sub-model helpers
# ---------------------------------------------------------------------------

def _extract_code_block(text: str) -> str:
    """Strip markdown fences; return bare Python code."""
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


_CODER_TIMEOUT_S = 45  # FIX-164: hard cap on coder model call to prevent loop starvation


def _call_coder_model(task: str, context_vars: dict, coder_model: str, coder_cfg: dict) -> str:
    """Call MODEL_CODER with minimal context to generate Python 3 code for task.
    Only passes task description and available variable names — no main-loop history.
    Hard timeout: _CODER_TIMEOUT_S seconds (FIX-164)."""
    import signal as _signal

    system = (
        "You are a Python 3 code generator. Output ONLY runnable Python code — "
        "no markdown fences, no explanation.\n"
        "Rules:\n"
        "- Modules datetime/json/re/math are pre-loaded — use directly, NO import statements\n"
        "- context_vars are injected as local variables — access by name (e.g. print(len(data)))\n"
        "- Print the final answer with print()\n"
        "Example task: 'count entries in list'\n"
        "Example context_vars keys: ['data']\n"
        "Example output: print(len(data))"
    )
    user_msg = f"Task: {task}\nAvailable variables: {list(context_vars.keys())}"

    def _coder_timeout(_sig, _frame):
        raise TimeoutError(f"coder model timed out after {_CODER_TIMEOUT_S}s")

    # FIX-177: reject oversized context_vars before calling coder model
    _ctx_total = sum(len(str(v)) for v in context_vars.values())
    if _ctx_total > 2000:
        return f"[code_eval rejected] context_vars too large ({_ctx_total} chars). Use 'paths' field for vault files instead of embedding content in context_vars."

    old_handler = _signal.signal(_signal.SIGALRM, _coder_timeout)
    _signal.alarm(_CODER_TIMEOUT_S)
    try:
        raw = call_llm_raw(
            system=system,
            user_msg=user_msg,
            model=coder_model,
            cfg=coder_cfg,
            max_tokens=256,  # FIX-164: short code only — was 512
            think=False,
            max_retries=1,   # FIX-164: 1 retry max — was 2 (3 attempts × slow model = starvation)
            plain_text=True,  # FIX-181: coder must output Python, not JSON
        )
        return _extract_code_block(raw or "print('[coder] empty response')")
    except TimeoutError as _te:
        print(f"\033[33m[coder] {_te} — returning error stub\033[0m")
        return "print('[error] coder model timeout')"
    finally:
        _signal.alarm(0)
        _signal.signal(_signal.SIGALRM, old_handler)


# ---------------------------------------------------------------------------
# Secrets loader
# ---------------------------------------------------------------------------

def _load_secrets(path: str = ".secrets") -> None:
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


_load_secrets(".env")   # model names (no credentials) — loads first; .secrets and real env vars override
_load_secrets()         # credentials (.secrets)


# ---------------------------------------------------------------------------
# LLM clients
# ---------------------------------------------------------------------------

_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
_OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
_OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")

# Primary: Anthropic SDK for Claude models
anthropic_client: anthropic.Anthropic | None = (
    anthropic.Anthropic(api_key=_ANTHROPIC_KEY) if _ANTHROPIC_KEY else None
)

# Tier 2: OpenRouter (Claude + open models via cloud)
openrouter_client: OpenAI | None = (
    OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=_OPENROUTER_KEY,
        default_headers={
            "HTTP-Referer": "http://localhost",
            "X-Title": "bitgn-agent",
        },
    )
    if _OPENROUTER_KEY
    else None
)

# Tier 3: Ollama via OpenAI-compatible API (local fallback)
ollama_client = OpenAI(base_url=_OLLAMA_URL, api_key="ollama")

_active = "anthropic" if _ANTHROPIC_KEY else ("openrouter" if _OPENROUTER_KEY else "ollama")
print(f"[dispatch] Active backend: {_active} (anthropic={'✓' if _ANTHROPIC_KEY else '✗'}, openrouter={'✓' if _OPENROUTER_KEY else '✗'}, ollama=✓)")


# ---------------------------------------------------------------------------
# Model capability detection
# ---------------------------------------------------------------------------

# Static capability hints: model name substring → response_format mode
# Checked in order; first match wins. Values: "json_object" | "json_schema" | "none"
_STATIC_HINTS: dict[str, str] = {
    "anthropic/claude": "json_object",
    "qwen/qwen":        "json_object",
    "meta-llama/":      "json_object",
    "mistralai/":       "json_object",
    "google/gemma":     "json_object",
    "google/gemini":    "json_object",
    "deepseek/":        "json_object",
    "openai/gpt":       "json_object",
    "gpt-4":            "json_object",
    "gpt-3.5":          "json_object",
    "perplexity/":      "none",
}

# Cached NextStep JSON schema (computed once; used for json_schema response_format)
def _nextstep_json_schema() -> dict:
    from .models import NextStep
    return NextStep.model_json_schema()

_NEXTSTEP_SCHEMA: dict | None = None

# Runtime cache: model name → detected format mode
_CAPABILITY_CACHE: dict[str, str] = {}


def _get_static_hint(model: str) -> str | None:
    m = model.lower()
    for substring, fmt in _STATIC_HINTS.items():
        if substring in m:
            return fmt
    return None


def probe_structured_output(client: OpenAI, model: str, hint: str | None = None) -> str:
    """Detect if model supports response_format. Returns 'json_object' or 'none'.
    Checks hint → static table → runtime probe (cached per model name)."""
    if model in _CAPABILITY_CACHE:
        return _CAPABILITY_CACHE[model]

    mode = hint or _get_static_hint(model)
    if mode is not None:
        _CAPABILITY_CACHE[model] = mode
        print(f"[capability] {model}: {mode} (static hint)")
        return mode

    print(f"[capability] Probing {model} for structured output support...")
    try:
        client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": 'Reply with valid JSON: {"ok": true}'}],
            max_completion_tokens=20,
        )
        mode = "json_object"
    except Exception as e:
        err = str(e).lower()
        if any(kw in err for kw in ("response_format", "unsupported", "not supported", "invalid_request")):
            mode = "none"
        else:
            mode = "json_object"  # transient error — assume supported
    _CAPABILITY_CACHE[model] = mode
    print(f"[capability] {model}: {mode} (probed)")
    return mode


def get_response_format(mode: str) -> dict | None:
    """Build response_format dict for the given mode, or None if mode='none'."""
    global _NEXTSTEP_SCHEMA
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "json_schema":
        if _NEXTSTEP_SCHEMA is None:
            _NEXTSTEP_SCHEMA = _nextstep_json_schema()
        return {"type": "json_schema", "json_schema": {"name": "NextStep", "strict": True, "schema": _NEXTSTEP_SCHEMA}}
    return None


# ---------------------------------------------------------------------------
# Lightweight raw LLM call (used by classify_task_llm in classifier.py)
# ---------------------------------------------------------------------------

# Transient error keywords — single source of truth; imported by loop.py
TRANSIENT_KWS = (
    "503", "502", "429", "NoneType", "overloaded",
    "unavailable", "server error", "rate limit", "rate-limit",
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()  # DEBUG → log think blocks


def is_ollama_model(model: str) -> bool:
    """True for Ollama-format models (name:tag, no slash).
    Examples: qwen3.5:9b, deepseek-v3.1:671b-cloud, qwen3.5:cloud.
    These must be routed directly to Ollama tier, skipping OpenRouter."""
    return ":" in model and "/" not in model


def call_llm_raw(
    system: str,
    user_msg: str,
    model: str,
    cfg: dict,
    max_tokens: int = 20,
    think: bool | None = None,  # None=use cfg, False=disable, True=enable
    max_retries: int = 3,  # classifier passes 0 → 1 attempt, no retries
    plain_text: bool = False,  # FIX-181: skip response_format (for code generation, not JSON)
) -> str | None:
    """Lightweight LLM call with 3-tier routing and transient-error retry.
    Returns raw text (think blocks stripped), or None if all tiers fail.
    Used by classify_task_llm(); caller handles JSON parsing and fallback.
    max_retries controls retry count per tier (0 = 1 attempt only).
    plain_text=True skips response_format constraints (use for code generation)."""

    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    # --- Tier 1: Anthropic SDK ---
    if is_claude_model(model) and anthropic_client is not None:
        ant_model = get_anthropic_model_id(model)
        for attempt in range(max_retries + 1):
            try:
                resp = anthropic_client.messages.create(
                    model=ant_model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user_msg}],
                )
                # Iterate blocks — take first type="text" (skip thinking blocks)
                for block in resp.content:
                    if getattr(block, "type", None) == "text" and block.text.strip():
                        return block.text.strip()
                if attempt < max_retries:
                    print(f"[Anthropic] Empty response (attempt {attempt + 1}) — retrying")
                    continue
                print("[Anthropic] Empty after all retries — falling through to next tier")
                break  # do not return "" — let next tier try
            except Exception as e:
                if any(kw.lower() in str(e).lower() for kw in TRANSIENT_KWS) and attempt < max_retries:
                    print(f"[Anthropic] Transient (attempt {attempt + 1}): {e} — retrying in 4s")
                    time.sleep(4)
                    continue
                print(f"[Anthropic] Error: {e}")
                break

    # --- Tier 2: OpenRouter (skip Ollama-format models) ---
    if openrouter_client is not None and not is_ollama_model(model):
        so_mode = probe_structured_output(openrouter_client, model, hint=cfg.get("response_format_hint"))
        rf = {"type": "json_object"} if (so_mode == "json_object" and not plain_text) else None  # FIX-181
        for attempt in range(max_retries + 1):
            try:
                create_kwargs: dict = dict(model=model, max_tokens=max_tokens, messages=msgs)
                if rf is not None:
                    create_kwargs["response_format"] = rf
                resp = openrouter_client.chat.completions.create(**create_kwargs)
                _content = resp.choices[0].message.content or ""
                if _LOG_LEVEL == "DEBUG":
                    _m = re.search(r"<think>(.*?)</think>", _content, re.DOTALL)
                    if _m:
                        print(f"[OpenRouter][THINK]: {_m.group(1).strip()}")
                raw = _THINK_RE.sub("", _content).strip()
                if not raw:
                    if attempt < max_retries:
                        print(f"[OpenRouter] Empty response (attempt {attempt + 1}) — retrying")
                        continue
                    print("[OpenRouter] Empty after all retries — falling through to next tier")
                    break  # do not return "" — let next tier try
                return raw
            except Exception as e:
                if any(kw.lower() in str(e).lower() for kw in TRANSIENT_KWS) and attempt < max_retries:
                    print(f"[OpenRouter] Transient (attempt {attempt + 1}): {e} — retrying in 4s")
                    time.sleep(4)
                    continue
                print(f"[OpenRouter] Error: {e}")
                break

    # --- Tier 3: Ollama (local fallback) ---
    ollama_model = cfg.get("ollama_model") or os.environ.get("OLLAMA_MODEL", model)
    # explicit think= overrides cfg; None means use cfg default
    _think_flag = think if think is not None else cfg.get("ollama_think")
    _ollama_extra: dict = {}
    if _think_flag is not None:
        _ollama_extra["think"] = _think_flag
    _opts = cfg.get("ollama_options")
    if _opts is not None:  # None=not configured; {}=valid (though empty) — use `is not None`
        _ollama_extra["options"] = _opts
    for attempt in range(max_retries + 1):
        try:
            # Do not pass max_tokens to Ollama — output is short (~8 tokens); the model stops
            # naturally; explicit cap causes empty responses under GPU load.
            _create_kw: dict = dict(
                model=ollama_model,
                messages=msgs,
            )
            if not plain_text:  # FIX-181: skip json_object for code generation
                _create_kw["response_format"] = {"type": "json_object"}
            if _ollama_extra:
                _create_kw["extra_body"] = _ollama_extra
            resp = ollama_client.chat.completions.create(**_create_kw)
            _content = resp.choices[0].message.content or ""
            if _LOG_LEVEL == "DEBUG":
                _m = re.search(r"<think>(.*?)</think>", _content, re.DOTALL)
                if _m:
                    print(f"[Ollama][THINK]: {_m.group(1).strip()}")
            raw = _THINK_RE.sub("", _content).strip()
            if not raw:
                if attempt < max_retries:
                    print(f"[Ollama] Empty response (attempt {attempt + 1}) — retrying")
                    continue
                print("[Ollama] Empty after all retries — returning None")
                break  # do not return "" — fall through to return None
            return raw
        except Exception as e:
            if any(kw.lower() in str(e).lower() for kw in TRANSIENT_KWS) and attempt < max_retries:
                print(f"[Ollama] Transient (attempt {attempt + 1}): {e} — retrying in 4s")
                time.sleep(4)
                continue
            print(f"[Ollama] Error: {e}")
            break

    # Plain-text retry — if all json_object attempts failed, try without response_format
    try:
        _pt_kw: dict = dict(model=ollama_model, messages=msgs)  # no max_tokens
        if _ollama_extra:
            _pt_kw["extra_body"] = _ollama_extra
        resp = ollama_client.chat.completions.create(**_pt_kw)
        _content = resp.choices[0].message.content or ""
        if _LOG_LEVEL == "DEBUG":
            _m = re.search(r"<think>(.*?)</think>", _content, re.DOTALL)
            if _m:
                print(f"[Ollama-pt][THINK]: {_m.group(1).strip()}")
        raw = _THINK_RE.sub("", _content).strip()
        if raw:
            print(f"[Ollama] Plain-text retry succeeded: {raw[:60]!r}")
            return raw
    except Exception as e:
        print(f"[Ollama] Plain-text retry failed: {e}")

    return None


# ---------------------------------------------------------------------------
# Model routing helpers
# ---------------------------------------------------------------------------

_ANTHROPIC_MODEL_MAP = {
    "claude-haiku-4.5": "claude-haiku-4-5-20251001",
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "claude-sonnet-4.6": "claude-sonnet-4-6",
    "claude-opus-4.6": "claude-opus-4-6",
}


def is_claude_model(model: str) -> bool:
    return "claude" in model.lower()


def get_anthropic_model_id(model: str) -> str:
    """Map alias (e.g. 'anthropic/claude-haiku-4.5') to Anthropic API model ID."""
    clean = model.removeprefix("anthropic/").lower()
    return _ANTHROPIC_MODEL_MAP.get(clean, clean)


# ---------------------------------------------------------------------------
# CLI colors
# ---------------------------------------------------------------------------

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"
CLI_YELLOW = "\x1B[33m"


# ---------------------------------------------------------------------------
# Outcome map
# ---------------------------------------------------------------------------

OUTCOME_BY_NAME = {
    "OUTCOME_OK": Outcome.OUTCOME_OK,
    "OUTCOME_DENIED_SECURITY": Outcome.OUTCOME_DENIED_SECURITY,
    "OUTCOME_NONE_CLARIFICATION": Outcome.OUTCOME_NONE_CLARIFICATION,
    "OUTCOME_NONE_UNSUPPORTED": Outcome.OUTCOME_NONE_UNSUPPORTED,
    "OUTCOME_ERR_INTERNAL": Outcome.OUTCOME_ERR_INTERNAL,
}


# ---------------------------------------------------------------------------
# Dispatch: Pydantic models -> PCM runtime methods
# ---------------------------------------------------------------------------

def dispatch(vm: PcmRuntimeClientSync, cmd: BaseModel,  # FIX-163: coder sub-agent params
             coder_model: str = "", coder_cfg: "dict | None" = None):
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
        return vm.read(ReadRequest(
            path=cmd.path,
            number=cmd.number,
            start_line=cmd.start_line,
            end_line=cmd.end_line,
        ))
    if isinstance(cmd, Req_Write):
        return vm.write(WriteRequest(
            path=cmd.path,
            content=cmd.content,
            start_line=cmd.start_line,
            end_line=cmd.end_line,
        ))
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

    if isinstance(cmd, Req_CodeEval):
        # FIX-163: delegate code generation to MODEL_CODER; only task+vars passed (no loop history)
        # FIX-166: auto-read vault paths via vm.read(); inject content as context_vars so coder
        # model never needs to embed file contents in context — paths keep context_vars compact.
        ctx = dict(cmd.context_vars)
        for _vpath in cmd.paths:
            _key = _vpath.lstrip("/").replace("/", "__").replace(".", "_")
            try:
                _raw = vm.read(ReadRequest(path=_vpath))
                ctx[_key] = MessageToDict(_raw).get("content", "")
            except Exception as _e:
                ctx[_key] = f"[read error: {_e}]"
        code = _call_coder_model(cmd.task, ctx, coder_model or "", coder_cfg or {})
        return _execute_code_safe(code, ctx)

    raise ValueError(f"Unknown command: {cmd}")
