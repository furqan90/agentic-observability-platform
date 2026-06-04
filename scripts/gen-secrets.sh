#!/usr/bin/env bash
# Generates the three crypto secrets LangFuse v3 requires, plus the base64
# OTLP auth header for the Collector. Paste the output into .env.
set -euo pipefail

echo "# --- Paste into .env (replace the change-me values) ---"
echo "NEXTAUTH_SECRET=$(openssl rand -hex 32)"
echo "SALT=$(openssl rand -hex 16)"
echo "ENCRYPTION_KEY=$(openssl rand -hex 32)   # 64 hex chars"
echo

# LANGFUSE_OTEL_AUTH = base64("<public_key>:<secret_key>")
PUB="${LANGFUSE_INIT_PROJECT_PUBLIC_KEY:-pk-lf-local}"
SEC="${LANGFUSE_INIT_PROJECT_SECRET_KEY:-sk-lf-local}"
B64=$(printf '%s:%s' "$PUB" "$SEC" | base64 | tr -d '\n')
echo "# Derived from keys '$PUB' / '$SEC' (override via env before running):"
echo "LANGFUSE_OTEL_AUTH=$B64"
