# Handover: multi-project Langfuse tracing

Merged to `main` in PR #1 (`aa80496`).
Status: **Live. The Collector runs this config on the pinned image, with
`agentic-coding-platform` registered. `pilot-agent` (SDK 0.3.0) traces reach the default
project, LiteLLM traces reach `agentic-coding-platform`, and the Grafana Project and Agent
filters have data. Open: lost exports on the published-port path, and the org API check.**
Design and runbook: [`docs/MULTI-PROJECT.md`](MULTI-PROJECT.md). This file covers
what's done, what isn't, and what to do next.

---

## 1. Goal and decisions (agreed with the owner)

Before: one set of Langfuse credentials, so every trace landed in one project. The
Collector exported everything with a single static `Authorization` header
(`LANGFUSE_OTEL_AUTH`). Agents' own `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` were
silently ignored on the Collector path.

| # | Decision |
|---|---|
| 1 | **Plan B: route by project name.** The agent stamps the `tl.project` resource attribute. The Collector's `routing` connector picks a per-project exporter. Secrets stay on the platform. (Rejected: agents forwarding credentials through `headers_setter`, which breaks with tail sampling and puts secrets on client VMs.) |
| 2 | **One project per process** for now. Multi-project-per-process is *documented only* (MULTI-PROJECT.md §4). |
| 3 | **Projects created in the Langfuse UI.** Org API researched for future automation/migration (MULTI-PROJECT.md §5). |
| 4 | **Unknown/missing project → default project** (the `LANGFUSE_INIT_*` bootstrap project, still using `LANGFUSE_OTEL_AUTH`). |

---

## 2. What's done (5 commits on top of `9074613`)

| Commit | Change |
|---|---|
| `bb3e8d3` Collector routing | `otel/projects.yaml` (registry), `otel/otel-collector-config.tmpl.yaml` (template), `scripts/gen-collector-config.py` (`--check`, `--list-env`), generated `otel/otel-collector-config.yaml`, `span_metrics` dimension `tl.project` (`default: unset`), compose loads optional `otel/langfuse-projects.env` (gitignored, `.example` committed) |
| `aa41b9b` SDK | `LANGFUSE_PROJECT` → `tl.project` on trace + metric resource; `get_project()` exported; `record_usage` adds `tl.project` datapoint label. When a project is set the SDK builds the TracerProvider itself (Langfuse has no hook for extra resource attributes), mirroring `langfuse.environment`, `langfuse.release`, `LANGFUSE_SAMPLE_RATE`. No project = old code path untouched. Docs: CONTRACT.md, AGENT-ONBOARDING.md, SDK README, `.env.example` |
| `9ea5ff2` Grafana | `project` variable + filters on RED/cost panels. **Also fixed** stale metric names (`calls_total` → `traces_span_metrics_calls_total`, same for duration), which were likely leaving panels empty |
| `d507330` Tooling/docs | `send_test_trace.py`: unique run tag per run + isolation check (trace must NOT be in the default project when `DEFAULT_LANGFUSE_PUBLIC_KEY/SECRET_KEY` are given). COMMANDS.md, gen-secrets hints, README, `docs/MULTI-PROJECT.md` |
| `3475575` Ingress binding | nginx `map $http_authorization $client_project` + `proxy_set_header X-Langfuse-Project`. Collector receivers `include_metadata: true` + `resource/project_from_ingress` (first in `traces/in`, before `batch`) upserts `tl.project` from the header |

Tests (all passing in the cloud session):
```bash
pip install -e sdk/python pyyaml pytest
pytest scripts/tests sdk/python/tests          # 23 passed
python scripts/gen-collector-config.py --check # OK
```

---

## 3. Validation

### 3.0 Results so far (2026-11-02, Collector v0.153.0, Langfuse 3.178.0)

The stack on the dev machine is shared: `pilot-*`, `hrp-*` and `litellm` all send to
the live Collector. So most checks used temporary containers on other ports. The owner
then restarted the live Collector on this config at 09:20 UTC. **Its config is bind-mounted
from this checkout, so any restart of it loads whatever config is checked out.** Keep the
checkout on an up-to-date `main`: an older commit would drop the `agentic-coding-platform`
route on the next restart.

| Handover step | Result |
|---|---|
| 3.1 `validate` | Pass. No template changes needed; all four syntax assumptions hold on 0.153.0 |
| 3.2 Default path | Pass with real Langfuse, through a temporary Collector |
| 3.3 Second project | Pass with real Langfuse through the live Collector, project `agentic-coding-platform` (trace arrived, not in default). Unknown slug → default passes on a temporary Collector. A real `pilot-agent` request reached the default project on the live Collector |
| 3.3 step 7 cost metric | Name is `agent_cost_usd_total`, no unit added. Grafana view checked |
| 3.4 Org API | **Not run: needs an org key from the UI** |
| 3.5 nginx binding | Pass against a sink, 16/16 cases overall including gRPC |

`OTLPSpanExporter` does pick up `OTEL_EXPORTER_OTLP_HEADERS` and
`OTEL_EXPORTER_OTLP_CERTIFICATE` when constructed with an explicit endpoint, and the
metric exporter uses them too. With a token bound to `eka` and `LANGFUSE_PROJECT=agents`,
the trace goes to `eka` but the cost metric stays labelled `agents` (the limitation in §4).

Client side: the SDK is now **0.3.0** with `langfuse>=4.2.0`, tested at that floor on
Python 3.11. `AGENT-ONBOARDING.md` covers moving an existing agent and the ingress
variables.

Client rollout:

| App | Project | How it selects it | SDK | State |
|---|---|---|---|---|
| `pilot-agent` | Default | `LANGFUSE_PROJECT` unset | 0.3.0, vendored wheel in `retrieval_systems_platform` (PR #5) | Deployed. A real request shows `agent_name="secp-pilot-advisor"` in Prometheus |
| `proxy-litellm-1` | `agentic-coding-platform` | `OTEL_RESOURCE_ATTRIBUTES` | None | Live. A real request reached its project and not the default. Nobody uses OpenCode yet, so there's no real traffic |
| `hrp-*` | Default | `LANGFUSE_PROJECT` unset | 0.2.0 | Gets 0.3.0 at its next build, from the same `vendor/` folder |
| `pilot-agent-stable` | Default | `LANGFUSE_PROJECT` unset | 0.2.0 | Separate image, unchanged |

`@trace_agent` sets `agent.name` on its root span, so RED panels can split by agent. Spans
from older SDKs and non-SDK apps keep an empty `agent_name`, and the dashboard's Agent
"All" still includes them. The Collector image is pinned to 0.153.0.

Found along the way (not caused by this branch, not fixed yet):

- **Exports sometimes fail on the published-port path, and this can drop part of a trace.**
  The SDK logs "Failed to export" after about 11s, and that batch is lost.
  - Short-lived processes on Windows: 3/40 lost on this config, 4/40 on the pre-branch config.
  - Long-running `pilot-agent`: one failure at 12:03 UTC. That trace is missing its
    `__start__` and `_route_entry` spans compared with an identical earlier request, so
    it looks complete at a glance.
  - Inside the Docker network: 0/600 raw requests failed.
  - Cause not identified. A failed export makes `send_test_trace.py` report a false FAIL,
    so rerun before debugging routing. The next thing to try is putting `pilot-agent` on
    the Collector's Docker network, which is untested.
- **The Jenkins smoke test can fail a good deploy.** In build 16, every container came up
  healthy, then `curl` to `host.docker.internal:8010/health` hung for 32s with an empty
  reply. The request never reached `pilot-agent`. Repeating the check from the Jenkins
  container gave 119/120 across three services, and the one failure was a 15s hang on
  Prometheus. This looks like the same path as above, but that's not proven. Adding retries
  to the smoke `curl` in `retrieval_systems_platform` would stop one hang failing a build.
- **`send_test_trace.py`'s "Metrics in Prom" check is weak.** It passes on any existing
  series, not this run's. It passed here even though this run's metrics went to a
  Collector that Prometheus doesn't scrape.

### Remaining steps

The 3.1, 3.2 and 3.5 commands below remain as the recipe for rerunning.

### 3.1 Collector accepts the generated config
```bash
docker run --rm -v "$PWD/otel/otel-collector-config.yaml:/cfg.yaml" \
  -e LANGFUSE_OTEL_AUTH=x otel/opentelemetry-collector-contrib:latest validate --config=/cfg.yaml
```
Assumptions to watch (fix in the **template** or generator, then regenerate):
- `routing` connector syntax: `table[].context: resource` + `condition:` + `default_pipelines` + `error_mode`. Older contrib versions used `statement: route() where …`. The compose image is `:latest`.
- `resource` processor accepts `from_context: metadata.x-langfuse-project` (shared attraction code with the `attributes` processor).
- `span_metrics` dimension `default:` field.
- Component names with `-` (`otlp_http/langfuse-agents`, `traces/langfuse-agents`).

### 3.2 Default path unchanged (regression)
```bash
./scripts/setup.sh && ./scripts/health-check.sh
LANGFUSE_PUBLIC_KEY=pk-lf-local LANGFUSE_SECRET_KEY=sk-lf-local LANGFUSE_HOST=http://localhost:3000 \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 DEPLOY_ENV=local python scripts/send_test_trace.py
```
Expect PASS with no `LANGFUSE_PROJECT` (goes to the default route). Check
`docker compose -f docker-compose/docker-compose.yml logs otel-collector` for errors.

### 3.3 Second project end to end
1. Langfuse UI → create project (e.g. `eka`) → create API keys.
2. Uncomment `eka:` in `otel/projects.yaml`, then `python scripts/gen-collector-config.py`.
3. `cp otel/langfuse-projects.env.example otel/langfuse-projects.env` and add
   `LANGFUSE_OTEL_AUTH_EKA=$(printf '%s' 'pk…:sk…' | base64 | tr -d '\n')`.
4. `docker compose -f docker-compose/docker-compose.yml --env-file .env up -d --force-recreate otel-collector`
5. Run with `LANGFUSE_PROJECT=eka`, eka's keys, and
   `DEFAULT_LANGFUSE_PUBLIC_KEY=pk-lf-local DEFAULT_LANGFUSE_SECRET_KEY=sk-lf-local`.
   Expect *Trace in LangFuse: PASS* and *Project isolation: PASS*.
6. Also try `LANGFUSE_PROJECT=nope` → should land in the default project.
7. Grafana (http://localhost:3001) → Agent Ops → Project variable shows `eka`, `unset`.
   Confirm the panels now render (metric-name fix). Check the cost panel's metric
   name in Prometheus too (`agent_cost_usd_total`): the unit `usd` might be
   appended by the Prometheus exporter.

### 3.4 Organization API availability on this edition
In the Langfuse UI, check whether Organization Settings shows an **API Keys** tab.
If it does, create an org key and run:
```bash
curl -u "$ORG_PK:$ORG_SK" http://localhost:3000/api/public/organizations/projects
```
200 = usable. 403 or a "plan" error = Enterprise-gated (expected per the docs, but
unconfirmed). Record the result in MULTI-PROJECT.md §5.2 and §6.
Also confirm the self-hosted Admin API (`ADMIN_API_KEY`) paths from the current docs.

### 3.5 nginx token → project binding
`nginx/nginx.conf` isn't wired into compose (see its header). Generate a self-signed
cert, add an nginx service in `docker-compose.override.yml`, bind
`"Bearer local-dev-token"` to `"eka"`, send a trace through `https://otlp.localhost:4318`
with **no** `LANGFUSE_PROJECT`, and expect it in `eka`. Also check that a client-sent
`X-Langfuse-Project` header is dropped when the token is unbound. The SDK doesn't
currently send a bearer token; use `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20local-dev-token`
(check whether `OTLPSpanExporter` picks it up when constructed with an explicit endpoint).

---

## 4. Known limitations / follow-ups

- **Metric counters ignore ingress binding:** `record_usage`'s `tl_project` label is
  agent-supplied, so the nginx override applies to traces/span metrics only.
- **Unset per-project env var:** the header becomes `Basic `, so that project gets 401s
  (other projects are unaffected). Consider a startup check, e.g. extend
  `health-check.sh` with `gen-collector-config.py --list-env` against the Collector's env.
- **Direct Collector ports 4317/4318** bypass nginx. Production must expose only the ingress.
- **Pre-registered TracerProvider:** if another library sets up OTel before
  `get_tracer()`, the SDK warns and the project isn't stamped (workaround:
  `OTEL_RESOURCE_ATTRIBUTES=tl.project=<slug>`).
- **CI:** add `python scripts/gen-collector-config.py --check` and
  `pytest scripts/tests sdk/python/tests` to CI (none exists in the repo yet).
- **TypeScript SDK** (`sdk/typescript/`, README only) must implement `tl.project`
  per CONTRACT.md §7 when it's built.
- **Pre-existing, not addressed:** AGENT-ONBOARDING.md still mentions "LangFuse v3
  detects the existing provider". The images use `:latest` tags.

## 5. After validation

Done: validation, PR #1 merged, and `pilot-agent` deployed on SDK 0.3.0. Next:

1. Find the cause of the failed exports (§3.0), before starting nginx.
2. Add nginx in stages: alongside the direct ports first, then move clients one at a time,
   then close ports 4317/4318.
3. Check the org API (§3.4), and add larger latency buckets, since P95 tops out at 30s.
4. Later: `scripts/provision-project.py` using the org API (design in MULTI-PROJECT.md §5.3),
   and multi-project-per-process if a gateway use case appears (§4).
