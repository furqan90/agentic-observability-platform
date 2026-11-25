"""Cross-service context (CONTRACT.md §6, SDK 0.4.1): get_propagation_headers / trace_context /
restore_propagation_context.

0.4.0 and earlier: restore_propagation_context() extracted the context and discarded it, so nothing was
restored. These tests pin the fix. Each scenario runs in a fresh interpreter (global OTel providers).
Run from sdk/python:  pytest tests
"""

from test_logs import run


def test_a_logging_only_callee_gets_the_callers_trace_id_without_langfuse():
    out = run("""
        import importlib.abc
        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name == "langfuse" or name.startswith("langfuse."):
                    raise ImportError("langfuse blocked for this test")
        sys.meta_path.insert(0, Block())

        # The caller: any traced process. Its span context is what crosses the wire.
        from opentelemetry.sdk.trace import TracerProvider
        caller = TracerProvider().get_tracer("caller")
        from agentic_observability_sdk import get_propagation_headers, trace_context, restore_propagation_context
        with caller.start_as_current_span("tool") as span:
            headers = get_propagation_headers()
            want = format(span.get_span_context().trace_id, "032x")
        outside = get_propagation_headers()

        # The callee: logs only, no TracerProvider of its own. Headers as an HTTP server sees them.
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
        from agentic_observability_sdk.logs import init_logging, flush_logging
        logging.getLogger().setLevel(logging.INFO)
        mem = InMemoryLogRecordExporter()
        init_logging(exporter=mem)
        log = logging.getLogger("api")
        with trace_context({"Traceparent": headers["traceparent"]}):      # header names arrive in any case
            log.info("retrieve.ranked")
        log.info("after the request")
        with trace_context({}):                                            # no context: a no-op
            log.info("no traceparent")
        with trace_context({"traceparent": "garbage"}):                    # malformed: never raises
            log.info("bad traceparent")
        none_token = restore_propagation_context({"x-other": "1"})
        flush_logging()
        print(json.dumps({"want": want, "has_traceparent": "traceparent" in headers, "outside": outside,
                          "by_body": {r["body"]: r["trace_id"] for r in recs(mem)},
                          "none_token": none_token is None, "langfuse_loaded": "langfuse" in sys.modules}))
    """)
    assert out["has_traceparent"] and out["outside"] == {}
    assert out["by_body"]["retrieve.ranked"] == out["want"]
    assert out["by_body"]["after the request"] is None          # detached
    assert out["by_body"]["no traceparent"] is None
    assert out["by_body"]["bad traceparent"] is None
    assert out["none_token"] and not out["langfuse_loaded"]


def test_a_traced_callee_nests_under_the_caller():
    out = run("""
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        import agentic_observability_sdk.tracer as tracer
        spans = InMemorySpanExporter()
        tracer._make_collector_exporter = lambda: spans
        from agentic_observability_sdk import get_tracer, trace_agent, trace_context
        client = get_tracer()
        from opentelemetry.sdk.trace import TracerProvider
        caller = TracerProvider().get_tracer("agent-a")
        with caller.start_as_current_span("handoff") as span:
            from agentic_observability_sdk import get_propagation_headers
            headers = get_propagation_headers()
            want = format(span.get_span_context().trace_id, "032x")

        @trace_agent(name="agent-b")
        def agent_b():
            return 1
        with trace_context(headers):
            agent_b()
        agent_b()                                                          # outside: a trace of its own
        client.flush()
        got = [format(s.context.trace_id, "032x") for s in spans.get_finished_spans() if s.name == "agent-b"]
        print(json.dumps({"want": want, "got": got}))
    """, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"})
    assert out["got"][0] == out["want"]
    assert out["got"][1] != out["want"]
