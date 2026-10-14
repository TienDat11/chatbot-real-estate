# Applies the Artifact Registry cleanup policy to the Cloud Run source-deploy
# and ragre repositories. Dry-run by default; pass -Apply to actually set the
# policy, which lets Artifact Registry delete matching versions asynchronously.

param(
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'

# gcloud is not on PATH in this environment; the wrapper resolves the SDK.
$GcWrapper = 'D:/gcloud/gc.ps1'
$Project = 'sale-chat-ai'
$Location = 'asia-southeast1'
$PolicyFile = Join-Path $PSScriptRoot 'ar-cleanup-policy.json'
$Repositories = @('cloud-run-source-deploy', 'ragre')

# Cloud Run revision tags (messenger, candidate-web, ...) do NOT protect the
# Artifact Registry digest: the cleanup policy only sees AR tags. Every digest a
# live or rollback revision can pull must therefore carry an AR tag before any
# Delete rule is enabled. Tagging is additive and idempotent.
$ProtectedDigests = @(
    # ragre-api-00044-qbb — serving 100% traffic. Untagged, 10 days old, so the
    # untagged-age rule would delete the image the live revision pulls on new
    # instance starts.
    @{
        Image = 'asia-southeast1-docker.pkg.dev/sale-chat-ai/cloud-run-source-deploy/ragre-api'
        Digest = 'sha256:0fdce4aecdeaf0e1721344002e1e3ce14e67d4060bef3b913d09d3be4ba44cb9'
        Tag = 'live-serving'
        Reason = 'ragre-api-00044-qbb serving 100% traffic'
    }
    # ragre-web-00009-pos — rollback target. Its Cloud Run tag 'candidate-web'
    # lives on the revision, not on the AR digest, so without an AR tag the
    # 14-day untagged rule removes it and rollback stops working.
    @{
        Image = 'asia-southeast1-docker.pkg.dev/sale-chat-ai/ragre/ragre-web'
        Digest = 'sha256:60b4c4d4cefe7674c8329c9372608c55fedac5fd1969afd826a6855de2fed407'
        Tag = 'rollback-web-00009'
        Reason = 'ragre-web-00009-pos rollback target (candidate-web)'
    }
)

function Invoke-Gcloud {
    param([string[]]$Arguments)

    Write-Host "  > gcloud $($Arguments -join ' ')"
    & powershell -NoProfile -File $GcWrapper @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud failed with exit code $LASTEXITCODE"
    }
}

$Mode = if ($Apply) { 'APPLY (policy will delete matching versions)' } else { 'DRY-RUN (no versions deleted)' }
Write-Host "Mode: $Mode"
Write-Host "Policy file: $PolicyFile"
Write-Host "Project: $Project  Location: $Location"

Write-Host ''
# Tagging is a WRITE to Artifact Registry. It is deliberately gated behind
# -Apply: a dry run must never mutate production, or the operator loses the
# ability to inspect the policy safely.
if ($Apply) {
    Write-Host 'Step 0: protect live and rollback digests with AR tags'
    foreach ($entry in $ProtectedDigests) {
        Write-Host "  $($entry.Reason) -> tag '$($entry.Tag)'"
        Invoke-Gcloud @(
            'artifacts', 'docker', 'tags', 'add',
            "$($entry.Image)@$($entry.Digest)",
            "$($entry.Image):$($entry.Tag)",
            "--project=$Project"
        )
    }
} else {
    Write-Host 'Step 0 (SKIPPED in dry-run): would protect these digests with AR tags'
    foreach ($entry in $ProtectedDigests) {
        Write-Host "  $($entry.Reason)"
        Write-Host "    $($entry.Image):$($entry.Tag) -> $($entry.Digest)"
    }
    Write-Host '  Re-run with -Apply to write these tags, then the policy below is set.'
}

foreach ($Repo in $Repositories) {
    Write-Host ''
    Write-Host "Repository: $Repo ($Location)"
    $Args = @(
        'artifacts', 'repositories', 'set-cleanup-policies', $Repo,
        "--location=$Location",
        "--project=$Project",
        "--policy=$PolicyFile"
    )
    if (-not $Apply) {
        $Args += '--dry-run'
    }
    Invoke-Gcloud $Args
}

Write-Host ''
Write-Host 'Done.'
