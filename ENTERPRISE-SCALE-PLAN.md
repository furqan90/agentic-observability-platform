#  Agentic Observability — Enterprise Scale Planning

> **Status:** Phase 1 complete (Docker Compose stack + basic SDK). This document
> captures design decisions and execution phases for Phases 2–4.
> Last updated: 2026-06-04

---

## Architecture at Scale

```
Agent fleet (runs anywhere)
  ──► Ingress (mTLS / per-client bearer token)
        ──► OTel Collector (fan-out, PII redaction, tail sampling, spanmetrics)
              ──► LangFuse v3 (OTLP/HTTP)   · traces · evals · cost
              │     ├── ClickHouse (trace OLAP, TTL-managed)
              │     ├── Postgres (metadata, datasets)
              │     ├── Redis (async queue / cache)
              │     └── MinIO / Blob (event uploads)
              ──► Prometheus (spanmetrics + cost exporter)
                    ──► Grafana (4-level dashboard stack · Alertmanager → Teams)
```

Two planes, not redundant:
- **LangFuse plane** — "which model, which prompt, which token cost?"
- **Operational plane** — "model or box?" (agent RED against host/container/DB)

---

## Phase Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Docker Compose stack, basic SDK, LangChain/OpenAI integrations | ✅ Complete |
| 2 | Metadata envelope, multi-project, Azure AD SSO, tail sampling, session stitching, alerting | 🔲 Next |
| 3 | Eval pipeline, 4-level dashboards, cost alerts, cost → Prometheus exporter | 🔲 Sprint +2 |
| 4 | AKS promotion, Private Link, full RBAC, per-client key rotation | 🔲 Cloud-readiness |

---

## Phase 2 Design Decisions

### Metadata Envelope (contract layer)

Every trace must carry this envelope. Downstream dashboards, cost rollups, RBAC
scoping, and alert routing all depend on it being consistent. Enforced at the SDK
facade — no direct `langfuse` or `opentelemetry` imports permitted outside `sdk/`.

```python
METADATA_ENVELOPE = {
    "tl.team":        "eka-team",            # owning team slug
    "tl.agent":       "incident-agent",      # agent identifier
    "tl.agent_ver":   "1.4.2",               # semver
    "tl.env":         "prod",                # prod | staging | dev
    "tl.user_id":     "<sha256_of_aad_upn>", # NEVER plaintext — hashed at SDK layer
    "tl.session_id":  "<uuid>",              # conversation/session boundary
    "tl.project":     "eka",       # maps to LangFuse project
    "tl.cost_center": "AI-OPS-001",          # for finance rollup
}
```

**User ID hashing must be enforced BEFORE any client data lands in production.**
Hash at SDK layer, keep reverse-mapping in AAD, treat ClickHouse as a PII-free zone.
This is a GDPR/data governance hard requirement.

### LangFuse Project Hierarchy

```
Organization: 
├── Project: eka-prod
├── Project: eka-dev
├── Project: devenv-agent-prod      (separate eval metrics — see §DevEnv Agent)
├── Project: client-delivery-prod
└── Project: shared-infra           (cross-cutting eval datasets)
```

**Rule:** One project per team/domain, NOT one per agent. Agent-level separation
happens via `tl.agent` metadata filtering, not project boundaries. Projects are
billing and access control units.

### Azure AD SSO

Required so users inherit team identity from AAD groups — enables project-level
RBAC without manual user management.

```yaml
# langfuse-web env vars
AUTH_DISABLE_SIGNUP: true
AUTH_AZURE_AD_CLIENT_ID: ${AAD_APP_ID}
AUTH_AZURE_AD_CLIENT_SECRET: ${AAD_SECRET}
AUTH_AZURE_AD_TENANT_ID: ${AAD_TENANT}
```

### API Key Governance

- One API key per team × environment
- Stored in Azure Key Vault (not in agent env vars directly)
- Ingress layer maps bearer token → correct LangFuse project key
- Agents never hold LangFuse credentials directly
- Keys are revocable per client engagement

### RBAC Policy

| Role | LangFuse Permission | Raw LLM I/O? |
|---|---|---|
| Platform team | Org Owner | Yes |
| Team lead | Project Member | Yes (own team) |
| Developer | Project Member | Yes (own team) |
| Manager / Stakeholder | Project Viewer | No (dashboards only) |
| Auditor | Project Viewer | No |

### Tail-Based Sampling (OTel Collector)

Full capture at enterprise scale will overwhelm ClickHouse. Head-based sampling
loses error cases. Use tail-based sampling via the `tailsampling` processor:

```yaml
tail_sampling:
  decision_wait: 10s
  policies:
    - name: errors-policy
      type: status_code
      status_code: {status_codes: [ERROR]}
    - name: slow-traces-policy
      type: latency
      latency: {threshold_ms: 5000}
    - name: baseline-policy
      type: probabilistic
      probabilistic: {sampling_percentage: 10}
```

- **Prod:** 100% errors + slow traces, 10% baseline
- **Staging:** 100%
- **Dev:** 100%, 7-day ClickHouse TTL

### Cross-Agent Session Stitching

`tl.session_id` is necessary but not sufficient. The mechanism is **W3C Trace
Context propagation** (`traceparent`/`tracestate` headers). When agent A hands off
to agent B, the OTel span context must be propagated — otherwise LangFuse shows
two disconnected traces instead of one session tree.

**SDK gap to close in Phase 2:** Add two functions to the facade:

```python
get_propagation_headers() -> dict   # {"traceparent": "...", "tracestate": "..."}
restore_propagation_context(headers: dict) -> None
```

Uses OTel's `inject()` / `extract()` on the global propagator. The `tl.session_id`
in metadata provides the LangFuse-level grouping; the traceparent provides the
OTel-level parent-child link.

### Alertmanager → Teams

Route alerts to owning team, not central platform ops. `group_by` on
`tl_team` + `tl_agent` ensures the right team is paged.

```yaml
receivers:
  - name: teams-eka
    msteams_configs:
      - webhook_url: ${TEAMS_WEBHOOK_EKA}
        title: 'Agent Alert: {{ .GroupLabels.tl_agent }}'
        text: |
          Team: {{ .GroupLabels.tl_team }}
          Error rate: {{ .Value }}%
          Runbook: https://wiki.example.com/agents/runbooks/{{ .GroupLabels.tl_agent }}

route:
  group_by: [tl_team, tl_agent]
  routes:
    - match: {tl_team: eka-team}
      receiver: teams-eka
```

Each team owns their alert thresholds within a platform-defined floor
(error rate > 5% is always P2 regardless of team preference).

---

## Phase 3 Design Decisions

### Eval Pipeline

Production traces → eval datasets → async LLM-as-judge scoring.

**Eval trigger policy (tiered — NOT 100% or random):**

| Tier | Trigger | Eval to run |
|---|---|---|
| Always | User negative feedback / escalation / retry | Full RAGAS suite |
| Always | Latency > p95 threshold | `latency_sla_breach` only |
| Always | Tool call failed or retried | `tool_call_accuracy` |
| Sampled | 5% of normal traces | Full RAGAS suite (baseline drift) |
| Weekly batch | 2% random sample | Full suite (trend tracking) |

Implemented as an async worker triggered by LangFuse v3 outbound webhooks
on trace completion. Roughly 10-15% of traces get scored, weighted toward
interesting cases.

**Scores to track per agent:**
- `faithfulness` — does the response follow from retrieved context?
- `answer_relevancy` — does the response address user intent?
- `tool_call_accuracy` — did the agent call the right tools?
- `latency_sla_breach` — binary: exceeded p95 threshold?

**Key insight:** Production traces are a hard-negative mine for RAGAS eval sets.
Low-scoring traces should be automatically routed into the eval dataset to close
retrieval experiment gaps (chunking strategy, hard negatives, etc.).

### Cost → Prometheus (split approach)

| Use case | Mechanism | Rationale |
|---|---|---|
| Cost dashboards (L1-L2) | ClickHouse Grafana plugin | Real-time, no extra infra, direct CH query |
| Cost alerting (Alertmanager) | Lightweight Python exporter → Pushgateway | Alertmanager needs Prometheus metrics |

The exporter (`scripts/cost-exporter.py`) runs on a 5-minute cron, hits
`/api/public/metrics` on LangFuse, pushes per-team cost counters to Pushgateway.
~50 lines of Python. Not a blocking dependency for Phase 2.

### Per-Team Cost Alert Thresholds

Set per LangFuse project, not globally.

- **80% of budget:** notification to team lead via Teams
- **100% of budget:** key rotation to rate-limited key + alert to platform ops + team lead
- **Emergency kill:** ingress-level token revocation (no LangFuse involvement needed)

### 4-Level Dashboard Stack

| Level | Audience | Surface | Key Metrics |
|---|---|---|---|
| 1 | CTO / Executive | LangFuse Org Dashboard | Total cost by team, agent success rate, active sessions |
| 2 | Team lead | Grafana + LangFuse project view | p95 latency, error rate, cost rate per team |
| 3 | Developer | LangFuse trace explorer | Full span waterfall, LLM I/O, scores, token cost |
| 4 | Governance | LangFuse Users tab (gated) | Per-user traces (restricted to team leads+) |

**Key PromQL queries (Level 2):**

```promql
# Agent success rate by team
rate(tl_agent_calls_total{tl_team="eka-team",status_code="200"}[5m])
/ rate(tl_agent_calls_total{tl_team="eka-team"}[5m])

# p95 latency per agent
histogram_quantile(0.95,
  rate(tl_agent_duration_milliseconds_bucket{tl_team="eka-team"}[5m])
)
```

---

## DevEnv Agent Telemetry (special case)

Separate LangFuse project (`devenv-internal`) sharing the same infrastructure.

Key differences from production agents:
- No user ID hashing required (internal tool, AAD identity acceptable)
- Success metrics are structural: build success rate, template accuracy,
  dependency resolution time — not conversation quality
- Evals are structural (did it scaffold the right files?) not semantic
- No client data — lower governance burden
- Can share `eka-dev` or get its own project as usage grows

---

## Known Corrections to Source Document

These are errors in the original architecture doc to avoid propagating:

1. **Authorization header:** Doc has `Bearer ${LANGFUSE_PUBLIC_KEY}`. Correct
   for LangFuse v3 OTLP endpoint is Basic auth: `base64(public_key:secret_key)`.
   Already correct in `otel-collector-config.yaml`.

2. **`spanmetrics` placement:** Doc puts `spanmetrics` under `processors:`.
   In OTel Collector contrib 0.8x+ it is a **connector**, not a processor.
   Syntax is `connectors: { spanmetrics: ... }`. Already correct in our config.

3. **SDK API examples use v2:** `langfuse_context.update_current_trace()` and
   `langfuse.score()` are v2 patterns. v3 uses `get_client().update_current_trace()`
   and `get_client().score()`.

---

## Self-Hosted vs Cloud Decision

**Self-hosted for all client-facing agents** — data residency is a hard constraint.
Client engagement data on a shared SaaS platform requires a signed DPA and
region-pinned storage that LangFuse Cloud does not yet offer.

**Cloud viable for internal tooling** (DevEnv Agent, internal productivity agents)
once LangFuse offers EU/AU residency with signed DPA. Not a near-term action item.

Operational overhead crossover (~$600/month AKS + platform engineering bandwidth)
is not a near-term concern at current agent count.

---

## Open Items (not yet decided)

- [ ] Lint rule implementation: PR gate flagging direct `langfuse`/`opentelemetry`
  imports outside `sdk/` — integrate into CI before Phase 2 agents go live
- [ ] AAD group → LangFuse role mapping: define group naming convention
- [ ] ClickHouse Grafana plugin: evaluate schema stability of LangFuse v3 internal
  tables before building cost dashboards against them (schema may change between
  LangFuse versions)
- [ ] Pushgateway: add as a service to the Compose stack in Phase 3
- [ ] LangFuse webhook config: document how to configure outbound webhooks for
  the eval trigger pipeline
- [ ] `get_propagation_headers()` / `restore_propagation_context()`: add to SDK
  facade in Phase 2

---

* internal · Owner: Furqan · Classification: Internal*
