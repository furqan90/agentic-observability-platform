#!/usr/bin/env bash
# Verify all platform services respond. (Linux / macOS)
set -uo pipefail

test_svc() {
  local name="$1" url="$2" code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" || echo "000")
  case "$code" in
    200|302|307) echo "  [OK]  $name ($url)" ;;
    *)           echo "  [!!]  $name ($url) -- HTTP $code" ;;
  esac
}

echo "=== Health check ==="
test_svc "LangFuse"     "http://localhost:3000/api/public/health"
test_svc "Grafana"      "http://localhost:3001/api/health"
test_svc "Prometheus"   "http://localhost:9090/-/healthy"
test_svc "Collector"    "http://localhost:8889/metrics"
test_svc "Alertmanager" "http://localhost:9093/-/healthy"
test_svc "Alert logger" "http://localhost:9095/healthz"
