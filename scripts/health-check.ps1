# Verify all platform services respond. (Windows / PowerShell)
function Test-Svc($name, $url) {
    try {
        $code = (Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5).StatusCode
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
    }
    if ($code -in 200, 302, 307) { Write-Host "  [OK]  $name ($url)" }
    else { Write-Host "  [!!]  $name ($url) -- HTTP $code" }
}

Write-Host "=== Health check ==="
Test-Svc "LangFuse"   "http://localhost:3000/api/public/health"
Test-Svc "Grafana"    "http://localhost:3001/api/health"
Test-Svc "Prometheus" "http://localhost:9090/-/healthy"
Test-Svc "Collector"  "http://localhost:8889/metrics"
