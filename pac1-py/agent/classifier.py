"""Task type classifier and model router for multi-model PAC1 agent."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_JSON_TYPE_RE = re.compile(r'\{[^}]*"type"\s*:\s*"(\w+)"[^}]*\}')  # extract type from partial/wrapped JSON

from typing import TYPE_CHECKING

from .dispatch import call_llm_raw

if TYPE_CHECKING:
    from .prephase import PrephaseResult

# Task type literals
TASK_DEFAULT = "default"
TASK_THINK = "think"
TASK_LONG_CONTEXT = "longContext"
TASK_EMAIL = "email"
TASK_LOOKUP = "lookup"
TASK_INBOX = "inbox"
TASK_DISTILL = "distill"
TASK_CODER = "coder"


_PATH_RE = re.compile(r"/[a-zA-Z0-9_\-\.]+")

# Structured rule engine — explicit bulk and think patterns
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

# Unit 8: new task type patterns
_INBOX_RE = re.compile(
    r"\b(process|check|handle)\s+(the\s+)?inbox\b",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(
    r"\b(send|compose|write|email)\b.*\b(to|recipient|subject)\b",
    re.IGNORECASE,
)

_LOOKUP_RE = re.compile(
    r"\b(what\s+is|find|lookup|search\s+for)\b.*\b(email|phone|contact|account)\b",
    re.IGNORECASE,
)

# Write-verbs used to distinguish lookup from distill/email
_WRITE_VERBS_RE = re.compile(
    r"\b(write|create|add|update|send|compose|delete|move|rename)\b",
    re.IGNORECASE,
)

# FIX-175: counting/aggregation queries without write intent → lookup (read-only vault data query).
# Note: _CODER_RE (FIX-152r) was removed — TASK_CODER is now a sub-agent (FIX-163), not a route.
# Keywords that imply date arithmetic (e.g. "2 weeks") are NOT here — those tasks include write ops
# and route to default. Only pure read-aggregation keywords belong in _COUNT_QUERY_RE.
_COUNT_QUERY_RE = re.compile(
    r"\b(how\s+many|count|sum\s+of|total\s+of|average|aggregate)\b",
    re.IGNORECASE,
)


@dataclass
class _Rule:
    must: list[re.Pattern]
    must_not: list[re.Pattern]
    result: str
    label: str  # for logging


# Priority-ordered rule matrix
# Priority: longContext > inbox > email > lookup > distill > think > default
# FIX-163: TASK_CODER removed from routing — coder model is now a sub-agent called within steps
_RULE_MATRIX: list[_Rule] = [
    # Rule 1: bulk-scope keywords → longContext
    _Rule(
        must=[_BULK_RE],
        must_not=[],
        result=TASK_LONG_CONTEXT,
        label="bulk-keywords",
    ),
    # Rule 2: inbox process/check/handle → inbox
    _Rule(
        must=[_INBOX_RE],
        must_not=[_BULK_RE],
        result=TASK_INBOX,
        label="inbox-keywords",
    ),
    # Rule 3: send/compose email with recipient/subject → email
    _Rule(
        must=[_EMAIL_RE],
        must_not=[_BULK_RE, _INBOX_RE],
        result=TASK_EMAIL,
        label="email-keywords",
    ),
    # Rule 4: lookup contact/email/phone with no write intent → lookup
    _Rule(
        must=[_LOOKUP_RE],
        must_not=[_BULK_RE, _INBOX_RE, _EMAIL_RE, _WRITE_VERBS_RE],
        result=TASK_LOOKUP,
        label="lookup-keywords",
    ),
    # Rule 4b: counting/aggregation query with no write intent → lookup  # FIX-175
    # Covers: "how many X", "count X", "sum of X", "total of X", "average", "aggregate"
    # must_not _WRITE_VERBS_RE ensures tasks like "calculate total and update" route to default
    _Rule(
        must=[_COUNT_QUERY_RE],
        must_not=[_BULK_RE, _INBOX_RE, _EMAIL_RE, _WRITE_VERBS_RE],
        result=TASK_LOOKUP,
        label="count-query",
    ),
    # Rule 5: think-words AND write-verbs simultaneously → distill
    _Rule(
        must=[_THINK_WORDS, _WRITE_VERBS_RE],
        must_not=[_BULK_RE, _INBOX_RE, _EMAIL_RE],
        result=TASK_DISTILL,
        label="distill-keywords",
    ),
    # Rule 6: reasoning keywords AND NOT bulk → think
    _Rule(
        must=[_THINK_WORDS],
        must_not=[_BULK_RE],
        result=TASK_THINK,
        label="think-keywords",
    ),
]


def classify_task(task_text: str) -> str:
    """Regex-based structured rule engine for task type classification.
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
# LLM-based task classification (pre-requisite before agent start)
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM = (
    "You are a task router. Classify the task into exactly one type. "
    'Reply ONLY with valid JSON: {"type": "<type>"} where <type> is one of: '
    "think, longContext, email, lookup, inbox, distill, default.\n"  # FIX-163: coder removed (sub-agent, not a task route)
    "longContext = batch/all files/multiple files/3+ explicit file paths\n"
    "inbox = process/check/handle the inbox\n"
    "email = send/compose/write email to a recipient\n"
    "lookup = find, count, or query vault data (contacts, files, channels) with no write action\n"  # FIX-175
    "distill = analysis/reasoning AND writing a card/note/summary\n"
    "think = analysis/reasoning/summarize/compare/evaluate/explain (no write)\n"
    "default = everything else (read, write, create, capture, delete, move, standard tasks)"
)

# FIX-198: TASK_CODER removed — since FIX-163 coder is a sub-agent, not a valid task route.
# If LLM returns "coder", it falls through to regex fallback (returns default).
_VALID_TYPES = frozenset({TASK_THINK, TASK_LONG_CONTEXT, TASK_DEFAULT,
                          TASK_EMAIL, TASK_LOOKUP, TASK_INBOX, TASK_DISTILL})

# Ordered keyword → task_type table for plain-text LLM response fallback.
# Most-specific types first; longContext listed with all common spellings.
_PLAINTEXT_FALLBACK: list[tuple[tuple[str, ...], str]] = [
    (("longcontext", "long_context", "long context"), TASK_LONG_CONTEXT),
    (("inbox",),   TASK_INBOX),
    (("email",),   TASK_EMAIL),
    # FIX-198: ("coder",) removed — coder is a sub-agent (FIX-163), not a valid task route
    (("lookup",),  TASK_LOOKUP),
    (("distill",), TASK_DISTILL),
    (("think",),   TASK_THINK),
    (("default",), TASK_DEFAULT),
]


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
    """Classify task type using an LLM, with regex fast-path and multi-tier fallbacks.

    Fast-path: if regex already returns a non-default type (explicit bulk/think/inbox/email
    keywords), the LLM call is skipped entirely — those keywords are unambiguous and the
    LLM would only add latency. The LLM is only invoked when regex returns 'default' and
    vault context (AGENTS.MD) might reveal the task is actually analytical or bulk-scope.

    ollama_options filtering: only 'num_ctx', 'temperature', and 'seed' are forwarded to
    the classifier call. Agent-loop options (repeat_penalty, repeat_last_n, top_k) are
    tuned for long generation and cause empty responses for the short 8-token output.

    Token budget: max_completion_tokens is capped at 512. The classifier output is always
    {"type":"X"} (~8 tokens); 512 leaves headroom for implicit reasoning without wasting
    the model's full budget.

    Retry policy: max_retries=1 (one retry on empty response, then fall back to regex).

    Returns one of the TASK_* literals defined in this module.
    """
    # Regex pre-check fast-path: if regex is already confident, skip the LLM call.
    # Explicit keywords (distill, analyze, all-files, batch) are unambiguous;
    # LLM is only useful when regex returns 'default' and vault context might change the outcome.
    _regex_pre = classify_task(task_text)
    if _regex_pre != TASK_DEFAULT:
        print(f"[MODEL_ROUTER] Regex-confident type={_regex_pre!r}, skipping LLM")
        return _regex_pre
    user_msg = f"Task: {task_text[:150]}"  # truncate to 150 chars to avoid injection content
    if vault_hint:
        # Truncate vault_hint to 400 chars — first lines of AGENTS.MD contain the
        # role/folder summary which is sufficient for classification.
        user_msg += f"\nContext: {vault_hint[:400]}"
    # Cap classifier tokens — output is always {"type":"X"} (~8 tokens);
    # strip agent-loop ollama_options, classifier only needs num_ctx, temperature, seed.
    # Priority: ollama_options_classifier (deterministic profile) > ollama_options (agent profile).
    _base_opts = model_config.get("ollama_options_classifier") or model_config.get("ollama_options", {})
    _cls_opts = {k: v for k, v in _base_opts.items() if k in ("num_ctx", "temperature", "seed")}
    _cls_cfg = {
        **model_config,
        "max_completion_tokens": min(model_config.get("max_completion_tokens", 512), 512),
        "ollama_options": _cls_opts or None,
    }
    try:
        raw = call_llm_raw(_CLASSIFY_SYSTEM, user_msg, model, _cls_cfg,
                           max_tokens=_cls_cfg["max_completion_tokens"],
                           think=False,
                           max_retries=1)
        if not raw:  # catch both None and "" (empty string after retry exhaustion)
            print("[MODEL_ROUTER] All LLM tiers failed or empty, falling back to regex")
            return classify_task(task_text)
        # Try strict JSON parse first
        try:
            detected = str(json.loads(raw).get("type", "")).strip()
        except (json.JSONDecodeError, AttributeError):
            # JSON parse failed — try regex extraction from response text
            m = _JSON_TYPE_RE.search(raw)
            detected = m.group(1).strip() if m else ""
            if detected:
                print(f"[MODEL_ROUTER] Extracted type via regex from: {raw!r}")
        # Plain-text keyword extraction (after JSON + regex fallbacks)
        # Ordered: most-specific types first; longContext checked with all its spellings.
        if not detected:
            raw_lower = raw.lower()
            for keywords, task_type in _PLAINTEXT_FALLBACK:
                if any(kw in raw_lower for kw in keywords):
                    detected = task_type
                    print(f"[MODEL_ROUTER] Extracted type {task_type!r} from plain text: {raw[:60]!r}")
                    break
        if detected in _VALID_TYPES:
            print(f"[MODEL_ROUTER] LLM classified task as '{detected}'")
            return detected
        print(f"[MODEL_ROUTER] LLM returned unknown type '{detected}', falling back to regex")
    except Exception as exc:
        print(f"[MODEL_ROUTER] LLM classification failed ({exc}), falling back to regex")
    return classify_task(task_text)


@dataclass
class ModelRouter:
    """Routes tasks to appropriate models based on task type classification."""
    default: str
    think: str
    long_context: str
    # Classifier is a first-class routing tier — dedicated model for classification only
    classifier: str
    # Unit 8: new task type model overrides (fall back to default/think if not provided)
    email: str = ""
    lookup: str = ""
    inbox: str = ""
    # Unit 9: coder task type model override
    coder: str = ""
    configs: dict[str, dict] = field(default_factory=dict)

    def _select_model(self, task_type: str) -> str:
        return {
            TASK_THINK: self.think,
            TASK_LONG_CONTEXT: self.long_context,
            TASK_EMAIL: self.email or self.default,
            TASK_CODER: self.default,  # FIX-163: coder is a sub-agent; task routes to default model
            TASK_LOOKUP: self.lookup or self.default,
            TASK_INBOX: self.inbox or self.think,
            TASK_DISTILL: self.think,
        }.get(task_type, self.default)

    def resolve(self, task_text: str) -> tuple[str, dict, str]:
        """Return (model_id, model_config, task_type) using regex-only classification."""
        task_type = classify_task(task_text)
        model_id = self._select_model(task_type)
        print(f"[MODEL_ROUTER] type={task_type} → model={model_id}")
        return model_id, self.configs.get(model_id, {}), task_type

    def _adapt_config(self, cfg: dict, task_type: str) -> dict:
        """Apply task-type specific ollama_options overlay (shallow merge).
        Merges ollama_options_{task_type} on top of base ollama_options if present."""
        key = f"ollama_options_{task_type}"
        override = cfg.get(key)
        if not override:
            return cfg
        adapted = {**cfg, "ollama_options": {**cfg.get("ollama_options", {}), **override}}
        print(f"[MODEL_ROUTER] Adapted ollama_options for type={task_type}: {adapted['ollama_options']}")
        return adapted

    def resolve_after_prephase(self, task_text: str, pre: "PrephaseResult") -> tuple[str, dict, str]:
        """Classify once after prephase using AGENTS.MD content as context.
        AGENTS.MD describes task workflows and complexity — single LLM call with full context.
        Applies task-type adaptive ollama_options via _adapt_config before returning."""
        file_count = _count_tree_files(pre.log)
        vault_hint = None
        if pre.agents_md_content:
            vault_hint = f"AGENTS.MD:\n{pre.agents_md_content}\nvault files: {file_count}"
        task_type = classify_task_llm(
            task_text, self.classifier, self.configs.get(self.classifier, {}),
            vault_hint=vault_hint,
        )
        model_id = self._select_model(task_type)
        print(f"[MODEL_ROUTER] type={task_type} → model={model_id}")
        adapted_cfg = self._adapt_config(self.configs.get(model_id, {}), task_type)
        return model_id, adapted_cfg, task_type
