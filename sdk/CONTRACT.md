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
| `tl.project` | string | ✅ | LangFuse project slug (`otel/projects.yaml`). Set as an OTel **resource** attribute. The Collector routes traces on it. Unknown/missing → default project |
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
restorePropagationContext(headers: dict) → void
```

The `tl.session_id` attribute provides the LangFuse-level session grouping;
the `traceparent` provides the OTel-level parent-child link. Both are required.

---

## 7. Environment Variables (all languages)

| Variable | Required | Purpose |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY` | ✅ | LangFuse project public key (same project as `LANGFUSE_PROJECT`) |
| `LANGFUSE_SECRET_KEY` | ✅ | LangFuse project secret key (same project as `LANGFUSE_PROJECT`) |
| `LANGFUSE_PROJECT` | ✅ | Project slug → `tl.project` resource attribute. Picks the LangFuse project on the Collector path. Unset → default project |
| `LANGFUSE_HOST` | ✅ | LangFuse web URL (auth handshake only) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | ✅ | OTel Collector HTTP endpoint (`:4318`) |
| `DEPLOY_ENV` | ✅ | `prod` \| `staging` \| `dev` |
| `OTEL_SERVICE_NAME` | optional | Overrides `service.name` resource attribute |

**Project routing:** on the Collector path, the `tl.project` **resource** attribute
alone picks the LangFuse project. Credentials never leave the platform. Every SDK
MUST stamp it on the trace *and* metric resource from `LANGFUSE_PROJECT`, and add
it as a datapoint attribute on the token/cost counters (Prometheus label `tl_project`).
One project per process. See `docs/MULTI-PROJECT.md`.

**Important:** `LANGFUSE_HOST` and `OTEL_EXPORTER_OTLP_ENDPOINT` point at
different services. `LANGFUSE_HOST` is for key auth only. Spans and metrics
travel via `OTEL_EXPORTER_OTLP_ENDPOINT` (the Collector). Pointing spans
directly at LangFuse bypasses PII redaction and spanmetrics.

---

## 8. Language SDK Checklist

Before shipping a new language SDK, verify:

- [ ] All 8 metadata envelope attributes stamped on every root trace
- [ ] `tl.project` stamped on the OTel trace + metric **resource** from `LANGFUSE_PROJECT`
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
