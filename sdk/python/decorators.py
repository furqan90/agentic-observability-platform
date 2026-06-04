"""
@trace_agent  — wraps a function as a named LangFuse trace (root observation)
@trace_tool   — wraps a tool call as a child span

Usage:
    @trace_agent(name="eka-query-agent", tags=["eka", "rag"])
    def run_query(user_input: str) -> str: ...

    @trace_tool(name="vector-search")
    def search(query: str) -> list: ...
"""

from functools import wraps

from langfuse import observe, get_client


def trace_agent(name: str, tags: list[str] | None = None, user_id: str | None = None):
    def decorator(fn):
        @wraps(fn)
        @observe(name=name)
        def wrapper(*args, **kwargs):
            get_client().update_current_trace(
                name=name,
                tags=tags or [],
                user_id=user_id,
                metadata={"agent": name, "source": "platform"},
            )
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def trace_tool(name: str):
    def decorator(fn):
        @wraps(fn)
        @observe(name=name, as_type="span")
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        return wrapper

    return decorator
