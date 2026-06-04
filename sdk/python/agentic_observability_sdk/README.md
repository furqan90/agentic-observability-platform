#  Observability SDK

Instrumentation facade for all  agent projects.
**Agents import this — never `langfuse` directly.**

> **New to this?** Start with **[AGENT-ONBOARDING.md](AGENT-ONBOARDING.md)** —
> it covers everything from copying the folder to verifying your first trace
> appears in LangFuse and Grafana.

---

## Quick reference

### Install

```bash
pip install -e ./sdk
# or: pip install "langfuse>=3.0,<4.0" opentelemetry-sdk opentelemetry-exporter-otlp
```

### Five env vars (agent host)

```env
LANGFUSE_PUBLIC_KEY=pk-lf-local
LANGFUSE_SECRET_KEY=sk-lf-local
LANGFUSE_HOST=http://<platform-host>:3000
OTEL_EXPORTER_OTLP_ENDPOINT=http://<platform-host>:4318
DEPLOY_ENV=local
```

### Minimum viable trace

```python
from sdk.python import get_tracer, trace_agent, trace_tool, record_usage

get_tracer()  # once at startup

@trace_agent(name="my-agent", tags=["demo"])
def run(user_input: str) -> str:
    result = search(user_input)
    record_usage("my-agent", "gpt-4o", 800, 200, 0.012)
    return result

@trace_tool(name="search")
def search(q: str) -> str:
    ...
```

### What goes where

| You want to see… | Go to |
|---|---|
| Trace tree, cost, eval scores, token I/O | LangFuse `:3000` |
| Agent rate / error / latency next to infra | Grafana `:3001` |
| To change the tracing backend | Edit `python/tracer.py` only |

---

Full details, troubleshooting, and the "one rule you must not break" →
**[AGENT-ONBOARDING.md](AGENT-ONBOARDING.md)**
