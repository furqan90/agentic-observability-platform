"""
 LangFuse v3 tracer — the ONLY module allowed to import ``langfuse``.

Reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST from env.
If we ever leave LangFuse, we rewrite this one file — not N agents.
"""

import os
from functools import lru_cache

from langfuse import Langfuse, get_client, observe  # sole langfuse import point
from langfuse.callback import CallbackHandler as _LangFuseCallbackHandler


@lru_cache(maxsize=1)
def get_tracer() -> "Langfuse":
    """Initialise LangFuse + OTel metrics. Idempotent — safe to call multiple times.

    Call once at agent startup:
        from agentic_observability_sdk import get_tracer
        get_tracer()
    """
    Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
        environment=os.environ.get("DEPLOY_ENV", "local"),
    )

    # Wire token/cost metrics to the OTel Collector -> Prometheus -> Grafana.
    # Lazy import keeps this file the sole langfuse swap point.
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

    try:
        from opentelemetry import metrics as otel_metrics
        provider = otel_metrics.get_meter_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception:
        pass


def get_langchain_handler() -> "_LangFuseCallbackHandler":
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
    Returns a no-op compatible stub when called outside a traced context so
    uninstrumented code paths don't raise.
    """
    try:
        return get_client().get_langchain_handler()
    except Exception:
        # Outside a trace context or LangFuse not initialised — return a
        # bare handler that records nothing rather than crashing the agent.
        return _LangFuseCallbackHandler()


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
    """Return a LangFuse-instrumented Anthropic client.

    Drop-in replacement for ``anthropic.Anthropic(**kwargs)``. Every
    ``client.messages.create()`` call is automatically recorded as a
    LangFuse generation observation — exact messages, model, token counts,
    stop reason — nested under the current ``@trace_agent`` span.

    Requires the anthropic package:
        pip install "agentic-observability-sdk[anthropic]"
        # or: pip install anthropic

    Usage:
        from agentic_observability_sdk import get_anthropic_client

        client = get_anthropic_client()   # replaces anthropic.Anthropic()

        @trace_agent(name="my-agent")
        def run(messages):
            return client.messages.create(
                model="claude-sonnet-4-6", messages=messages, max_tokens=1024
            ).content[0].text

    Must be called after get_tracer() so LangFuse is initialised.
    Pass any kwargs accepted by anthropic.Anthropic() (api_key, base_url, etc.).
    """
    try:
        from langfuse.anthropic import Anthropic
        return Anthropic(**kwargs)
    except ImportError as exc:
        raise ImportError(
            "Anthropic integration requires the anthropic package.\n"
            "Run: pip install \"agentic-observability-sdk[anthropic]\""
            " or pip install anthropic"
        ) from exc


# Re-export langfuse helpers so decorators.py never imports langfuse directly.
# This keeps the "one langfuse import file" rule intact.
_observe = observe
_get_client = get_client
