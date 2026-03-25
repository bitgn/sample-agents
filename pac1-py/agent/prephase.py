from dataclasses import dataclass

from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import ReadRequest, TreeRequest

from .dispatch import CLI_BLUE, CLI_CLR, CLI_GREEN, CLI_YELLOW


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
        prephase_parts.append(
            f"\n{agents_md_path} CONTENT (source of truth for vault semantics):\n{agents_md_content}"
        )
    prephase_parts.append(
        "\nNOTE: Use the vault structure and AGENTS.MD above to identify actual folder "
        "paths. Verify paths with list/find before acting. Do not assume paths."
    )

    log.append({"role": "user", "content": "\n".join(prephase_parts)})

    # preserve_prefix: always kept during log compaction
    preserve_prefix = list(log)

    print(f"{CLI_BLUE}[prephase] done{CLI_CLR}")

    return PrephaseResult(
        log=log,
        preserve_prefix=preserve_prefix,
        agents_md_content=agents_md_content,
        agents_md_path=agents_md_path,
    )
