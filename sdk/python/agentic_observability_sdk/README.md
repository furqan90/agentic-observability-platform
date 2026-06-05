#  Observability SDK

Instrumentation facade for all  agent projects.
**Agents import this — never `langfuse` directly.**

> **New to this?** Start with **[AGENT-ONBOARDING.md](AGENT-ONBOARDING.md)** —
> it covers everything from install to verifying your first trace in LangFuse and Grafana.

---

## Quick reference

### Install

```bash
# From the platform repo (local / monorepo)
pip install -e ./agentic_observability_sdk

# From GitHub (agent projects outside the monorepo)
pip install "agentic-observability-sdk @ git+https://github.com/furqan90/agentic_observability_platform.git#subdirectory=agentic_observability_sdk"
```

### Five env vars (set on every agent host)

```env
LANGFUSE_PUBLIC_KEY=pk-lf-local
LANGFUSE_SECRET_KEY=sk-lf-local
LANGFUSE_HOST=http://<platform-host>:3000
OTEL_EXPORTER_OTLP_ENDPOINT=http://<platform-host>:4318
DEPLOY_ENV=local
```

### Minimum viable trace

```python
import atexit
from agentic_observability_sdk import get_tracer, trace_agent, trace_tool, record_usage, shutdown

get_tracer()               # once at startup
atexit.register(shutdown)  # flush on exit

@trace_agent(name="my-agent", tags=["demo"])
def run(user_input: str) -> str:
    result = search(user_input)
    record_usage("my-agent", "gpt-4o", 800, 200, 0.012)
    return result

@trace_tool(name="search")
def search(q: str) -> str:
    ...
```

### Making LLM calls visible (prompts + responses)

`@trace_agent` records function args/return — not internal LLM calls. Use the
framework-specific helper so prompts and responses become LangFuse observations:

```python
# LangChain / LangGraph
from agentic_observability_sdk import get_langchain_handler
handler = get_langchain_handler()
result = llm.invoke(messages, config={"callbacks": [handler]})

# OpenAI SDK (drop-in replacement for openai.OpenAI())
from agentic_observability_sdk import get_openai_client
client = get_openai_client()
client.chat.completions.create(model="gpt-4o", messages=messages)

# Anthropic SDK — via OpenInference auto-instrumentation (full native SDK)
from agentic_observability_sdk import get_anthropic_client
client = get_anthropic_client()   # instruments anthropic at process level
client.messages.create(model="claude-sonnet-4-6", messages=messages, max_tokens=1024)

# -- OR -- via OpenAI-compatible endpoint (no extra install, limited surface)
client = get_openai_client(api_key=os.environ["ANTHROPIC_API_KEY"],
                           base_url="https://api.anthropic.com/v1")
client.chat.completions.create(model="claude-sonnet-4-6", messages=messages)
```

Install only what your agent uses:
```bash
pip install "agentic-observability-sdk[openai]"      # OpenAI
pip install "agentic-observability-sdk[anthropic]"   # Anthropic
pip install "agentic-observability-sdk[langchain]"   # LangChain/LangGraph
pip install "agentic-observability-sdk[all]"         # everything
```

### What goes where

| You want to see… | Go to |
|---|---|
| Trace tree, prompts, responses, cost, eval scores | LangFuse `:3000` |
| Agent rate / error / latency next to infra | Grafana `:3001` |
| To change the tracing backend | Edit `tracer.py` only |

---

Full details, troubleshooting, and the "one rule you must not break" →
**[AGENT-ONBOARDING.md](AGENT-ONBOARDING.md)**
