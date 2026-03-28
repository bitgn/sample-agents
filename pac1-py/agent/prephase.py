import re
from dataclasses import dataclass

from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import ContextRequest, ReadRequest, TreeRequest

from .dispatch import CLI_BLUE, CLI_CLR, CLI_GREEN, CLI_YELLOW

_AGENTS_MD_BUDGET = 2500  # chars; if AGENTS.MD exceeds this, filter to relevant sections only


def _filter_agents_md(content: str, task_text: str) -> tuple[str, bool]:
    """Return (filtered_content, was_filtered).
    Splits AGENTS.MD by ## headings, keeps preamble + sections most relevant to task_text.
    If content is under budget, returns as-is."""
    if len(content) <= _AGENTS_MD_BUDGET:
        return content, False

    # Split by markdown headings (## or #), preserving heading lines
    parts = re.split(r'^(#{1,3} .+)$', content, flags=re.MULTILINE)
    # parts = [preamble, heading1, body1, heading2, body2, ...]

    sections: list[tuple[str, str]] = []
    if parts[0].strip():
        sections.append(("", parts[0]))  # preamble (no heading)
    for i in range(1, len(parts) - 1, 2):
        sections.append((parts[i], parts[i + 1]))

    if len(sections) <= 1:
        return content[:_AGENTS_MD_BUDGET] + "\n[...truncated]", True

    task_words = set(re.findall(r'\b\w{3,}\b', task_text.lower()))

    def _score(heading: str, body: str) -> int:
        if not heading:
            return 1000  # preamble always first
        h_words = set(re.findall(r'\b\w{3,}\b', heading.lower()))
        b_words = set(re.findall(r'\b\w{3,}\b', body[:400].lower()))
        return len(task_words & h_words) * 5 + len(task_words & b_words)

    scored = sorted(sections, key=lambda s: -_score(s[0], s[1]))

    result_parts: list[str] = []
    used = 0
    for heading, body in scored:
        chunk = (heading + body) if heading else body
        if used + len(chunk) <= _AGENTS_MD_BUDGET:
            result_parts.append(chunk)
            used += len(chunk)

    return "".join(result_parts), True


@dataclass
class PrephaseResult:
    log: list
    preserve_prefix: list  # messages to never compact
    agents_md_content: str = ""  # content of AGENTS.md if found
    agents_md_path: str = ""  # path where AGENTS.md was found


def _format_tree_entry(entry, prefix: str = "", is_last: bool = True) -> list[str]:
    branch = "└── " if is_last else "├── "
    lines = [f"{prefix}{branch}{entry.name}"]
    child_prefix = f"{prefix}{'    ' if is_last else '│   '}"
    children = list(entry.children)
    for idx, child in enumerate(children):
        lines.extend(_format_tree_entry(child, prefix=child_prefix, is_last=idx == len(children) - 1))
    return lines


def _render_tree_result(result, root_path: str = "/", level: int = 2) -> str:
    """Render TreeResponse into compact shell-like output."""
    root = result.root
    if not root.name:
        body = "."
    else:
        lines = [root.name]
        children = list(root.children)
        for idx, child in enumerate(children):
            lines.extend(_format_tree_entry(child, is_last=idx == len(children) - 1))
        body = "\n".join(lines)
    level_arg = f" -L {level}" if level > 0 else ""
    return f"tree{level_arg} {root_path}\n{body}"


# FIX-102: few-shot user→assistant pair — strongest signal for JSON-only output.
# Placed immediately after system prompt so the model sees its own expected format
# before any task context. More reliable than response_format for Ollama-proxied
# cloud models that ignore json_object enforcement.
# NOTE: generic path used intentionally — discovery-first principle (no vault-specific hardcoding).
_FEW_SHOT_USER = "Example: what files are in the notes folder?"
_FEW_SHOT_ASSISTANT = (
    '{"current_state":"listing notes folder to identify files",'
    '"plan_remaining_steps_brief":["list /notes","act on result"],'
    '"task_completed":false,'
    '"function":{"tool":"list","path":"/notes"}}'
)


def run_prephase(
    vm: PcmRuntimeClientSync,
    task_text: str,
    system_prompt_text: str,
) -> PrephaseResult:
    """Pre-phase: expose vault structure and AGENTS.MD to the agent before main loop.

    The agent discovers all relevant paths itself during task execution via
    list/find/grep tools — no paths are extracted or hardcoded here.
    """
    print(f"\n{CLI_BLUE}[prephase] Starting pre-phase exploration{CLI_CLR}")

    log: list = [
        {"role": "system", "content": system_prompt_text},
        {"role": "user", "content": _FEW_SHOT_USER},
        {"role": "assistant", "content": _FEW_SHOT_ASSISTANT},
        {"role": "user", "content": task_text},
    ]

    # Step 1: tree "/" -L 2 — gives the agent the top-level vault layout upfront
    print(f"{CLI_BLUE}[prephase] tree -L 2 /...{CLI_CLR}", end=" ")
    tree_txt = ""
    try:
        tree_result = vm.tree(TreeRequest(root="/", level=2))
        tree_txt = _render_tree_result(tree_result, root_path="/", level=2)
        print(f"{CLI_GREEN}ok{CLI_CLR}")
    except Exception as e:
        tree_txt = f"(tree failed: {e})"
        print(f"{CLI_YELLOW}failed: {e}{CLI_CLR}")

    # Step 2: read AGENTS.MD — source of truth for vault semantics and folder roles
    agents_md_content = ""
    agents_md_path = ""
    for candidate in ["/AGENTS.MD", "/AGENTS.md", "/02_distill/AGENTS.md"]:
        try:
            r = vm.read(ReadRequest(path=candidate))
            if r.content:
                agents_md_content = r.content
                agents_md_path = candidate
                print(f"{CLI_BLUE}[prephase] read {candidate}:{CLI_CLR} {CLI_GREEN}ok{CLI_CLR}")
                break
        except Exception:
            pass

    # Inject vault layout + AGENTS.MD as context — the agent reads this to discover
    # where "cards", "threads", "inbox", etc. actually live in the vault.
    prephase_parts = [f"VAULT STRUCTURE:\n{tree_txt}"]
    if agents_md_content:
        agents_md_injected, was_filtered = _filter_agents_md(agents_md_content, task_text)
        if was_filtered:
            print(f"{CLI_YELLOW}[prephase] AGENTS.MD filtered: {len(agents_md_content)} → {len(agents_md_injected)} chars{CLI_CLR}")
        prephase_parts.append(
            f"\n{agents_md_path} CONTENT (source of truth for vault semantics):\n{agents_md_injected}"
        )
    prephase_parts.append(
        "\nNOTE: Use the vault structure and AGENTS.MD above to identify actual folder "
        "paths. Verify paths with list/find before acting. Do not assume paths."
    )

    log.append({"role": "user", "content": "\n".join(prephase_parts)})

    # Step 3: context — task-level metadata from the harness
    print(f"{CLI_BLUE}[prephase] context...{CLI_CLR}", end=" ")
    try:
        ctx_result = vm.context(ContextRequest())
        if ctx_result.content:
            log.append({"role": "user", "content": f"TASK CONTEXT:\n{ctx_result.content}"})
            print(f"{CLI_GREEN}ok{CLI_CLR}")
        else:
            print(f"{CLI_YELLOW}empty{CLI_CLR}")
    except Exception as e:
        print(f"{CLI_YELLOW}not available: {e}{CLI_CLR}")

    # preserve_prefix: always kept during log compaction
    preserve_prefix = list(log)

    print(f"{CLI_BLUE}[prephase] done{CLI_CLR}")

    return PrephaseResult(
        log=log,
        preserve_prefix=preserve_prefix,
        agents_md_content=agents_md_content,
        agents_md_path=agents_md_path,
    )
