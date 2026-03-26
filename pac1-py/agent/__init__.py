from __future__ import annotations

from bitgn.vm.pcm_connect import PcmRuntimeClientSync

from .classifier import ModelRouter
from .loop import run_loop
from .prephase import run_prephase
from .prompt import system_prompt


def run_agent(model: str | ModelRouter, harness_url: str, task_text: str, model_config: dict | None = None) -> dict:
    """Universal agent entry point for PAC1 benchmark using PCM runtime.
    Returns token usage stats dict: {input_tokens, output_tokens, thinking_tokens}."""
    vm = PcmRuntimeClientSync(harness_url)

    if isinstance(model, ModelRouter):
        model, cfg = model.resolve_llm(task_text)  # FIX-75: LLM-based pre-classification
    else:
        cfg = model_config or {}

    pre = run_prephase(vm, task_text, system_prompt)
    stats = run_loop(vm, model, task_text, pre, cfg)
    stats["model_used"] = model
    return stats
