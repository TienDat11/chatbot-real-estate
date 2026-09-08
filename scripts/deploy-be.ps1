# One-command deploy for the ragre Cloud Run backend (prod or staging).
#
# Usage:
#   .\scripts\deploy-be.ps1                       # prod: ragre-api, env-prod.yaml
#   .\scripts\deploy-be.ps1 -Env staging          # staging: ragre-api-staging, env-staging.yaml
#   .\scripts\deploy-be.ps1 -DryRun               # print resolved deploy command, change nothing
#
# Prerequisites:
#   - gcloud authenticated against project sale-chat-ai
#   - env-prod.yaml / env-staging.yaml present at repo root (untracked; non-secret env)
#   - .venv with the backend dependencies installed (build gates run against it)
#
# Secrets are never printed: the script only references Secret Manager names via
# --set-secrets, and the env yaml is consumed by gcloud without echoing contents.

param(
    [switch]$DryRun,
    [ValidateSet('staging', 'prod')]
    [string]$EnvName = 'prod',
    [string]$Region = 'asia-southeast1'
)

$ErrorActionPreference = 'Stop'

$Project = 'sale-chat-ai'
# $Env shadows the automatic env provider in PS, so the switch is $EnvName.
$ServiceName = if ($EnvName -eq 'staging') { 'ragre-api-staging' } else { 'ragre-api' }
$EnvFile = "env-$EnvName.yaml"
$BaseUrl = "https://$ServiceName-448504678237.$Region.run.app"

# Secret bindings must match Secret Manager names exactly; ":latest" pins the version alias.
# Shared across envs: the same Secret Manager versions back staging and prod.
$Secrets = @(
    'POSTGRES_PASSWORD',
    'ANON_IDENTITY_SECRET',
    'LEAD_MIRROR_HMAC_SECRET',
    'LLM_API_KEY',
    'EMBEDDING_API_KEY',
    'RERANK_API_KEY',
    'FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY',
    'R2_SECRET_ACCESS_KEY',
    'R2_ACCESS_KEY_ID'
) | ForEach-Object { "$_=$_`:latest" }

$DeployArgs = @(
    'run', 'deploy', $ServiceName,
    '--source', '.',
    '--region', $Region,
    '--allow-unauthenticated',
    '--min-instances=1',
    '--cpu-boost',
    '--no-cpu-throttling',
    '--timeout=360',
    '--startup-probe=periodSeconds=10,timeoutSeconds=5,failureThreshold=8,httpGet.path=/ready,httpGet.port=8080',
    "--set-secrets=$($Secrets -join ',')",
    "--env-vars-file=$EnvFile"
)

# [1/6] Preflight
Write-Host "[1/6] Preflight ($EnvName)"
if (-not (Test-Path $EnvFile) -or -not (Test-Path 'Dockerfile')) {
    Write-Error "Run from repo root: $EnvFile and/or Dockerfile not found in current directory."
    exit 1
}
$CurrentProject = (gcloud config get-value project)
if ($LASTEXITCODE -ne 0 -or $CurrentProject -ne $Project) {
    Write-Error "gcloud project is '$CurrentProject', expected '$Project'. Run: gcloud config set project $Project"
    exit 1
}
# A staging env file with unfilled __...__ placeholders would deploy lead/audit
# write paths against whichever DB it still names — most dangerously the prod
# Supabase pooler. Fail before deploy instead of shipping a data-mutating service.
if ($EnvName -eq 'staging') {
    $placeholders = Select-String -Path $EnvFile -Pattern '__[A-Z0-9_]+__'
    if ($placeholders) {
        $names = ($placeholders | ForEach-Object { $_.Matches.Value }) | Select-Object -Unique
        Write-Error "staging: $EnvFile still has placeholders: $($names -join ', '). Fill a dedicated staging database (never the prod Supabase pooler) before deploying staging."
        exit 1
    }
}
# [2/6] Build gates
Write-Host '[2/6] Build gates'
& .venv\Scripts\python.exe -m compileall -q api ingest eval
if ($LASTEXITCODE -ne 0) {
    Write-Error 'compileall failed.'
    exit 1
}

# CORS_ORIGINS must be a JSON array string; a scalar string crashes pydantic
# list[str] validation at import (this exact failure caused the 2026-09-07
# outage). Parse the target env's own CORS from its yaml and import the app
# under that value — prod is the strictest shared path, staging gets prod
# semantics with its own origin list.
$corsLine = Select-String -Path $EnvFile -Pattern "^CORS_ORIGINS:\s*'(.*)'\s*$"
if (-not $corsLine -or -not $corsLine.Matches[0].Groups[1].Value.StartsWith('[')) {
    Write-Error "${EnvFile}: CORS_ORIGINS must be a single-line quoted JSON array."
    exit 1
}
$oldCors = $env:CORS_ORIGINS
$oldAppEnv = $env:APP_ENV
try {
    $env:APP_ENV = 'prod'
    $env:CORS_ORIGINS = $corsLine.Matches[0].Groups[1].Value
    & .venv\Scripts\python.exe -c "from api.interfaces.api.main import app"
    if ($LASTEXITCODE -ne 0) {
        Write-Error 'Import smoke failed: app does not import with prod settings.'
        exit 1
    }
}
finally {
    $env:APP_ENV = $oldAppEnv
    $env:CORS_ORIGINS = $oldCors
}

# [3/6] Deploy
if ($DryRun) {
    Write-Host "[3/6] DryRun: service=$ServiceName envFile=$EnvFile"
    Write-Host "gcloud $($DeployArgs -join ' ')"
    exit 0
}
Write-Host '[3/6] Deploy (this blocks until traffic is routed)'
gcloud @DeployArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error 'gcloud run deploy failed.'
    exit 1
}

# [4/6] Revision check
Write-Host '[4/6] Revision check'
$Deadline = (Get-Date).AddMinutes(5)
$Ready = $false
$LatestReady = $null
while ((Get-Date) -lt $Deadline) {
    # gcloud pretty-prints --format=json across multiple lines; ConvertFrom-Json in
    # PS 5.1 needs the whole payload as one string, not a line-by-line pipeline.
    $SvcJson = (gcloud run services describe $ServiceName --region $Region --format=json) -join "`n"
    $Svc = $SvcJson | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) {
        Write-Error 'gcloud run services describe failed.'
        exit 1
    }
    $LatestCreated = $Svc.status.latestCreatedRevisionName
    $LatestReady = $Svc.status.latestReadyRevisionName
    $ReadyCond = $Svc.status.conditions | Where-Object { $_.type -eq 'Ready' }
    if ($LatestReady -eq $LatestCreated -and $ReadyCond.status -eq 'True') {
        $Ready = $true
        break
    }
    Start-Sleep -Seconds 15
}
if (-not $Ready) {
    Write-Error "Revision $LatestCreated not Ready within 5 minutes (latestReady: $LatestReady)."
    exit 1
}

# [5/6] Smoke
Write-Host '[5/6] Smoke checks'
function Invoke-StepCheck {
    param([string]$Name, [scriptblock]$Action)
    for ($i = 1; $i -le 3; $i++) {
        try {
            return & $Action
        } catch {
            if ($i -eq 3) {
                Write-Error "$Name failed after 3 attempts: $($_.Exception.Message)"
                exit 1
            }
            Start-Sleep -Seconds 5
        }
    }
}

$null = Invoke-StepCheck -Name '/health' -Action { Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 30 }
$ReadyBody = Invoke-StepCheck -Name '/ready' -Action { Invoke-RestMethod -Uri "$BaseUrl/ready" -TimeoutSec 30 }
if (-not $ReadyBody.ok) {
    Write-Error "/ready returned ok=$($ReadyBody.ok) (ok = pg AND lightrag)."
    exit 1
}

# CORS env on the live revision must be a JSON array; a scalar string here is
# the regression shape that broke startup before. --format=value(spec...env)
# renders the env list as a PS-style dict repr (name/value pairs separated by
# ';'), not NAME=VALUE lines, so match the dict shape instead.
$EnvLines = @(gcloud run revisions describe $LatestReady --region $Region --format='value(spec.containers[0].env)') -join ';'
$CorsLine = @($EnvLines -split ';' | Where-Object { $_ -match "name': 'CORS_ORIGINS', 'value': (.*)" })[0]
if (-not $CorsLine -or (-not ($Matches[1].Trim().TrimEnd('}').Trim().Trim("'").StartsWith('[')))) {
    Write-Error 'CORS_ORIGINS on the ready revision is not a JSON array string.'
    exit 1
}

# [6/6] Summary
Write-Host '[6/6] Deploy summary'
Write-Host "  Env      : $EnvName"
Write-Host "  Service  : $ServiceName"
Write-Host "  Revision : $LatestReady"
Write-Host "  URL      : $BaseUrl"
Write-Host "  /ready   : ok=$($ReadyBody.ok)"
exit 0
