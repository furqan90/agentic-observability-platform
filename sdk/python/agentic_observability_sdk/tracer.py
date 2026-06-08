"""
 LangFuse v3 tracer — the ONLY module allowed to import ``langfuse``.

Reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST from env.
If we ever leave LangFuse, we rewrite this one file — not N agents.
"""

import os
from functools import lru_cache

from langfuse import Langfuse, get_client, observe  # sole langfuse import point


def _init_otel_trace_provider() -> None:
    """Set up the global OTel TracerProvider → Collector BEFORE LangFuse initialises.

    Without this, Langfuse(host=...) creates its own TracerProvider that sends
    spans directly to LangFuse, bypassing the Collector — which means:
      - PII redaction processor never runs
      - spanmetrics connector never fires (no RED metrics in Grafana)
      - The "one wire-level swap point" architectural rule is violated

    When OTEL_EXPORTER_OTLP_ENDPOINT is set (pointing at the Collector on :4318),
    this wires the global TracerProvider to route ALL spans through the Collector.
    LangFuse v3 detects the existing global provider and adds its span processor
    to it rather than creating a new direct-to-LangFuse one.

    When OTEL_EXPORTER_OTLP_ENDPOINT is unset (local dev without a Collector),
    this is a no-op and LangFuse falls back to its direct path — acceptable for
    local development where the full stack isn't running.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return

    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource

    # Only configure if not already set — preserves any host-level OTel setup.
    existing = otel_trace.get_tracer_provider()
    if type(existing).__name__ not in ("ProxyTracerProvider", "NoOpTracerProvider"):
        return  # already configured by host or a prior call

    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "platform-agent"),
        "deployment.environment": os.environ.get("DEPLOY_ENV", "local"),
    })
    # /v1/traces is the standard OTLP HTTP traces path
    exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    otel_trace.set_tracer_provider(provider)


@lru_cache(maxsize=1)
def get_tracer() -> "Langfuse":
    """Initialise LangFuse + OTel. Idempotent — safe to call multiple times.

    Call once at agent startup:
        from agentic_observability_sdk import get_tracer
        get_tracer()

    Trace path (when OTEL_EXPORTER_OTLP_ENDPOINT is set):
        @trace_agent → OTel TracerProvider → Collector :4318
                         → PII redaction → LangFuse (traces + evals)
                         → spanmetrics → Prometheus → Grafana (RED)

    Trace path (when OTEL_EXPORTER_OTLP_ENDPOINT is unset — local dev only):
        @trace_agent → LangFuse TracerProvider → LangFuse :3000 directly
        (no PII redaction, no spanmetrics — acceptable without a Collector)
    """
    # Must happen BEFORE Langfuse() — see _init_otel_trace_provider docstring.
    _init_otel_trace_provider()

    Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
        environment=os.environ.get("DEPLOY_ENV", "local"),
    )

    # Metrics path: token/cost counters → Collector → Prometheus → Grafana.
    from .metrics import init_metrics
    init_metrics()

    return get_client()


def shutdown() -> None:
    """Flush and shut down all telemetry providers cleanly.

    Call at process exit in CLI tools, scripts, or serverless handlers to
    ensure buffered spans and metrics are flushed before the process dies.

        import atexit
        from agentic_observability_sdk import shutdown
        atexit.register(shutdown)
    """
    # Flush LangFuse REST queue (scores, datasets, etc.)
    try:
        get_client().flush()
    except Exception:
        pass

    # Flush + shut down OTel trace provider (spans in BatchSpanProcessor buffer)
    try:
        from opentelemetry import trace as otel_trace
        provider = otel_trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception:
        pass

    # Flush + shut down OTel metric provider (cost/token counters)
    try:
        from opentelemetry import metrics as otel_metrics
        provider = otel_metrics.get_meter_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception:
        pass


def get_langchain_handler():
    """Return a LangFuse callback handler bound to the current trace context.

    Pass this to any LangChain model invocation so the exact messages
    (system prompt, human, tool messages) and the raw LLM response are
    recorded as child observations inside the current @trace_agent span:

        from agentic_observability_sdk import get_langchain_handler

        @trace_agent(name="my-agent")
        def run(input: str) -> str:
            handler = get_langchain_handler()
            result = llm.invoke(messages, config={"callbacks": [handler]})
            return result.content

    Or bind once on the model so every call is captured automatically:

        handler = get_langchain_handler()
        llm = ChatOpenAI(...).with_config({"callbacks": [handler]})

    Must be called AFTER get_tracer() — i.e. after LangFuse is initialised.
    Requires: pip install "agentic-observability-sdk[langchain]"
    """
    try:
        from langfuse.langchain import CallbackHandler
        # v3: CallbackHandler auto-binds to the active span context — no
        # get_client().get_langchain_handler() method exists in v3.
        return CallbackHandler()
    except ImportError as exc:
        raise ImportError(
            "LangChain integration requires the langchain package.\n"
            'Run: pip install "agentic-observability-sdk[langchain]"'
        ) from exc


def get_openai_client(**kwargs):
    """Return a LangFuse-instrumented OpenAI client.

    Drop-in replacement for ``openai.OpenAI(**kwargs)``. Every
    ``client.chat.completions.create()`` call is automatically recorded as a
    LangFuse generation observation — exact messages, model, token counts,
    finish reason — nested under the current ``@trace_agent`` span.

    Requires the openai package:
        pip install "agentic-observability-sdk[openai]"
        # or: pip install openai

    Usage:
        from agentic_observability_sdk import get_openai_client

        client = get_openai_client()   # replaces openai.OpenAI()

        @trace_agent(name="my-agent")
        def run(messages):
            # All calls below are auto-recorded — no per-call changes needed.
            return client.chat.completions.create(
                model="gpt-4o", messages=messages
            ).choices[0].message.content

    Must be called after get_tracer() so LangFuse is initialised.
    Pass any kwargs accepted by openai.OpenAI() (api_key, base_url, etc.).
    """
    try:
        from langfuse.openai import OpenAI
        return OpenAI(**kwargs)
    except ImportError as exc:
        raise ImportError(
            "OpenAI integration requires the openai package.\n"
            "Run: pip install \"agentic-observability-sdk[openai]\""
            " or pip install openai"
        ) from exc


def get_anthropic_client(**kwargs):
    """Return an auto-instrumented Anthropic client via OpenInference.

    LangFuse v3 does not ship a ``langfuse.anthropic`` wrapper. The official
    supported path is OpenInference's ``AnthropicInstrumentor``, which
    monkey-patches the ``anthropic`` SDK at the process level so every
    ``client.messages.create()`` call is auto-recorded — exact messages,
    model, token counts, stop reason — as a LangFuse observation.

    This function instruments once (idempotent) and returns a plain
    ``anthropic.Anthropic(**kwargs)`` instance. Use it exactly like
    ``anthropic.Anthropic()``:

        from agentic_observability_sdk import get_anthropic_client

        client = get_anthropic_client()   # replaces anthropic.Anthropic()

        @trace_agent(name="my-agent")
        def run(messages: list) -> str:
            return client.messages.create(
                model="claude-sonnet-4-6", messages=messages, max_tokens=1024
            ).content[0].text

    Requires the anthropic extra:
        pip install "agentic-observability-sdk[anthropic]"

    ----
    Alternative — OpenAI-compatible endpoint (no extra install):

    Anthropic exposes an OpenAI-compatible API, so ``get_openai_client()``
    works for Claude models if you point it at Anthropic's endpoint:

        client = get_openai_client(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            base_url="https://api.anthropic.com/v1",
        )
        client.chat.completions.create(model="claude-sonnet-4-6", messages=messages)

    Trade-off: OpenAI-compat path works today with zero extra deps, but only
    exposes the OpenAI-compatible surface — Claude-specific features (extended
    thinking, native vision formats) are not accessible through it.
    """
    try:
        from openinference.instrumentation.anthropic import AnthropicInstrumentor
        import anthropic as _anthropic
        AnthropicInstrumentor().instrument()   # idempotent — safe to call multiple times
        return _anthropic.Anthropic(**kwargs)
    except ImportError as exc:
        raise ImportError(
            "Anthropic auto-instrumentation requires openinference-instrumentation-anthropic.\n"
            'Run: pip install "agentic-observability-sdk[anthropic]"'
        ) from exc


def get_propagation_headers() -> dict:
    """Return W3C Trace Context headers for cross-agent handoff.

    Call this in agent A before handing off to agent B. Pass the returned
    dict to agent B (via HTTP headers, message metadata, etc.) and call
    restore_propagation_context() there to link both agents under the same
    session trace in LangFuse.

        # Agent A (before handoff)
        headers = get_propagation_headers()
        requests.post(agent_b_url, headers=headers, json=payload)

        # Agent B (at startup of the new invocation)
        restore_propagation_context(request.headers)

    See CONTRACT.md §6 for the full cross-agent session stitching design.
    """
    from opentelemetry import propagate
    carrier: dict = {}
    propagate.inject(carrier)
    return carrier


def restore_propagation_context(headers: dict) -> None:
    """Restore W3C Trace Context from headers received at a handoff boundary.

    Call this at the start of agent B's invocation with the headers dict
    received from agent A. This links agent B's spans as children of agent A's
    trace so LangFuse shows them as one connected session.

        # Agent B (at start of invocation)
        restore_propagation_context(request.headers)
        get_tracer()  # must be called after context is restored
    """
    from opentelemetry import propagate
    propagate.extract(headers)


# Re-export langfuse helpers so decorators.py never imports langfuse directly.
# This keeps the "one langfuse import file" rule intact.
_observe = observe
_get_client = get_client
