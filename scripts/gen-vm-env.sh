#!/usr/bin/env bash
# Generate the pilot VM's .env for the observability stack (docs/PLAN_vm_observability.md, V2).
#
#   bash scripts/gen-vm-env.sh --admin-email you@company.com [--out .env.vm] [--project secp-pilot]
#
# Every secret is fresh, and none is printed. Upload the file to Jenkins as the Secret file credential
# "observability-vm-env", then keep it somewhere safe (it holds the LangFuse admin password and the project keys
# the pilot's agent needs in V3) or delete it: Jenkins holds the copy that deploys.
#
# What it guarantees (each one a V1 finding):
#   - LANGFUSE_OTEL_AUTH = base64("<public key>:<secret key>") of the bootstrap project, so the Collector's
#     exports authenticate against a fresh LangFuse (this PC's .env doesn't match its own keys);
#   - hex secrets only: URL-safe, since the MinIO keys go into minio-init's MC_HOST_local URL;
#   - no spaces in any value, so the file is also safe to source from a shell.
set -euo pipefail

OUT=.env.vm
EMAIL=
PROJECT=secp-pilot
TEST=0
while [ $# -gt 0 ]; do
    case "$1" in
        --admin-email) EMAIL="$2"; shift 2 ;;
        --out)         OUT="$2"; shift 2 ;;
        --project)     PROJECT="$2"; shift 2 ;;
        # Local test of the overlay on this PC: other ports and network, so the PC's own stack is untouched.
        --test)        TEST=1; shift ;;
        -h|--help)     sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -n "$EMAIL" ] || { echo "--admin-email is required (the LangFuse admin's sign-in)" >&2; exit 2; }
case "$PROJECT" in *[!a-z0-9-]*|'') echo "--project must be lowercase letters, digits and '-'" >&2; exit 2 ;; esac
[ -e "$OUT" ] && { echo "$OUT exists; not overwriting (move it away first)" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl is needed" >&2; exit 1; }

hex() { openssl rand -hex "$1"; }

PUB="pk-lf-$(hex 16)"
SEC="sk-lf-$(hex 24)"
PG_PW=$(hex 24)
CH_PW=$(hex 24)
S3_ID=$(hex 10)
S3_SECRET=$(hex 24)

if [ "$TEST" = 1 ]; then
    LF_URL=http://localhost:13000; GF_URL=http://localhost:13001; LF_PORT=13000; GF_PORT=13001; NET=telemetry-test
else
    LF_URL=http://172.20.0.46:3000; GF_URL=http://172.20.0.46:3001; LF_PORT=3000; GF_PORT=3001; NET=telemetry
fi

umask 077
cat > "$OUT" <<EOF
# Pilot VM observability stack. Generated $(date -u +%Y-%m-%dT%H:%M:%SZ) by scripts/gen-vm-env.sh. Secret: never commit.

# Where people open the UIs, and the shared network the pilot's agent joins (docker-compose.vm.yml)
LANGFUSE_PUBLIC_URL=$LF_URL
GRAFANA_PUBLIC_URL=$GF_URL
LANGFUSE_PORT=$LF_PORT
GRAFANA_PORT=$GF_PORT
TELEMETRY_NETWORK=$NET

# LangFuse
NEXTAUTH_URL=$LF_URL
NEXTAUTH_SECRET=$(hex 32)
SALT=$(hex 32)
ENCRYPTION_KEY=$(hex 32)
LANGFUSE_INIT_ORG_ID=secp
LANGFUSE_INIT_ORG_NAME=SECP
LANGFUSE_INIT_PROJECT_ID=$PROJECT
LANGFUSE_INIT_PROJECT_NAME=$PROJECT
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=$PUB
LANGFUSE_INIT_PROJECT_SECRET_KEY=$SEC
LANGFUSE_INIT_USER_EMAIL=$EMAIL
LANGFUSE_INIT_USER_NAME=admin
LANGFUSE_INIT_USER_PASSWORD=$(hex 12)

# Postgres
POSTGRES_USER=langfuse
POSTGRES_PASSWORD=$PG_PW
POSTGRES_DB=langfuse
DATABASE_URL=postgresql://langfuse:$PG_PW@postgres:5432/langfuse

# ClickHouse
CLICKHOUSE_USER=clickhouse
CLICKHOUSE_PASSWORD=$CH_PW
CLICKHOUSE_URL=http://clickhouse:8123
CLICKHOUSE_MIGRATION_URL=clickhouse://clickhouse:9000

# Redis
REDIS_HOST=redis
REDIS_PORT=6379

# MinIO (S3 event upload)
LANGFUSE_S3_EVENT_UPLOAD_BUCKET=langfuse
LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT=http://minio:9000
LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID=$S3_ID
LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY=$S3_SECRET
LANGFUSE_S3_EVENT_UPLOAD_REGION=us-east-1
LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE=true

# Collector -> LangFuse: derived from the bootstrap project's keys above
LANGFUSE_OTEL_AUTH=$(printf '%s:%s' "$PUB" "$SEC" | base64 | tr -d '\n')

# Grafana (user: admin)
GRAFANA_ADMIN_PASSWORD=$(hex 12)
EOF

echo "Wrote $OUT (mode 600). LangFuse admin: $EMAIL; project: $PROJECT. Secrets are in the file, not shown here."
[ "$TEST" = 1 ] || echo "Next: upload it to Jenkins as the Secret file credential 'observability-vm-env'."
