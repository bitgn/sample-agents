"""Task type classifier and model router for multi-model PAC1 agent."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_JSON_TYPE_RE = re.compile(r'\{[^}]*"type"\s*:\s*"(\w+)"[^}]*\}')  # FIX-82: extract type from partial/wrapped JSON

from typing import TYPE_CHECKING

from .dispatch import call_llm_raw

if TYPE_CHECKING:
    from .prephase import PrephaseResult

# Task type literals
TASK_DEFAULT = "default"
TASK_THINK = "think"
TASK_LONG_CONTEXT = "longContext"


_PATH_RE = re.compile(r"/[a-zA-Z0-9_\-\.]+")

# FIX-98: structured rule engine — explicit bulk and think patterns
_BULK_RE = re.compile(
    r"\b(all files|every file|batch|multiple files|all cards|all threads|each file"
    r"|remove all|delete all|discard all|clean all)\b",
    re.IGNORECASE,
)

_THINK_WORDS = re.compile(
    r"\b(distill|analyze|analyse|summarize|summarise|compare|evaluate|review|infer"
    r"|explain|interpret|assess|what does|what is the|why does|how does|what should)\b",
    re.IGNORECASE,
)


@dataclass
class _Rule:
    must: list[re.Pattern]
    must_not: list[re.Pattern]
    result: str
    label: str  # for logging


# FIX-98: priority-ordered rule matrix (longContext > think > default)
_RULE_MATRIX: list[_Rule] = [
    # Rule 1: bulk-scope keywords → longContext
    _Rule(
        must=[_BULK_RE],
        must_not=[],
        result=TASK_LONG_CONTEXT,
        label="bulk-keywords",
    ),
    # Rule 2: reasoning keywords AND NOT bulk → think
    _Rule(
        must=[_THINK_WORDS],
        must_not=[_BULK_RE],
        result=TASK_THINK,
        label="think-keywords",
    ),
]


def classify_task(task_text: str) -> str:
    """FIX-98: structured rule engine (replaces bare regex chain).
    Priority: 3+-paths > bulk-keywords (longContext) > think-keywords > default."""
    # path_count cannot be expressed as regex rule — handle separately
    if len(_PATH_RE.findall(task_text)) >= 3:
        return TASK_LONG_CONTEXT
    for rule in _RULE_MATRIX:
        if (all(r.search(task_text) for r in rule.must)
                and not any(r.search(task_text) for r in rule.must_not)):
            return rule.result
    return TASK_DEFAULT


# ---------------------------------------------------------------------------
# FIX-75: LLM-based task classification (pre-requisite before agent start)
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM = (
    "You are a task router. Classify the task into exactly one type. "
    'Reply ONLY with valid JSON: {"type": "<type>"} where <type> is one of: '
    "think, longContext, default.\n"
    "think = analysis/reasoning/summarize/compare/evaluate/explain/distill\n"
    "longContext = batch/all files/multiple files/3+ explicit file paths\n"
    "default = everything else (read, write, create, capture, delete, move, standard tasks)"
)

_VALID_TYPES = frozenset({TASK_THINK, TASK_LONG_CONTEXT, TASK_DEFAULT})


def _count_tree_files(prephase_log: list) -> int:
    """Extract tree text from prephase log and count file entries (non-directory lines)."""
    for msg in prephase_log:
        if msg.get("role") == "user" and "VAULT STRUCTURE:" in msg.get("content", ""):
            tree_block = msg["content"]
            break
    else:
        return 0
    # File lines: contain └/├/─ and do NOT end with /
    file_lines = [
        ln for ln in tree_block.splitlines()
        if ("─" in ln or "└" in ln or "├" in ln) and not ln.rstrip().endswith("/")
    ]
    return len(file_lines)


def classify_task_llm(task_text: str, model: str, model_config: dict,
                      vault_hint: str | None = None) -> str:
    """FIX-75: Use LLM (classifier model) to classify task type.
    Uses call_llm_raw() for 3-tier routing + retry; falls back to regex.
    FIX-79: treat empty string same as None (empty response after retries).
    FIX-81: truncate to 150 chars — enough for task verb, avoids injection tail.
    FIX-82: JSON regex-extraction fallback if json.loads fails.
    FIX-99: optional vault_hint appended to user message for context.
    FIX-120: regex pre-check fast-path — skip LLM when regex is already confident."""
    # FIX-120: if regex already signals think/longContext, skip the LLM call entirely.
    # Rationale: explicit keywords (distill, analyze, all-files, batch) are unambiguous;
    # LLM call adds latency + GPU contention without changing the outcome.
    # LLM is only useful when regex returns 'default' and vault context might reveal
    # that the task is actually analytical or bulk-scope.
    _regex_pre = classify_task(task_text)
    if _regex_pre != TASK_DEFAULT:
        print(f"[MODEL_ROUTER][FIX-120] Regex-confident type={_regex_pre!r}, skipping LLM")
        return _regex_pre
    user_msg = f"Task: {task_text[:150]}"  # FIX-81: 600→150 to avoid injection content
    if vault_hint:  # FIX-99: add vault context when available
        user_msg += f"\nContext: {vault_hint}"
    # FIX-94: cap classifier tokens — output is always {"type":"X"} (~8 tokens);
    # 512 leaves room for implicit thinking chains without wasting full model budget.
    _cls_cfg = {**model_config, "max_completion_tokens": min(model_config.get("max_completion_tokens", 512), 512)}
    try:
        raw = call_llm_raw(_CLASSIFY_SYSTEM, user_msg, model, _cls_cfg,
                           max_tokens=_cls_cfg["max_completion_tokens"],
                           think=False,  # FIX-103: disable think + use configured token budget
                           max_retries=0)  # FIX-108: 1 attempt only → instant fallback to regex
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
        # FIX-105: plain-text keyword extraction (after JSON + regex fallbacks)
        if not detected:
            raw_lower = raw.lower()
            if "longcontext" in raw_lower or "long_context" in raw_lower or "long context" in raw_lower:
                detected = TASK_LONG_CONTEXT
                print(f"[MODEL_ROUTER][FIX-105] Extracted type 'longContext' from plain text: {raw[:60]!r}")
            elif "think" in raw_lower:
                detected = TASK_THINK
                print(f"[MODEL_ROUTER][FIX-105] Extracted type 'think' from plain text: {raw[:60]!r}")
            elif "default" in raw_lower:
                detected = TASK_DEFAULT
                print(f"[MODEL_ROUTER][FIX-105] Extracted type 'default' from plain text: {raw[:60]!r}")
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
    long_context: str
    # FIX-90: classifier is a first-class routing tier — dedicated model for classification only
    classifier: str
    configs: dict[str, dict] = field(default_factory=dict)

    def _select_model(self, task_type: str) -> str:
        return {
            TASK_THINK: self.think,
            TASK_LONG_CONTEXT: self.long_context,
        }.get(task_type, self.default)

    def resolve(self, task_text: str) -> tuple[str, dict, str]:
        """Return (model_id, model_config, task_type) using regex-only classification."""
        task_type = classify_task(task_text)
        model_id = self._select_model(task_type)
        print(f"[MODEL_ROUTER] type={task_type} → model={model_id}")
        return model_id, self.configs.get(model_id, {}), task_type

    def _adapt_config(self, cfg: dict, task_type: str) -> dict:
        """FIX-119: apply task-type specific ollama_options overlay (shallow merge).
        Merges ollama_options_{task_type} on top of base ollama_options if present."""
        key = f"ollama_options_{task_type}"
        override = cfg.get(key)
        if not override:
            return cfg
        adapted = {**cfg, "ollama_options": {**cfg.get("ollama_options", {}), **override}}
        print(f"[MODEL_ROUTER][FIX-119] adapted ollama_options for type={task_type}: {adapted['ollama_options']}")
        return adapted

    def resolve_after_prephase(self, task_text: str, pre: "PrephaseResult") -> tuple[str, dict, str]:
        """FIX-117: classify once AFTER prephase using AGENTS.MD content as context.
        AGENTS.MD describes task workflows and complexity — single LLM call with full context.
        FIX-119: applies task-type adaptive ollama_options via _adapt_config before returning."""
        file_count = _count_tree_files(pre.log)
        vault_hint = None
        if pre.agents_md_content:
            vault_hint = f"AGENTS.MD:\n{pre.agents_md_content}\nvault files: {file_count}"
        task_type = classify_task_llm(
            task_text, self.classifier, self.configs.get(self.classifier, {}),
            vault_hint=vault_hint,
        )
        model_id = self._select_model(task_type)
        print(f"[MODEL_ROUTER][FIX-117] type={task_type} → model={model_id}")
        adapted_cfg = self._adapt_config(self.configs.get(model_id, {}), task_type)
        return model_id, adapted_cfg, task_type


