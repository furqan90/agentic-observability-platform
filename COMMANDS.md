# Observability Platform — Command Reference

All commands run from the **repo root** (`C:\Work\Projects\agentic_observability_platform`).

---

## One-time setup

```powershell
# 1. Generate real crypto secrets and paste them into .env
.\scripts\gen-secrets.ps1

# 2. Start the full stack (copies .env.example→.env if absent, then up -d)
.\scripts\setup.ps1
```

The `.env` file is git-ignored. Never commit it.

---

## Daily start / stop

### Start (detached, Windows default)

```powershell
docker compose -f docker-compose/docker-compose.yml --env-file .env up -d
```

### Start with debug ports exposed (Postgres 5432, ClickHouse 8123/9000, Redis 6379, MinIO 9002/9001)

```powershell
docker compose `
  -f docker-compose/docker-compose.yml `
  -f docker-compose/docker-compose.override.yml `
  --env-file .env up -d
```

### Start on Linux / CI — includes node-exporter + cadvisor for host metrics

```bash
docker compose \
  -f docker-compose/docker-compose.yml \
  --profile linux \
  --env-file .env up -d
```

### Stop containers (keep volumes / data intact)

```powershell
docker compose -f docker-compose/docker-compose.yml --env-file .env stop
```

### Stop + remove containers (volumes survive — data is safe)

```powershell
docker compose -f docker-compose/docker-compose.yml --env-file .env down
```

### ⚠️ Nuke everything including volumes (destroys all data — fresh start)

```powershell
docker compose -f docker-compose/docker-compose.yml --env-file .env down -v
```

---

## Health & status

```powershell
# Health check script (LangFuse / Grafana / Prometheus / OTel Collector)
.\scripts\health-check.ps1

# Container status table
docker compose -f docker-compose/docker-compose.yml --env-file .env ps

# Prometheus scrape targets (check each job is "up")
# Open in browser:
start http://localhost:9090/targets
```

---

## Logs

```powershell
# Follow all logs
docker compose -f docker-compose/docker-compose.yml --env-file .env logs -f

# Follow a single service
docker compose -f docker-compose/docker-compose.yml --env-file .env logs -f langfuse-web
docker compose -f docker-compose/docker-compose.yml --env-file .env logs -f langfuse-worker
docker compose -f docker-compose/docker-compose.yml --env-file .env logs -f otel-collector
docker compose -f docker-compose/docker-compose.yml --env-file .env logs -f clickhouse

# Last N lines of a service (no follow)
docker compose -f docker-compose/docker-compose.yml --env-file .env logs --tail 50 langfuse-web
```

---

## Restart / rebuild

```powershell
# Restart a single service (e.g. after config change)
docker compose -f docker-compose/docker-compose.yml --env-file .env restart otel-collector
docker compose -f docker-compose/docker-compose.yml --env-file .env restart langfuse-web

# Pull latest images and restart everything
docker compose -f docker-compose/docker-compose.yml --env-file .env pull
docker compose -f docker-compose/docker-compose.yml --env-file .env up -d
```

---

## Service endpoints

| Service            | URL                              | Notes                              |
|--------------------|----------------------------------|------------------------------------|
| LangFuse UI        | http://localhost:3000            | Default login: furqan@example.com / local |
| Grafana            | http://localhost:3001            | admin / $GRAFANA_ADMIN_PASSWORD                  |
| Prometheus         | http://localhost:9090            | Metrics store + targets view       |
| OTLP gRPC          | localhost:4317                   | Agents send spans here             |
| OTLP HTTP          | localhost:4318                   | Agents send spans here (HTTP)      |
| OTel metrics       | http://localhost:8889/metrics    | Prometheus scrapes this            |

### Debug ports (override file only)

| Service            | Host port | Notes                              |
|--------------------|-----------|------------------------------------|
| PostgreSQL         | 5432      | `psql -h localhost -U langfuse`    |
| ClickHouse HTTP    | 8123      | `curl http://localhost:8123/ping`  |
| ClickHouse native  | 9000      | clickhouse-client                  |
| Redis              | 6379      | `redis-cli -h localhost`           |
| MinIO S3 API       | 9002      | (9000 taken by ClickHouse native)  |
| MinIO console      | http://localhost:9001 | minio / miniosecret   |
| Postgres exporter  | 9187      | Raw Prometheus metrics             |

---

## Secrets

```powershell
# Re-generate secrets (if rotating)
.\scripts\gen-secrets.ps1
# Then paste new values into .env and restart affected services:
docker compose -f docker-compose/docker-compose.yml --env-file .env restart langfuse-web langfuse-worker
```

---

## Langfuse projects (multi-project routing)

Full runbook: [`docs/MULTI-PROJECT.md`](docs/MULTI-PROJECT.md).

```powershell
# 1. Create the project in the Langfuse UI, then create its API keys.
# 2. Add it to otel/projects.yaml, then regenerate the Collector config:
python scripts/gen-collector-config.py
python scripts/gen-collector-config.py --list-env      # which env vars the Collector needs
# 3. Put base64("pk:sk") into otel/langfuse-projects.env as LANGFUSE_OTEL_AUTH_<SLUG>
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("pk-lf-...:sk-lf-..."))
# 4. Reload the Collector (recreate so the env file is re-read)
docker compose -f docker-compose/docker-compose.yml --env-file .env up -d --force-recreate otel-collector
# 5. Verify routing + isolation
$env:LANGFUSE_PROJECT = "eka"
$env:LANGFUSE_PUBLIC_KEY = "pk-lf-<eka>"; $env:LANGFUSE_SECRET_KEY = "sk-lf-<eka>"
$env:DEFAULT_LANGFUSE_PUBLIC_KEY = "pk-lf-local"; $env:DEFAULT_LANGFUSE_SECRET_KEY = "sk-lf-local"
python scripts/send_test_trace.py
```

```powershell
# CI / pre-commit: fail if the committed collector config is stale
python scripts/gen-collector-config.py --check
pytest scripts/tests sdk/python/tests
```

---

## Eval dataset

```powershell
# Seed the baseline eval dataset into LangFuse (idempotent)
$env:LANGFUSE_PUBLIC_KEY = "pk-lf-local"
$env:LANGFUSE_SECRET_KEY = "sk-lf-local"
$env:LANGFUSE_HOST       = "http://localhost:3000"
python scripts/seed-eval-dataset.py
```

---

## Quick fixes

### LangFuse web crash-loops on first boot

ClickHouse Keeper needs a few seconds to elect. Wait ~15s then:

```powershell
docker compose -f docker-compose/docker-compose.yml --env-file .env restart langfuse-web langfuse-worker
```

### node-exporter / cadvisor show "down" in Prometheus

Expected on Windows Docker Desktop. They're behind the `--profile linux` flag. Harmless — only those two infra panels in Grafana will be empty until you run on Linux/AKS.

### Port conflict on startup

ClickHouse native port (9000) and MinIO S3 port (9000) would collide. In the override file MinIO is remapped to host port **9002**. If you see a port bind error, check nothing else on your machine uses 3000, 3001, 4317, 4318, 8889, or 9090.
