"""
Emit token + cost counters as OpenTelemetry metrics so they reach Prometheus
(via the Collector) for the Grafana cost panel.

Point OTEL_EXPORTER_OTLP_ENDPOINT at the Collector (http://otel-collector:4318
inside the stack, or http://localhost:4318 from the host).

LangFuse still owns the authoritative per-trace cost/eval views; Prometheus
just gets rolled-up counters for the ops dashboard.
"""

from opentelemetry import metrics

_meter = metrics.get_meter("platform.agents")

_tokens = _meter.create_counter(
    "agent_tokens_total", unit="1", description="Tokens consumed per agent/model"
)
_cost = _meter.create_counter(
    "agent_cost_usd_total", unit="usd", description="Estimated USD cost per agent/model"
)


def record_usage(
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    attrs_in = {"agent.name": agent, "gen_ai.request.model": model, "token.type": "input"}
    attrs_out = {"agent.name": agent, "gen_ai.request.model": model, "token.type": "output"}
    _tokens.add(input_tokens, attrs_in)
    _tokens.add(output_tokens, attrs_out)
    _cost.add(cost_usd, {"agent.name": agent, "gen_ai.request.model": model})
