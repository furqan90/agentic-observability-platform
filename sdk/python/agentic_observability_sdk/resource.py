"""
One OpenTelemetry Resource for all three signals (CONTRACT.md §8).

A log line, the span it was written in, and that turn's token counters carry the same identity, which is
what lets Grafana (Loki, Prometheus) and LangFuse line them up. No ``langfuse`` import here.

| Attribute              | From                                          | Loki label             |
|------------------------|-----------------------------------------------|------------------------|
| service.name           | OTEL_SERVICE_NAME (default "agent")           | service_name           |
| service.version        | SERVICE_VERSION, else APP_VERSION (if set)    | -                      |
| service.instance.id    | the process's host name (container id)        | -                      |
| deployment.environment | DEPLOY_ENV (default "local")                  | deployment_environment |
| host.name              | HOST_NAME, else the host name                 | host_name              |
| tl.project             | LANGFUSE_PROJECT (if set)                     | tl_project             |

In a container the host name is the container id, which changes on every recreate. Set HOST_NAME to the
machine's name (e.g. in the compose file) when ``host_name`` should say which machine it was.
OTEL_RESOURCE_ATTRIBUTES is merged in by Resource.create; the values above win.
"""

import os
import socket

from .project import PROJECT_ATTR, get_project


def resource_attributes(extra: "dict | None" = None) -> dict:
    """The attributes of the shared Resource, plus ``extra`` (which wins)."""
    hostname = socket.gethostname()
    attrs = {
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "agent"),
        "service.instance.id": hostname,
        "deployment.environment": os.environ.get("DEPLOY_ENV", "local"),
        "host.name": os.environ.get("HOST_NAME") or hostname,
    }
    version = os.environ.get("SERVICE_VERSION") or os.environ.get("APP_VERSION")
    if version:
        attrs["service.version"] = version
    project = get_project()
    if project:
        attrs[PROJECT_ATTR] = project
    if extra:
        attrs.update(extra)
    return attrs


def build_resource(extra: "dict | None" = None):
    """The Resource every provider (traces, metrics, logs) is built with."""
    from opentelemetry.sdk.resources import Resource

    return Resource.create(resource_attributes(extra))
