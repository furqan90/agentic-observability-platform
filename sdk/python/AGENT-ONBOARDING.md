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
  ├── @trace_agent / @trace_tool ──OTLP──► Collector :4318 ──► PII redaction
  │                                                          ──► LangFuse  (trace / eval / cost)
  │                                                          ──► spanmetrics → Prometheus
  │
  └── record_usage() ─────────────OTLP──► Collector :4318 ──► Prometheus (cost counters)
                                                              └──► Grafana (dashboards + alerts)
```

**The Collector is the only path out.** This is not optional — sending traces directly
to LangFuse bypasses PII redaction and the spanmetrics connector that feeds Grafana.
The SDK enforces this when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (see Step 3).

The `agentic_observability_sdk` package is a thin facade over LangFuse v3 + the OTel SDK.
You import it — never `langfuse` directly. That single rule is what keeps
a backend swap from becoming a multi-repo emergency.

---

## Step 1 — Install the package

`agentic-observability-sdk` ships as a proper pip-installable package. No folder copying needed.

> **Python version requirement:** Python **3.10+** (including 3.14).
> A virtual environment is recommended to isolate dependencies:
> ```bash
> python -m venv .venv
> source .venv/bin/activate        # Linux / macOS
> .venv\Scripts\Activate.ps1      # Windows PowerShell
> ```

**Option A — from the platform repo (local/monorepo):**

```bash
pip install -e /path/to/agentic-observability-platform/sdk/python
```

**Option B — from a Git URL (recommended for agent projects outside the monorepo):**

```bash
pip install "agentic-observability-sdk @ git+https://github.com/furqan90/agentic_observability_platform.git#subdirectory=sdk/python"
```

**Option C — pin in `requirements.txt` or `pyproject.toml`:**

```
# requirements.txt
agentic-observability-sdk @ git+https://github.com/furqan90/agentic_observability_platform.git#subdirectory=sdk/python
```

```toml
# pyproject.toml
dependencies = [
    "agentic-observability-sdk @ git+https://github.com/furqan90/agentic_observability_platform.git#subdirectory=sdk/python",
]
```

**Upgrading an existing install:**

pip caches git installs by commit hash, so `--upgrade` alone won't pull new commits.
Use `--force-reinstall`:

```bash
pip install --force-reinstall \
  "agentic-observability-sdk @ git+https://github.com/furqan90/agentic_observability_platform.git#subdirectory=sdk/python"
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

> **`LANGFUSE_HOST` vs `OTEL_EXPORTER_OTLP_ENDPOINT` — they do different things:**
>
> - `LANGFUSE_HOST` (`:3000`) — used by the LangFuse SDK for REST calls only
>   (scoring, dataset management, eval). Not the trace export path.
> - `OTEL_EXPORTER_OTLP_ENDPOINT` (`:4318`) — this is where spans and metrics
>   actually travel. Must point at the **Collector**, not LangFuse directly.
>
> **What `get_tracer()` does with these vars:**
> When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, `get_tracer()` configures a global
> OTel `TracerProvider` pointing at the Collector *before* initialising LangFuse.
> LangFuse v3 detects the existing provider and uses it — meaning every
> `@trace_agent` span travels Agent → Collector → PII redaction → LangFuse.
> When `OTEL_EXPORTER_OTLP_ENDPOINT` is **not** set (local dev without a Collector),
> `get_tracer()` falls back to a direct LangFuse connection — traces arrive but
> PII redaction and spanmetrics are skipped. Acceptable for dev; never for production.

---

## Step 4 — Instrument the agent

### 4a. Initialise once at startup

```python
import atexit
from agentic_observability_sdk import get_tracer, shutdown

get_tracer()               # one call wires everything — see below
atexit.register(shutdown)  # flushes all buffered spans/metrics on process exit
```

**What `get_tracer()` does in order:**

1. Reads `OTEL_EXPORTER_OTLP_ENDPOINT` — if set, builds an OTLPSpanExporter
   pointing at the Collector (`/v1/traces`) and passes it directly to `Langfuse()`.
   LangFuse uses this exporter instead of its default direct-to-LangFuse one,
   so every span travels Agent → Collector → PII redaction → LangFuse.
2. Calls `Langfuse(span_exporter=..., host=LANGFUSE_HOST)` — initialises LangFuse
   and sets the global OTel `TracerProvider` with the Collector-bound exporter.
3. Calls `init_metrics()` — sets up the OTel `MeterProvider` for `record_usage()`
   counters, also pointing at the Collector.

Put this at the top of your entry point (`main.py`, `app.py`, FastAPI lifespan, etc.).
The `atexit` registration is critical for CLI tools or short-lived scripts — without it,
spans buffered in the `BatchSpanProcessor` may not flush before the process exits.

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

### 4d. Capture LangChain LLM calls (prompts + responses)

`@trace_agent` and `@trace_tool` record a function's inputs and return value.
They do **not** automatically record what happens *inside* the function — so
`llm.invoke(messages)` calls are invisible: LangFuse never sees the exact
system prompt, the human message, tool call rounds, or the raw response.

Fix: pass `get_langchain_handler()` as a LangChain callback. It hooks into
LangChain's callback bus and records every LLM round-trip as a child
observation nested inside the current span, including:
- exact messages array (system, human, tool messages)
- raw assistant response and tool calls
- token counts per call

```python
from agentic_observability_sdk import trace_agent, get_langchain_handler

@trace_agent(name="discover-models", tags=["rag"])
def run_discover_models_node(profile, spec):
    messages = [system_prompt, human_message]

    # Without this, the LLM call is invisible in LangFuse:
    # result = llm_with_tools.invoke(messages)

    # With the handler — prompts + response recorded as child observations:
    handler = get_langchain_handler()
    result = llm_with_tools.invoke(messages, config={"callbacks": [handler]})
    ...
```

**Or bind once on the model** so every call is captured automatically:

```python
from agentic_observability_sdk import get_langchain_handler

# At model construction time:
handler = get_langchain_handler()
llm_with_tools = ChatOpenAI(model="gpt-4o").with_config({"callbacks": [handler]})

# Now every invoke() is captured — no per-call changes needed.
result = llm_with_tools.invoke(messages)
```

> `get_langchain_handler()` must be called **after** `get_tracer()`. When
> called inside a `@trace_agent` span it returns a context-aware handler that
> automatically nests the LLM observation under that trace. Outside a trace
> it returns a bare handler that records nothing rather than raising.

Requires the full `langchain` package (not just `langchain-core`):
```bash
pip install "agentic-observability-sdk[langchain]"
```

---

### 4e. Capture OpenAI SDK calls (direct, non-LangChain)

If your agent calls OpenAI directly, use `get_openai_client()` as a drop-in
replacement for `openai.OpenAI()`. Every `client.chat.completions.create()`
is automatically recorded — no per-call changes:

```python
from agentic_observability_sdk import get_openai_client

# Replaces: from openai import OpenAI; client = OpenAI()
client = get_openai_client()   # or get_openai_client(api_key="...", base_url="...")

@trace_agent(name="my-agent")
def run(messages: list) -> str:
    response = client.chat.completions.create(model="gpt-4o", messages=messages)
    return response.choices[0].message.content
```

Requires the openai package:
```bash
pip install "agentic-observability-sdk[openai]"
```

---

### 4f. Anthropic SDK calls (direct, non-LangChain)

LangFuse v3 does not ship a `langfuse.anthropic` wrapper. The official supported
path is **OpenInference's `AnthropicInstrumentor`**, which monkey-patches the
`anthropic` SDK at process level — no per-call changes needed.

`get_anthropic_client()` handles this automatically:

```python
from agentic_observability_sdk import get_anthropic_client

client = get_anthropic_client()   # instruments the anthropic SDK, returns Anthropic()

@trace_agent(name="my-agent")
def run(messages: list) -> str:
    # Every call below is auto-recorded — messages, model, tokens, stop reason
    return client.messages.create(
        model="claude-sonnet-4-6", messages=messages, max_tokens=1024
    ).content[0].text
```

```bash
pip install "agentic-observability-sdk[anthropic]"
```

**Alternative — OpenAI-compatible endpoint (no extra install):**

Anthropic natively supports the OpenAI API surface, so `get_openai_client()`
works for Claude models if you point it at Anthropic's endpoint:

```python
import os
from agentic_observability_sdk import get_openai_client

client = get_openai_client(
    api_key=os.environ["ANTHROPIC_API_KEY"],
    base_url="https://api.anthropic.com/v1",
)

@trace_agent(name="my-agent")
def run(messages: list) -> str:
    return client.chat.completions.create(
        model="claude-sonnet-4-6", messages=messages
    ).choices[0].message.content
```

**When to use which path:**

| Path | Install | Claude-specific features | Recommendation |
|---|---|---|---|
| `get_anthropic_client()` | `[anthropic]` | ✅ Full native SDK | Default choice |
| `get_openai_client(base_url=...)` | `[openai]` | ❌ OpenAI surface only | Only if already using OpenAI client and want zero extra deps |

---

### Framework integration summary

| Your agent uses | Function to add | Install extra |
|---|---|---|
| **LangChain / LangGraph** | `get_langchain_handler()` → pass as callback | `[langchain]` |
| **OpenAI SDK directly** | `get_openai_client()` → replaces `openai.OpenAI()` | `[openai]` |
| **Anthropic SDK directly** | `get_anthropic_client()` → instruments native SDK via OpenInference | `[anthropic]` |
| **Any other framework** | `@trace_tool` with messages as args | — |

All three are optional — the core SDK (`@trace_agent`, `@trace_tool`, `record_usage`)
works without any of them installed.

---

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

## Step 5 — Verify the Collector path is active

Before checking LangFuse and Grafana, confirm spans are routing through the
Collector and not going direct. This is the most important check.

### Confirm the Collector is reachable

```bash
curl -s http://<platform-host>:4318/health/status
# Expected: {"status":"Serving"}
```

### Confirm the trace path in Python

Run this snippet in your agent's environment. It prints which path `get_tracer()`
configured without needing to send a real trace:

```python
import os
os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-local"
os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-local"
os.environ["LANGFUSE_HOST"]       = "http://<platform-host>:3000"
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://<platform-host>:4318"

from agentic_observability_sdk import get_tracer
from opentelemetry import trace as otel_trace

get_tracer()

provider = otel_trace.get_tracer_provider()
print("TracerProvider:", type(provider).__name__)
# Expected: TracerProvider  (the OTel SDK class — Collector path is active)
# Wrong:    ProxyTracerProvider  (OTEL_EXPORTER_OTLP_ENDPOINT was not set)
```

### Send a test trace (use the platform script)

From the platform repo root:

```bash
# Set env vars, then run:
python scripts/send_test_trace.py
```

This sends a synthetic agent trace and checks that it arrives in both LangFuse
and Prometheus. See the script for full output and manual verification steps.

---

## Step 6 — Verify the connection

### Check LangFuse

1. Open **http://\<platform-host\>:3000** and log in.
2. Go to **Agent Platform → Traces**.
3. Run your agent once — a new trace should appear within a few seconds.

### Check Prometheus

```bash
# spanmetrics — rate of agent/tool invocations (auto-generated from every span)
curl -s "http://<platform-host>:9090/api/v1/query?query=traces_span_metrics_calls_total" \
  | python3 -m json.tool | grep span_name

# cost/token counters — only present if record_usage() was called
curl -s "http://<platform-host>:9090/api/v1/query?query=agent_tokens_total" \
  | python3 -m json.tool | grep agent_name
```

Or browse: `http://<platform-host>:9090/graph?g0.expr=traces_span_metrics_calls_total`

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
