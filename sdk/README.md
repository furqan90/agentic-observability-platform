# Observability SDK

Language-specific instrumentation facades for the Agentic Observability Platform.
All SDKs implement the same [CONTRACT.md](CONTRACT.md) — metadata envelope, span naming,
OTel attribute names, and required public API — so dashboards, cost rollups, and alert
routing work identically regardless of the language an agent is written in.

## Available SDKs

| Language | Package | Install | Status |
|---|---|---|---|
| **Python** | `agentic-observability-sdk` | `pip install "agentic-observability-sdk @ git+https://github.com/furqan90/agentic_observability_platform.git#subdirectory=sdk/python"` | ✅ Available |
| **TypeScript / Node** | `@agentic/observability-sdk` | — | 🔲 Planned |

## Quick start (Python)

```python
import atexit
from agentic_observability_sdk import get_tracer, trace_agent, trace_tool, record_usage, shutdown

get_tracer()               # once at startup
atexit.register(shutdown)

@trace_agent(name="my-agent", tags=["demo"])
def run(user_input: str) -> str:
    result = search(user_input)
    record_usage("my-agent", "gpt-4o", 800, 200, 0.012)
    return result
```

Full guide → [python/AGENT-ONBOARDING.md](python/AGENT-ONBOARDING.md)

## The one rule

Agent code imports only from the SDK facade — never from `langfuse`, `openai`,
`anthropic`, or OTel primitives directly. The SDK is the swap point. If ever changes backends, one package is rewritten, not N agents across M client environments.

## Adding a new language SDK

1. Read [CONTRACT.md](CONTRACT.md) — implement everything in it.
2. Create `sdk/<language>/` as a peer of `sdk/python/`.
3. Name the package `agentic-observability-sdk-<language>` on the relevant registry.
4. Write an `AGENT-ONBOARDING.md` following the same structure as the Python one.
5. Run the checklist in `CONTRACT.md §8` before shipping.
