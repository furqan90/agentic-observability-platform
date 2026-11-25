# Observability SDK — Cross-Language Contract

> Every language SDK in this repository must implement this contract.
> It is the single source of truth for attribute names, span structure,
> and the required public API. Deviating from it breaks dashboards,
> cost rollups, and alert routing for every team on the platform.

---

## 1. Metadata Envelope

Every root trace MUST carry these attributes. The SDK facade is responsible
for stamping them — agent code should never set them directly.

| Attribute | Type | Required | Notes |
|---|---|---|---|
| `tl.team` | string | ✅ | Owning team slug. e.g. `eka-team` |
| `tl.agent` | string | ✅ | Agent identifier. e.g. `incident-agent` |
| `tl.agent_ver` | string | ✅ | Semver. e.g. `1.4.2` |
| `tl.env` | string | ✅ | `prod` \| `staging` \| `dev` |
| `tl.user_id` | string | ✅ | `sha256(AAD UPN)` — NEVER plaintext |
| `tl.session_id` | string | ✅ | UUID. Conversation/session boundary |
| `tl.project` | string | optional | LangFuse project slug (`otel/projects.yaml`). Set as an OTel **resource** attribute when `LANGFUSE_PROJECT` is set. The Collector routes traces on it. Unknown/missing → default project |
| `tl.cost_center` | string | ✅ | Finance rollup code. e.g. `AI-OPS-001` |

**User ID rule:** Hash at the SDK layer using `sha256(upn)`. Never allow a
plaintext identity to reach ClickHouse. Maintain a reverse-mapping table in
your directory service for governance investigations.

---

## 2. Required Public API

Every language SDK must expose these functions (names adapted to language
conventions — snake_case for Python, camelCase for TypeScript, etc.):

| Function | Purpose |
|---|---|
| `getTracer()` | Initialise the LangFuse client and OTel provider. Idempotent. |
| `shutdown()` | Flush and shut down all telemetry providers cleanly. |
| `traceAgent(name, tags?, userId?)` | Decorator/wrapper that creates a root LangFuse trace. |
| `traceTool(name)` | Decorator/wrapper that creates a child span. |
| `recordUsage(agent, model, inputTokens, outputTokens, costUsd)` | Emit token/cost OTel counters. |
| `getLangchainHandler()` | Return a LangFuse callback handler for LangChain/LangGraph. |
| `getOpenaiClient(**kwargs)` | Return a LangFuse-instrumented OpenAI client. |
| `getAnthropicClient(**kwargs)` | Return an auto-instrumented Anthropic client. |
| `traceAttributes(userId?, sessionId?)` | Per-request context: every span of the trace started inside it carries the user and session (§8.3). |
| `initLogging(level?)` | Ship the process's log records over OTLP (§8). Usable without LangFuse installed. |

**Enforcement rule:** Agent code imports only from the SDK facade. Direct
imports of `langfuse`, `openai`, `anthropic`, or OTel primitives outside the
SDK package are a review-blocking violation. Enforce via PR lint from day one.

---

## 3. Span Naming Conventions

Consistent span names are what make the Grafana dashboards and PromQL queries
work across all agents and teams.

| Span type | Naming pattern | Example |
|---|---|---|
| Agent root trace | `agent.<name>` | `agent.incident-agent` |
| Tool call | `tool.<name>` | `tool.jira-search` |
| LLM generation | `llm.<model-slug>` | `llm.gpt-4o` |
| Sub-agent handoff | `handoff.<target-agent>` | `handoff.escalation-agent` |
| Retrieval step | `retrieval.<store>` | `retrieval.qdrant` |

---

## 4. OTel Attribute Names

Use these exact attribute names so Prometheus spanmetrics and Grafana dashboards
work without per-language dashboard configuration.

### Agent / Span attributes (from metadata envelope)
```
tl.team            tl.agent           tl.agent_ver
tl.env             tl.user_id         tl.session_id
tl.project         tl.cost_center
```

### GenAI semantic conventions (OTel GenAI spec)
```
gen_ai.request.model           # model identifier
gen_ai.usage.input_tokens      # prompt tokens
gen_ai.usage.output_tokens     # completion tokens
gen_ai.system                  # "openai" | "anthropic" | "azure"
```

### Custom cost/token OTel counter attributes
```
agent.name                     # maps to tl.agent
gen_ai.request.model           # as above
token.type                     # "input" | "output"
```

---

## 5. Trace Hierarchy

For multi-step and multi-agent workflows the span tree MUST follow this
structure so LangFuse renders a coherent waterfall and session stitching works:

```
Trace: <tl.agent> / <tl.session_id>
└── Span: agent.<name>                   ← traceAgent() root
    ├── Span: tool.<name>                ← traceTool()
    ├── Generation: llm.<model>          ← get*Client() auto-instrumented
    │       gen_ai.usage.input_tokens
    │       gen_ai.usage.output_tokens
    └── Span: handoff.<target>           ← cross-agent handoff boundary
            traceparent → propagated to target agent
```

---

## 6. Cross-Agent Context Propagation

When agent A hands off to agent B, propagate the W3C Trace Context
(`traceparent` / `tracestate` headers) so LangFuse links them as one session.

Every SDK must expose:

```
getPropagationHeaders() → dict          # {"traceparent": "...", "tracestate": "..."}
traceContext(headers) → context manager # attach the headers' context for a block, then detach (0.4.1)
restorePropagationContext(headers) → token | null   # attach only; the caller detaches
```

`restorePropagationContext` must **attach** the extracted context (Python SDK ≤ 0.4.0 extracted and discarded
it, a no-op). Header names are case-insensitive; missing or malformed headers are a no-op, never an error.
None of these needs LangFuse: a service that only logs (§8) restores its caller's context per request, so its
log records carry the caller's `trace_id` without creating spans of its own.

The `tl.session_id` attribute provides the LangFuse-level session grouping;
the `traceparent` provides the OTel-level parent-child link. Both are required.

---

## 7. Environment Variables (all languages)

| Variable | Required | Purpose |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY` | ✅ | LangFuse project public key (same project as `LANGFUSE_PROJECT`) |
| `LANGFUSE_SECRET_KEY` | ✅ | LangFuse project secret key (same project as `LANGFUSE_PROJECT`) |
| `LANGFUSE_PROJECT` | optional | Project slug → `tl.project` resource attribute. Picks the LangFuse project on the Collector path. Unset → default project |
| `LANGFUSE_HOST` | ✅ | LangFuse web URL (auth handshake only) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | ✅ | OTel Collector HTTP endpoint (`:4318`) |
| `DEPLOY_ENV` | ✅ | `prod` \| `staging` \| `dev` |
| `OTEL_SERVICE_NAME` | optional | Overrides `service.name` resource attribute |
| `OTEL_LOG_LEVEL` | optional | Lowest level `initLogging()` ships (default `INFO`; the Collector drops DEBUG anyway) |
| `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | optional | Full logs URL, when logs go elsewhere than `<OTEL_EXPORTER_OTLP_ENDPOINT>/v1/logs` |
| `HOST_NAME` | optional | `host.name` resource attribute (Loki label `host_name`). In a container, set it to the machine's name |
| `SERVICE_VERSION` / `APP_VERSION` | optional | `service.version` resource attribute |

**Project routing:** on the Collector path, the `tl.project` **resource** attribute
alone picks the LangFuse project. Credentials never leave the platform. When
`LANGFUSE_PROJECT` is set, every SDK MUST stamp it on the trace *and* metric resource.
Every SDK MUST also add a `tl.project` datapoint attribute on the token/cost counters
(Prometheus label `tl_project`), using `unset` when there is no project.
One project per process. See `docs/MULTI-PROJECT.md`.

**Important:** `LANGFUSE_HOST` and `OTEL_EXPORTER_OTLP_ENDPOINT` point at
different services. `LANGFUSE_HOST` is for key auth only. Spans and metrics
travel via `OTEL_EXPORTER_OTLP_ENDPOINT` (the Collector). Pointing spans
directly at LangFuse bypasses PII redaction and spanmetrics.

---

## 8. Logs (SDK 0.4.0)

Logs are the third signal, on the same OTLP interface as traces and metrics: the SDK sends log records to
`<OTEL_EXPORTER_OTLP_ENDPOINT>/v1/logs`; the Collector redacts, filters and exports them to Loki.

### 8.1 One Resource for all three signals

Traces, metrics and logs are built with the same Resource, so a log line, its span and that turn's counters
line up:

| Attribute | From | Loki label |
|---|---|---|
| `service.name` | `OTEL_SERVICE_NAME` (default `agent`) | `service_name` |
| `deployment.environment` | `DEPLOY_ENV` (default `local`) | `deployment_environment` |
| `tl.project` | `LANGFUSE_PROJECT` | `tl_project` |
| `host.name` | `HOST_NAME`, else the host name | `host_name` |
| `service.instance.id` | the host name (a container id in Docker) | — |
| `service.version` | `SERVICE_VERSION` / `APP_VERSION` | — |

Only those four become Loki labels. Everything else is structured metadata: queryable, not indexed.
**Never** make trace, thread, page or user ids labels: unbounded values blow up the index.

### 8.2 `initLogging()`

- **One handler on the root logger,** at `OTEL_LOG_LEVEL` (default INFO), next to the service's own console
  and file handlers. The local files stay the full record.
- **Loggers filter first:** `initLogging()` changes no logger's level. Set the service's levels before calling
  it.
- **A no-op without an endpoint; idempotent; never raises.** A setup failure leaves the service on local logs.
- **Never blocks the caller:** a background batch (queue 2048). When the queue is full or the Collector is down,
  records are dropped and counted: `agent_log_records_dropped_total{reason="queue_full"|"export_failed",
  tl_project}`.
- **What a record carries:**
  - the `trace_id` / `span_id` of the active span (the ids LangFuse shows);
  - `severity_number` / `severity_text`;
  - the message as `body`;
  - `code.*`;
  - an exception as **one** record with `exception.type`, `exception.message` and `exception.stacktrace`;
  - every `extra={...}` field as an attribute.
- **Loop guard:** records from the export path (`opentelemetry.*`, `urllib3.connectionpool`) never enter the
  queue.
- **`shutdown()` flushes the logs too.** `logging.basicConfig(force=True)` after `initLogging()` removes the
  handler; `shutdown()` warns when that happened.
- **Importable without LangFuse:** `from agentic_observability_sdk.logs import init_logging` needs only the
  `opentelemetry-*` packages.
- **Write events, not prose:** a short event name as the message, the values as `extra` fields (so LogQL can
  filter `| ms > 20000`). Never log a user's question, an answer, retrieved text, keys or emails: LangFuse
  holds the conversation, and the Collector redacts what slips through.

### 8.3 Users and sessions

`traceAttributes(userId, sessionId)` wraps one request:
- **`userId` becomes LangFuse's user.** It must be an opaque id: a hash or a random per-browser id, never a
  name or an email.
- **`sessionId` groups a conversation's turns.**
- **Nesting:** an inner `traceAgent` without a `userId` keeps the outer value.

---

## 9. Language SDK Checklist

Before shipping a new language SDK, verify:

- [ ] All required metadata envelope attributes stamped on every root trace
- [ ] `tl.project` stamped on the OTel trace + metric **resource** when `LANGFUSE_PROJECT` is set
- [ ] Traces, metrics and logs share one Resource (§8.1)
- [ ] `initLogging()`: non-blocking, drop counter, loop guard, flushed by `shutdown()`, importable without LangFuse (§8.2)
- [ ] `traceAttributes()` reaches every span of the trace, and survives a nested `traceAgent` (§8.3)
- [ ] All required public API functions implemented
- [ ] Span naming follows §3 conventions
- [ ] OTel attribute names match §4 exactly
- [ ] `getPropagationHeaders()` / `restorePropagationContext()` implemented
- [ ] User ID hashing enforced at SDK layer (never raw identity)
- [ ] `shutdown()` / flush callable and registered at process exit
- [ ] `langfuse` / OTel / provider SDK imports isolated to one module
- [ ] Package published as `agentic-observability-sdk-<lang>` on the relevant registry
- [ ] `AGENT-ONBOARDING.md` written for the language
- [ ] Integration tests pass against a local Docker Compose stack

---

*internal · Classification: Internal*
