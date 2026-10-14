# Plans the removal of always-CPU (no CPU throttling) from revisions that hold
# the messenger tags on the ragre-api Cloud Run service.
#
# Default mode is read-only: it describes live state and prints the plan.
# -Apply does NOT execute anything; it prints copy-paste commands for the owner.

[CmdletBinding()]
param(
    [string]$Project = 'sale-chat-ai',
    [string]$Region = 'asia-southeast1',
    [string]$Service = 'ragre-api',
    [string[]]$Tags = @('messenger', 'messenger-rc2'),
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# gcloud is not on PATH on this workstation; the wrapper is the only entry point.
$Gc = 'D:/gcloud/gc.ps1'

function Invoke-GcloudJson {
    param([string[]]$GcArgs)
    $out = & powershell -NoProfile -File $Gc @GcArgs
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud read failed: $($GcArgs -join ' ')"
    }
    return ($out -join "`n") | ConvertFrom-Json
}

# Strict mode throws on missing properties; optional fields are absent when unset.
function Get-OptionalProperty {
    param($Object, [string]$Name)
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}

# Read-only: service status shows the live traffic split and tags.
$svc = Invoke-GcloudJson @('run', 'services', 'describe', $Service,
    "--project=$Project", "--region=$Region", '--format=json')

$liveTraffic = @($svc.status.traffic)
$servingEntries = @($liveTraffic | Where-Object { (Get-OptionalProperty $_ 'percent') -eq 100 })
$taggedEntries = @($liveTraffic | Where-Object { $null -ne (Get-OptionalProperty $_ 'tag') })

# Each tagged revision is inspected; the spec of a revision is immutable once created.
$revisionNames = @($servingEntries + $taggedEntries | ForEach-Object { $_.revisionName } | Select-Object -Unique)
$revisionInfo = @{}
foreach ($name in $revisionNames) {
    $rev = Invoke-GcloudJson @('run', 'revisions', 'describe', $name,
        "--project=$Project", "--region=$Region", '--format=json')
    $ann = $rev.metadata.annotations
    $throttle = Get-OptionalProperty $ann 'run.googleapis.com/cpu-throttling'
    $revisionInfo[$name] = [pscustomobject]@{
        Name       = $name
        MinScale   = Get-OptionalProperty $ann 'autoscaling.knative.dev/minScale'
        Throttling = if ($null -eq $throttle) { 'absent (default: throttled)' } else { $throttle }
        AlwaysCpu  = ($throttle -eq 'false')
        Image      = $rev.spec.containers[0].image
    }
}

$targetRevisions = @($taggedEntries |
    ForEach-Object { $_.revisionName } |
    Select-Object -Unique |
    Where-Object { $revisionInfo[$_].AlwaysCpu })

Write-Host "=== Live state: service $Service ($Project / $Region) ==="
foreach ($entry in $liveTraffic) {
    $pct = Get-OptionalProperty $entry 'percent'
    $tag = Get-OptionalProperty $entry 'tag'
    $label = if ($null -ne $tag) { "tag=$tag" } else { 'untagged' }
    Write-Host ("  {0,3}%  {1}  ({2})" -f $(if ($null -eq $pct) { 0 } else { $pct }), $entry.revisionName, $label)
}
Write-Host ''
Write-Host '=== Revision details ==='
foreach ($name in $revisionNames) {
    $info = $revisionInfo[$name]
    Write-Host ("  {0}: minScale={1}, cpu-throttling={2}, always-CPU={3}" -f `
        $info.Name, $info.MinScale, $info.Throttling, $info.AlwaysCpu)
    Write-Host "      image=$($info.Image)"
}
Write-Host ''

if ($targetRevisions.Count -eq 0) {
    Write-Host 'No tagged revision has always-CPU enabled. Nothing to remediate.'
    exit 0
}

$oldRevision = $targetRevisions[0]
Write-Host "Target (always-CPU, holds tags): $($targetRevisions -join ', ')"
Write-Host "Tags to move: $($Tags -join ', ')"
Write-Host ''

# The new revision pins the exact digest the tagged revision already runs, so the
# deploy changes only scaling config and never rebuilds from a possibly-dirty
# working tree. maxScale=2 matches the live service template; 20 would raise the
# ceiling 10x and multiply cost under spam.
$oldImage = $revisionInfo[$oldRevision].Image

# A bare tag or a tag-only reference would make `run deploy --image` resolve a
# moving target and could ship code that never served. Only an immutable digest
# is acceptable here.
if ($oldImage -notmatch '@sha256:[0-9a-f]{64}$') {
    throw "Refusing to plan: image of $oldRevision is not an immutable digest ('$oldImage'). " +
          'Resolve it with `run revisions describe ... --format=value(spec.containers[0].image)` first.'
}

$tagPairs = ($Tags | ForEach-Object { "$_=<NEW_REVISION>" }) -join ','
$deployArgs = "run deploy $Service --image=$oldImage --region $Region --project $Project " +
    '--no-traffic --min-instances=1 --max-instances=2 --cpu-boost --timeout=360'
$updateArgs = "run services update-traffic $Service --region $Region --project $Project " +
    "--update-tags=$tagPairs"
$verifyArgs = "run revisions describe <NEW_REVISION> --region $Region --project $Project " +
    "--format=`"yaml(metadata.annotations,spec.containers[0].image)`""

if (-not $Apply) {
    Write-Host '=== PLAN (dry run, nothing executed) ==='
    Write-Host ''
    Write-Host '(a) Deploy a new revision with default CPU throttling and no traffic shift:'
    Write-Host "    powershell -NoProfile -File $Gc $deployArgs"
    Write-Host '    Note: --no-cpu-throttling is intentionally omitted. --no-traffic keeps'
    Write-Host '    100% on the current serving revision. --min-instances=1 mirrors CI and is an'
    Write-Host '    owner decision; it still costs idle instance time under throttling.'
    Write-Host ''
    Write-Host '(b) MANDATORY GATE — compare env of the new revision against the tagged one.'
    Write-Host '    The deploy above does not pass --set-secrets/--env-vars-file, so the env of'
    Write-Host '    the new revision is NOT PREDICTABLE: it may come from the service template'
    Write-Host '    or from the revision currently serving. This has NOT been measured.'
    Write-Host '    Do not move the tags before running the comparison.'
    Write-Host "    powershell -NoProfile -File D:/gcloud/envcmp.ps1 -A <NEW_REVISION> -B $oldRevision"
    Write-Host '    Names must match. envcmp prints hashes for literals, never values.'
    Write-Host '    Do not proceed to (c) if any messenger variable is missing.'
    Write-Host ''
    Write-Host '(c) Move the messenger tags to the new revision (no traffic change):'
    Write-Host "    powershell -NoProfile -File $Gc $updateArgs"
    Write-Host '    Replace <NEW_REVISION> with the name printed by step (a).'
    Write-Host ''
    Write-Host '(d) Verify: new revision has no always-CPU, tags resolve, serving split unchanged:'
    Write-Host "    powershell -NoProfile -File $Gc $verifyArgs"
    Write-Host "    powershell -NoProfile -File $Gc run services describe $Service --project $Project --region $Region --format=`"yaml(status.traffic)`""
    Write-Host ''
    Write-Host "Old always-CPU revision $oldRevision is kept until verification passes. Re-run with -Apply to see the owner command block."
    exit 0
}

# -Apply never executes gcloud. The owner runs the printed commands after review.
Write-Host '=== -Apply: NOT EXECUTED. Owner must review and run these commands manually ===' 
Write-Host ''
Write-Host "gcloud equivalent (via wrapper), in order:"
Write-Host "  powershell -NoProfile -File $Gc $deployArgs"
Write-Host "  powershell -NoProfile -File $Gc $updateArgs"
Write-Host "  powershell -NoProfile -File $Gc $verifyArgs"
exit 0
