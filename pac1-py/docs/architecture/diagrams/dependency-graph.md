# pac1-py — Component Dependency Graph

Generated: 2026-03-28

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

    subgraph External["External LLM Backends"]
        ANTHROPIC["Anthropic SDK\n(Tier 1)"]
        OPENROUTER["OpenRouter\n(Tier 2, optional)"]
        OLLAMA["Ollama\n(Tier 3, local)"]
    end

    subgraph ExternalAPI["External Services"]
        BITGN_API["api.bitgn.com\nBitGN Benchmark API"]
    end

    %% Entry-point wiring
    MAIN --> INIT
    MAIN --> CLASSIFIER
    MAIN --> HARNESS

    %% Agent init wiring
    INIT --> CLASSIFIER
    INIT --> PREPHASE
    INIT --> LOOP
    INIT --> PROMPT
    INIT --> HARNESS

    %% Classifier uses dispatch for LLM call (FIX-75/76)
    CLASSIFIER --> DISPATCH

    %% Loop wiring
    LOOP --> DISPATCH
    LOOP --> MODELS
    LOOP --> PREPHASE
    LOOP --> HARNESS

    %% Prephase wiring
    PREPHASE --> DISPATCH
    PREPHASE --> HARNESS

    %% Dispatch wiring (models + runtime + LLM tiers)
    DISPATCH --> MODELS
    DISPATCH --> HARNESS
    DISPATCH --> ANTHROPIC
    DISPATCH --> OPENROUTER
    DISPATCH --> OLLAMA

    %% External API
    HARNESS --> BITGN_API

    %% Color coding by layer
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

## Layer Legend

| Color | Layer | Description |
|-------|-------|-------------|
| Light blue | Presentation | Entry point / benchmark runner |
| Light yellow | Business | Agent logic, classifier, prompt, models |
| Light green | Infrastructure | LLM dispatch, PCM/harness clients |
| Gray | External | Third-party APIs and LLM backends |
