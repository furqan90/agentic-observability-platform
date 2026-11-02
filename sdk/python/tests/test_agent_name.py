"""`agent.name` on the @trace_agent root span -> Prometheus label agent_name.

Runs in a fresh interpreter (the global TracerProvider can only be set once),
with an in-memory exporter swapped in for the Collector exporter.

Run from sdk/python:  pytest tests
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SDK_ROOT = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent("""
    import asyncio, json
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    import agentic_observability_sdk.tracer as tracer

    mem = InMemorySpanExporter()
    tracer._make_collector_exporter = lambda: mem

    from agentic_observability_sdk import get_tracer, trace_agent, trace_tool
    client = get_tracer()

    @trace_tool(name="sync-tool")
    def sync_tool():
        return 1

    @trace_tool(name="async-tool")
    async def async_tool():
        return 1

    @trace_agent(name="sync-agent")
    def sync_agent():
        return sync_tool()

    @trace_agent(name="async-agent")
    async def async_agent():
        return await async_tool()

    sync_agent()
    asyncio.run(async_agent())
    client.flush()
    print(json.dumps({s.name: s.attributes.get("agent.name") for s in mem.get_finished_spans()}))
""")


@pytest.mark.parametrize("project", [None, "eka"])
def test_agent_name_is_on_root_spans_only(project):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LANGFUSE_", "OTEL_"))}
    env.update({
        "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
        "LANGFUSE_SECRET_KEY": "sk-lf-test",
        "LANGFUSE_HOST": "http://127.0.0.1:9",
        "PYTHONPATH": str(SDK_ROOT),
    })
    if project:
        env["LANGFUSE_PROJECT"] = project
    out = subprocess.run(
        [sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {
        "sync-agent": "sync-agent",
        "async-agent": "async-agent",
        "sync-tool": None,
        "async-tool": None,
    }
