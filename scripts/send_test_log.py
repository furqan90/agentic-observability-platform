"""
End-to-end log check (docs/LOGS-PLAN.md, phases L0/L1). Standard library only, so it runs in a bare
python image (the deploy smoke runs it in python:3.12-slim on the stack's network).

Sends one OTLP log record, then reads it back from Loki and checks:
  1. it arrived (found by a unique run tag in its body);
  2. its labels: service_name, deployment_environment, tl_project, host_name;
  3. its trace_id is kept as structured metadata, unchanged;
  4. (--grafana) the Loki data source's "Open in LangFuse" link was provisioned with real values,
     not unexpanded $VARIABLES.

    python scripts/send_test_log.py --to loki            # straight to Loki's OTLP endpoint (L0)
    python scripts/send_test_log.py --to collector       # through the Collector's logs pipeline (L1)

Environment:
    LOKI_URL                     default http://loki:3100
    OTEL_EXPORTER_OTLP_ENDPOINT  default http://otel-collector:4318   (--to collector)
    GRAFANA_URL                  default http://grafana:3000          (--grafana)
    GRAFANA_PASSWORD             the admin password                   (--grafana)
    DEPLOY_ENV                   default smoke
Exit code 1 on any failed check.
"""

import argparse
import base64
import json
import os
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICE = "obs-log-smoke"
PROJECT = "smoke"


def attr(key, value):
    return {"key": key, "value": {"stringValue": str(value)}}


def http(method, url, body=None, headers=None, timeout=10):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def send(endpoint, tag, trace_id, span_id, deploy_env):
    payload = {
        "resourceLogs": [{
            "resource": {"attributes": [
                attr("service.name", SERVICE),
                attr("deployment.environment", deploy_env),
                attr("tl.project", PROJECT),
                attr("host.name", socket.gethostname()),
            ]},
            "scopeLogs": [{
                "scope": {"name": "send_test_log"},
                "logRecords": [{
                    "timeUnixNano": str(time.time_ns()),
                    "severityNumber": 9,
                    "severityText": "INFO",
                    "body": {"stringValue": f"log smoke {tag}"},
                    # OTLP/JSON carries ids as hex, not base64
                    "traceId": trace_id,
                    "spanId": span_id,
                    "attributes": [attr("smoke.run", tag)],
                }],
            }],
        }]
    }
    status, _ = http("POST", endpoint, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    return status


def find(loki, tag, timeout_s=60):
    query = '{service_name="%s"} |= "%s"' % (SERVICE, tag)
    start = time.time_ns() - 15 * 60 * 10**9
    url = f"{loki}/loki/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": query, "start": start, "limit": 10, "direction": "backward"})
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            _, raw = http("GET", url)
            for stream in json.loads(raw)["data"]["result"]:
                if stream.get("values"):
                    return stream["stream"]
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            pass
        time.sleep(3)
    return None


def check_grafana(grafana, password):
    token = base64.b64encode(f"admin:{password}".encode()).decode()
    _, raw = http("GET", f"{grafana}/api/datasources/uid/loki", headers={"Authorization": f"Basic {token}"})
    fields = json.loads(raw).get("jsonData", {}).get("derivedFields", [])
    url = fields[0]["url"] if fields else ""
    ok = url.startswith("http") and "$LANGFUSE" not in url and url.endswith("/traces/${__value.raw}")
    return ok, url


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", choices=["loki", "collector"], default="loki")
    ap.add_argument("--grafana", action="store_true", help="also check the Loki data source's LangFuse link")
    args = ap.parse_args()

    loki = os.environ.get("LOKI_URL", "http://loki:3100").rstrip("/")
    if args.to == "loki":
        endpoint = f"{loki}/otlp/v1/logs"
    else:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318").rstrip("/") + "/v1/logs"
    deploy_env = os.environ.get("DEPLOY_ENV", "smoke")
    tag = f"run-{secrets.token_hex(6)}"
    trace_id, span_id = secrets.token_hex(16), secrets.token_hex(8)

    print(f"[1/3] Sending one record to {endpoint} (tag {tag}, trace_id {trace_id})")
    try:
        status = send(endpoint, tag, trace_id, span_id, deploy_env)
    except urllib.error.HTTPError as e:
        print(f"      [FAIL] HTTP {e.code}: {e.read()[:300]!r}")
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"      [FAIL] {e}")
        return 1
    print(f"      HTTP {status}")

    print(f"[2/3] Reading it back from {loki}")
    stream = find(loki, tag)
    failed = False
    if stream is None:
        print("      [FAIL] not found in Loki within 60 s")
        return 1
    expected = {"service_name": SERVICE, "deployment_environment": deploy_env, "tl_project": PROJECT,
                "host_name": socket.gethostname(), "trace_id": trace_id}
    for key, want in expected.items():
        got = stream.get(key)
        mark = "OK" if got == want else "FAIL"
        failed |= got != want
        print(f"      [{mark}] {key} = {got!r}" + ("" if got == want else f" (expected {want!r})"))

    if args.grafana:
        print("[3/3] Grafana's Loki data source link")
        grafana = os.environ.get("GRAFANA_URL", "http://grafana:3000").rstrip("/")
        try:
            ok, url = check_grafana(grafana, os.environ.get("GRAFANA_PASSWORD", ""))
        except (urllib.error.URLError, OSError, KeyError, ValueError, IndexError) as e:
            ok, url = False, f"error: {e}"
        failed |= not ok
        print(f"      [{'OK' if ok else 'FAIL'}] {url}")

    print("PASS" if not failed else "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
