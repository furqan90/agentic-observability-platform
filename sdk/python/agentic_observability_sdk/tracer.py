"""
LangFuse tracer -- the ONLY module allowed to import ``langfuse``.

Reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST and
LANGFUSE_PROJECT from env.
If we ever leave LangFuse, we rewrite this one file -- not N agents.
"""

import os
import re
import warnings
from functools import lru_cache

from langfuse import Langfuse, get_client, observe, propagate_attributes  # sole langfuse import point


def _make_collector_exporter():
    """Return an OTLPSpanExporter aimed at the Collector, or None.

    When OTEL_EXPORTER_OTLP_ENDPOINT is set (pointing at the Collector on :4318),
    returns an exporter that routes ALL spans through the Collector:
        PII redaction -> LangFuse (traces + evals)
        spanmetrics  -> Prometheus -> Grafana (RED metrics)

    When OTEL_EXPORTER_OTLP_ENDPOINT is unset (local dev without a Collector),
    returns None -- LangFuse falls back to its direct path. Acceptable for
    local development; never for production (no PII redaction, no spanmetrics).

    In LangFuse v4, passing span_exporter= to Langfuse() sets the global OTel
    TracerProvider with that exporter, so context propagation still works.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    return OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")


# Resource attribute the Collector routes on (otel/projects.yaml). See CONTRACT.md §1.
PROJECT_ATTR = "tl.project"
PROJECT_ENV = "LANGFUSE_PROJECT"
_PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def get_project() -> "str | None":
    """Return the Langfuse project slug this process traces into, or None.

    Read from LANGFUSE_PROJECT. The slug must be listed in the platform's
    otel/projects.yaml; an unknown slug is not an error -- the Collector
    routes it to the default project -- so a malformed one only warns.
    One project per process (see docs/MULTI-PROJECT.md for the multi-project
    provision).
    """
    project = os.environ.get(PROJECT_ENV, "").strip()
    if not project:
        return None
    if not _PROJECT_SLUG_RE.match(project):
        warnings.warn(
            f"{PROJECT_ENV}={project!r} is not a valid project slug "
            f"({_PROJECT_SLUG_RE.pattern}); the Collector will route these traces "
            "to the default project.",
            stacklevel=2,
        )
    return project


def _make_project_tracer_provider(project: str, environment: str):
    """Build and register a global TracerProvider whose Resource carries
    ``tl.project`` -- the attribute the Collector's routing connector reads.

    Langfuse's own provider exposes no hook for extra resource attributes, so
    when a project is set we build the provider ourselves, mirroring what
    Langfuse would set (environment, release, sample rate), and hand it to
    Langfuse(tracer_provider=...).

    Returns None (and warns) if another library already registered a global
    TracerProvider: its Resource cannot be changed after the fact. In that case
    set OTEL_RESOURCE_ATTRIBUTES=tl.project=<slug> before that library starts.
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    if not isinstance(otel_trace.get_tracer_provider(), otel_trace.ProxyTracerProvider):
        warnings.warn(
            f"A global OpenTelemetry TracerProvider is already registered, so "
            f"{PROJECT_ATTR}={project!r} cannot be added to its resource and traces "
            f"will go to the default project. Set "
            f"OTEL_RESOURCE_ATTRIBUTES={PROJECT_ATTR}={project} instead.",
            stacklevel=3,
        )
        return None

    attrs = {
        PROJECT_ATTR: project,
        "langfuse.environment": environment,      # mirrors Langfuse's own provider
    }
    release = os.environ.get("LANGFUSE_RELEASE")
    if release:
        attrs["langfuse.release"] = release

    sample_rate = float(os.environ.get("LANGFUSE_SAMPLE_RATE", 1.0))
    sampler = TraceIdRatioBased(sample_rate) if sample_rate < 1 else None

    provider = TracerProvider(resource=Resource.create(attrs), sampler=sampler)
    otel_trace.set_tracer_provider(provider)
    return provider


@lru_cache(maxsize=1)
def get_tracer() -> "Langfuse":
    """Initialise LangFuse + OTel. Idempotent -- safe to call multiple times.

    Call once at agent startup:
        from agentic_observability_sdk import get_tracer
        get_tracer()

    Trace path (when OTEL_EXPORTER_OTLP_ENDPOINT is set):
        @trace_agent -> Collector :4318
                         -> PII redaction -> LangFuse (traces + evals)
                         -> spanmetrics   -> Prometheus -> Grafana (RED)

    Trace path (when OTEL_EXPORTER_OTLP_ENDPOINT is unset -- local dev only):
        @trace_agent -> LangFuse directly on LANGFUSE_HOST
        (no PII redaction, no spanmetrics -- acceptable without a Collector)

    Project selection (Collector path): LANGFUSE_PROJECT=<slug> stamps the
    ``tl.project`` resource attribute and the Collector routes the spans to
    that Langfuse project; unset/unknown -> the platform's default project.
    On the direct path the project is whichever one LANGFUSE_PUBLIC_KEY
    belongs to.
    """
    environment = os.environ.get("DEPLOY_ENV", "local")
    project = get_project()
    tracer_provider = _make_project_tracer_provider(project, environment) if project else None

    Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
        environment=environment,
        span_exporter=_make_collector_exporter(),
        tracer_provider=tracer_provider,
    )

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

    Must be called AFTER get_tracer() -- i.e. after LangFuse is initialised.
    Requires: pip install "agentic-observability-sdk[langchain]"
    """
    try:
        from langfuse.langchain import CallbackHandler
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
    LangFuse generation observation -- exact messages, model, token counts,
    finish reason -- nested under the current ``@trace_agent`` span.

    Requires the openai package:
        pip install "agentic-observability-sdk[openai]"

    Usage:
        from agentic_observability_sdk import get_openai_client

        client = get_openai_client()   # replaces openai.OpenAI()

        @trace_agent(name="my-agent")
        def run(messages):
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
            'Run: pip install "agentic-observability-sdk[openai]"'
        ) from exc


def get_anthropic_client(**kwargs):
    """Return an auto-instrumented Anthropic client via OpenInference.

    LangFuse does not ship a ``langfuse.anthropic`` wrapper. The supported
    path is OpenInference's ``AnthropicInstrumentor``, which monkey-patches
    the ``anthropic`` SDK at process level so every ``client.messages.create()``
    call is auto-recorded -- exact messages, model, token counts, stop reason.

    This function instruments once (idempotent) and returns a plain
    ``anthropic.Anthropic(**kwargs)`` instance:

        from agentic_observability_sdk import get_anthropic_client

        client = get_anthropic_client()   # replaces anthropic.Anthropic()

        @trace_agent(name="my-agent")
        def run(messages: list) -> str:
            return client.messages.create(
                model="claude-sonnet-4-6", messages=messages, max_tokens=1024
            ).content[0].text

    Requires: pip install "agentic-observability-sdk[anthropic]"

    Alternative -- OpenAI-compatible endpoint (no extra install):
        client = get_openai_client(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            base_url="https://api.anthropic.com/v1",
        )
    Trade-off: OpenAI-compat path omits Claude-specific features (extended
    thinking, native vision formats).
    """
    try:
        from openinference.instrumentation.anthropic import AnthropicInstrumentor
        import anthropic as _anthropic
        AnthropicInstrumentor().instrument()   # idempotent
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

    See CONTRACT.md section 6 for the full cross-agent session stitching design.
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
_propagate_attributes = propagate_attributes
