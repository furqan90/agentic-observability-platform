# Connecting an Agent to the  Observability Platform

> **Time to first trace: ~10 minutes.**
> This document is everything a developer needs to instrument a new agent project.
> No prior knowledge of LangFuse or OpenTelemetry required.

---

## How it works (30-second version)

Your agent emits OpenTelemetry spans and counters. They travel to a central
**OTel Collector** which fans them out in two directions:

```
Your agent
  │
  ├── spans (traces) ──OTLP──► OTel Collector :4318 ──► LangFuse  (trace / eval / cost)
  └── metrics        ──OTLP──► OTel Collector :4318 ──► Prometheus (RED metrics + cost)
                                                        └──► Grafana (dashboards + alerts)
```

The `agentic_observability_sdk` package is a thin facade over LangFuse v3 + the OTel SDK.
You import it — never `langfuse` directly. That single rule is what keeps
a backend swap from becoming a multi-repo emergency.

---

## Step 1 — Install the package

`agentic-observability-sdk` ships as a proper pip-installable package. No folder copying needed.

**Option A — from the platform repo (local/monorepo):**

```bash
pip install -e /path/to/agentic-observability-platform/agentic_observability_sdk
```

**Option B — from a Git URL (recommended for agent projects outside the monorepo):**

```bash
pip install "agentic-observability-sdk @ git+https://github.com/platform/observability-platform.git#subdirectory=agentic_observability_sdk"
```

**Option C — pin in `requirements.txt` or `pyproject.toml`:**

```
# requirements.txt
agentic-observability-sdk @ git+https://...#subdirectory=agentic_observability_sdk
```

```toml
# pyproject.toml
dependencies = [
    "agentic-observability-sdk @ git+https://...#subdirectory=agentic_observability_sdk",
]
```

---

## Step 2 — Verify the install

```bash
python -c "from agentic_observability_sdk import get_tracer, trace_agent, trace_tool, record_usage, shutdown; print('OK')"
# Expected: OK
```

The package pulls in its own dependencies (`langfuse`, `opentelemetry-sdk`,
`opentelemetry-exporter-otlp`) — no separate installs needed.

---

## Step 3 — Set environment variables on the agent host

These five variables are the entire configuration. Set them in your `.env`,
Docker Compose environment block, K8s Secret, or shell — wherever your agent
picks up its environment.

```env
# ── Auth (who you are) ─────────────────────────────────────────────────────
LANGFUSE_PUBLIC_KEY=pk-lf-local
LANGFUSE_SECRET_KEY=sk-lf-local

# ── Platform location ──────────────────────────────────────────────────────
# LANGFUSE_HOST   : the LangFuse web UI / API (used for auth handshake only)
# OTEL_EXPORTER_OTLP_ENDPOINT : where spans + metrics actually go = the Collector
LANGFUSE_HOST=http://<platform-host>:3000
OTEL_EXPORTER_OTLP_ENDPOINT=http://<platform-host>:4318

# ── Tenant tag ─────────────────────────────────────────────────────────────
# Identifies which client / environment this agent belongs to.
# Shows as a tag on every trace in LangFuse. Use a short slug.
DEPLOY_ENV=local              # examples: local  client-acme  aks-prod  teams-bot
```

### What to use for `<platform-host>`

| Where the agent runs | `<platform-host>` |
|---|---|
| Same machine as the platform | `localhost` |
| Different machine on the same network | IP or hostname of the platform machine |
| Docker container in the same Compose stack | `otel-collector` (service name) |
| Client VM phoning home | Public IP / domain of the platform ingress |

> **Important:** `LANGFUSE_HOST` and `OTEL_EXPORTER_OTLP_ENDPOINT` point at
> **different ports** for a reason. `LANGFUSE_HOST` is only used by the SDK
> to validate your API keys. `OTEL_EXPORTER_OTLP_ENDPOINT` is where spans
> actually travel — it must point at the **Collector** (`:4318`), not at
> LangFuse directly (`:3000`). Pointing spans straight at LangFuse bypasses
> the Collector's PII redaction and the `spanmetrics` fan-out that feeds Grafana.

---

## Step 4 — Instrument the agent

### 4a. Initialise once at startup

```python
import atexit
from agentic_observability_sdk import get_tracer, shutdown

get_tracer()              # registers LangFuse + OTel provider (memoised — safe to call repeatedly)
atexit.register(shutdown) # flushes buffered spans/metrics cleanly on process exit
```

Put this at the top of your entry point (`main.py`, `app.py`, FastAPI lifespan, etc.).
The `atexit` registration is especially important for CLI tools or short-lived scripts
where the process may exit before the export buffer flushes automatically.

### 4b. Wrap agent entry points with `@trace_agent`

```python
from agentic_observability_sdk import trace_agent, record_usage

@trace_agent(name="eka-query-agent", tags=["eka", "rag"])
def run_agent(user_input: str) -> str:
    results = search_knowledge_base(user_input)
    answer  = generate_response(results)

    # Record token usage + cost so they appear in the Grafana cost panel.
    # LangFuse also captures these from the span attributes automatically.
    record_usage(
        agent="eka-query-agent",
        model="gpt-4o",
        input_tokens=1200,
        output_tokens=350,
        cost_usd=0.018,
    )
    return answer
```

`@trace_agent` creates a **root trace** in LangFuse. Each call to `run_agent()`
appears as a separate trace entry in the LangFuse UI.

### 4c. Wrap tool calls with `@trace_tool`

```python
from agentic_observability_sdk import trace_tool

@trace_tool(name="vector-search")
def search_knowledge_base(query: str) -> list:
    # ... call your vector DB ...
    return results

@trace_tool(name="llm-generate")
def generate_response(context: list) -> str:
    # ... call OpenAI / Azure OAI / etc. ...
    return answer
```

`@trace_tool` creates a **child span** inside the parent trace. The nesting is
automatic — any `@trace_tool` call inside a `@trace_agent` call is linked to
the same trace in LangFuse.

### Minimum complete example

```python
# my_agent.py
from agentic_observability_sdk import get_tracer, trace_agent, trace_tool, record_usage

get_tracer()   # startup init

@trace_agent(name="my-agent", tags=["demo"])
def run(user_input: str) -> str:
    docs    = retrieve(user_input)
    answer  = generate(docs, user_input)
    record_usage("my-agent", "gpt-4o", 800, 200, 0.012)
    return answer

@trace_tool(name="retrieve")
def retrieve(query: str) -> list:
    return []   # your retrieval logic

@trace_tool(name="generate")
def generate(docs: list, query: str) -> str:
    return ""   # your LLM call

if __name__ == "__main__":
    print(run("What is the refund policy?"))
```

---

## Step 5 — Verify the connection

### Check LangFuse

1. Open **http://\<platform-host\>:3000** and log in.
2. Go to **Agent Platform → Traces**.
3. Run your agent once — a new trace should appear within a few seconds.

### Check Grafana

1. Open **http://\<platform-host\>:3001** (admin / platform).
2. Go to **Dashboards → Agents → agent-ops**.
3. The **Invocation Rate** panel should show a spike after you run the agent.

### Quick CLI check (from the agent host)

```bash
# Confirm the Collector is reachable and accepting OTLP
curl -s http://<platform-host>:4318/health/status
# Expected: {"status":"Serving"}

# Confirm LangFuse API keys work
curl -s http://<platform-host>:3000/api/public/projects \
  -H "Authorization: Basic $(echo -n 'pk-lf-local:sk-lf-local' | base64)"
# Expected: {"data":[{"id":"agents","name":"Agent Platform",...}]}
```

---

## The one rule you must not break

```python
# NEVER do this in agent code:
from langfuse import observe, Langfuse, get_client   # ❌ direct import

# ALWAYS do this:
from agentic_observability_sdk import trace_agent, trace_tool       # ✅ facade import
```

The `langfuse` package is imported in exactly **one** file in the entire
platform: `agentic_observability_sdk/tracer.py`. That is the swap point.
If  ever moves to a different tracing backend, we rewrite one file —
not every agent across every client environment. A direct `langfuse` import in
agent code is a review-blocking violation.

---

## Reference

| Item | Detail |
|---|---|
| Platform OTLP port (HTTP) | `:4318` — use this for `OTEL_EXPORTER_OTLP_ENDPOINT` |
| Platform OTLP port (gRPC) | `:4317` — alternative if your OTel SDK prefers gRPC |
| LangFuse UI + API | `:3000` — use this for `LANGFUSE_HOST` |
| Grafana | `:3001` |
| Prometheus | `:9090` |
| Default public key | `pk-lf-local` (local dev only — rotate for production) |
| Default secret key | `sk-lf-local` (local dev only — rotate for production) |
| Platform command reference | [`../COMMANDS.md`](../COMMANDS.md) |
| Architecture & design rationale | [`../observability-plan.md`](../observability-plan.md) |
| Official LangFuse v3 OTel docs | https://langfuse.com/docs/opentelemetry/introduction |
