#!/usr/bin/env python3
"""
Render the OTel Collector config from the Langfuse project registry.

    otel/projects.yaml                    (which projects exist, which env var holds each key)
  + otel/otel-collector-config.tmpl.yaml  (everything else)
  = otel/otel-collector-config.yaml       (GENERATED — committed so compose works out of the box)

Usage (from repo root):
    python scripts/gen-collector-config.py              # write the config
    python scripts/gen-collector-config.py --check      # exit 1 if the committed config is stale (CI)
    python scripts/gen-collector-config.py --list-env   # print the env vars the Collector needs

Requires PyYAML (pip install pyyaml).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "otel" / "projects.yaml"
TEMPLATE = ROOT / "otel" / "otel-collector-config.tmpl.yaml"
OUTPUT = ROOT / "otel" / "otel-collector-config.yaml"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
ROUTING_ID = "routing/langfuse_projects"
PROJECT_ATTR = "tl.project"


class RegistryError(ValueError):
    pass


def default_auth_env(slug: str) -> str:
    return "LANGFUSE_OTEL_AUTH_" + slug.upper().replace("-", "_")


def load_registry(text: str) -> dict:
    """Parse and validate the registry. Returns a normalised dict:
    {"default": slug, "projects": [{"slug", "auth_env", "endpoint"}, ...]}"""
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise RegistryError("registry must be a mapping")

    base_endpoint = data.get("langfuse_endpoint")
    projects_in = data.get("projects")
    default = data.get("default_project")

    if not isinstance(projects_in, dict) or not projects_in:
        raise RegistryError("`projects` must be a non-empty mapping of slug -> settings")

    projects: list[dict] = []
    seen_env: dict[str, str] = {}
    for slug, cfg in projects_in.items():
        cfg = cfg or {}
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            raise RegistryError(f"invalid project slug {slug!r}: must match {SLUG_RE.pattern}")
        if not isinstance(cfg, dict):
            raise RegistryError(f"project {slug!r}: settings must be a mapping")
        auth_env = cfg.get("auth_env") or default_auth_env(slug)
        if not ENV_RE.match(auth_env):
            raise RegistryError(f"project {slug!r}: invalid auth_env {auth_env!r}")
        if auth_env in seen_env:
            raise RegistryError(
                f"projects {seen_env[auth_env]!r} and {slug!r} share auth_env {auth_env!r}"
            )
        seen_env[auth_env] = slug
        endpoint = cfg.get("endpoint") or base_endpoint
        if not endpoint:
            raise RegistryError(
                f"project {slug!r}: no endpoint (set `langfuse_endpoint` or a per-project `endpoint`)"
            )
        projects.append({"slug": slug, "auth_env": auth_env, "endpoint": endpoint})

    slugs = [p["slug"] for p in projects]
    if default is None:
        raise RegistryError("`default_project` is required")
    if default not in slugs:
        raise RegistryError(f"default_project {default!r} is not listed under `projects`")

    return {"default": default, "projects": projects}


def _exporter_id(slug: str) -> str:
    return f"otlp_http/langfuse-{slug}"


def _pipeline_id(slug: str) -> str:
    return f"traces/langfuse-{slug}"


def render(registry: dict, template: str) -> str:
    projects = registry["projects"]
    default = registry["default"]

    routing = [
        "  # Routes each resource's spans to its Langfuse project by `tl.project`.",
        "  # Missing / unknown project → default_pipelines (the default project).",
        f"  {ROUTING_ID}:",
        f"    default_pipelines: [{_pipeline_id(default)}]",
        "    error_mode: ignore",
        "    table:",
    ]
    for p in projects:
        routing += [
            "      - context: resource",
            f"        condition: attributes[\"{PROJECT_ATTR}\"] == \"{p['slug']}\"",
            f"        pipelines: [{_pipeline_id(p['slug'])}]",
        ]

    exporters: list[str] = []
    for p in projects:
        tag = " (default)" if p["slug"] == default else ""
        exporters += [
            f"  # project: {p['slug']}{tag}",
            f"  {_exporter_id(p['slug'])}:",
            f"    endpoint: {p['endpoint']}",
            "    headers:",
            f"      Authorization: \"Basic ${{env:{p['auth_env']}}}\"",
        ]

    pipelines: list[str] = []
    for p in projects:
        pipelines += [
            f"    {_pipeline_id(p['slug'])}:",
            f"      receivers: [{ROUTING_ID}]",
            f"      exporters: [{_exporter_id(p['slug'])}]",
        ]

    notice = (
        "#\n"
        "# ─────────────────────────────────────────────────────────────────────\n"
        "#  GENERATED FILE — do not edit by hand.\n"
        "#  Edit otel/otel-collector-config.tmpl.yaml and/or otel/projects.yaml, then run:\n"
        "#    python scripts/gen-collector-config.py\n"
        "# ─────────────────────────────────────────────────────────────────────"
    )

    out = template
    for marker, lines in (
        ("@@GENERATED_NOTICE@@", notice),
        ("@@ROUTING_CONNECTOR@@", "\n".join(routing)),
        ("@@LANGFUSE_EXPORTERS@@", "\n".join(exporters)),
        ("@@PROJECT_PIPELINES@@", "\n".join(pipelines)),
    ):
        if marker not in out:
            raise RegistryError(f"template is missing marker {marker}")
        out = out.replace(marker, lines)
    return out


def generate() -> tuple[dict, str]:
    registry = load_registry(REGISTRY.read_text(encoding="utf-8"))
    return registry, render(registry, TEMPLATE.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail if the committed config is stale")
    mode.add_argument("--list-env", action="store_true", help="print required Collector env vars")
    args = ap.parse_args(argv)

    try:
        registry, rendered = generate()
    except (RegistryError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.list_env:
        for p in registry["projects"]:
            tag = "  (default project)" if p["slug"] == registry["default"] else ""
            print(f"{p['auth_env']:<36} project={p['slug']}{tag}")
        return 0

    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            print(f"STALE: {OUTPUT.relative_to(ROOT)} does not match the registry/template.\n"
                  "Run: python scripts/gen-collector-config.py", file=sys.stderr)
            return 1
        print("OK: collector config is up to date.")
        return 0

    OUTPUT.write_text(rendered, encoding="utf-8")
    slugs = ", ".join(p["slug"] for p in registry["projects"])
    print(f"Wrote {OUTPUT.relative_to(ROOT)} — projects: {slugs} (default: {registry['default']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
