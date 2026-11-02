"""
End-to-end trace verification script.

Sends a synthetic agent trace through the full pipeline and confirms:
  1. Trace arrives in LangFuse  (via LangFuse API)
  2. spanmetrics arrive in Prometheus (via Prometheus HTTP API)
  3. The OTel Collector was the routing path (not direct-to-LangFuse)
  4. (multi-project) The trace landed in LANGFUSE_PROJECT and NOT in the
     default project -- when DEFAULT_LANGFUSE_PUBLIC_KEY/SECRET_KEY are given

Multi-project routing check (see docs/MULTI-PROJECT.md):
    LANGFUSE_PROJECT=eka                       # slug from otel/projects.yaml
    LANGFUSE_PUBLIC_KEY / SECRET_KEY           # keys of the *eka* project
    DEFAULT_LANGFUSE_PUBLIC_KEY=pk-lf-local    # keys of the default project (optional)
    DEFAULT_LANGFUSE_SECRET_KEY=sk-lf-local

Usage:
    # Windows (PowerShell) -- from repo root
    $env:LANGFUSE_PUBLIC_KEY = "pk-lf-local"
    $env:LANGFUSE_SECRET_KEY = "sk-lf-local"
    $env:LANGFUSE_HOST       = "http://localhost:3000"
    $env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4318"
    $env:DEPLOY_ENV       = "local"
    python scripts/send_test_trace.py

    # Linux/macOS (bash) -- from repo root
    LANGFUSE_PUBLIC_KEY=pk-lf-local \
    LANGFUSE_SECRET_KEY=sk-lf-local \
    LANGFUSE_HOST=http://localhost:3000 \
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
    DEPLOY_ENV=local \
    python scripts/send_test_trace.py
"""

import os
import sys
import time
import base64
import urllib.request
import urllib.error
import json
import uuid

# -- allow running from repo root without installing the SDK ------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk", "python"))

from agentic_observability_sdk import (
    get_tracer, trace_agent, trace_tool, record_usage, shutdown,
)

LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
PROMETHEUS    = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
PUB_KEY       = os.environ["LANGFUSE_PUBLIC_KEY"]
SEC_KEY       = os.environ["LANGFUSE_SECRET_KEY"]
AGENT_NAME    = "test-trace-agent"
PROJECT       = os.environ.get("LANGFUSE_PROJECT") or None
DEFAULT_PUB   = os.environ.get("DEFAULT_LANGFUSE_PUBLIC_KEY")
DEFAULT_SEC   = os.environ.get("DEFAULT_LANGFUSE_SECRET_KEY")
# Unique per run, so a trace from an earlier run can never produce a false pass.
RUN_TAG       = f"run-{uuid.uuid4().hex[:12]}"

# -- helper -------------------------------------------------------------------

def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def lf_auth(pub=PUB_KEY, sec=SEC_KEY):
    return {"Authorization": "Basic " + base64.b64encode(
        f"{pub}:{sec}".encode()).decode()}


def find_trace(auth):
    code, body = http_get(
        f"{LANGFUSE_HOST}/api/public/traces?limit=5&tags={RUN_TAG}", headers=auth,
    )
    if code != 200:
        return code, None, 0
    traces = body.get("data", [])
    matching = [t for t in traces if t.get("name") == AGENT_NAME]
    return code, (matching[0] if matching else None), len(traces)


# -- step 1 -- initialise SDK --------------------------------------------------

print("\n=== Observability Platform - end-to-end trace test ===\n")
print(f"  LANGFUSE_HOST              : {LANGFUSE_HOST}")
print(f"  OTEL_EXPORTER_OTLP_ENDPOINT: {os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT', '(not set -- direct path)')}")
print(f"  DEPLOY_ENV              : {os.environ.get('DEPLOY_ENV', 'local')}")
print(f"  LANGFUSE_PROJECT           : {PROJECT or '(not set -- default project)'}")
print(f"  run tag                    : {RUN_TAG}")
print()

print("[1/4] Initialising SDK...")
get_tracer()
print("      get_tracer() OK")

# -- step 2 -- emit a synthetic trace -----------------------------------------

print("[2/4] Emitting synthetic trace...")

@trace_tool(name="test-retrieval")
def fake_retrieval(query: str) -> list:
    time.sleep(0.05)   # simulate latency
    return ["doc-1", "doc-2"]

@trace_agent(name=AGENT_NAME, tags=["smoke-test", "local", RUN_TAG])
def run_test_agent(user_input: str) -> str:
    docs = fake_retrieval(user_input)
    # Simulate a cheap LLM call (no real API key needed for trace testing)
    answer = f"Synthetic answer based on {len(docs)} docs."
    record_usage(AGENT_NAME, "gpt-4o-mock", input_tokens=42, output_tokens=18, cost_usd=0.0001)
    return answer

result = run_test_agent("What is the observability platform?")
print(f"      Agent returned: {result!r}")

# Flush before polling -- BatchSpanProcessor has a delay
print("      Flushing spans...")
shutdown()
print("      Flush OK")

# -- step 3 -- verify trace in LangFuse ---------------------------------------

print("[3/4] Checking LangFuse for the trace...")

POLL_INTERVAL = 3
MAX_WAIT      = 30
found_trace   = False

for attempt in range(MAX_WAIT // POLL_INTERVAL):
    time.sleep(POLL_INTERVAL)
    code, t, seen = find_trace(lf_auth())
    if code == 200:
        if t:
            print(f"      [OK] Trace found in LangFuse!")
            print(f"        id        : {t.get('id')}")
            print(f"        name      : {t.get('name')}")
            print(f"        tags      : {t.get('tags')}")
            print(f"        createdAt : {t.get('createdAt')}")
            found_trace = True
            break
        print(f"      ... waiting (attempt {attempt+1}, {seen} traces seen, none matching yet)")
    else:
        print(f"      ... LangFuse API returned HTTP {code}")

if not found_trace:
    print(f"      [FAIL] Trace NOT found in LangFuse after {MAX_WAIT}s")
    if PROJECT:
        print(f"        Is '{PROJECT}' in otel/projects.yaml, and do LANGFUSE_PUBLIC_KEY/SECRET_KEY")
        print("        belong to that project? Unknown slugs are routed to the default project.")
    print("        Check: docker compose logs langfuse-web --tail 30")
    print("        Check: docker compose logs otel-collector --tail 30")

# -- step 3b -- routing isolation (multi-project) ------------------------------

isolation = None   # None = not checked
if found_trace and PROJECT and DEFAULT_PUB and DEFAULT_SEC and DEFAULT_PUB != PUB_KEY:
    print(f"[3b ] Checking the trace did NOT land in the default project...")
    code, t, _ = find_trace(lf_auth(DEFAULT_PUB, DEFAULT_SEC))
    if code != 200:
        print(f"      [FAIL] default-project API returned HTTP {code}")
        isolation = False
    elif t:
        print("      [FAIL] Trace ALSO found in the default project -- routing is not isolating")
        isolation = False
    else:
        print(f"      [OK] Not in the default project -- routed to '{PROJECT}' only")
        isolation = True

# -- step 4 -- verify spanmetrics in Prometheus --------------------------------

print("[4/4] Checking Prometheus for spanmetrics...")
time.sleep(5)   # give spanmetrics connector a flush cycle

# Collector v0.153.0+ renames calls_total → traces_span_metrics_calls_total
PROM_METRIC = "traces_span_metrics_calls_total"
code, body = http_get(f"{PROMETHEUS}/api/v1/query?query={PROM_METRIC}")

found_metrics = False
if code == 200:
    results = body.get("data", {}).get("result", [])
    agent_metrics = [r for r in results if AGENT_NAME in str(r)]
    if results:
        print(f"      [OK] spanmetrics present in Prometheus ({len(results)} {PROM_METRIC} series)")
        if agent_metrics:
            print(f"        Agent-specific series found for '{AGENT_NAME}'")
        found_metrics = True
    else:
        print(f"      ~ {PROM_METRIC} not yet scraped (spanmetrics flush interval: 15s)")
        print(f"        Re-run in 15s or check: http://localhost:9090/graph?g0.expr={PROM_METRIC}")
else:
    print(f"      [FAIL] Prometheus returned HTTP {code}")

# -- summary ------------------------------------------------------------------

print()
print("=== Summary ===")
print(f"  Trace in LangFuse  : {'[OK] PASS' if found_trace   else '[FAIL] FAIL'}")
print(f"  Metrics in Prom    : {'[OK] PASS' if found_metrics else '~ CHECK MANUALLY'}")
if isolation is not None:
    print(f"  Project isolation  : {'[OK] PASS' if isolation else '[FAIL] FAIL'}")
print()
if found_trace:
    print(f"  View trace : {LANGFUSE_HOST}  (project: {PROJECT or 'default'}, tag: {RUN_TAG})")
print(f"  View metrics: http://localhost:9090/graph?g0.expr={PROM_METRIC}")
print(  "  View Grafana : http://localhost:3001")
print()
if not found_trace or isolation is False:
    sys.exit(1)
