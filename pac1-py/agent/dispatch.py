import os
import re
import time
from pathlib import Path

import anthropic
from openai import OpenAI
from pydantic import BaseModel

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
# FIX-76: lightweight raw LLM call (used by classify_task_llm in classifier.py)
# ---------------------------------------------------------------------------

# Transient error keywords — copy also in loop.py; keep both in sync
_TRANSIENT_KWS_RAW = (
    "503", "502", "429", "NoneType", "overloaded",
    "unavailable", "server error", "rate limit", "rate-limit",
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def call_llm_raw(
    system: str,
    user_msg: str,
    model: str,
    cfg: dict,
    max_tokens: int = 20,
    think: bool | None = None,  # FIX-84: None=use cfg, False=disable, True=enable
) -> str | None:
    """FIX-76: Lightweight LLM call with 3-tier routing and FIX-27 retry.
    Returns raw text (think blocks stripped), or None if all tiers fail.
    Used by classify_task_llm(); caller handles JSON parsing and fallback."""

    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    # --- Tier 1: Anthropic SDK ---
    if is_claude_model(model) and anthropic_client is not None:
        ant_model = get_anthropic_model_id(model)
        for attempt in range(4):
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
                if attempt < 3:
                    print(f"[FIX-76][Anthropic] Empty response (attempt {attempt + 1}) — retrying")
                    continue
                print("[FIX-80][Anthropic] Empty after all retries — falling through to next tier")
                break  # FIX-80: do not return "" — let next tier try
            except Exception as e:
                if any(kw.lower() in str(e).lower() for kw in _TRANSIENT_KWS_RAW) and attempt < 3:
                    print(f"[FIX-76][Anthropic] Transient (attempt {attempt + 1}): {e} — retrying in 4s")
                    time.sleep(4)
                    continue
                print(f"[FIX-76][Anthropic] Error: {e}")
                break

    # --- Tier 2: OpenRouter (skip Ollama-format models) ---
    if openrouter_client is not None and not is_ollama_model(model):  # FIX-83
        so_mode = probe_structured_output(openrouter_client, model, hint=cfg.get("response_format_hint"))
        rf = {"type": "json_object"} if so_mode == "json_object" else None
        for attempt in range(4):
            try:
                create_kwargs: dict = dict(model=model, max_tokens=max_tokens, messages=msgs)
                if rf is not None:
                    create_kwargs["response_format"] = rf
                resp = openrouter_client.chat.completions.create(**create_kwargs)
                raw = _THINK_RE.sub("", resp.choices[0].message.content or "").strip()
                if not raw:
                    if attempt < 3:
                        print(f"[FIX-76][OpenRouter] Empty response (attempt {attempt + 1}) — retrying")
                        continue
                    print("[FIX-80][OpenRouter] Empty after all retries — falling through to next tier")
                    break  # FIX-80: do not return "" — let next tier try
                return raw
            except Exception as e:
                if any(kw.lower() in str(e).lower() for kw in _TRANSIENT_KWS_RAW) and attempt < 3:
                    print(f"[FIX-76][OpenRouter] Transient (attempt {attempt + 1}): {e} — retrying in 4s")
                    time.sleep(4)
                    continue
                print(f"[FIX-76][OpenRouter] Error: {e}")
                break

    # --- Tier 3: Ollama (local fallback) ---
    ollama_model = cfg.get("ollama_model") or os.environ.get("OLLAMA_MODEL", model)
    # FIX-84: explicit think= overrides cfg; None means use cfg default
    _think_flag = think if think is not None else cfg.get("ollama_think")
    _ollama_extra: dict | None = {"think": _think_flag} if _think_flag is not None else None
    for attempt in range(4):
        try:
            _create_kw: dict = dict(
                model=ollama_model,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                messages=msgs,
            )
            if _ollama_extra:
                _create_kw["extra_body"] = _ollama_extra
            resp = ollama_client.chat.completions.create(**_create_kw)
            raw = _THINK_RE.sub("", resp.choices[0].message.content or "").strip()
            if not raw:
                if attempt < 3:
                    print(f"[FIX-76][Ollama] Empty response (attempt {attempt + 1}) — retrying")
                    continue
                print("[FIX-80][Ollama] Empty after all retries — returning None")
                break  # FIX-80: do not return "" — fall through to return None
            return raw
        except Exception as e:
            if any(kw.lower() in str(e).lower() for kw in _TRANSIENT_KWS_RAW) and attempt < 3:
                print(f"[FIX-76][Ollama] Transient (attempt {attempt + 1}): {e} — retrying in 4s")
                time.sleep(4)
                continue
            print(f"[FIX-76][Ollama] Error: {e}")
            break

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


def is_ollama_model(model: str) -> bool:
    """FIX-83: True for Ollama-format models (name:tag, no slash).
    Examples: qwen3.5:9b, deepseek-v3.1:671b-cloud, qwen3.5:cloud.
    These must be routed directly to Ollama tier, skipping OpenRouter."""
    return ":" in model and "/" not in model


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

    raise ValueError(f"Unknown command: {cmd}")
