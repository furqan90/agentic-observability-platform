# Agentic Observability Platform — Implementation Plan

> This document is a Claude Code scaffold brief. Use it as the authoritative reference to generate the full project structure, configuration files, and instrumentation code.
>
> **Revision 2 (2026-06-03):** Corrected to LangFuse **v3** architecture, added an **OpenTelemetry Collector** as the metrics path, fixed the Python SDK to the v3 API, and added the previously-missing networking, security, retention, and headless-bootstrap sections.

---

## Context

**Organization:** Inc. — technology consulting & outsourced software development
**Owner:** Furqan (Principal Software Developer)
**Purpose:** Internal observability platform for all agentic AI workloads — local-first, cloud-ready (Azure)
**Philosophy:** Agents run anywhere (AKS, a Teams bot, a client VM); traces always phone home to this platform — and **client data never leaves a trace boundary we don't control**.

---

## Architecture Overview

```
                         Observability Platform
┌───────────────────────────────────────────────────────────────────────┐
│                                                                         │
│   OTel Collector ──traces(OTLP)──► LangFuse-web ──► PostgreSQL (txn)    │
│        │                              │   │         ClickHouse (traces) │
│        │                              │   └───────► Redis (queue/cache) │
│        │                          LangFuse-worker ─► S3/MinIO (events)  │
│        │                                                                 │
│        └──metrics(spanmetrics)──► Prometheus ──► Grafana (dashboards)   │
│                                                                         │
│   Nginx / Ingress (TLS termination, per-client token auth)             │
└───────────────────────────────────────────────────────────────────────┘
            ▲                    ▲                     ▲
      Agent on AKS         Agent in Teams        Agent on client VM
      (platform)          bot (client)          (egress over mTLS)
            │                    │                     │
            └──── OpenTelemetry SDK (traces + metrics) ┘
```

**Key flow:** every agent emits OpenTelemetry. The Collector fans out — spans go to LangFuse over OTLP for tracing/eval/cost, and a `spanmetrics` connector derives RED metrics (Rate, Errors, Duration) that Prometheus scrapes and Grafana visualizes.

### Division of labor: LangFuse dashboards vs. Grafana

These two are **not** redundant — they answer different questions, and the plan assigns each its proper job:

| Question | Tool | Why |
|----------|------|-----|
| *Why did this agent give a bad answer? What did it cost? How are eval scores trending?* | **LangFuse** (built-in dashboards) | LangFuse natively visualizes traces, cost, token usage, latency, and eval scores dimensioned by model/user/session/prompt-version. For LLM-specific analytics it is the better tool — use it, don't rebuild it in Grafana. |
| *Is the agent slow because of the model or the box it runs on? Page DevOps if error rate spikes. How did RED look over the last year?* | **Grafana + Prometheus** | LangFuse only knows about traces — it has no view of pod memory, DB pool exhaustion, or client-VM CPU. Grafana sits over **both** agent RED metrics *and* infra exporters, giving one pane for agent↔infra correlation, mature Alertmanager-based paging, cheap long-horizon retention, and metrics that stay accurate even when traces are sampled. |

> **Do not** treat the Grafana agent panels as a replacement for LangFuse's LLM analytics. Their purpose is **operational** — correlating agents with the infrastructure they run on (especially on client environments) and alerting. The four baseline panels exist so agents appear in the *same* DevOps pane as infra; LangFuse remains the home for cost/eval/trace deep-dives. (LangFuse does not export Prometheus metrics natively, which is why the spanmetrics connector exists.)

---

## Portability & Vendor Strategy

The platform is deliberately structured so that **switching tracing/eval backends never requires touching agent code or client deployments.** Lock-in is most expensive at the *instrumentation* layer — the span-producing code scattered across every agent on every client site. We isolate that layer behind two swap points.

### Two hard constraints (treat as architectural rules, not conventions)

1. **Agents import the `sdk/` facade — never `langfuse` directly.**
   All agent code uses `from sdk.python import trace_agent, trace_tool, record_usage`. The `langfuse` import lives in exactly one place: `sdk/python/tracer.py`. If we ever leave LangFuse, we rewrite **one package**, not N agents across M client environments. A `langfuse` import anywhere outside `sdk/` is a review-blocking violation.

2. **All telemetry egresses through the OTel Collector — agents never point at a backend directly.**
   Even though LangFuse v3 could accept OTLP straight from agents, routing through the Collector gives us a vendor-neutral swap point *at the wire*. Changing backends is a Collector exporter config change, not a redeploy.

### Why this works (and where it doesn't)

LangFuse v3 is itself **built on OpenTelemetry** — its SDK produces OTel spans and its `/api/public/otel` endpoint ingests OTLP. So using the LangFuse SDK is *not* the deep lock-in it was in v2; under the hood it's standard OTel plus GenAI-specific semantic conventions (token usage, cost, structured prompt/completion I/O).

| Layer | Portable? | Notes |
|-------|-----------|-------|
| Trace pipeline (spans, RED metrics) | ✅ Yes | Pure OTLP; swap backend via Collector exporter |
| Agent instrumentation | ✅ Yes | Hidden behind `sdk/` facade — one package to rewrite |
| GenAI semantics (cost, token I/O) | ⚠️ Partial | LangFuse conventions; a generic OTel backend won't fully interpret them |
| **Eval / prompt-mgmt / scoring / datasets** | ❌ No | Proprietary to LangFuse; **not** part of the OTel spec. This is where genuine stickiness remains. |

**Honest bottom line:** the "agnostic" guarantee covers the **trace pipeline and ops metrics**, not the **evaluation workflow**. Whichever platform we lean on for evals (not just tracing) is where real switching cost lives — go in with eyes open. The Collector also enables **dual-export** (run LangFuse and a candidate replacement side-by-side during any future migration) and **central PII redaction** before traces reach *any* backend.

---

## Stack

| Component        | Technology              | Purpose                                         |
|------------------|-------------------------|-------------------------------------------------|
| Tracing & Eval   | LangFuse v3 (OSS)       | Core observability platform (web + worker)      |
| Trace storage    | ClickHouse 24           | OLAP store for traces/observations/scores (v3)  |
| Transactional DB | PostgreSQL 15           | LangFuse metadata, users, projects, datasets    |
| Cache / Queue    | Redis 7 (or Valkey)     | LangFuse async queue + cache                     |
| Blob store       | MinIO (local) / Azure Blob | Event uploads, multimodal, batch exports     |
| Telemetry intake | OpenTelemetry Collector | Fan-out: traces → LangFuse, metrics → Prometheus|
| Metrics store    | Prometheus              | Time-series metrics (from spanmetrics connector) |
| Dashboards       | Grafana                 | Visualization and ops monitoring                |
| Reverse proxy    | Nginx / Ingress         | Entry point, TLS termination, per-client auth   |

> **Why v3 matters:** the old single-container `langfuse/langfuse` image is deprecated. v3 splits into `langfuse-web` + `langfuse-worker` and *requires* ClickHouse and an S3-compatible blob store. A compose file built on the v2 assumption will not boot.

---

## Project Structure to Scaffold

```
observability/
├── README.md
├── .env.example
├── .gitignore
├── .dockerignore
│
├── docker-compose/
│   ├── docker-compose.yml          # Stage 1: local dev (full v3 stack)
│   └── docker-compose.override.yml # Local overrides (exposed ports, dev volumes)
│
├── k8s/                            # Stage 2: Kubernetes (local → AKS)
│   ├── namespace.yaml
│   ├── postgres/        { deployment, service, pvc }.yaml
│   ├── clickhouse/      { statefulset, service, pvc }.yaml
│   ├── redis/           { deployment, service }.yaml
│   ├── minio/           { deployment, service, pvc }.yaml
│   ├── langfuse-web/    { deployment, service, secret }.yaml
│   ├── langfuse-worker/ { deployment, secret }.yaml
│   ├── otel-collector/  { deployment, service, configmap }.yaml
│   ├── prometheus/      { deployment, service, configmap, pvc }.yaml
│   ├── grafana/         { deployment, service, pvc }.yaml
│   └── ingress.yaml                # TLS + per-client routing
│
├── otel/
│   └── otel-collector-config.yaml  # Receivers, spanmetrics connector, exporters
│
├── prometheus/
│   └── prometheus.yml              # Scrape config (scrapes the OTel Collector)
│
├── grafana/
│   ├── provisioning/
│   │   ├── datasources/prometheus.yaml
│   │   └── dashboards/dashboard.yaml
│   └── dashboards/
│       └── agent-ops.json          # Pre-built agent ops dashboard
│
├── nginx/
│   └── nginx.conf                  # Local TLS termination + upstreams
│
├── sdk/                            # Reusable instrumentation wrappers
│   ├── python/
│   │   ├── __init__.py
│   │   ├── tracer.py               # LangFuse v3 client (singleton) + OTel setup
│   │   ├── decorators.py           # @trace_agent, @trace_tool (v3 observe API)
│   │   └── metrics.py              # Token/cost OTel counters
│   ├── pyproject.toml
│   └── README.md
│
└── scripts/
    ├── setup.sh / setup.ps1        # One-command local setup (bash + PowerShell)
    ├── gen-secrets.ps1             # Generate NEXTAUTH_SECRET, SALT, ENCRYPTION_KEY
    ├── health-check.sh / .ps1      # Verify all services are running
    └── seed-eval-dataset.py        # Seed LangFuse with baseline eval examples
```

> **Windows note:** the platform owner runs Windows 11 / PowerShell. Every `.sh` script ships with a `.ps1` twin so Day-1 setup does not require WSL.

---

## Stage 1: Docker Compose Configuration

### `.env.example`

```env
# ── LangFuse core ────────────────────────────────────────────────
# Generate with scripts/gen-secrets.ps1 (do NOT ship these defaults)
NEXTAUTH_URL=http://localhost:3000
NEXTAUTH_SECRET=change-me-32+chars
SALT=change-me-salt
ENCRYPTION_KEY=change-me-64-hex-chars   # openssl rand -hex 32  (REQUIRED in v3)

# ── LangFuse headless bootstrap (creates org/project/keys on first boot) ──
# Lets agents trace on Day 2 without manually clicking through the UI first.
LANGFUSE_INIT_ORG_ID=platform
LANGFUSE_INIT_ORG_NAME=LANGFUSE_INIT_PROJECT_ID=agents
LANGFUSE_INIT_PROJECT_NAME=Agent Platform
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-local
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-local
LANGFUSE_INIT_USER_EMAIL=furqan@example.com
LANGFUSE_INIT_USER_NAME=Furqan
LANGFUSE_INIT_USER_PASSWORD=change-me

# ── PostgreSQL (transactional) ───────────────────────────────────
POSTGRES_USER=langfuse
POSTGRES_PASSWORD=langfuse
POSTGRES_DB=langfuse
DATABASE_URL=postgresql://langfuse:langfuse@postgres:5432/langfuse

# ── ClickHouse (trace storage, v3) ───────────────────────────────
CLICKHOUSE_USER=clickhouse
CLICKHOUSE_PASSWORD=clickhouse
CLICKHOUSE_URL=http://clickhouse:8123
CLICKHOUSE_MIGRATION_URL=clickhouse://clickhouse:9000

# ── Redis ────────────────────────────────────────────────────────
REDIS_HOST=redis
REDIS_PORT=6379

# ── S3 / MinIO (event + media blob storage, v3) ──────────────────
LANGFUSE_S3_EVENT_UPLOAD_BUCKET=langfuse
LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT=http://minio:9000
LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID=minio
LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY=miniosecret
LANGFUSE_S3_EVENT_UPLOAD_REGION=us-east-1
LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE=true

# ── Grafana ──────────────────────────────────────────────────────
GRAFANA_ADMIN_PASSWORD=platform
```

### `docker-compose/docker-compose.yml`

```yaml
services:
  postgres:
    image: postgres:15
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 10s
      timeout: 5s
      retries: 5

  clickhouse:
    image: clickhouse/clickhouse-server:24
    restart: unless-stopped
    environment:
      CLICKHOUSE_USER: ${CLICKHOUSE_USER}
      CLICKHOUSE_PASSWORD: ${CLICKHOUSE_PASSWORD}
    volumes:
      - clickhouse_data:/var/lib/clickhouse
    ulimits:
      nofile: { soft: 262144, hard: 262144 }
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://localhost:8123/ping"]
      interval: 10s
      timeout: 5s
      retries: 10

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: ["redis-server", "--maxmemory-policy", "noeviction"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5

  minio:
    image: minio/minio:latest
    restart: unless-stopped
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID}
      MINIO_ROOT_PASSWORD: ${LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY}
    volumes:
      - minio_data:/data
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 10s
      timeout: 5s
      retries: 5

  # One-shot: create the langfuse bucket, then exit.
  minio-init:
    image: minio/mc:latest
    depends_on:
      minio:
        condition: service_healthy
    entrypoint: >
      /bin/sh -c "
      mc alias set local http://minio:9000 ${LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID} ${LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY} &&
      mc mb -p local/${LANGFUSE_S3_EVENT_UPLOAD_BUCKET} &&
      echo 'bucket ready'"

  langfuse-worker:
    image: langfuse/langfuse-worker:3
    restart: unless-stopped
    depends_on:
      postgres:    { condition: service_healthy }
      clickhouse:  { condition: service_healthy }
      redis:       { condition: service_healthy }
      minio:       { condition: service_healthy }
    env_file: [../.env]
    environment:
      REDIS_HOST: ${REDIS_HOST}
      REDIS_PORT: ${REDIS_PORT}

  langfuse-web:
    image: langfuse/langfuse:3
    restart: unless-stopped
    depends_on:
      postgres:    { condition: service_healthy }
      clickhouse:  { condition: service_healthy }
      redis:       { condition: service_healthy }
      minio:       { condition: service_healthy }
    ports:
      - "3000:3000"
    env_file: [../.env]
    environment:
      REDIS_HOST: ${REDIS_HOST}
      REDIS_PORT: ${REDIS_PORT}

  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    restart: unless-stopped
    depends_on:
      - langfuse-web
    volumes:
      - ../otel/otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml
    ports:
      - "4317:4317"   # OTLP gRPC (agents send here)
      - "4318:4318"   # OTLP HTTP
      - "8889:8889"   # Prometheus exporter (scraped by Prometheus)

  prometheus:
    image: prom/prometheus:latest
    restart: unless-stopped
    volumes:
      - ../prometheus/prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    ports:
      - "9090:9090"
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.retention.time=30d"

  grafana:
    image: grafana/grafana:latest
    restart: unless-stopped
    depends_on:
      - prometheus
    ports:
      - "3001:3000"
    volumes:
      - grafana_data:/var/lib/grafana
      - ../grafana/provisioning:/etc/grafana/provisioning
      - ../grafana/dashboards:/var/lib/grafana/dashboards
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD}
      GF_USERS_ALLOW_SIGN_UP: "false"

volumes:
  postgres_data:
  clickhouse_data:
  minio_data:
  prometheus_data:
  grafana_data:
```

> Note: the obsolete top-level `version:` key has been dropped (Compose v2 ignores it). Internal service ports (Postgres 5432, ClickHouse, Redis, MinIO) are **not** published to the host by default — add them in `docker-compose.override.yml` only when you need direct local access.

---

## OpenTelemetry Collector Config

### `otel/otel-collector-config.yaml`

This is the heart of the metrics fix. The `spanmetrics` connector turns incoming spans into RED metrics with **real, stable metric names**. Note: Collector v0.153.0+ prefixes these `traces_span_metrics_calls_total` / `traces_span_metrics_duration_milliseconds_bucket` (not the bare `calls_total` / `duration_milliseconds_bucket` older docs may reference) — verify the actual name in your deployed Collector version via `/metrics` on :8889 before wiring dashboards or alerts to it.

```yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318 }

connectors:
  spanmetrics:
    histogram:
      explicit:
        buckets: [50ms, 100ms, 250ms, 500ms, 1s, 2s, 5s, 10s, 30s]
    dimensions:
      - name: agent.name
      - name: gen_ai.request.model
    metrics_flush_interval: 15s

processors:
  batch: {}
  resource:
    attributes:
      - { key: deployment.platform, value: platform, action: upsert }

exporters:
  # Traces → LangFuse via its native OTLP endpoint.
  otlphttp/langfuse:
    endpoint: http://langfuse-web:3000/api/public/otel
    headers:
      # base64("public_key:secret_key") — inject via env in real config
      Authorization: "Basic ${env:LANGFUSE_OTEL_AUTH}"

  # Metrics → Prometheus scrape endpoint on :8889
  prometheus:
    endpoint: 0.0.0.0:8889
    enable_open_metrics: true

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [resource, batch]
      exporters: [otlphttp/langfuse, spanmetrics]
    metrics:
      receivers: [spanmetrics]
      processors: [batch]
      exporters: [prometheus]
```

> `LANGFUSE_OTEL_AUTH` = `base64(PUBLIC_KEY:SECRET_KEY)`. Generate once and add to the Collector's environment.

---

## Stage 2: Kubernetes Manifests

### `k8s/namespace.yaml`

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: observability
```

### Key patterns for all deployments

- Namespace: `observability`
- ClickHouse runs as a **StatefulSet** (stable identity + per-pod PVC); stateless apps run as Deployments
- All secrets via `Secret` objects (or Azure Key Vault CSI on AKS) — never ConfigMap
- All persistent data via `PersistentVolumeClaim` (Postgres, ClickHouse, MinIO, Prometheus, Grafana)
- Health checks on all deployments (`livenessProbe`, `readinessProbe`)
- Resource requests and limits set on all containers (ClickHouse especially needs generous memory)
- Services use `ClusterIP` internally; expose via `Ingress` (TLS) — NodePort only for quick local poking

### Services and ports

| Service          | Container Port | NodePort (local) |
|------------------|---------------|------------------|
| LangFuse web     | 3000          | 30000            |
| Grafana          | 3000          | 30001            |
| Prometheus       | 9090          | 30090            |
| OTel Collector   | 4317 / 4318   | 30317 / 30318    |
| PostgreSQL       | 5432          | —                |
| ClickHouse       | 8123 / 9000   | —                |
| Redis            | 6379          | —                |
| MinIO            | 9000 / 9001   | —                |

---

## Prometheus Scrape Config

### `prometheus/prometheus.yml`

Prometheus scrapes the **OTel Collector** for agent RED metrics **and infrastructure exporters** for the host/DB side — the combination is what enables agent↔infra correlation in Grafana. Agent metrics alone would just duplicate LangFuse.

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  # Agent RED metrics (rate/errors/duration) from the spanmetrics connector
  - job_name: "otel-collector"
    static_configs:
      - targets: ["otel-collector:8889"]

  # ── Infra side: this is what LangFuse cannot see ──
  - job_name: "node"            # host CPU/mem/disk/net (node_exporter)
    static_configs:
      - targets: ["node-exporter:9100"]

  - job_name: "cadvisor"        # per-container resource usage
    static_configs:
      - targets: ["cadvisor:8080"]

  - job_name: "postgres"        # connection pool, query latency (postgres_exporter)
    static_configs:
      - targets: ["postgres-exporter:9187"]

  - job_name: "prometheus"
    static_configs:
      - targets: ["localhost:9090"]
```

> Add `node-exporter`, `cadvisor`, and `postgres-exporter` as compose services / K8s DaemonSet+Deployments. On AKS these are often replaced by **Azure Monitor managed Prometheus** scraping the same targets. Redis and ClickHouse exporters can be added the same way as volume grows.

---

## Grafana Provisioning

### `grafana/provisioning/datasources/prometheus.yaml`

```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: false
```

### `grafana/provisioning/dashboards/dashboard.yaml`

```yaml
apiVersion: 1
providers:
  - name: "Agent Ops"
    folder: "Agents"
    type: file
    options:
      path: /var/lib/grafana/dashboards
```

### Agent Ops Dashboard panels (seed in `grafana/dashboards/agent-ops.json`)

This dashboard is the **operational** view — its reason to exist is putting agent RED metrics *next to the infrastructure they run on*, so DevOps can answer "model or box?" in one pane and alert on it. For cost/eval/trace deep-dives, send people to LangFuse, not here.

**Agent RED (from the spanmetrics connector):**

| Panel            | Metric / Query                                                                          | Visualization |
|------------------|------------------------------------------------------------------------------------------|---------------|
| Invocation Rate  | `sum(rate(traces_span_metrics_calls_total[5m])) by (agent_name)`                                              | Time series   |
| Error Rate       | `sum(rate(traces_span_metrics_calls_total{status_code="STATUS_CODE_ERROR"}[5m])) by (agent_name)`             | Stat + alert  |
| P95 Latency      | `histogram_quantile(0.95, sum(rate(traces_span_metrics_duration_milliseconds_bucket[5m])) by (le, agent_name))` | Time series |
| Daily Token Cost | `sum(increase(agent_cost_usd_total[24h])) by (gen_ai_request_model)`                       | Bar chart     |

**Infra correlation (the part LangFuse cannot show) — same dashboard, aligned time axis:**

| Panel                | Metric / Query                                                       | Visualization |
|----------------------|----------------------------------------------------------------------|---------------|
| Host CPU / Memory    | `node_cpu_seconds_total`, `node_memory_MemAvailable_bytes`           | Time series   |
| Container Resources  | `container_memory_usage_bytes`, `rate(container_cpu_usage_seconds_total[5m])` | Time series |
| Postgres Connections | `pg_stat_activity_count` / `pg_settings_max_connections`             | Gauge + alert |

> RED panels come free from spans via spanmetrics; the cost panel is fed by the SDK's `agent_cost_usd_total` OTel counter (see `sdk/python/metrics.py`). LangFuse still owns the authoritative per-trace cost/eval views — Prometheus just gets rolled-up counters. The infra panels are the differentiator: laying P95 latency over host CPU and DB connections is exactly the correlation LangFuse structurally can't do.

---

## Python SDK Wrappers (LangFuse v3 API)

> The v3 SDK is OpenTelemetry-based. The old `langfuse.decorators` / `langfuse_context` imports are **v2 and removed**. v3 uses `from langfuse import observe, get_client`.

### `sdk/python/tracer.py`

```python
"""
LangFuse v3 tracer — single import for all agents.
Reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST from env.
"""

import os
from functools import lru_cache
from langfuse import Langfuse, get_client


@lru_cache(maxsize=1)
def get_tracer() -> Langfuse:
    # Constructing Langfuse() registers the global client; get_client()
    # then returns that same instance from anywhere in the process.
    Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
        environment=os.environ.get("DEPLOY_ENV", "local"),
    )
    return get_client()
```

### `sdk/python/decorators.py`

```python
"""
@trace_agent  — wraps a function as a named LangFuse trace (root observation)
@trace_tool   — wraps a tool call as a child span

Usage:
    @trace_agent(name="eka-query-agent", tags=["eka", "rag"])
    def run_query(user_input: str) -> str: ...

    @trace_tool(name="vector-search")
    def search(query: str) -> list: ...
"""

from functools import wraps
from langfuse import observe, get_client


def trace_agent(name: str, tags: list[str] | None = None, user_id: str | None = None):
    def decorator(fn):
        @wraps(fn)
        @observe(name=name)
        def wrapper(*args, **kwargs):
            get_client().update_current_trace(
                name=name,
                tags=tags or [],
                user_id=user_id,
                metadata={"agent": name, "source": "platform"},
            )
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def trace_tool(name: str):
    def decorator(fn):
        @wraps(fn)
        @observe(name=name, as_type="span")
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)
        return wrapper
    return decorator
```

### `sdk/python/metrics.py`

```python
"""
Emit token + cost counters as OpenTelemetry metrics so they reach
Prometheus (via the Collector) for the Grafana cost panel.
Point OTEL_EXPORTER_OTLP_ENDPOINT at the Collector (http://otel-collector:4318).
"""

from opentelemetry import metrics

_meter = metrics.get_meter("platform.agents")

_tokens = _meter.create_counter("agent_tokens_total", unit="1",
                                description="Tokens consumed per agent/model")
_cost = _meter.create_counter("agent_cost_usd_total", unit="usd",
                              description="Estimated USD cost per agent/model")


def record_usage(agent: str, model: str, input_tokens: int,
                 output_tokens: int, cost_usd: float) -> None:
    attrs_in  = {"agent.name": agent, "gen_ai.request.model": model, "token.type": "input"}
    attrs_out = {"agent.name": agent, "gen_ai.request.model": model, "token.type": "output"}
    _tokens.add(input_tokens, attrs_in)
    _tokens.add(output_tokens, attrs_out)
    _cost.add(cost_usd, {"agent.name": agent, "gen_ai.request.model": model})
```

---

## Setup & Secret Scripts

### `scripts/gen-secrets.ps1`

```powershell
# Generates the three secrets LangFuse v3 requires and prints them.
function New-Secret([int]$bytes = 32) {
    $b = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    return [System.BitConverter]::ToString($b).Replace("-", "").ToLower()
}
Write-Host "NEXTAUTH_SECRET=$(New-Secret 32)"
Write-Host "SALT=$(New-Secret 16)"
Write-Host "ENCRYPTION_KEY=$(New-Secret 32)   # must be 64 hex chars"
```

### `scripts/setup.ps1` (Windows-first; `setup.sh` mirrors it for Linux/macOS)

```powershell
Write-Host "=== Observability Platform — Local Setup ==="

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Docker not found. Install Docker Desktop."; exit 1
}

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env — run scripts/gen-secrets.ps1 and paste real secrets before use."
}

docker compose -f docker-compose/docker-compose.yml --env-file .env up -d

Write-Host ""
Write-Host "=== Services Starting (ClickHouse may take ~30s on first boot) ==="
Write-Host "  LangFuse:    http://localhost:3000"
Write-Host "  Grafana:     http://localhost:3001  (admin / admin)"
Write-Host "  Prometheus:  http://localhost:9090"
Write-Host "  OTLP intake: localhost:4317 (gRPC) / 4318 (HTTP)"
Write-Host ""
Write-Host "Run scripts/health-check.ps1 to verify all services are healthy."
```

### `scripts/health-check.ps1`

```powershell
function Test-Svc($name, $url) {
    try {
        $code = (Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5).StatusCode
    } catch { $code = $_.Exception.Response.StatusCode.value__ }
    if ($code -in 200,302,307) { Write-Host "  [OK]  $name ($url)" }
    else { Write-Host "  [!!]  $name ($url) -- HTTP $code" }
}
Test-Svc "LangFuse"   "http://localhost:3000/api/public/health"
Test-Svc "Grafana"    "http://localhost:3001/api/health"
Test-Svc "Prometheus" "http://localhost:9090/-/healthy"
Test-Svc "Collector"  "http://localhost:8889/metrics"
```

---

## Security & Multi-Tenant "Phone Home" Model

The core premise — agents on *client* networks sending traces back to — is a security and compliance surface, not an afterthought. Treat it as a first-class design area.

- **Ingress auth:** terminate TLS at Nginx/Ingress; require a **per-client bearer token or mTLS client cert** on the OTLP endpoint. One credential per client engagement so traffic can be revoked/attributed independently.
- **Tenant isolation in LangFuse:** one **project per client** (use `environment` + tags). Prevents one client's traces from appearing in another's views.
- **PII / client-data handling:** prompts and tool I/O *will* contain client data. Decide policy up front — scrub via an OTel processor (`attributes`/`redaction` processor) at the Collector before traces hit LangFuse, and document a retention SLA per client.
- **Network path:** on AKS, prefer **Private Link / VPN** over public ingress for client-VM agents; public OTLP only where unavoidable, always token-scoped.
- **Secrets:** local uses `.env`; AKS uses **Azure Key Vault + CSI driver**, never plaintext in manifests.

---

## Data Retention & Backup

| Store      | Growth driver        | Policy to set                                                        |
|------------|----------------------|---------------------------------------------------------------------|
| ClickHouse | Trace/observation rows | TTL on trace tables (e.g. 90d) + sized PVC; this is the big grower |
| PostgreSQL | Metadata, datasets   | Nightly `pg_dump` (or Azure automated backups in cloud)             |
| Prometheus | Metrics samples      | `--storage.tsdb.retention.time=30d` (already set)                   |
| MinIO/Blob | Event + media uploads| Lifecycle rule to expire old event payloads; Azure Blob tiering     |

Add a backup CronJob (K8s) / scheduled task for Postgres + ClickHouse before any client data lands in production.

---

## Cloud Promotion Checklist (Azure AKS)

When promoting from local Kubernetes to AKS:

- [ ] Swap PostgreSQL pod → **Azure Database for PostgreSQL Flexible Server**
- [ ] Swap Redis pod → **Azure Cache for Redis**
- [ ] Swap MinIO → **Azure Blob Storage** (S3-compatible config or native SDK)
- [ ] Keep **ClickHouse** in-cluster as a StatefulSet (or evaluate ClickHouse Cloud) — it has no managed Azure equivalent
- [ ] Create AKS cluster; `kubectl apply -f k8s/`
- [ ] Add **cert-manager** + Let's Encrypt Ingress for TLS
- [ ] Move all secrets → **Azure Key Vault + CSI driver**
- [ ] Enable **Azure Monitor managed Prometheus** to scrape the Collector (or keep self-hosted Prometheus)
- [ ] Configure **Private Link / VPN** for agent connectivity from client environments
- [ ] Set up **Azure Container Registry (ACR)** for custom agent images
- [ ] Provision per-client OTLP credentials + ingress routing rules

---

## Instrumentation Quick Reference

```python
# Install
pip install langfuse opentelemetry-sdk opentelemetry-exporter-otlp

# Minimum viable trace (any agent, anywhere)
from sdk.python.tracer import get_tracer
from sdk.python.decorators import trace_agent, trace_tool
from sdk.python.metrics import record_usage

get_tracer()  # initialize once at startup

@trace_agent(name="eka-query-agent", tags=["eka", "rag"])
def run_agent(user_input: str) -> str:
    results = search_knowledge_base(user_input)
    answer = generate_response(results)
    record_usage("eka-query-agent", "gpt-4o", 1200, 350, 0.018)
    return answer

@trace_tool(name="vector-search")
def search_knowledge_base(query: str) -> list:
    ...
```

Environment variables required on every agent host:

```env
LANGFUSE_PUBLIC_KEY=your-public-key
LANGFUSE_SECRET_KEY=your-secret-key
LANGFUSE_HOST=https://langfuse.your-internal-domain.com   # or http://localhost:3000
DEPLOY_ENV=client-acme                                  # project/tenant tag
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318     # for metrics fan-out
```

---

## Week 1 Goals

| Day | Goal                                                          |
|-----|---------------------------------------------------------------|
| 1   | `setup.ps1` runs; all v3 services (incl. ClickHouse, MinIO) healthy |
| 2   | First agent instrumented via SDK; trace visible in LangFuse   |
| 3   | OTel Collector wired; Grafana dashboard live with 4 panels    |
| 4   | `sdk/python` wrappers tested across 2 agent types; cost panel populating |
| 5   | Health check + secret-gen scripts passing; README complete    |

## Week 2 Goals

| Day | Goal                                                          |
|-----|---------------------------------------------------------------|
| 1-2 | Kubernetes manifests generated (incl. ClickHouse StatefulSet, MinIO) and applied locally |
| 3   | Full v3 stack running on local K8s (Docker Desktop)           |
| 4   | Verify trace + metric flow works identically in K8s mode      |
| 5   | Security model (per-client tokens, PII scrubbing) + cloud checklist reviewed with DevOps |

---

*Generated for internal use. Owner: Furqan. Classification: Internal.*
