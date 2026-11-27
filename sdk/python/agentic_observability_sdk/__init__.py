"""Observability SDK — agent instrumentation facade.

Install:
    pip install agentic-observability-sdk

Import:
    from agentic_observability_sdk import get_tracer, trace_agent, trace_tool, record_usage, shutdown
    from agentic_observability_sdk import init_logging, trace_attributes          # 0.4.0
    from agentic_observability_sdk import score_trace, delete_score, score_id_for  # 0.5.0

This is the single swap point over LangFuse v4 + OpenTelemetry. The ``langfuse``
package is imported in exactly one module (``tracer.py``); a direct ``langfuse``
import anywhere outside that module is a review-blocking violation.

Imports are lazy (CONTRACT.md §8): ``from agentic_observability_sdk.logs import init_logging``
works with only the ``opentelemetry-*`` packages installed, so services that log but don't trace
(the retrieval API) need no LangFuse. The tracing names load ``tracer.py`` on first use.

See ``AGENT-ONBOARDING.md`` for the full walkthrough.
"""

from importlib import import_module

__version__ = "0.5.0"

# name -> submodule that defines it
_EXPORTS = {
    # Core (tracer.py: imports langfuse)
    "get_tracer": "tracer",
    "shutdown": "tracer",
    "get_langchain_handler": "tracer",          # requires: langchain-core
    "get_openai_client": "tracer",              # requires: openai
    "get_anthropic_client": "tracer",           # requires: anthropic
    # Cross-agent / cross-service context (CONTRACT.md §6; propagation.py: no langfuse)
    "get_propagation_headers": "propagation",
    "restore_propagation_context": "propagation",
    "trace_context": "propagation",
    # Decorators and per-request attributes (decorators.py: imports tracer.py)
    "trace_agent": "decorators",
    "trace_tool": "decorators",
    "trace_attributes": "decorators",           # user_id / session_id per request
    # Metrics (no langfuse)
    "record_usage": "metrics",
    # Project (no langfuse): LANGFUSE_PROJECT slug (tl.project) or None
    "get_project": "project",
    # Scores on a trace, confirmed (no langfuse: LangFuse's REST API), SDK 0.5.0
    "score_trace": "scores",
    "delete_score": "scores",
    "score_id_for": "scores",
    # Logs (no langfuse)
    "init_logging": "logs",
    "flush_logging": "logs",
    "shutdown_logging": "logs",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
