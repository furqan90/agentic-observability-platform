# Agentic Observability Platform

Internal observability for **all** agentic AI workloads. Agents run
anywhere — AKS, a Teams bot, a client VM — and **phone home** with
OpenTelemetry. Local-first, Azure-ready.

> Full design rationale lives in [`observability-plan.md`](observability-plan.md).
> A visual overview is in [`observability-architecture.html`](observability-architecture.html).

## How it fits together

```
Agents ──OTel──► Nginx/Ingress ──► OTel Collector ─┬─ traces  ─► LangFuse v3 ─► PG / ClickHouse / Redis / MinIO
                  (TLS, token/mTLS)                 └─ metrics ─► Prometheus  ─► Grafana
                                                                    │  ▲
                                                                    │  node-exporter · cAdvisor · postgres-exporter
                                                                    ▼
                                                            Alertmanager ─► alert-webhook-logger (demo) / Slack / PagerDuty
```

**Two planes, not redundant:**
- **LangFuse** owns trace / eval / cost / token deep-dives — the LLM analytics home.
- **Grafana + Prometheus** put agent RED metrics *next to the infrastructure they
  run on* (host, container, DB) for "model or box?" correlation and alerting.

**SLIs, SLOs, alerts:** two SLOs are defined per agent straight from the RED
metrics above — **99.5% availability** and **95% of calls under 3000ms**.
Multi-window burn-rate alerts (`prometheus/rules/slo-alerts.yml`) fire through
Alertmanager when an agent is burning its error budget too fast. See
[`prometheus/rules/slo-alerts.yml`](prometheus/rules/slo-alerts.yml) for the
recording rules/alerts and [`alertmanager/alertmanager.yml`](alertmanager/alertmanager.yml)
for routing. Run `scripts/simulate_slo_breach.py` to trigger one end-to-end.

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

| Service      | URL                     | Notes                          |
|--------------|-------------------------|--------------------------------|
| LangFuse     | http://localhost:3000   | trace / eval / cost            |
| Grafana      | http://localhost:3001   | admin / `$GRAFANA_ADMIN_PASSWORD`, SLO panels in "Agent Ops" |
| Prometheus   | http://localhost:9090   | metrics store + `/alerts`      |
| Alertmanager | http://localhost:9093   | alert routing/grouping UI      |
| OTLP intake  | localhost:4317 / 4318   | where agents send telemetry    |

First boot bootstraps an org/project/keys from the `LANGFUSE_INIT_*` vars, so
agents can trace immediately (see `.env.example`).

## Try the SLO alerting demo

With the stack up, generate synthetic traffic bad enough to breach the SLOs
and watch the alert fire end to end (Prometheus rule → Alertmanager → the
built-in webhook logger, no Slack/PagerDuty needed):

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-local LANGFUSE_SECRET_KEY=sk-lf-local \
LANGFUSE_HOST=http://localhost:3000 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
DEPLOY_ENV=local python scripts/simulate_slo_breach.py --mode both --duration 240
```

Watch it land: `docker compose -f docker-compose/docker-compose.yml --env-file .env logs -f alert-webhook-logger`

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
prometheus/       scrape config + rules/ (SLI recording rules, SLO burn-rate alerts)
alertmanager/     alert routing config (demo webhook receiver + real-world examples)
grafana/          provisioned datasource + agent-ops dashboard (RED + SLO panels)
nginx/            reverse proxy / TLS model for the phone-home boundary
sdk/python/       agent instrumentation facade
scripts/          setup / gen-secrets / health-check (.ps1 + .sh) + eval seed +
                  simulate_slo_breach.py (demo) + alert_webhook_logger.py (demo receiver)
k8s/              Stage 2 — Kubernetes manifests (not yet scaffolded)
```

## Stages

- **Stage 1 (this scaffold):** Docker Compose — full v3 stack runs locally.
- **Stage 2:** Kubernetes manifests (local → AKS). See the plan's Stage 2 section.

---
*Personal project. Owner: Furqan.*
