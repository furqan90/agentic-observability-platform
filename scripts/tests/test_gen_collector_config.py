"""Tests for scripts/gen-collector-config.py.

Run from repo root:  pytest scripts/tests
"""

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("gencfg", ROOT / "scripts" / "gen-collector-config.py")
gencfg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gencfg)

TEMPLATE = (ROOT / "otel" / "otel-collector-config.tmpl.yaml").read_text(encoding="utf-8")

MULTI = """
langfuse_endpoint: http://langfuse-web:3000/api/public/otel
default_project: agents
projects:
  agents:
    auth_env: LANGFUSE_OTEL_AUTH
  eka:
  client-acme:
    endpoint: https://other-langfuse.example.com/api/public/otel
"""


def _render(text: str) -> dict:
    return yaml.safe_load(gencfg.render(gencfg.load_registry(text), TEMPLATE))


def _all_pipeline_components(cfg: dict) -> set[str]:
    used = set()
    for p in cfg["service"]["pipelines"].values():
        for k in ("receivers", "processors", "exporters"):
            used.update(p.get(k, []))
    return used


def test_committed_config_is_up_to_date():
    assert gencfg.main(["--check"]) == 0


def test_every_project_gets_exporter_route_and_pipeline():
    cfg = _render(MULTI)
    routing = cfg["connectors"]["routing/langfuse_projects"]
    routes = {r["condition"]: r["pipelines"] for r in routing["table"]}
    for slug in ("agents", "eka", "client-acme"):
        assert routes[f'attributes["tl.project"] == "{slug}"'] == [f"traces/langfuse-{slug}"]
        pipe = cfg["service"]["pipelines"][f"traces/langfuse-{slug}"]
        assert pipe["receivers"] == ["routing/langfuse_projects"]
        assert pipe["exporters"] == [f"otlp_http/langfuse-{slug}"]
        assert f"otlp_http/langfuse-{slug}" in cfg["exporters"]
    assert all(r["context"] == "resource" for r in routing["table"])


def test_unknown_and_missing_project_go_to_default():
    routing = _render(MULTI)["connectors"]["routing/langfuse_projects"]
    assert routing["default_pipelines"] == ["traces/langfuse-agents"]


def test_auth_env_defaults_and_overrides():
    exp = _render(MULTI)["exporters"]
    assert exp["otlp_http/langfuse-agents"]["headers"]["Authorization"] == "Basic ${env:LANGFUSE_OTEL_AUTH}"
    assert exp["otlp_http/langfuse-eka"]["headers"]["Authorization"] == "Basic ${env:LANGFUSE_OTEL_AUTH_EKA}"
    assert (exp["otlp_http/langfuse-client-acme"]["headers"]["Authorization"]
            == "Basic ${env:LANGFUSE_OTEL_AUTH_CLIENT_ACME}")


def test_per_project_endpoint_override():
    exp = _render(MULTI)["exporters"]
    assert exp["otlp_http/langfuse-eka"]["endpoint"] == "http://langfuse-web:3000/api/public/otel"
    assert exp["otlp_http/langfuse-client-acme"]["endpoint"] == "https://other-langfuse.example.com/api/public/otel"


def test_ingest_pipeline_fans_out_to_routing_and_spanmetrics():
    cfg = _render(MULTI)
    p = cfg["service"]["pipelines"]["traces/in"]
    assert p["receivers"] == ["otlp"]
    assert p["exporters"] == ["routing/langfuse_projects", "span_metrics"]
    procs = p["processors"]
    # Ingress project pin reads request metadata, which batch drops → must precede batch.
    assert procs.index("resource/project_from_ingress") < procs.index("batch")
    # PII redaction must run before anything leaves the ingest pipeline.
    assert procs.index("attributes/redaction") < procs.index("batch")
    pin = cfg["processors"]["resource/project_from_ingress"]["attributes"][0]
    assert pin == {"key": "tl.project", "from_context": "metadata.x-langfuse-project", "action": "upsert"}
    for proto in ("grpc", "http"):
        assert cfg["receivers"]["otlp"]["protocols"][proto]["include_metadata"] is True
    assert "tl.project" in [d["name"] for d in cfg["connectors"]["span_metrics"]["dimensions"]]


def test_every_component_referenced_is_defined_and_vice_versa():
    cfg = _render(MULTI)
    defined = set()
    for section in ("receivers", "processors", "exporters", "connectors"):
        defined.update(cfg.get(section, {}).keys())
    used = _all_pipeline_components(cfg)
    assert used <= defined, f"undefined: {used - defined}"
    assert defined <= used, f"unused: {defined - used}"


@pytest.mark.parametrize("text, msg", [
    ("default_project: a\nprojects: {}\nlangfuse_endpoint: x", "non-empty"),
    ("langfuse_endpoint: x\nprojects:\n  a: {}\n", "default_project"),
    ("langfuse_endpoint: x\ndefault_project: b\nprojects:\n  a: {}\n", "not listed"),
    ("langfuse_endpoint: x\ndefault_project: A\nprojects:\n  A: {}\n", "invalid project slug"),
    ("default_project: a\nprojects:\n  a: {}\n", "no endpoint"),
    ("langfuse_endpoint: x\ndefault_project: a\nprojects:\n  a: {auth_env: X}\n  b: {auth_env: X}\n",
     "share auth_env"),
    ("langfuse_endpoint: x\ndefault_project: a\nprojects:\n  a: {auth_env: 'bad-env'}\n", "invalid auth_env"),
])
def test_registry_validation(text, msg):
    with pytest.raises(gencfg.RegistryError, match=msg):
        gencfg.load_registry(text)
