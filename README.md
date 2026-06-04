# Agentic Observability Platform

Internal observability for **all**  agentic AI workloads. Agents run
anywhere —  AKS, a Teams bot, a client VM — and **phone home** with
OpenTelemetry. Local-first, Azure-ready.

> Full design rationale lives in [`observability-plan.md`](observability-plan.md).
> A visual overview is in [`observability-architecture.html`](observability-architecture.html).

## How it fits together

```
Agents ──OTel──► Nginx/Ingress ──► OTel Collector ─┬─ traces  ─► LangFuse v3 ─► PG / ClickHouse / Redis / MinIO
                  (TLS, token/mTLS)                 └─ metrics ─► Prometheus  ─► Grafana
                                                                    ▲
                                              node-exporter · cAdvisor · postgres-exporter
```

**Two planes, not redundant:**
- **LangFuse** owns trace / eval / cost / token deep-dives — the LLM analytics home.
- **Grafana + Prometheus** put agent RED metrics *next to the infrastructure they
  run on* (host, container, DB) for "model or box?" correlation and alerting.

## Quick start (local)

```powershell
# Windows / PowerShell
.\scripts\gen-secrets.ps1     # generate secrets, paste into .env
.\scripts\setup.ps1           # copies .env.example -> .env (if absent), brings the stack up
.\scripts\health-check.ps1    # verify everything is healthy
```

```bash
# Linux / macOS
./scripts/gen-secrets.sh
./scripts/setup.sh
./scripts/health-check.sh
```

| Service     | URL                     | Notes                          |
|-------------|-------------------------|--------------------------------|
| LangFuse    | http://localhost:3000   | trace / eval / cost            |
| Grafana     | http://localhost:3001   | admin / `platform`            |
| Prometheus  | http://localhost:9090   | metrics store                  |
| OTLP intake | localhost:4317 / 4318   | where agents send telemetry    |

First boot bootstraps an org/project/keys from the `LANGFUSE_INIT_*` vars, so
agents can trace immediately (see `.env.example`).

## Instrument an agent

> **Full onboarding guide → [`sdk/AGENT-ONBOARDING.md`](sdk/AGENT-ONBOARDING.md)**
> Covers: copying the SDK, installing deps, all env vars, decorator usage,
> verifying the first trace, and the one rule you must not break.

```python
from sdk.python import get_tracer, trace_agent, trace_tool, record_usage

get_tracer()  # once at startup

@trace_agent(name="eka-query-agent", tags=["eka", "rag"])
def run_agent(q: str) -> str:
    ...
    record_usage("eka-query-agent", "gpt-4o", 1200, 350, 0.018)
    return answer
```

Agents **import the `sdk/` facade, never `langfuse` directly** — that is the
portability swap point. Details in [`sdk/README.md`](sdk/README.md).

## Layout

```
docker-compose/   Stage 1 local stack (this) + local override
otel/             OTel Collector config (spanmetrics fan-out + PII redaction)
prometheus/       scrape config (collector + infra exporters)
grafana/          provisioned datasource + agent-ops dashboard
nginx/            reverse proxy / TLS model for the phone-home boundary
sdk/python/       agent instrumentation facade
scripts/          setup / gen-secrets / health-check (.ps1 + .sh) + eval seed
k8s/              Stage 2 — Kubernetes manifests (not yet scaffolded)
```

## Stages

- **Stage 1 (this scaffold):** Docker Compose — full v3 stack runs locally.
- **Stage 2:** Kubernetes manifests (local → AKS). See the plan's Stage 2 section.

---
*Personal project. Owner: Furqan.*
