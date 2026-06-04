"""
 agent instrumentation facade.

Agents import from here — never from `langfuse` directly. This is the single
swap point that keeps instrumentation portable across backends (see plan
§Portability). A `langfuse` import anywhere outside this package is a
review-blocking violation.

    from sdk.python import get_tracer, trace_agent, trace_tool, record_usage
"""

from .tracer import get_tracer
from .decorators import trace_agent, trace_tool
from .metrics import record_usage

__all__ = ["get_tracer", "trace_agent", "trace_tool", "record_usage"]
