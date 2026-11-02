"""
SLO breach demo — generates real synthetic agent traffic bad enough to
breach the Availability and/or Latency SLOs defined in
prometheus/rules/slo-alerts.yml, then polls Prometheus + Alertmanager so you
can watch the alert go from "no data" -> "pending" -> "firing" end to end.

This is the fastest way to prove the whole chain works:

    SDK spans -> OTel Collector -> spanmetrics -> Prometheus
        -> recording rules -> burn-rate alerts -> Alertmanager
        -> alert-webhook-logger (or Grafana / Alertmanager UI)

Usage (from repo root, stack already up via scripts/setup.ps1 or .sh):

    # Windows / PowerShell
    $env:LANGFUSE_PUBLIC_KEY = "pk-lf-local"
    $env:LANGFUSE_SECRET_KEY = "sk-lf-local"
    $env:LANGFUSE_HOST       = "http://localhost:3000"
    $env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4318"
    $env:DEPLOY_ENV       = "local"
    python scripts/simulate_slo_breach.py --mode both --duration 240

    # Linux/macOS (bash)
    LANGFUSE_PUBLIC_KEY=pk-lf-local LANGFUSE_SECRET_KEY=sk-lf-local \
    LANGFUSE_HOST=http://localhost:3000 \
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 DEPLOY_ENV=local \
    python scripts/simulate_slo_breach.py --mode both --duration 240

--mode availability  -> mostly-erroring calls (breaches the 99.5% availability SLO)
--mode latency       -> mostly-slow calls (breaches the 95%-under-3s latency SLO)
--mode both          -> alternates both, on separate synthetic agent names (default)

The fast-burn alerts require "for: 2m" of sustained breach on top of the 5m/1h
burn-rate windows, so give this at least ~3-4 minutes (--duration 240+) to see
alerts actually reach "firing" rather than just "pending".

If Grafana / Prometheus show nothing at all after a run, it's one of two things:
  1. OTEL_EXPORTER_OTLP_ENDPOINT wasn't set in the shell you ran this from --
     the SDK then sends traces straight to LangFuse and never touches the
     Collector, so no span_metrics / calls_total ever get created. This script
     checks for that env var and warns loudly if it's missing.
  2. The availability workload's tight loop outran the SDK's span export
     queue ("Queue full, dropping Span" in the output) and most spans were
     dropped before they ever left the process. --rps throttles that workload
     (default 5 calls/sec/worker) specifically to avoid this.
"""

import argparse
import concurrent.futures
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk", "python"))

from agentic_observability_sdk import get_tracer, trace_agent, trace_tool, shutdown  # noqa: E402

PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
ALERTMANAGER = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093")

AVAILABILITY_AGENT = "demo-breach-availability-agent"
LATENCY_AGENT = "demo-breach-latency-agent"


# -- synthetic workloads --------------------------------------------------

@trace_tool(name="flaky-downstream-call")
def _flaky_call(fail_rate: float):
    if random.random() < fail_rate:
        raise RuntimeError("simulated downstream failure (breach demo)")
    return "ok"


@trace_agent(name=AVAILABILITY_AGENT, tags=["demo", "slo-breach", "availability"])
def bad_availability_call():
    # ~95% failure rate -- comfortably past the 99.5% SLO's error budget
    return _flaky_call(fail_rate=0.95)


@trace_tool(name="slow-downstream-call")
def _slow_call():
    time.sleep(random.uniform(3.2, 4.5))  # breaches the 3000ms latency objective
    return "ok"


@trace_agent(name=LATENCY_AGENT, tags=["demo", "slo-breach", "latency"])
def bad_latency_call():
    return _slow_call()


def worker_loop(fn, stop_at: float, label: str, calls_per_sec: float):
    # IMPORTANT: the availability workload has no inherent delay (unlike the
    # latency workload, which is naturally rate-limited by its own sleep()).
    # Without a throttle here, a tight loop generates spans far faster than
    # the SDK's BatchSpanProcessor can export them -- its default queue
    # (2048) fills up almost immediately and the rest are silently dropped
    # client-side ("Queue full, dropping Span"), so nothing ever reaches the
    # Collector / Prometheus even though the script reports huge call counts.
    delay = 1.0 / calls_per_sec if calls_per_sec > 0 else 0
    calls, errors = 0, 0
    while time.time() < stop_at:
        started = time.time()
        try:
            fn()
        except Exception:
            errors += 1
        calls += 1
        if delay:
            elapsed = time.time() - started
            if elapsed < delay:
                time.sleep(delay - elapsed)
    return label, calls, errors


# -- Prometheus / Alertmanager polling -------------------------------------

def http_json(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def query_prometheus(promql):
    code, body = http_json(f"{PROMETHEUS}/api/v1/query?query={urllib.parse.quote(promql)}")
    if code == 200:
        return body.get("data", {}).get("result", [])
    return []


def check_span_metrics_arriving():
    """Distinguish 'no data reached Prometheus at all' from 'not breached yet'.

    Queries the raw calls_total series for the demo agents directly (no rate()/
    burn-rate math involved), so it works even seconds into the run.
    """
    results = query_prometheus('traces_span_metrics_calls_total{agent_name=~"demo-breach.*"}')
    if not results:
        print("      [!!] traces_span_metrics_calls_total has NO series yet for demo-breach-* agents.")
        return False
    total = sum(float(r["value"][1]) for r in results)
    agents = sorted({r["metric"].get("agent_name", "?") for r in results})
    print(f"      [OK] traces_span_metrics_calls_total series present for: {', '.join(agents)} (sum={total:.0f})")
    return True


def print_alert_states():
    code, body = http_json(f"{PROMETHEUS}/api/v1/rules")
    if code != 200:
        print(f"  [!!] could not reach Prometheus rules API (HTTP {code})")
        return
    found_any = False
    for group in body.get("data", {}).get("groups", []):
        for rule in group.get("rules", []):
            if rule.get("type") != "alerting":
                continue
            for alert in rule.get("alerts", []):
                labels = alert.get("labels", {})
                if "demo-breach" not in labels.get("agent_name", ""):
                    continue
                found_any = True
                print(f"    {rule['name']:32s} state={alert.get('state'):8s} agent={labels.get('agent_name')} value={alert.get('value')}")
    if not found_any:
        print("    (no matching alerts yet -- pending/firing takes a few evaluation cycles)")


def print_alertmanager_alerts():
    code, body = http_json(f"{ALERTMANAGER}/api/v2/alerts")
    if code != 200:
        print(f"  [!!] could not reach Alertmanager API (HTTP {code})")
        return
    matching = [a for a in body if "demo-breach" in a.get("labels", {}).get("agent_name", "")]
    if not matching:
        print("    (Alertmanager has not received a matching alert yet)")
        return
    for a in matching:
        labels = a.get("labels", {})
        status = a.get("status", {}).get("state", "?")
        print(f"    {labels.get('alertname'):32s} status={status:8s} agent={labels.get('agent_name')} severity={labels.get('severity')}")


# -- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Generate traffic that breaches the demo SLOs.")
    ap.add_argument("--mode", choices=["availability", "latency", "both"], default="both")
    ap.add_argument("--duration", type=int, default=240, help="seconds of bad traffic to generate")
    ap.add_argument("--concurrency", type=int, default=4, help="parallel workers per workload")
    ap.add_argument("--rps", type=float, default=5.0,
                     help="calls/sec per worker for the availability workload (throttled to avoid "
                          "overflowing the SDK's span export queue). Latency workload is naturally "
                          "rate-limited by its own sleep() and ignores this.")
    args = ap.parse_args()

    print("\n=== SLO breach demo ===\n")
    print(f"  mode        : {args.mode}")
    print(f"  duration    : {args.duration}s")
    print(f"  concurrency : {args.concurrency}")
    print(f"  rps/worker  : {args.rps} (availability workload only)")
    print(f"  Prometheus  : {PROMETHEUS}")
    print(f"  Alertmanager: {ALERTMANAGER}")
    print()

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not otlp_endpoint:
        print("  [!!] OTEL_EXPORTER_OTLP_ENDPOINT is NOT set in this shell.")
        print("       Without it, the SDK sends traces straight to LangFuse and SKIPS the")
        print("       OTel Collector entirely -- no span_metrics, no calls_total, no SLO data,")
        print("       ever, no matter how long this runs. Set it and re-run:")
        print('         $env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4318"   (PowerShell)')
        print("       Continuing anyway so you can see the difference in the diagnostic below...\n")
    else:
        print(f"  OTLP endpoint: {otlp_endpoint} (traces will route through the Collector)\n")

    print("[1/3] Initialising SDK...")
    get_tracer()
    print("      get_tracer() OK\n")

    stop_at = time.time() + args.duration
    jobs = []
    if args.mode in ("availability", "both"):
        jobs.append((bad_availability_call, "availability", args.rps))
    if args.mode in ("latency", "both"):
        jobs.append((bad_latency_call, "latency", 0))  # unthrottled -- self-limited by sleep()

    print(f"[2/3] Generating bad traffic for {args.duration}s "
          f"({args.concurrency} workers x {len(jobs)} workload(s))...")
    print("      (fast-burn alerts need ~2-4 min of sustained breach to reach 'firing' --")
    print("       watch it progress with the Prometheus / Alertmanager checks below)\n")

    total_calls = total_errors = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency * len(jobs)) as pool:
        futures = [
            pool.submit(worker_loop, fn, stop_at, label, rps)
            for fn, label, rps in jobs
            for _ in range(args.concurrency)
        ]
        # Periodic progress + data/alert-state check while workers run
        first_check = True
        while any(not f.done() for f in futures):
            time.sleep(20)
            remaining = max(0, int(stop_at - time.time()))
            print(f"      ... {remaining}s remaining -- checking whether data is arriving:")
            data_ok = check_span_metrics_arriving()
            if first_check and not data_ok:
                print("      ~ if this stays [!!] past ~60s, stop the run (Ctrl+C) and check:")
                print("        1) OTEL_EXPORTER_OTLP_ENDPOINT set in THIS shell (see above)")
                print("        2) docker compose ... logs --tail 50 otel-collector (receiving spans?)")
                print("        3) LangFuse UI (http://localhost:3000) -- traces tagged 'slo-breach'")
                print("           showing up there but NOT in calls_total means the collector path")
                print("           isn't being used (env var), not a Prometheus/Grafana problem.")
                first_check = False
            print_alert_states()

        for f in concurrent.futures.as_completed(futures):
            _, calls, errors = f.result()
            total_calls += calls
            total_errors += errors

    print(f"\n      Sent {total_calls} calls ({total_errors} simulated errors).")
    print("      Flushing spans...")
    shutdown()
    print("      Flush OK\n")

    print("[3/3] Final state (give the collector ~15-30s flush + Prometheus one more scrape):")
    time.sleep(20)
    print("\n  calls_total data check:")
    check_span_metrics_arriving()
    print("\n  Prometheus rule states:")
    print_alert_states()
    print("\n  Alertmanager alerts:")
    print_alertmanager_alerts()

    print("\n=== Where to look ===")
    print(f"  Prometheus alerts     : {PROMETHEUS}/alerts")
    print(f"  Alertmanager UI       : {ALERTMANAGER}/#/alerts")
    print("  alert-webhook-logger  : docker compose -f docker-compose/docker-compose.yml --env-file .env logs -f alert-webhook-logger")
    print("  Grafana Agent Ops     : http://localhost:3001  (SLO / Error Budget row)")
    print()
    print("  If alerts are still 'pending' rather than 'firing', re-run with a longer")
    print("  --duration -- the fast-burn alerts require 2 minutes of sustained breach")
    print("  AFTER the 5m/1h burn-rate windows already show the violation.")


if __name__ == "__main__":
    main()
