"""
@trace_agent  -- wraps a function as a named LangFuse trace (root observation).
                 Works with both sync and async functions.
@trace_tool   -- wraps a tool call as a child span.
                 Works with both sync and async functions.

Usage:
    from agentic_observability_sdk import trace_agent, trace_tool

    @trace_agent(name="eka-query-agent", tags=["eka", "rag"])
    async def run_query(user_input: str) -> str: ...

    @trace_tool(name="vector-search")
    def search(query: str) -> list: ...

Note: langfuse is imported through tracer.py -- never directly -- so this module
stays clean of the langfuse dependency and the swap-point rule is preserved.
"""

import asyncio
import contextvars
from functools import wraps

from opentelemetry import trace as _otel_trace

# Import via tracer.py -- the sole langfuse gateway -- not from langfuse directly.
from .tracer import _observe, _propagate_attributes

# Tracks the enclosing @trace_agent's name for the current call stack (thread
# or asyncio task) so nested @trace_tool spans can be tagged with the same
# agent.name -- see _tag_agent_name below for why this attribute has to be
# set explicitly at all.
_current_agent_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_agent_name", default=None
)


def _tag_agent_name(name: str | None) -> None:
    """Attach agent.name as a raw OTel span attribute on the current span.

    IMPORTANT: this is NOT redundant with LangFuse's own tracing. LangFuse's
    observe()/propagate_attributes() (used below) store LangFuse-internal
    trace metadata -- they do not set a plain "agent.name" span attribute.
    The Collector's span_metrics connector (otel/otel-collector-config.yaml,
    dimensions: - name: agent.name) reads exactly that raw attribute to build
    the "agent_name" Prometheus label on calls_total / duration_milliseconds.
    Without this call, every span's agent_name label comes back empty and
    every agent's RED metrics blend into one indistinguishable series.
    """
    if not name:
        return
    span = _otel_trace.get_current_span()
    if span is not None and span.is_recording():
        span.set_attribute("agent.name", name)


def trace_agent(name: str, tags: list[str] | None = None, user_id: str | None = None):
    """Decorator that creates a root LangFuse trace for each call.

    Supports both sync and async functions.
    """
    def decorator(fn):
        if asyncio.iscoroutinefunction(fn):
            @wraps(fn)
            @_observe(name=name)
            async def async_wrapper(*args, **kwargs):
                _tag_agent_name(name)
                token = _current_agent_name.set(name)
                try:
                    with _propagate_attributes(
                        tags=tags or [],
                        user_id=user_id,
                        metadata={"agent": name, "source": "platform"},
                    ):
                        return await fn(*args, **kwargs)
                finally:
                    _current_agent_name.reset(token)
            return async_wrapper
        else:
            @wraps(fn)
            @_observe(name=name)
            def sync_wrapper(*args, **kwargs):
                _tag_agent_name(name)
                token = _current_agent_name.set(name)
                try:
                    with _propagate_attributes(
                        tags=tags or [],
                        user_id=user_id,
                        metadata={"agent": name, "source": "platform"},
                    ):
                        return fn(*args, **kwargs)
                finally:
                    _current_agent_name.reset(token)
            return sync_wrapper
    return decorator


def trace_tool(name: str):
    """Decorator that creates a child span inside the current trace.

    Supports both sync and async functions. Inherits agent.name from the
    enclosing @trace_agent call (via contextvar) so tool-level spans are
    also correctly attributed in the RED metrics, not just the root span.
    """
    def decorator(fn):
        if asyncio.iscoroutinefunction(fn):
            @wraps(fn)
            @_observe(name=name, as_type="span")
            async def async_wrapper(*args, **kwargs):
                _tag_agent_name(_current_agent_name.get())
                return await fn(*args, **kwargs)
            return async_wrapper
        else:
            @wraps(fn)
            @_observe(name=name, as_type="span")
            def sync_wrapper(*args, **kwargs):
                _tag_agent_name(_current_agent_name.get())
                return fn(*args, **kwargs)
            return sync_wrapper
    return decorator
