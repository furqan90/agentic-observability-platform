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
from functools import wraps

from opentelemetry import trace as otel_trace

# Import via tracer.py -- the sole langfuse gateway -- not from langfuse directly.
from .tracer import _observe, _propagate_attributes


def _mark_agent_span(name: str) -> None:
    """Set `agent.name` on the agent's root span (Prometheus label: agent_name).

    Root span only, so RED panels count one call per agent run rather than
    also counting its tool and LLM child spans.
    """
    otel_trace.get_current_span().set_attribute("agent.name", name)


def trace_agent(name: str, tags: list[str] | None = None, user_id: str | None = None):
    """Decorator that creates a root LangFuse trace for each call.

    Supports both sync and async functions.
    """
    def decorator(fn):
        if asyncio.iscoroutinefunction(fn):
            @wraps(fn)
            @_observe(name=name)
            async def async_wrapper(*args, **kwargs):
                _mark_agent_span(name)
                with _propagate_attributes(
                    tags=tags or [],
                    user_id=user_id,
                    metadata={"agent": name, "source": "platform"},
                ):
                    return await fn(*args, **kwargs)
            return async_wrapper
        else:
            @wraps(fn)
            @_observe(name=name)
            def sync_wrapper(*args, **kwargs):
                _mark_agent_span(name)
                with _propagate_attributes(
                    tags=tags or [],
                    user_id=user_id,
                    metadata={"agent": name, "source": "platform"},
                ):
                    return fn(*args, **kwargs)
            return sync_wrapper
    return decorator


def trace_attributes(*, user_id: str | None = None, session_id: str | None = None):
    """Context manager: who is asking (user_id) and which conversation (session_id), per request.

    ``@trace_agent(user_id=...)`` is fixed when the function is defined; this sets them for one call.
    Wrap the call to your ``@trace_agent`` function, and every span of that trace carries them, so
    LangFuse's Users view counts users and its Sessions view groups a conversation's turns:

        with trace_attributes(user_id=client_id, session_id=thread_id):
            answer = chat_turn(question)        # a @trace_agent function

    An inner ``@trace_agent`` with no user_id keeps the outer value. None leaves an attribute unset.
    LangFuse drops values over 200 characters (with a warning), so pass an opaque id, never a name or
    an email: they would end up in every trace.
    """
    return _propagate_attributes(user_id=user_id, session_id=session_id)


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
