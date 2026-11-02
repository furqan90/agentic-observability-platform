# Per-client LangFuse project routing — plan

**Status:** proposed · **Owner:** platform · **Date:** 2026-10-30

## Problem

The OTel Collector exports every trace to LangFuse with one fixed credential pair (`LANGFUSE_OTEL_AUTH` in `.env`). LangFuse assigns a span to the project that owns the credentials it was sent with, so **every client's traces land in the bootstrap project** (`LANGFUSE_INIT_PROJECT_*`), whichever team or service sent them.

Clients cannot choose a project, even though the SDK contract (`sdk/CONTRACT.md`) already asks each one to configure its own `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`. The collector discards those keys.

Found while wiring the LiteLLM proxy (`agentic-coding-platform`): its traces could only go to "Agent Platform". The only alternative was to change the collector's single credential, which would move every other client too.

## Goal

- A client that sends OTLP to the collector decides which LangFuse project its traces go to.
- Clients that send nothing project-specific keep working unchanged and land in the default project.
- Prometheus span metrics (`span_metrics` connector) keep covering all clients in one place.
- No collector config change or restart to onboard a new project.

## Options considered

| | A. Forward client credentials (recommended) | B. Route on a resource attribute |
|---|---|---|
| Client sends | `Authorization: Basic base64(pk:sk)` on its OTLP export | resource attribute e.g. `langfuse.project=<slug>` |
| Collector holds | only the default credential | a key pair + exporter + pipeline **per project** |
| New project | create keys in LangFuse, give them to the client | edit collector config + `.env`, restart |
| Authorization | enforced by LangFuse: a client can only write to projects it has keys for | none: any client can write to any project by setting the attribute |
| Fits SDK contract | yes: keys are already per-client | no: adds a new attribute |

**Recommendation: A.** It needs no collector change per project, and LangFuse stays the authority on who may write where. B fits only clients that must not hold LangFuse keys. It can be added later as a routing table in front of A.

## Design (option A)

The collector propagates the incoming `authorization` header to the LangFuse exporter, and falls back to the default credential when the client sends none.

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
        include_metadata: true          # expose incoming gRPC metadata / HTTP headers to the pipeline
      http:
        endpoint: 0.0.0.0:4318
        include_metadata: true

extensions:
  headers_setter:
    headers:
      - key: Authorization
        from_context: authorization     # client-supplied header, lower-case key
        default_value: "Basic ${env:LANGFUSE_OTEL_AUTH}"   # unchanged behaviour for clients that send nothing
        action: upsert

processors:
  batch:
    metadata_keys: [authorization]      # REQUIRED: never mix two clients' spans in one export batch
    metadata_cardinality_limit: 100     # bounds the number of distinct projects batched concurrently

exporters:
  otlp_http/langfuse:
    endpoint: http://langfuse-web:3000/api/public/otel
    auth:
      authenticator: headers_setter     # replaces the static `headers:` block

service:
  extensions: [headers_setter]
```

Important details:

- **`batch.metadata_keys` is not optional.** Without it the batch processor merges spans from different clients and loses the request context, so either every batch gets the default project, or spans are sent with another client's credentials.
- The `span_metrics` connector reads span data, not credentials. Metrics stay project-agnostic and keep the `agent.name` / `gen_ai.request.model` dimensions.
- `attributes/redaction` and `resource` processors are unchanged and still run before export.
- `from_context` / `default_value` behaviour must be checked against the pinned collector version. The compose file currently uses `otel/opentelemetry-collector-contrib:latest`, and pinning it is part of this work.

## Client changes

| Client | Change |
|---|---|
| Python SDK (`agentic_observability_sdk`) | When `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` are set and `OTEL_EXPORTER_OTLP_ENDPOINT` points at the collector, attach `Authorization: Basic base64(pk:sk)` to the `OTLPSpanExporter` headers. Opt-out flag for clients that should use the default project. |
| LiteLLM proxy | Set `OTEL_EXPORTER_OTLP_HEADERS=authorization=Basic%20<base64(pk:sk)>` (URL-encoded per OTel spec) in `proxy/.env`. No code change. |
| Any other OTel SDK | Same standard env var `OTEL_EXPORTER_OTLP_HEADERS`. |
| Clients sending nothing | No change; default project. |

`sdk/CONTRACT.md` and `sdk/python/AGENT-ONBOARDING.md`: document that the LangFuse keys now also pick the project for collector-routed traces, and how to obtain project keys (LangFuse → project → Settings → API Keys).

## Security

- Client LangFuse secret keys now transit the collector. Ports 4317/4318 must not be exposed beyond the trusted network, and TLS on the receivers is required before any non-local deployment.
- Do not log metadata. Keep the `debug` exporter off, or make sure it doesn't print `authorization`.
- An invalid client key makes LangFuse return 401 for that batch only. Spans are dropped and **do not fall back to the default project**, because silently re-routing would hide misconfiguration. Surface it through collector exporter metrics (`otelcol_exporter_send_failed_spans`) and an Alertmanager rule.

## Rollout

1. Pin the collector image version; confirm `headers_setter` with `from_context` + `default_value` is available.
2. Apply the collector config above. With no client changes, verify all traces still land in the default project (regression check).
3. Create a second LangFuse project. Send a test span with its credentials via `curl`/`telemetrygen` to both 4317 (gRPC) and 4318 (HTTP), and confirm it lands there and not in the default.
4. Send concurrent spans from two clients with different keys. Confirm no cross-contamination (validates `batch.metadata_keys`).
5. Send a span with a bad key: confirm 401 is counted in `otelcol_exporter_send_failed_spans` and nothing appears in either project.
6. Ship SDK change + docs; switch the LiteLLM proxy to its own project via `OTEL_EXPORTER_OTLP_HEADERS`.
7. Add the export-failure alert rule.

## Open questions

- Should a client be able to opt *into* the default project explicitly while holding keys (e.g. shared dev keys)?
- Do any clients need option B (no LangFuse keys on the client)? If so, add a `routing` connector keyed on a resource attribute that maps to collector-held credentials, evaluated after the header check.
- Per-project rate limiting at the collector, or rely on LangFuse ingestion limits?
