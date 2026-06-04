""" Observability SDK — agent instrumentation facade.

Public API — import from here, not from sub-modules:

    from observability_sdk import get_tracer, trace_agent, trace_tool, record_usage, shutdown

This is the single swap point over LangFuse v3 + OpenTelemetry. The ``langfuse``
package is imported in exactly one module (``python/tracer.py``); a direct
``langfuse`` import anywhere outside that module is a review-blocking violation.

See ``AGENT-ONBOARDING.md`` for the full walkthrough.
"""

from .python.tracer import get_tracer, shutdown
from .python.decorators import trace_agent, trace_tool
from .python.metrics import record_usage

__all__ = ["get_tracer", "trace_agent", "trace_tool", "record_usage", "shutdown"]
