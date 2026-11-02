# Multi-project tracing

How the platform sends traces into **more than one Langfuse project**, how to
add a project, and what is recorded for later: more than one project per
process, and automating project provisioning through the Langfuse API.

> Design rule (from `ENTERPRISE-SCALE-PLAN.md`): **one project per team/domain,
> not per agent.** Agents are told apart with `tl.agent` metadata, not with
> project boundaries. That keeps the project count small (tens, not hundreds),
> which is why per-project Collector config is acceptable.

---

## 1. How routing works

```
Agent (SDK)                          OTel Collector                                   Langfuse
LANGFUSE_PROJECT=eka                 traces/in
  └─► resource attr tl.project=eka ─► [ingress pin] ─► redaction ─► batch ─┬─► routing/langfuse_projects
                                                                     │     tl.project == "eka"    ─► traces/langfuse-eka    ─► otlp_http/langfuse-eka    (Basic $LANGFUSE_OTEL_AUTH_EKA) ─► project "eka"
                                                                     │     tl.project == "agents" ─► traces/langfuse-agents ─► otlp_http/langfuse-agents (Basic $LANGFUSE_OTEL_AUTH)     ─► project "agents"
                                                                     │     missing / unknown      ─► traces/langfuse-agents  (default_project)
                                                                     └─► span_metrics (dimension tl.project → label tl_project) ─► Prometheus
```

- **The agent selects the project, the platform holds the credentials.** The
  SDK stamps the `tl.project` *resource* attribute from `LANGFUSE_PROJECT`.
  Langfuse secrets for routing live only in the Collector's environment, so agents
  on client VMs never need another team's keys, and never send any.
- **Unknown or missing project → default project** (`default_project` in
  `otel/projects.yaml`). Existing agents that don't set `LANGFUSE_PROJECT` keep
  working exactly as before.
- **PII redaction runs once, before routing**, so every project gets
  redacted data.
- **Pieces:**

  | File | Role |
  |---|---|
  | `otel/projects.yaml` | Project list: slug → auth env var (+ optional endpoint) |
  | `otel/otel-collector-config.tmpl.yaml` | Everything in the Collector config except the per-project parts |
  | `scripts/gen-collector-config.py` | Renders the two above into `otel/otel-collector-config.yaml` (committed) |
  | `otel/langfuse-projects.env` | Per-project auth for non-default projects (gitignored; `.example` committed) |
  | `.env` → `LANGFUSE_OTEL_AUTH` | Auth for the default project (unchanged from single-project setup) |
  | SDK `get_tracer()` | Stamps `tl.project` on the trace + metric resource; `record_usage()` adds it as a datapoint label |

### Why route on an attribute rather than forward credentials

We chose this over having agents send their own Langfuse credentials through the
Collector (`headers_setter`):

1. Secrets stay on the platform. Client VMs only need a project slug.
2. It still works after Phase 2 adds tail sampling. `tail_sampling` / `groupbytrace`
   drop the per-request context that header forwarding depends on; a resource
   attribute travels with the data.
3. It matches the existing contract (`CONTRACT.md` §1 `tl.project`).

The cost is one registry entry plus a Collector reload per new project, which is
fine at team-level project counts.

---

## 2. Adding a project (runbook)

1. **Create the project in the Langfuse UI** (Organization → New project), then
   create an API key pair for it (Project → Settings → API Keys).
2. **Register it** in `otel/projects.yaml`:
   ```yaml
   projects:
     eka:
       description: EKA team agents
   ```
   Slugs must match `[a-z0-9][a-z0-9_-]*`. The auth env var defaults to
   `LANGFUSE_OTEL_AUTH_<SLUG>` (uppercase, `-` → `_`). Override it with `auth_env:`.
   A project on a different Langfuse instance can set `endpoint:`.
3. **Regenerate** the Collector config and commit both files:
   ```bash
   python scripts/gen-collector-config.py
   python scripts/gen-collector-config.py --list-env   # shows LANGFUSE_OTEL_AUTH_EKA
   ```
4. **Add the secret** to `otel/langfuse-projects.env` (copy from the `.example`):
   ```bash
   echo "LANGFUSE_OTEL_AUTH_EKA=$(printf '%s' 'pk-lf-…:sk-lf-…' | base64 | tr -d '\n')" >> otel/langfuse-projects.env
   ```
   In AKS this becomes a key in the Collector's Kubernetes Secret.
5. **Reload the Collector** (recreate it so the env file is re-read):
   ```bash
   docker compose -f docker-compose/docker-compose.yml --env-file .env up -d --force-recreate otel-collector
   ```
6. **Tell the team** to set `LANGFUSE_PROJECT=eka` and use the `eka` project's
   API keys for `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY`. Those keys are for REST calls
   (scores, datasets). If they belong to a different project, scores and traces
   split across projects. They need SDK 0.3.0 or later: older versions ignore
   `LANGFUSE_PROJECT` without any warning. Client steps are in
   [`AGENT-ONBOARDING.md`](../sdk/python/AGENT-ONBOARDING.md).
7. **Verify** routing and isolation:
   ```bash
   LANGFUSE_PROJECT=eka LANGFUSE_PUBLIC_KEY=pk-lf-<eka> LANGFUSE_SECRET_KEY=sk-lf-<eka> \
   DEFAULT_LANGFUSE_PUBLIC_KEY=pk-lf-local DEFAULT_LANGFUSE_SECRET_KEY=sk-lf-local \
   LANGFUSE_HOST=http://localhost:3000 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
   python scripts/send_test_trace.py
   ```
   The script tags the trace with a unique run tag. It checks the trace arrived in
   `eka` **and** did not arrive in the default project.

**Removing a project:** delete it from `projects.yaml`, regenerate, reload. Any
agent still sending that slug falls through to the default project. Delete the
Langfuse project afterwards if needed.
**Renaming a slug:** add the new slug, migrate agents, then remove the old one.

`compose` needs v2.24+ for the optional `env_file` (`required: false`).

---

## 3. Failure modes and how to spot them

| Symptom | Cause | Effect |
|---|---|---|
| Traces for a project land in the default project | Slug typo, or slug not in `projects.yaml` | Silent fallback by design. Check the Grafana **Project** variable: values not in the registry are agents sending unknown slugs. |
| Collector logs `401` for `otlp_http/langfuse-<slug>` | `LANGFUSE_OTEL_AUTH_<SLUG>` unset or wrong. An unset `${env:…}` expands to an empty string, so the header is `Basic ` | Only that project's export fails (retried, then dropped). Other projects are unaffected. |
| `tl_project="unset"` in Grafana | Agent doesn't set `LANGFUSE_PROJECT` | Routed to the default project. |
| SDK warning "global TracerProvider is already registered" | Another library set up OTel before `get_tracer()` | `tl.project` can't be added to that provider's resource. Set `OTEL_RESOURCE_ATTRIBUTES=tl.project=<slug>` or call `get_tracer()` first. |
| `gen-collector-config.py --check` fails in CI | `projects.yaml`/template edited without regenerating | Run the generator and commit. |

**Spoofing:** without ingress enforcement, any agent that can reach the Collector
can set any slug and write into that project. Ingress binding fixes this:

- `nginx/nginx.conf` → `map $http_authorization $client_project` ties each client
  bearer token to a slug, and `proxy_set_header X-Langfuse-Project $client_project`
  always sets the header itself. An empty mapping drops the header, so a client
  can't inject its own value through the proxy.
- The Collector's `resource/project_from_ingress` processor (first in `traces/in`,
  receivers have `include_metadata: true`) upserts `tl.project` from that header.
  Bound tokens therefore win over whatever the agent stamped. With no header,
  nothing changes.
- Bind **every external client token**. Unbound tokens (`""`) fall back to the
  agent-supplied slug, which is fine for trusted internal agents.
- Limitation: the override applies to traces (and therefore span metrics). The
  SDK's `record_usage` cost counters still carry the agent-supplied `tl_project`
  label.
- The Collector's direct ports (4317/4318) bypass nginx. In production expose
  only the ingress (as the AKS plan already requires), or the binding can be
  sidestepped.

---

## 4. Multiple projects in one process: provision, not built

**Today: one project per process.** `tl.project` is a resource attribute, and an
OTel `TracerProvider`'s Resource is fixed for the life of the process. That is
enough for team-level projects. The cases that would need more are a shared
gateway/router agent serving several teams, or a multi-tenant bot where each
conversation belongs to a different client project.

### Recommended path when it's needed: per-trace project via span attribute + baggage

1. **SDK**
   - Add an optional `project=` to `trace_agent(...)` (non-breaking; defaults to
     `LANGFUSE_PROJECT`).
   - At the root, put `tl.project` into OTel **baggage** for the duration of the
     call. Register a span processor that copies baggage `tl.project` onto **every**
     span as a span attribute (`opentelemetry-processor-baggage` does this, or
     ~15 lines of our own). Children, auto-instrumented LLM spans and
     `get_openai_client()` spans all inherit it. Baggage also crosses process
     boundaries in `get_propagation_headers()`, so a downstream agent's spans
     follow the caller's project.
   - `record_usage(..., project=None)`: optional override for the datapoint label.
   - REST calls (scores, datasets) per project need a Langfuse client per project:
     Langfuse v4 keeps clients keyed by public key (`Langfuse(public_key=…)`,
     `get_client(public_key=…)`). The agent would then need keys for every project
     it serves, which goes against the "secrets on the platform" goal. Keep REST use
     on the process's home project where possible.
2. **Collector:** the generator adds span-level routes **ahead of** the resource
   routes, one per project:
   ```yaml
   - context: span
     condition: attributes["tl.project"] == "eka"
     pipelines: [traces/langfuse-eka]
   ```
   Span attribute wins, resource attribute is the fallback, unknown goes to the
   default. `span_metrics` already looks up span attributes before resource
   attributes, so the `tl_project` label keeps working.
3. **Invariant to enforce:** every span of one trace must carry the same project,
   or Langfuse receives half a trace in each project. The baggage processor
   guarantees this inside a process; across services it depends on baggage being
   propagated (it is, through our propagation helpers).

### Rejected alternative

**One TracerProvider per project in the same process.** Langfuse warns that
active-span context is shared across providers ("may lead to broken trace trees"),
and the global-provider assumptions in `get_propagation_headers()` and
OpenInference would break. Not recommended.

### What was kept compatible today so this stays additive

- One routing key name, `tl.project`, in the SDK, Collector, metrics and contract.
- Routes are generated (`context: resource` rows). Adding `context: span` rows
  is a generator-only change, with no hand edits per project.
- `get_project()` is the single place the SDK reads the project, and the
  `trace_agent` signature can take a keyword-only `project=` without breaking callers.

Rough effort when needed: SDK + tests ~1–2 days, generator + Collector ~½ day,
plus an end-to-end two-project test.

---

## 5. Automating project provisioning: Langfuse Organization API findings

Projects are created in the **UI for now**. Below is what's available for future
automation and for migrating between Langfuse instances.

### 5.1 Endpoints (confirmed in the generated API client of `langfuse` 4.18.0)

All of these require an **organization-scoped** API key (HTTP Basic,
`org_public_key:org_secret_key`). A project-scoped key is refused.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/public/organizations/projects` | List all projects in the org |
| `POST` | `/api/public/projects` | Create project. Body: `name` (str), `retention` (int, days), `metadata` (dict, optional) |
| `PUT` | `/api/public/projects/{projectId}` | Update project |
| `DELETE` | `/api/public/projects/{projectId}` | Delete project (processed asynchronously) |
| `GET` | `/api/public/projects/{projectId}/apiKeys` | List a project's API keys |
| `POST` | `/api/public/projects/{projectId}/apiKeys` | Create project API key. Body: `note`, and optionally **`publicKey` / `secretKey`** (caller-supplied values). Response includes `publicKey`, `secretKey` |
| `DELETE` | `/api/public/projects/{projectId}/apiKeys/{apiKeyId}` | Revoke a key |
| `GET`/`PUT`/`DELETE` | `/api/public/organizations/memberships` | Org memberships |
| `GET`/`PUT`/`DELETE` | `/api/public/projects/{projectId}/memberships` | Project memberships (user must already be in the org) |
| `GET` | `/api/public/organizations/apiKeys` | List org API keys |
| — | `/api/public/scim/{Users,Schemas,ResourceTypes,ServiceProviderConfig}` | SCIM user provisioning (pairs with Azure AD) |

`GET /api/public/projects` with a *project* key returns that key's own project.
That's useful as a smoke check that a key belongs to the project we think it does.

### 5.2 Licensing and bootstrap (from docs; **verify before relying on it**)

I couldn't read langfuse.com from this environment, so these points come from
search results summarising older doc snapshots and a third-party integration guide:

- On **self-hosted** Langfuse, the Organization Management API (org-scoped keys)
  is an **Enterprise Edition** feature that needs a license key
  (`LANGFUSE_EE_LICENSE_KEY`). Without it, the UI hides the org "API Keys" tab
  and the endpoints return a "not available on your current plan" error.
- Self-hosted also documents an instance **Admin API** authenticated with an
  `ADMIN_API_KEY` env var (Bearer token). It can create organizations and
  org-scoped API keys, which is the bootstrap for headless automation. Exact paths
  still need confirming.
- The `LANGFUSE_INIT_*` headless bootstrap creates one org + one project + one
  project key pair. As far as I know it does not create an org-scoped key.

**To confirm on our instance:** with an org key from the UI (if the tab is visible),
`curl -u "$ORG_PK:$ORG_SK" http://localhost:3000/api/public/organizations/projects`.
A 200 means the API is usable on our edition; a 403/plan error means it is EE-gated.

### 5.3 What automation would look like

A `scripts/provision-project.py <slug>` run by the platform team:

1. `POST /api/public/projects` `{name: <slug>, retention: <days>, metadata: {tl_project: <slug>}}`
2. `POST /api/public/projects/{id}/apiKeys` `{note: "otel-collector"}`, then base64 → `LANGFUSE_OTEL_AUTH_<SLUG>`
   into the secret store (Key Vault / K8s Secret), never into git
3. Optionally a second key for the team's agents (REST/scores), delivered via
   their own secret store
4. Append the slug to `otel/projects.yaml`, run the generator, open a PR
5. `PUT /api/public/projects/{id}/memberships` for the team's AAD group members

Idempotency: match on `name` via `GET /organizations/projects`, and store the
Langfuse `projectId` as an optional `langfuse_project_id:` field in
`projects.yaml` (the generator ignores unknown fields today, so this is additive).

### 5.4 Migration angle

- `POST …/apiKeys` accepts **caller-supplied `publicKey`/`secretKey`**. When moving
  projects to a new Langfuse instance (e.g. local → Azure), we can recreate each
  project and **re-issue the same key pairs**, so neither the Collector secrets nor
  agent env vars change.
- The per-project `endpoint:` in `projects.yaml` lets projects move one at a time
  (old instance and new instance side by side behind one Collector).
- Trace *data* migration is a separate problem (ClickHouse export/import). It isn't
  covered by these APIs.

---

## 6. Validation status

| Check | Status |
|---|---|
| Generator unit tests (`pytest scripts/tests`): every project gets exporter/route/pipeline, default route, every referenced component defined, registry validation | ✅ pass |
| SDK tests (`pytest sdk/python/tests`): `tl.project` on every span's resource, no-project path unchanged, release mirrored, warnings | ✅ pass |
| `otelcol-contrib validate` against the generated config | ✅ pass on v0.153.0, including a hyphenated slug and an unset per-project env var. A deliberately broken config fails, so the check is real. Newer `:latest` images not tested |
| Default path, real Langfuse (`send_test_trace.py`, no `LANGFUSE_PROJECT`) | ✅ pass through a temporary Collector running this config |
| Routing logic: registered, unknown, missing and hyphenated slugs; one request with two projects; OTLP HTTP and gRPC | ✅ pass against a stand-in OTLP sink (route and credentials checked per case) |
| nginx token → project binding (§3) | ✅ pass with the real `nginx.conf` (only token maps changed) against the sink: bound token wins over agent slug and spoofed header, spoofed header dropped for unbound token, bad token gets 401 |
| End-to-end two-project trace in real Langfuse (`send_test_trace.py` isolation check) | ⏳ needs a second project and its API keys |
| Grafana panels and Project variable with `tl_project` data | ⏳ needs the live Collector on this config |
| Org API availability on our edition (§5.2) | ⏳ needs an org key from the UI. A project key gets 403, which says nothing about the edition. `.env` sets no `LANGFUSE_EE_LICENSE_KEY` or `ADMIN_API_KEY` |
