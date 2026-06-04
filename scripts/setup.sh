#!/usr/bin/env bash
#  Observability Platform — one-command local setup (Linux / macOS)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "===  Observability Platform - Local Setup ==="

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker not found. Install Docker Engine / Desktop." >&2
  exit 1
fi

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created .env from .env.example."
  echo "  -> Run scripts/gen-secrets.sh and paste real secrets before using with client data."
fi

docker compose -f "$ROOT/docker-compose/docker-compose.yml" --env-file "$ROOT/.env" up -d

cat <<'EOF'

=== Services starting (ClickHouse may take ~30s on first boot) ===
  LangFuse:    http://localhost:3000
  Grafana:     http://localhost:3001  (admin / platform)
  Prometheus:  http://localhost:9090
  OTLP intake: localhost:4317 (gRPC) / 4318 (HTTP)

Run scripts/health-check.sh to verify all services are healthy.
EOF
