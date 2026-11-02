"""Project stamping: LANGFUSE_PROJECT -> `tl.project` resource attribute.

The global OTel TracerProvider can only be set once per process, so each
scenario runs in a fresh interpreter. Spans are captured with an in-memory
exporter swapped in for the Collector exporter -- no network needed.

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
    import json, warnings
    warnings.simplefilter("always")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    import agentic_observability_sdk.tracer as tracer

    mem = InMemorySpanExporter()
    tracer._make_collector_exporter = lambda: mem
    PRE_REGISTER = {pre_register}
    if PRE_REGISTER:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        trace.set_tracer_provider(TracerProvider())

    from agentic_observability_sdk import get_tracer, trace_agent, trace_tool
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client = get_tracer()

    @trace_tool(name="tool")
    def tool():
        return 1

    @trace_agent(name="agent")
    def agent():
        return tool()

    agent()
    client.flush()
    spans = mem.get_finished_spans()
    print(json.dumps({{
        "names": sorted(s.name for s in spans),
        "resources": [dict(s.resource.attributes) for s in spans],
        "warnings": [str(w.message) for w in caught],
    }}))
""")


def run(project=None, pre_register=False, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LANGFUSE_", "OTEL_"))}
    env.update({
        "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
        "LANGFUSE_SECRET_KEY": "sk-lf-test",
        "LANGFUSE_HOST": "http://127.0.0.1:9",
        "DEPLOY_ENV": "test",
        "PYTHONPATH": str(SDK_ROOT),
    })
    if project is not None:
        env["LANGFUSE_PROJECT"] = project
    env.update(extra_env or {})
    out = subprocess.run(
        [sys.executable, "-c", SCRIPT.format(pre_register=pre_register)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_project_is_stamped_on_every_span_resource():
    r = run(project="eka")
    assert r["names"] == ["agent", "tool"]
    for res in r["resources"]:
        assert res["tl.project"] == "eka"
        assert res["langfuse.environment"] == "test"


def test_no_project_keeps_langfuse_default_provider():
    r = run()
    assert r["names"] == ["agent", "tool"]
    for res in r["resources"]:
        assert "tl.project" not in res
        assert res["langfuse.environment"] == "test"


def test_release_is_mirrored_onto_resource():
    r = run(project="eka", extra_env={"LANGFUSE_RELEASE": "1.2.3"})
    assert all(res["langfuse.release"] == "1.2.3" for res in r["resources"])


def test_invalid_slug_warns_but_still_stamps():
    r = run(project="Bad Slug")
    assert any("not a valid project slug" in w for w in r["warnings"])
    assert all(res["tl.project"] == "Bad Slug" for res in r["resources"])


def test_preregistered_provider_warns_and_points_to_otel_resource_attributes():
    r = run(project="eka", pre_register=True)
    assert any("OTEL_RESOURCE_ATTRIBUTES" in w for w in r["warnings"])


@pytest.mark.parametrize("value, expected", [("", None), ("  ", None), ("eka", "eka")])
def test_get_project(monkeypatch, value, expected):
    from agentic_observability_sdk.tracer import get_project
    monkeypatch.setenv("LANGFUSE_PROJECT", value)
    assert get_project() == expected


def test_record_usage_labels_datapoints_with_project(monkeypatch):
    from agentic_observability_sdk import metrics

    calls = []

    class FakeCounter:
        def add(self, value, attrs):
            calls.append(attrs)

    monkeypatch.setattr(metrics, "_get_counters", lambda: (FakeCounter(), FakeCounter()))
    monkeypatch.setenv("LANGFUSE_PROJECT", "eka")
    metrics.record_usage("a", "m", 1, 2, 0.1)
    assert [c["tl.project"] for c in calls] == ["eka", "eka", "eka"]

    calls.clear()
    monkeypatch.delenv("LANGFUSE_PROJECT")
    metrics.record_usage("a", "m", 1, 2, 0.1)
    assert [c["tl.project"] for c in calls] == ["unset", "unset", "unset"]
