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

# Keep _LONG_CONTEXT_WORDS as alias for backward compatibility
_LONG_CONTEXT_WORDS = _BULK_RE


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

# FIX-100: tracks whether the last classify_task_llm() call used LLM (True) or fell back to regex (False).
# Set per-task; reclassify_with_prephase() skips expensive LLM retry when False.
_classifier_llm_ok: bool = True


def _task_fingerprint(task_text: str) -> frozenset[str]:
    """FIX-97: Extract keyword fingerprint for cache lookup."""
    words: set[str] = set()
    for m in _THINK_WORDS.finditer(task_text):
        words.add(m.group(0).lower())
    for m in _LONG_CONTEXT_WORDS.finditer(task_text):
        words.add(m.group(0).lower())
    return frozenset(words)


def classify_task_llm(task_text: str, model: str, model_config: dict,
                      vault_hint: str | None = None) -> str:
    """FIX-75: Use LLM (classifier model) to classify task type before agent start.
    Uses FIX-76 call_llm_raw() for 3-tier routing + retry; falls back to regex.
    FIX-79: treat empty string same as None (empty response after retries).
    FIX-81: truncate to 150 chars — enough for task verb, avoids injection tail.
    FIX-82: JSON regex-extraction fallback if json.loads fails.
    FIX-99: optional vault_hint appended to user message for post-prephase re-class.
    FIX-100: sets _classifier_llm_ok flag — False on fallback, True on LLM success."""
    global _classifier_llm_ok
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
            _classifier_llm_ok = False
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
            _classifier_llm_ok = True
            return detected
        print(f"[MODEL_ROUTER][FIX-75] LLM returned unknown type '{detected}', falling back to regex")
    except Exception as exc:
        print(f"[MODEL_ROUTER][FIX-75] LLM classification failed ({exc}), falling back to regex")
    _classifier_llm_ok = False
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
    _type_cache: dict[frozenset[str], str] = field(default_factory=dict)

    def _select_model(self, task_type: str) -> str:
        return {
            TASK_THINK: self.think,
            TASK_LONG_CONTEXT: self.long_context,
        }.get(task_type, self.default)

    def resolve(self, task_text: str) -> tuple[str, dict, str]:
        """Return (model_id, model_config, task_type) for the given task text."""
        task_type = classify_task(task_text)
        model_id = self._select_model(task_type)
        print(f"[MODEL_ROUTER] type={task_type} → model={model_id}")
        return model_id, self.configs.get(model_id, {}), task_type

    def resolve_llm(self, task_text: str) -> tuple[str, dict, str]:
        """FIX-75: Use classifier model to classify task, then return (model_id, config, task_type).
        FIX-97: Cache classification results by keyword fingerprint — skip LLM on cache hit.
        FIX-112: Skip LLM when regex result is unambiguous — LLM only adds value when
        regex=default AND no bulk keywords (the only case where LLM can upgrade to think)."""
        global _classifier_llm_ok
        # FIX-97: check keyword fingerprint cache before calling LLM
        fp = _task_fingerprint(task_text)
        if fp:
            if fp in self._type_cache:
                cached = self._type_cache[fp]
                print(f"[MODEL_ROUTER][FIX-97] Cache hit {set(fp)} → '{cached}'")
                # FIX-100: reset flag — cache hit means LLM worked before; don't carry stale False
                _classifier_llm_ok = True
                model_id = self._select_model(cached)
                return model_id, self.configs.get(model_id, {}), cached

        # FIX-112: pre-check regex before spending LLM call.
        # LLM can only improve the result in one case: regex=default AND no bulk keywords
        # (where LLM might detect think-style reasoning regex missed).
        # Other cases: regex already non-default → LLM would agree or wrongly downgrade;
        # default + bulk → FIX-89 will upgrade to longContext anyway.
        regex_type = classify_task(task_text)
        has_bulk = bool(_BULK_TASK_RE.search(task_text))
        if regex_type != TASK_DEFAULT or has_bulk:
            print(f"[MODEL_ROUTER][FIX-112] Skipping LLM: regex={regex_type} bulk={has_bulk} → unambiguous")
            _classifier_llm_ok = False
            task_type = regex_type
        else:
            task_type = classify_task_llm(task_text, self.classifier, self.configs.get(self.classifier, {}))

        if fp:
            self._type_cache[fp] = task_type  # FIX-97: store in cache
        model_id = self._select_model(task_type)
        print(f"[MODEL_ROUTER][FIX-75] type={task_type} → model={model_id}")
        return model_id, self.configs.get(model_id, {}), task_type

    def model_for_type(self, task_type: str) -> tuple[str, dict]:
        """FIX-89: Return (model_id, config) for an already-known task_type."""
        model_id = self._select_model(task_type)
        return model_id, self.configs.get(model_id, {})


# ---------------------------------------------------------------------------
# FIX-89: Post-prephase reclassification using vault context
# ---------------------------------------------------------------------------

# Bulk-scope words in task text
_BULK_TASK_RE = re.compile(
    r"\b(all|every|each|batch|multiple|entire|whole)\b",
    re.IGNORECASE,
)


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


def reclassify_with_prephase(
    task_type: str,
    task_text: str,
    pre: PrephaseResult,
    model: str = "",
    model_config: dict | None = None,
) -> str:
    """FIX-89 + FIX-99: Refine task_type using vault context loaded during prephase.
    FIX-89: rule-based longContext upgrade (large vault + bulk task).
    FIX-99: optional LLM re-class with vault context (if model provided).
    Called after run_prephase(). Returns adjusted task_type string."""
    task_lower = task_text.lower()
    file_count = _count_tree_files(pre.log)
    is_bulk = bool(_BULK_TASK_RE.search(task_lower))

    # FIX-89: rule-based longContext upgrade
    if task_type in (TASK_DEFAULT, TASK_THINK) and is_bulk and file_count >= 8:
        print(
            f"[MODEL_ROUTER][FIX-89] {file_count} files in vault tree + bulk task "
            f"→ override '{task_type}' → 'longContext'"
        )
        return TASK_LONG_CONTEXT

    # FIX-99 + FIX-100: LLM re-class with vault context (only if classifier model provided
    # AND last LLM classify actually succeeded — skip if Ollama was empty/unavailable)
    if model and _classifier_llm_ok:
        vault_hint = (
            f"vault has {file_count} files, "
            f"bulk-scope: {'yes' if is_bulk else 'no'}"
        )
        refined = classify_task_llm(
            task_text, model, model_config or {}, vault_hint=vault_hint
        )
        if refined != task_type:
            print(
                f"[MODEL_ROUTER][FIX-99] LLM re-class with vault context: "
                f"'{task_type}' → '{refined}'"
            )
            return refined
    elif model:
        print("[MODEL_ROUTER][FIX-100] Skipping LLM re-class — classifier was unavailable")

    return task_type
