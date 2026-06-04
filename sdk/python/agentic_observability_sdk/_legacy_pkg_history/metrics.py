"""
Token + cost OTel counters -> OTel Collector -> Prometheus -> Grafana cost panel.

OTEL_EXPORTER_OTLP_ENDPOINT must point at the Collector (:4318), not LangFuse.
When the env var is unset, record_usage() is a no-op — the agent still runs.
"""

import os

from opentelemetry import metrics as otel_metrics

_metrics_initialised = False

# Counters are created lazily (inside _get_counters) so they are always bound
# to the real MeterProvider, not the NoOp provider that exists at import time.
_tokens = None
_cost = None


def init_metrics() -> None:
    """Wire the OTel MeterProvider with an OTLP exporter pointing at the Collector.

    Idempotent. Called once by get_tracer(). No-op when
    OTEL_EXPORTER_OTLP_ENDPOINT is unset (counters stay local no-ops).
    """
    global _metrics_initialised
    if _metrics_initialised:
        return
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "platform-agent"),
            "deployment.environment": os.environ.get("DEPLOY_ENV", "local"),
        })
        reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        otel_metrics.set_meter_provider(
            MeterProvider(resource=resource, metric_readers=[reader])
        )
        _metrics_initialised = True
    except Exception:
        # Never let observability wiring crash the agent.
        pass


def _get_counters():
    """Return (tokens_counter, cost_counter), creating them lazily after the
    MeterProvider is set so they bind to the real exporter, not the NoOp."""
    global _tokens, _cost
    if _tokens is None:
        meter = otel_metrics.get_meter("platform.agents")
        _tokens = meter.create_counter(
            "agent_tokens_total",
            unit="1",
            description="Tokens consumed per agent/model",
        )
        _cost = meter.create_counter(
            "agent_cost_usd_total",
            unit="usd",
            description="Estimated USD cost per agent/model",
        )
    return _tokens, _cost


def record_usage(
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    """Record token consumption and USD cost for a single agent invocation.

    Call once per LLM response, after receiving the completion:

        record_usage("my-agent", "gpt-4o", input_tokens=800, output_tokens=200, cost_usd=0.012)

    Values roll up in Prometheus and appear in the Grafana agent-ops dashboard.
    LangFuse retains the per-trace authoritative view; this feeds the ops counters.
    """
    try:
        tokens, cost = _get_counters()
        attrs_in  = {"agent.name": agent, "gen_ai.request.model": model, "token.type": "input"}
        attrs_out = {"agent.name": agent, "gen_ai.request.model": model, "token.type": "output"}
        tokens.add(input_tokens, attrs_in)
        tokens.add(output_tokens, attrs_out)
        cost.add(cost_usd, {"agent.name": agent, "gen_ai.request.model": model})
    except Exception:
        pass
