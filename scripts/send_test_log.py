"""
End-to-end log check (docs/LOGS-PLAN.md, phases L0/L1). Standard library only, so it runs in a bare
python image (the deploy smoke runs it in python:3.12-slim on the stack's network).

Sends OTLP log records, then reads them back from Loki and checks:
  1. the main record arrived (found by a unique run tag in its body);
  2. its labels: service_name, deployment_environment, tl_project, host_name;
  3. its trace_id is kept as structured metadata, unchanged;
  4. (--checks, through the Collector) the pipeline's rules: a record carrying an API key, a bearer token,
     a password and an email arrives redacted; a DEBUG record and a health-check access line never arrive;
  5. (--grafana) the Loki data source's "Open in LangFuse" link was provisioned with real values,
     not unexpanded $VARIABLES.

    python scripts/send_test_log.py --to loki                       # straight to Loki's OTLP endpoint (L0)
    python scripts/send_test_log.py --to collector --checks         # through the Collector's logs pipeline (L1)

Environment:
    LOKI_URL                     default http://loki:3100
    OTEL_EXPORTER_OTLP_ENDPOINT  default http://otel-collector:4318   (--to collector)
    GRAFANA_URL                  default http://grafana:3000          (--grafana)
    GRAFANA_PASSWORD             the admin password                   (--grafana)
    DEPLOY_ENV                   default smoke
    TRACE_ID                     32 hex characters: use a real trace's id (the deploy passes send_test_trace.py's),
                                 so the record's "Open in LangFuse" link opens an actual trace. Default: random
Exit code 1 on any failed check.
"""

import argparse
import base64
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICE = "obs-log-smoke"
PROJECT = "smoke"
# Fake secrets for the redaction check: shaped like the real thing, valid nowhere.
FAKE_KEY = "sk-" + "Z" * 24
FAKE_TOKEN = "eyJfake.smoke-token_value"
FAKE_EMAIL = "smoke.check@example.test"


def attr(key, value):
    return {"key": key, "value": {"stringValue": str(value)}}


def http(method, url, body=None, headers=None, timeout=10):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def record(severity_number, severity_text, body, trace_id, span_id, tag):
    return {
        "timeUnixNano": str(time.time_ns()),
        "severityNumber": severity_number,
        "severityText": severity_text,
        "body": {"stringValue": body},
        # OTLP/JSON carries ids as hex, not base64
        "traceId": trace_id,
        "spanId": span_id,
        "attributes": [attr("smoke.run", tag)],
    }


def send(endpoint, records, deploy_env):
    payload = {"resourceLogs": [{
        "resource": {"attributes": [
            attr("service.name", SERVICE),
            attr("deployment.environment", deploy_env),
            attr("tl.project", PROJECT),
            attr("host.name", socket.gethostname()),
        ]},
        "scopeLogs": [{"scope": {"name": "send_test_log"}, "logRecords": records}],
    }]}
    status, _ = http("POST", endpoint, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    return status


def query(loki, needle):
    q = '{service_name="%s"} |= "%s"' % (SERVICE, needle)
    url = f"{loki}/loki/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": q, "start": time.time_ns() - 15 * 60 * 10**9, "limit": 10, "direction": "backward"})
    _, raw = http("GET", url)
    return [(s["stream"], line) for s in json.loads(raw)["data"]["result"] for _, line in s.get("values", [])]


def wait_for(loki, needle, timeout_s=90):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            hits = query(loki, needle)
            if hits:
                return hits[0]
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
    ap.add_argument("--checks", action="store_true", help="also check redaction and dropping (needs --to collector)")
    ap.add_argument("--grafana", action="store_true", help="also check the Loki data source's LangFuse link")
    args = ap.parse_args()
    if args.checks and args.to != "collector":
        ap.error("--checks tests the Collector's pipeline: use it with --to collector")

    loki = os.environ.get("LOKI_URL", "http://loki:3100").rstrip("/")
    if args.to == "loki":
        endpoint = f"{loki}/otlp/v1/logs"
    else:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318").rstrip("/") + "/v1/logs"
    deploy_env = os.environ.get("DEPLOY_ENV", "smoke")
    tag = f"run-{secrets.token_hex(6)}"
    trace_id = os.environ.get("TRACE_ID", "").strip().lower()
    real_trace = bool(re.fullmatch(r"[0-9a-f]{32}", trace_id)) and trace_id != "0" * 32
    if not real_trace:
        trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)

    records = [record(9, "INFO", f"log smoke {tag}", trace_id, span_id, tag)]
    if args.checks:
        records += [
            record(9, "INFO", f"redact-{tag} key {FAKE_KEY} Authorization: Bearer {FAKE_TOKEN} "
                              f"password=hunter2-{tag[-4:]} contact {FAKE_EMAIL}", trace_id, span_id, tag),
            record(5, "DEBUG", f"debug-{tag} must not leave the machine", trace_id, span_id, tag),
            record(9, "INFO", f'10.0.0.1:5555 - "GET /health HTTP/1.1" 200 OK health-{tag}', trace_id, span_id, tag),
        ]

    print(f"[1/4] Sending {len(records)} record(s) to {endpoint} (tag {tag}, trace_id {trace_id}"
          f"{', a real trace' if real_trace else ', random'})")
    try:
        status = send(endpoint, records, deploy_env)
    except urllib.error.HTTPError as e:
        print(f"      [FAIL] HTTP {e.code}: {e.read()[:300]!r}")
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"      [FAIL] {e}")
        return 1
    print(f"      HTTP {status}")

    failed = False
    print(f"[2/4] Reading the main record back from {loki}")
    hit = wait_for(loki, f"log smoke {tag}")
    if hit is None:
        print("      [FAIL] not found in Loki within 90 s")
        return 1
    stream, _ = hit
    expected = {"service_name": SERVICE, "deployment_environment": deploy_env, "tl_project": PROJECT,
                "host_name": socket.gethostname(), "trace_id": trace_id}
    for key, want in expected.items():
        got = stream.get(key)
        failed |= got != want
        print(f"      [{'OK' if got == want else 'FAIL'}] {key} = {got!r}" + ("" if got == want else f" (expected {want!r})"))

    if args.checks:
        print("[3/4] The Collector's rules")
        hit = wait_for(loki, f"redact-{tag}", timeout_s=30)
        if hit is None:
            failed = True
            print("      [FAIL] the redaction record didn't arrive")
        else:
            line = hit[1]
            leaks = [s for s in (FAKE_KEY, FAKE_TOKEN, FAKE_EMAIL, f"hunter2-{tag[-4:]}") if s in line]
            marks = all(m in line for m in ("sk-<redacted>", "Bearer <redacted>", "password=<redacted>", "<email>"))
            ok = not leaks and marks
            failed |= not ok
            print(f"      [{'OK' if ok else 'FAIL'}] redacted: {line}" + (f"  LEAKED: {leaks}" if leaks else ""))
        # The main and redaction records arrived in the same batch, so anything still missing was dropped.
        for name, needle in (("DEBUG record", f"debug-{tag}"), ("health-check line", f"health-{tag}")):
            try:
                present = bool(query(loki, needle))
            except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
                present, name = True, f"{name} (query error: {e})"
            failed |= present
            print(f"      [{'FAIL' if present else 'OK'}] {name} {'arrived' if present else 'dropped'}")

    if args.grafana:
        print("[4/4] Grafana's Loki data source link")
        grafana = os.environ.get("GRAFANA_URL", "http://grafana:3000").rstrip("/")
        try:
            ok, url = check_grafana(grafana, os.environ.get("GRAFANA_PASSWORD", ""))
        except (urllib.error.URLError, OSError, KeyError, ValueError, IndexError) as e:
            ok, url = False, f"error: {e}"
        failed |= not ok
        print(f"      [{'OK' if ok else 'FAIL'}] {url}")
        if ok and real_trace:
            print(f"      This record's link: {url.replace('${__value.raw}', trace_id)}")

    print("PASS" if not failed else "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
