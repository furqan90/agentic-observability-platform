#!/usr/bin/env bash
# Deploy the observability stack to the pilot VM (docs/PLAN_vm_observability.md, V2). Run from the repo root by
# ci/Jenkinsfile.vm, or by hand:
#
#   DOCKER_HOST=ssh://secp@172.20.0.46:2223 ENV_FILE=.env.vm bash ci/vm-deploy.sh
#
# It drives the VM's Docker over SSH, as the pilot's Target stage does: nothing is installed on the VM and no
# sudo is needed. Compose itself runs on the VM, in a docker:cli container that mounts $OBS_DIR at the same
# path, so the base file's relative bind mounts (../otel, ../grafana, ../.env, ...) resolve to real VM paths.
#
# Inputs (environment):
#   DOCKER_HOST   the VM's Docker (unset = this machine's: the local test, with an env file from gen-vm-env.sh --test)
#   ENV_FILE      the stack's .env (from scripts/gen-vm-env.sh; Jenkins: credential observability-vm-env)
#   OBS_DIR       folder on the VM                                  default /opt/observability
#   OBS_PROJECT   compose project name                              default obs
#   OBS_PROFILE   compose profile for the host exporters            default linux ("" on Docker Desktop, which
#                 can't give node-exporter the host's / mount)
#   SKIP_TRACE    1 = skip the end-to-end test trace                default 0
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash on Windows would rewrite /opt/... and /var/run/... paths

: "${ENV_FILE:?ENV_FILE is not set: the .env of the stack}"
[ -f "$ENV_FILE" ] || { echo "ENV_FILE $ENV_FILE not found" >&2; exit 1; }
OBS_DIR=${OBS_DIR:-/opt/observability}
OBS_PROJECT=${OBS_PROJECT:-obs}
OBS_PROFILE=${OBS_PROFILE-linux}
PROFILE_ARGS=${OBS_PROFILE:+--profile $OBS_PROFILE}
CLI_IMAGE=docker:29.10.1-cli
ALPINE=alpine:3.20
CURL=curlimages/curl:8.10.1

envval() { grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2- || true; }
step() { echo; echo "== $*"; }

step "Target"
os=$(docker version --format '{{.Server.Os}}')
echo "${DOCKER_HOST:-local docker}: $os, Docker $(docker version --format '{{.Server.Version}}')"
[ "$os" = linux ] || { echo "The target's Docker runs $os containers; the stack needs a Linux engine." >&2; exit 1; }

step "Checking the env file (values not shown)"
missing=0
for k in NEXTAUTH_SECRET SALT ENCRYPTION_KEY LANGFUSE_INIT_PROJECT_PUBLIC_KEY LANGFUSE_INIT_PROJECT_SECRET_KEY \
         LANGFUSE_INIT_USER_EMAIL LANGFUSE_INIT_USER_PASSWORD POSTGRES_PASSWORD DATABASE_URL CLICKHOUSE_PASSWORD \
         LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY LANGFUSE_OTEL_AUTH \
         GRAFANA_ADMIN_PASSWORD; do
    [ -n "$(envval $k)" ] || { echo "missing: $k" >&2; missing=1; }
done
[ "$missing" = 0 ] || exit 1
expect=$(printf '%s:%s' "$(envval LANGFUSE_INIT_PROJECT_PUBLIC_KEY)" "$(envval LANGFUSE_INIT_PROJECT_SECRET_KEY)" | base64 | tr -d '\n')
[ "$expect" = "$(envval LANGFUSE_OTEL_AUTH)" ] \
    || { echo "LANGFUSE_OTEL_AUTH isn't base64(public:secret) of the bootstrap keys: every export would get 401" >&2; exit 1; }
echo "ok"
NET=$(envval TELEMETRY_NETWORK); NET=${NET:-telemetry}

step "Copying the config bundle to $OBS_DIR"
# Replace the config folders whole, so a file deleted in git is deleted there too. Data lives in named volumes.
tar -cf - docker-compose otel/otel-collector-config.yaml otel/projects.yaml clickhouse prometheus grafana \
          scripts/send_test_trace.py sdk/python/agentic_observability_sdk \
    | docker run --rm -i -v "$OBS_DIR:/obs" "$ALPINE" sh -c '
        cd /obs && rm -rf docker-compose otel clickhouse prometheus grafana scripts sdk && tar -xf - \
        && find . -path ./.env -prune -o -type d -exec chmod 755 {} + -o -type f -exec chmod 644 {} +'
docker run --rm -i -v "$OBS_DIR:/obs" "$ALPINE" sh -c 'umask 077 && cat > /obs/.env' < "$ENV_FILE"
echo "ok"

step "Network $NET"
docker network inspect "$NET" >/dev/null 2>&1 && echo "exists" || docker network create "$NET"

compose() {
    docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$OBS_DIR:$OBS_DIR" -w "$OBS_DIR" "$CLI_IMAGE" \
        docker compose -p "$OBS_PROJECT" -f docker-compose/docker-compose.yml -f docker-compose/docker-compose.vm.yml \
        --env-file .env $PROFILE_ARGS "$@"
}

step "Pull"
compose pull -q

step "Bucket (minio-init, run alone: 'up --wait' counts its exit as a failure)"
compose run --rm minio-init

step "Up"
services=$(compose config --services | grep -vx minio-init | tr '\n' ' ')
compose up -d --wait --wait-timeout 600 $services

step "Smoke: LangFuse over $NET"
ok=0
for i in $(seq 1 60); do
    if docker run --rm --network "$NET" "$CURL" -fsS -m 5 http://langfuse-web:3000/api/public/health; then ok=1; break; fi
    sleep 5
done
echo
[ "$ok" = 1 ] || { echo "LangFuse didn't answer on $NET within 5 minutes" >&2; exit 1; }

if [ "${SKIP_TRACE:-0}" != 1 ]; then
    step "Smoke: an end-to-end trace from a container on $NET"
    docker run --rm --network "$NET" --network "${OBS_PROJECT}_default" -v "$OBS_DIR:/w:ro" \
        -e LANGFUSE_PUBLIC_KEY="$(envval LANGFUSE_INIT_PROJECT_PUBLIC_KEY)" \
        -e LANGFUSE_SECRET_KEY="$(envval LANGFUSE_INIT_PROJECT_SECRET_KEY)" \
        -e LANGFUSE_HOST=http://langfuse-web:3000 -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318 \
        -e PROMETHEUS_URL=http://prometheus:9090 -e DEPLOY_ENV=vm-deploy-smoke -e OTEL_SERVICE_NAME=vm-deploy-smoke \
        python:3.12-slim sh -c 'pip install -q --root-user-action=ignore --disable-pip-version-check \
            "langfuse>=4.2.0,<5" "opentelemetry-sdk>=1.25" "opentelemetry-exporter-otlp>=1.25" \
            && cd /w && python scripts/send_test_trace.py' 2>&1 | grep -v '^\s*\.\.\.'
fi

step "Memory against the caps"
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' | grep "^${OBS_PROJECT}-" || true
echo
echo "Deployed. LangFuse: $(envval LANGFUSE_PUBLIC_URL)  Grafana: $(envval GRAFANA_PUBLIC_URL)"
