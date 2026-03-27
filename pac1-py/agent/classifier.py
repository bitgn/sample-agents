"""Task type classifier and model router for multi-model PAC1 agent."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_JSON_TYPE_RE = re.compile(r'\{[^}]*"type"\s*:\s*"(\w+)"[^}]*\}')  # FIX-82: extract type from partial/wrapped JSON

from .dispatch import call_llm_raw

# Task type literals
TASK_DEFAULT = "default"
TASK_THINK = "think"
TASK_TOOL = "tool"
TASK_LONG_CONTEXT = "longContext"


_THINK_WORDS = re.compile(
    r"\b(distill|analyze|analyse|summarize|summarise|compare|evaluate|review|infer|"
    r"explain|interpret|assess|what does|what is the|why does|how does|what should)\b",
    re.IGNORECASE,
)

_TOOL_WORDS = re.compile(
    r"\b(delete|remove|move|rename|copy|discard|trash|purge)\b",  # FIX-82: added discard/trash/purge
    re.IGNORECASE,
)

_LONG_CONTEXT_WORDS = re.compile(
    r"\b(all files|every file|batch|multiple files|all cards|all threads|each file)\b",
    re.IGNORECASE,
)

_PATH_RE = re.compile(r"/[a-zA-Z0-9_\-\.]+")


def classify_task(task_text: str) -> str:
    """Classify task text into one of: default, think, tool, longContext."""
    # longContext: many file paths OR explicit bulk keywords
    path_count = len(_PATH_RE.findall(task_text))
    if path_count >= 3 or _LONG_CONTEXT_WORDS.search(task_text):
        return TASK_LONG_CONTEXT

    # think: analysis/reasoning keywords
    if _THINK_WORDS.search(task_text):
        return TASK_THINK

    # tool: file manipulation keywords
    if _TOOL_WORDS.search(task_text):
        return TASK_TOOL

    return TASK_DEFAULT


# ---------------------------------------------------------------------------
# FIX-75: LLM-based task classification (pre-requisite before agent start)
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM = (
    "You are a task router. Classify the task into exactly one type. "
    'Reply ONLY with valid JSON: {"type": "<type>"} where <type> is one of: '
    "think, tool, longContext, default.\n"
    "think = analysis/reasoning/summarize/compare/evaluate/explain/distill\n"
    "tool = delete/remove/move/rename/copy/discard/trash/purge files or folders\n"
    "longContext = batch/all files/multiple files/3+ explicit file paths\n"
    "default = everything else (read, write, create, capture, standard tasks)"
)

_VALID_TYPES = frozenset({TASK_THINK, TASK_TOOL, TASK_LONG_CONTEXT, TASK_DEFAULT})


def classify_task_llm(task_text: str, model: str, model_config: dict) -> str:
    """FIX-75: Use LLM (default model) to classify task type before agent start.
    Uses FIX-76 call_llm_raw() for 3-tier routing + retry; falls back to regex.
    FIX-79: treat empty string same as None (empty response after retries).
    FIX-81: truncate to 150 chars — enough for task verb, avoids injection tail.
    FIX-82: JSON regex-extraction fallback if json.loads fails."""
    user_msg = f"Task: {task_text[:150]}"  # FIX-81: 600→150 to avoid injection content
    try:
        raw = call_llm_raw(_CLASSIFY_SYSTEM, user_msg, model, model_config, max_tokens=200, think=False)  # FIX-84: disable think + larger budget
        if not raw:  # FIX-79: catch both None and "" (empty string after retry exhaustion)
            print("[MODEL_ROUTER][FIX-75] All LLM tiers failed or empty, falling back to regex")
            return classify_task(task_text)
        # Try strict JSON parse first
        try:
            detected = str(json.loads(raw).get("type", "")).strip()
        except (json.JSONDecodeError, AttributeError):
            # FIX-82: JSON parse failed — try regex extraction from response text
            m = _JSON_TYPE_RE.search(raw)
            detected = m.group(1).strip() if m else ""
            if detected:
                print(f"[MODEL_ROUTER][FIX-82] Extracted type via regex from: {raw!r}")
        if detected in _VALID_TYPES:
            print(f"[MODEL_ROUTER][FIX-75] LLM classified task as '{detected}'")
            return detected
        print(f"[MODEL_ROUTER][FIX-75] LLM returned unknown type '{detected}', falling back to regex")
    except Exception as exc:
        print(f"[MODEL_ROUTER][FIX-75] LLM classification failed ({exc}), falling back to regex")
    return classify_task(task_text)


@dataclass
class ModelRouter:
    """Routes tasks to appropriate models based on task type classification."""
    default: str
    think: str
    tool: str
    long_context: str
    classifier: str = ""  # FIX-86: model for LLM classification; empty = use default
    configs: dict[str, dict] = field(default_factory=dict)

    def _select_model(self, task_type: str) -> str:
        return {
            TASK_THINK: self.think,
            TASK_TOOL: self.tool,
            TASK_LONG_CONTEXT: self.long_context,
        }.get(task_type, self.default)

    def resolve(self, task_text: str) -> tuple[str, dict, str]:
        """Return (model_id, model_config, task_type) for the given task text."""
        task_type = classify_task(task_text)
        model_id = self._select_model(task_type)
        print(f"[MODEL_ROUTER] type={task_type} → model={model_id}")
        return model_id, self.configs.get(model_id, {}), task_type

    def resolve_llm(self, task_text: str) -> tuple[str, dict, str]:
        """FIX-75: Use default model LLM to classify task, then return (model_id, config, task_type).
        Falls back to regex-based resolve() if LLM classification fails."""
        # FIX-86: use dedicated classifier model if configured, else fall back to default
        _cls_model = self.classifier or self.default
        task_type = classify_task_llm(task_text, _cls_model, self.configs.get(_cls_model, {}))
        model_id = self._select_model(task_type)
        print(f"[MODEL_ROUTER][FIX-75] LLM type={task_type} → model={model_id}")
        return model_id, self.configs.get(model_id, {}), task_type
