# pac1-py Architecture Documentation

Generated: 2026-03-28 | Complexity: **Standard** | Fix counter: FIX-102 (FIX-103 is next)

## Overview

**pac1-py** is a file-system agent for the BitGN PAC1 benchmark. It manages a personal knowledge vault through the PCM runtime (9 tools: tree/find/search/list/read/write/delete/mkdir/move + report_completion) using a discovery-first prompt strategy and a three-tier LLM dispatch stack.

**Benchmark results:**
- `anthropic/claude-sonnet-4.6` — 100.00% on bitgn/pac1-dev (stable, discovery-first prompt)
- `qwen/qwen3.5-9b` (OpenRouter) — 100.00% on bitgn/pac1-dev (stable, discovery-first prompt)
- `anthropic/claude-haiku-4.5` — ~97% on bitgn/pac1-dev (11 tasks, 2/3 iter at 100%)

## Files

| File | Description |
|------|-------------|
| [overview.yaml](overview.yaml) | Components, dependencies, quality attributes, env vars |
| [diagrams/dependency-graph.md](diagrams/dependency-graph.md) | Mermaid component dependency graph |
| [diagrams/data-flow-agent-execution.md](diagrams/data-flow-agent-execution.md) | Mermaid sequence diagram — full task execution flow |
| [diagrams/data-flow-llm-dispatch.md](diagrams/data-flow-llm-dispatch.md) | Mermaid flowchart — three-tier LLM dispatch with fallback |

## Architecture at a Glance

```
main.py  →  run_agent() [__init__.py]
              ├── ModelRouter.resolve_llm() [classifier.py]  ← FIX-75/97/98: LLM + cached classification
              ├── run_prephase() [prephase.py]               ← few-shot + tree + AGENTS.MD + context (FIX-102)
              ├── reclassify_with_prephase() [classifier.py] ← FIX-89/99: refine type with vault context
              └── run_loop() [loop.py]                       ← 30-step loop
                    ├── compact log (prefix + last 5 pairs)
                    ├── _call_llm() → NextStep [dispatch.py]
                    │     ├── Tier 1: Anthropic SDK (native thinking)
                    │     ├── Tier 2: OpenRouter (FIX-27 retry, FIX-101 bracket-extract)
                    │     └── Tier 3: Ollama (local fallback)
                    ├── stall detection [FIX-74]
                    └── dispatch tool → PcmRuntimeClientSync [bitgn/]
```

## Component Dependency Graph

```mermaid
graph TD
    subgraph Presentation
        MAIN["main.py\nBenchmark Runner"]
    end

    subgraph Business["Business Logic (agent/)"]
        INIT["__init__.py\nAgent Entry Point"]
        CLASSIFIER["classifier.py\nTask Classifier + ModelRouter"]
        PREPHASE["prephase.py\nPre-phase Explorer"]
        LOOP["loop.py\nMain Agent Loop"]
        PROMPT["prompt.py\nSystem Prompt"]
        MODELS["models.py\nPydantic Models"]
    end

    subgraph Infrastructure["Infrastructure"]
        DISPATCH["dispatch.py\nLLM Dispatch + PCM Bridge"]
        HARNESS["bitgn/\nHarness + PCM Clients"]
    end

    subgraph External["External"]
        ANTHROPIC["Anthropic SDK\n(Tier 1)"]
        OPENROUTER["OpenRouter\n(Tier 2)"]
        OLLAMA["Ollama\n(Tier 3)"]
        BITGN_API["api.bitgn.com"]
    end

    MAIN --> INIT
    MAIN --> CLASSIFIER
    MAIN --> HARNESS
    INIT --> CLASSIFIER
    INIT --> PREPHASE
    INIT --> LOOP
    INIT --> PROMPT
    INIT --> HARNESS
    CLASSIFIER --> DISPATCH
    LOOP --> DISPATCH
    LOOP --> MODELS
    LOOP --> PREPHASE
    LOOP --> HARNESS
    PREPHASE --> DISPATCH
    PREPHASE --> HARNESS
    DISPATCH --> MODELS
    DISPATCH --> HARNESS
    DISPATCH --> ANTHROPIC
    DISPATCH --> OPENROUTER
    DISPATCH --> OLLAMA
    HARNESS --> BITGN_API

    style MAIN fill:#e1f5ff
    style INIT fill:#fff4e1
    style CLASSIFIER fill:#fff4e1
    style PREPHASE fill:#fff4e1
    style LOOP fill:#fff4e1
    style PROMPT fill:#fff4e1
    style MODELS fill:#fff4e1
    style DISPATCH fill:#e1ffe1
    style HARNESS fill:#e1ffe1
    style ANTHROPIC fill:#f0f0f0
    style OPENROUTER fill:#f0f0f0
    style OLLAMA fill:#f0f0f0
    style BITGN_API fill:#f0f0f0
```

## Key Architectural Patterns

### Discovery-First Prompt
Zero hardcoded vault paths in the system prompt. The agent discovers folder roles from AGENTS.MD and vault tree pre-loaded in prephase context.

### Three-Tier LLM Fallback
`Anthropic SDK → OpenRouter → Ollama` with FIX-27 retry (4 attempts, 4s sleep) on transient errors (503/502/429).

### Adaptive Stall Detection (FIX-74)
Three task-agnostic signals:
1. Same tool+args fingerprint 3x in a row
2. Same path error 2+ times
3. 6+ steps without write/delete/move/mkdir

### Classifier Pipeline (FIX-89/97/98/99/100)
Four-stage classification:
1. Keyword-fingerprint cache lookup (FIX-97) — skip LLM on repeated patterns
2. LLM classify via classifier model (FIX-75/82/90) — one of: think / longContext / default
3. Post-prephase vault context re-class (FIX-89 rule-based, FIX-99 LLM) — upgrades type when vault is large
4. FIX-100: skip LLM re-class if classifier was unavailable during initial call

### Few-Shot Prephase Injection (FIX-102)
A generic user→assistant example pair is injected immediately after system prompt in prephase. This is the strongest signal for enforcing JSON-only output from Ollama-proxied cloud models that ignore `response_format`.

### Hardcode Fix Pattern
Each behavioral fix gets a sequential label `FIX-N` in code comments. Current counter: FIX-102.

## Components (8 total)

```toon
components[8]{id,type,path,layer}:
  main,entry_point,main.py,presentation
  agent-init,module,agent/__init__.py,business
  classifier,module,agent/classifier.py,business
  dispatch,module,agent/dispatch.py,infrastructure
  loop,module,agent/loop.py,business
  prephase,module,agent/prephase.py,business
  prompt,config,agent/prompt.py,business
  models,data_model,agent/models.py,business
```

## Dependencies (18 total)

```toon
dependency_graph:
  edges[18]{from,to,type}:
    main,agent-init,required
    main,classifier,required
    main,bitgn-harness,required
    agent-init,classifier,required
    agent-init,prephase,required
    agent-init,loop,required
    agent-init,prompt,required
    agent-init,bitgn-harness,required
    classifier,dispatch,required
    loop,dispatch,required
    loop,models,required
    loop,prephase,required
    loop,bitgn-harness,required
    prephase,dispatch,required
    prephase,bitgn-harness,required
    dispatch,models,required
    dispatch,anthropic-sdk,required
    dispatch,openrouter,optional
```
