"""init_logging() and trace_attributes() (CONTRACT.md §8, docs/LOGS-PLAN.md §2, phase L2).

OpenTelemetry's global providers can be set only once per process, so each scenario runs in a fresh
interpreter (as test_project_routing.py does) and prints a JSON result. No network: an in-memory exporter,
or a dead endpoint where the failure path is the point.

Run from sdk/python:  pytest tests
These tests pin the logs API the SDK uses (opentelemetry.sdk._logs, unstable): run them before raising the
opentelemetry-sdk bound.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

SDK_ROOT = Path(__file__).resolve().parents[1]

HELPERS = textwrap.dedent("""
    import json, logging, os, sys, time, warnings
    def recs(exporter):
        out = []
        for r in exporter.get_finished_logs():
            lr = getattr(r, "log_record", r)
            res = getattr(r, "resource", None) or getattr(lr, "resource", None)
            out.append({
                "body": str(lr.body),
                "severity": lr.severity_text,
                "trace_id": format(lr.trace_id, "032x") if lr.trace_id else None,
                "span_id": format(lr.span_id, "016x") if lr.span_id else None,
                "attributes": {k: (v if isinstance(v, (str, int, float, bool)) else str(v))
                               for k, v in dict(lr.attributes or {}).items()},
                "resource": dict(res.attributes) if res is not None else {},
            })
        return out
""")


def run(code: str, env_extra: "dict | None" = None, timeout: int = 60) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "LANGFUSE_", "DEPLOY_ENV", "HOST_NAME"))}
    env.update({"PYTHONPATH": str(SDK_ROOT), "LANGFUSE_PUBLIC_KEY": "pk-test", "LANGFUSE_SECRET_KEY": "sk-test",
                "LANGFUSE_HOST": "http://127.0.0.1:9"})
    env.update(env_extra or {})
    proc = subprocess.run([sys.executable, "-c", HELPERS + textwrap.dedent(code)], env=env, check=False,
                          capture_output=True, text=True, timeout=timeout)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_no_endpoint_is_a_no_op():
    out = run("""
        import logging
        from agentic_observability_sdk.logs import init_logging
        before = len(logging.getLogger().handlers)
        print(json.dumps({"shipping": init_logging(), "added": len(logging.getLogger().handlers) - before}))
    """)
    assert out == {"shipping": False, "added": 0}


def test_records_carry_trace_resource_extra_and_levels():
    out = run("""
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        import agentic_observability_sdk.tracer as tracer
        spans = InMemorySpanExporter()
        tracer._make_collector_exporter = lambda: spans
        from agentic_observability_sdk import get_tracer, trace_agent, init_logging, flush_logging
        client = get_tracer()
        mem = InMemoryLogRecordExporter()
        logging.basicConfig(level=logging.DEBUG)            # the service's own setup, first
        assert init_logging(exporter=mem) is True
        assert init_logging(exporter=mem) is True            # idempotent
        log = logging.getLogger("agent_app.main")

        @trace_agent(name="agent")
        def turn():
            log.info("turn.done", extra={"ms": 412, "cited": 3, "model": "m"})
            log.debug("debug detail stays local")
            try:
                1 / 0
            except ZeroDivisionError:
                log.exception("turn.failed")
        turn()
        log.info("outside any span")
        logging.getLogger("opentelemetry.exporter.otlp").warning("exporter chatter")
        client.flush(); flush_logging()
        s = [x for x in spans.get_finished_spans() if x.name == "agent"][0]
        handlers = [h for h in logging.getLogger().handlers if "LoggingHandler" in type(h).__name__]
        print(json.dumps({"recs": recs(mem), "trace_id": format(s.context.trace_id, "032x"),
                          "handlers": len(handlers)}))
    """, {"OTEL_SERVICE_NAME": "svc-test", "DEPLOY_ENV": "vm", "LANGFUSE_PROJECT": "secp-pilot",
          "HOST_NAME": "box-1", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"})
    recs = {r["body"]: r for r in out["recs"]}
    assert out["handlers"] == 1
    assert set(recs) == {"turn.done", "turn.failed", "outside any span"}, list(recs)  # no DEBUG, no OTel chatter
    done = recs["turn.done"]
    assert done["trace_id"] == out["trace_id"] and done["span_id"]
    assert done["severity"] == "INFO"
    assert done["attributes"]["ms"] == 412 and done["attributes"]["cited"] == 3
    assert any(k.startswith("code.") for k in done["attributes"])
    res = done["resource"]
    assert res["service.name"] == "svc-test" and res["deployment.environment"] == "vm"
    assert res["tl.project"] == "secp-pilot" and res["host.name"] == "box-1"
    failed = recs["turn.failed"]
    assert failed["severity"] == "ERROR" and failed["trace_id"] == out["trace_id"]
    assert "ZeroDivisionError" in failed["attributes"]["exception.stacktrace"]
    assert recs["outside any span"]["trace_id"] is None


def test_collector_down_never_blocks_and_counts_drops():
    out = run("""
        import agentic_observability_sdk.logs as logs
        counted = []
        logs._count_dropped = lambda n, reason: counted.append((n, reason))
        assert logs.init_logging() is True                      # endpoint set, nothing listens
        t = time.perf_counter()
        for i in range(200):
            logging.getLogger("svc").warning("line %d", i)
        elapsed = time.perf_counter() - t
        logs.flush_logging(20000)
        print(json.dumps({"elapsed": elapsed, "counted": counted}))
    """, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"}, timeout=90)
    assert out["elapsed"] < 1.0, out["elapsed"]
    assert sum(n for n, r in out["counted"] if r == "export_failed") == 200


def test_queue_full_is_counted():
    out = run("""
        import threading
        import agentic_observability_sdk.logs as logs
        from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult
        counted = []
        logs._count_dropped = lambda n, reason: counted.append((n, reason))
        gate = threading.Event()
        class Stuck(LogRecordExporter):
            def export(self, batch):
                gate.wait(10); return LogRecordExportResult.SUCCESS
            def shutdown(self): gate.set()
        logs.init_logging(exporter=Stuck())
        for i in range(2048 + 600):
            logging.getLogger("svc").warning("burst %d", i)
        gate.set()
        print(json.dumps({"queue_full": sum(n for n, r in counted if r == "queue_full")}))
    """)
    assert out["queue_full"] > 0


def test_logs_import_without_langfuse():
    out = run("""
        import importlib.abc
        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name == "langfuse" or name.startswith("langfuse."):
                    raise ImportError("langfuse blocked for this test")
        sys.meta_path.insert(0, Block())
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
        from agentic_observability_sdk.logs import init_logging, flush_logging
        from agentic_observability_sdk import init_logging as same, get_project
        mem = InMemoryLogRecordExporter()
        logging.getLogger().setLevel(logging.INFO)     # the service's level; init_logging doesn't change it
        init_logging(exporter=mem)
        logging.getLogger("api").info("retrieve.ranked")
        flush_logging()
        try:
            import agentic_observability_sdk.tracer
            tracer_blocked = False
        except ImportError:
            tracer_blocked = True
        print(json.dumps({"same": same is init_logging, "bodies": [r["body"] for r in recs(mem)],
                          "tracer_blocked": tracer_blocked, "langfuse_loaded": "langfuse" in sys.modules}))
    """)
    assert out == {"same": True, "bodies": ["retrieve.ranked"], "tracer_blocked": True, "langfuse_loaded": False}


def test_after_fork_the_child_still_ships():
    out = run("""
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
        from agentic_observability_sdk.logs import init_logging, flush_logging
        mem = InMemoryLogRecordExporter()
        logging.getLogger().setLevel(logging.INFO)
        init_logging(exporter=mem)
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            logging.getLogger("child").info("from the child")
            flush_logging()
            os.write(w, json.dumps([x["body"] for x in recs(mem)]).encode()); os._exit(0)
        os.waitpid(pid, 0); os.close(w)
        print(json.dumps({"child": json.loads(os.read(r, 65536))}))
    """)
    assert "from the child" in out["child"]


def test_trace_attributes_reach_every_span():
    out = run("""
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        import agentic_observability_sdk.tracer as tracer
        spans = InMemorySpanExporter()
        tracer._make_collector_exporter = lambda: spans
        from agentic_observability_sdk import get_tracer, trace_agent, trace_tool, trace_attributes
        client = get_tracer()
        @trace_tool(name="search")
        def search(): return 1
        @trace_agent(name="agent")                       # no user_id: must keep the outer one
        def turn(): return search()
        with trace_attributes(user_id="b-7f3c", session_id="thread-42"):
            turn()
        turn()                                            # outside: no user, no session
        client.flush()
        rows = [{"name": s.name, "user": s.attributes.get("user.id"), "session": s.attributes.get("session.id")}
                for s in spans.get_finished_spans()]
        print(json.dumps(rows))
    """, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"})
    inside, outside = out[:2], out[2:]
    assert {(r["name"], r["user"], r["session"]) for r in inside} == {
        ("search", "b-7f3c", "thread-42"), ("agent", "b-7f3c", "thread-42")}
    assert all(r["user"] is None and r["session"] is None for r in outside)


def test_shutdown_warns_when_the_handler_was_removed():
    out = run("""
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
        from agentic_observability_sdk.logs import init_logging, shutdown_logging
        init_logging(exporter=InMemoryLogRecordExporter())
        logging.basicConfig(level=logging.INFO, force=True)        # the pitfall
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            shutdown_logging()
        print(json.dumps({"warned": any("removed from the root logger" in str(x.message) for x in w)}))
    """)
    assert out == {"warned": True}
