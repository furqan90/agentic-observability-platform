"""
 agent instrumentation facade — internal sub-package.

Prefer importing from the top level:
    from observability_sdk import get_tracer, trace_agent, trace_tool, record_usage, shutdown

Direct sub-package imports also work:
    from observability_sdk.python import get_tracer, trace_agent, trace_tool, record_usage
"""

from .tracer import get_tracer, shutdown
from .decorators import trace_agent, trace_tool
from .metrics import record_usage

__all__ = ["get_tracer", "trace_agent", "trace_tool", "record_usage", "shutdown"]
