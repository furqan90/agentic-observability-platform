"""
@trace_agent  — wraps a function as a named LangFuse trace (root observation).
                Works with both sync and async functions.
@trace_tool   — wraps a tool call as a child span.
                Works with both sync and async functions.

Usage:
    from observability_sdk import trace_agent, trace_tool

    @trace_agent(name="eka-query-agent", tags=["eka", "rag"])
    async def run_query(user_input: str) -> str: ...

    @trace_tool(name="vector-search")
    def search(query: str) -> list: ...

Note: langfuse is imported through tracer.py — never directly — so this module
stays clean of the langfuse dependency and the swap-point rule is preserved.
"""

import asyncio
from functools import wraps

# Import via tracer.py — the sole langfuse gateway — not from langfuse directly.
from .tracer import _observe, _get_client


def trace_agent(name: str, tags: list[str] | None = None, user_id: str | None = None):
    """Decorator that creates a root LangFuse trace for each call.

    Supports both sync and async functions.
    """
    def decorator(fn):
        if asyncio.iscoroutinefunction(fn):
            @wraps(fn)
            @_observe(name=name)
            async def async_wrapper(*args, **kwargs):
                _get_client().update_current_trace(
                    name=name,
                    tags=tags or [],
                    user_id=user_id,
                    metadata={"agent": name, "source": "platform"},
                )
                return await fn(*args, **kwargs)
            return async_wrapper
        else:
            @wraps(fn)
            @_observe(name=name)
            def sync_wrapper(*args, **kwargs):
                _get_client().update_current_trace(
                    name=name,
                    tags=tags or [],
                    user_id=user_id,
                    metadata={"agent": name, "source": "platform"},
                )
                return fn(*args, **kwargs)
            return sync_wrapper
    return decorator


def trace_tool(name: str):
    """Decorator that creates a child span inside the current trace.

    Supports both sync and async functions.
    """
    def decorator(fn):
        if asyncio.iscoroutinefunction(fn):
            @wraps(fn)
            @_observe(name=name, as_type="span")
            async def async_wrapper(*args, **kwargs):
                return await fn(*args, **kwargs)
            return async_wrapper
        else:
            @wraps(fn)
            @_observe(name=name, as_type="span")
            def sync_wrapper(*args, **kwargs):
                return fn(*args, **kwargs)
            return sync_wrapper
    return decorator
