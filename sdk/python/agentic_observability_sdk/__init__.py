""" Observability SDK — agent instrumentation facade.

Install:
    pip install agentic-observability-sdk

Import:
    from agentic_observability_sdk import get_tracer, trace_agent, trace_tool, record_usage, shutdown

This is the single swap point over LangFuse v3 + OpenTelemetry. The ``langfuse``
package is imported in exactly one module (``tracer.py``); a direct ``langfuse``
import anywhere outside that module is a review-blocking violation.

See ``AGENT-ONBOARDING.md`` for the full walkthrough.
"""

from .tracer import (
    get_tracer, shutdown,
    get_langchain_handler, get_openai_client, get_anthropic_client,
    get_propagation_headers, restore_propagation_context,
)
from .decorators import trace_agent, trace_tool
from .metrics import record_usage

__all__ = [
    # Core
    "get_tracer",
    "shutdown",
    # Decorators
    "trace_agent",
    "trace_tool",
    # Metrics
    "record_usage",
    # Framework integrations (optional deps)
    "get_langchain_handler",          # requires: langchain-core
    "get_openai_client",              # requires: openai
    "get_anthropic_client",           # requires: anthropic
    # Cross-agent session stitching (see CONTRACT.md §6)
    "get_propagation_headers",
    "restore_propagation_context",
]
