# Generates the three crypto secrets LangFuse v3 requires, plus the base64
# OTLP auth header for the Collector. Paste the output into .env.

function New-Secret([int]$bytes = 32) {
    $b = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    return [System.BitConverter]::ToString($b).Replace("-", "").ToLower()
}

Write-Host "# --- Paste into .env (replace the change-me values) ---"
Write-Host "NEXTAUTH_SECRET=$(New-Secret 32)"
Write-Host "SALT=$(New-Secret 16)"
Write-Host "ENCRYPTION_KEY=$(New-Secret 32)   # 64 hex chars"
Write-Host ""

# LANGFUSE_OTEL_AUTH = base64("<public_key>:<secret_key>")
$pub = if ($env:LANGFUSE_INIT_PROJECT_PUBLIC_KEY) { $env:LANGFUSE_INIT_PROJECT_PUBLIC_KEY } else { "pk-lf-local" }
$sec = if ($env:LANGFUSE_INIT_PROJECT_SECRET_KEY) { $env:LANGFUSE_INIT_PROJECT_SECRET_KEY } else { "sk-lf-local" }
$pair = "{0}:{1}" -f $pub, $sec
$b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
Write-Host "# Derived from keys '$pub' / '$sec' (override via env before running):"
Write-Host "LANGFUSE_OTEL_AUTH=$b64"
Write-Host ""
Write-Host "# Additional Langfuse projects: one LANGFUSE_OTEL_AUTH_<SLUG> per project in"
Write-Host "# otel/langfuse-projects.env -- list them with: python scripts/gen-collector-config.py --list-env"
Write-Host "# (docs/MULTI-PROJECT.md)"
