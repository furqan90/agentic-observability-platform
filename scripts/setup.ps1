# Observability Platform — one-command local setup (Windows / PowerShell)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "=== Observability Platform - Local Setup ==="

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Docker not found. Install Docker Desktop."; exit 1
}

$envPath = Join-Path $root ".env"
if (-not (Test-Path $envPath)) {
    # Copy as UTF-8 without BOM -- PS 5.1 Copy-Item preserves encoding of the source,
    # so as long as .env.example is ASCII/UTF-8-no-BOM this stays clean.
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $content = [System.IO.File]::ReadAllText((Join-Path $root ".env.example"), $utf8NoBom)
    [System.IO.File]::WriteAllText($envPath, $content, $utf8NoBom)
    Write-Host "Created .env from .env.example."
    Write-Host "  -> Run scripts/gen-secrets.ps1 and paste real secrets before using with client data."
}

docker compose -f (Join-Path $root "docker-compose/docker-compose.yml") --env-file $envPath up -d

Write-Host ""
Write-Host "=== Services starting (ClickHouse may take ~30s on first boot) ==="
Write-Host "  LangFuse:    http://localhost:3000"
Write-Host "  Grafana:     http://localhost:3001  (admin / admin)"
Write-Host "  Prometheus:  http://localhost:9090"
Write-Host "  OTLP intake: localhost:4317 (gRPC) / 4318 (HTTP)"
Write-Host ""
Write-Host "Run scripts/health-check.ps1 to verify all services are healthy."
