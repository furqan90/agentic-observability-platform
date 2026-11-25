"""
W3C Trace Context across a process boundary (CONTRACT.md §6). No ``langfuse`` import: services that log but
don't trace (the retrieval API) use it to put a caller's trace id on their log lines (§8).

Caller (inside a traced call):

    headers = {**auth_headers, **get_propagation_headers()}     # adds traceparent / tracestate
    httpx.post(url, headers=headers, json=body)

Callee (per request; e.g. an HTTP middleware):

    with trace_context(request.headers):
        ...   # spans opened here are children of the caller's span; log records carry its trace_id

The callee needs no tracing of its own: with no TracerProvider, the restored context still holds the caller's
span context, which is what log records read their trace_id / span_id from.
"""

from contextlib import contextmanager


def get_propagation_headers() -> dict:
    """W3C Trace Context headers (``traceparent``, ``tracestate``) for the current span; ``{}`` outside one."""
    try:
        from opentelemetry import propagate

        carrier: dict = {}
        propagate.inject(carrier)
        return carrier
    except Exception:
        return {}


def restore_propagation_context(headers) -> "object | None":
    """Attach the context carried by *headers* to the current thread / task and return its token.

    Until 0.4.1 this only extracted the context and discarded it, so nothing was restored. Prefer
    ``trace_context()``, which also detaches it; with this function, call
    ``opentelemetry.context.detach(token)`` yourself when the work ends. Returns None (and attaches nothing)
    when *headers* carry no context or anything fails.
    """
    try:
        from opentelemetry import context, propagate

        carrier = {str(k).lower(): v for k, v in dict(headers or {}).items()}
        if "traceparent" not in carrier:
            return None
        return context.attach(propagate.extract(carrier))
    except Exception:
        return None


@contextmanager
def trace_context(headers):
    """Within the block, the current context is the one *headers* carry (a no-op without ``traceparent``).
    Never raises because of the headers; always detaches."""
    token = restore_propagation_context(headers)
    try:
        yield
    finally:
        if token is not None:
            try:
                from opentelemetry import context

                context.detach(token)
            except Exception:
                pass
