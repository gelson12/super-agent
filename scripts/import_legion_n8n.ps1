# Import Legion-related n8n workflow changes via the n8n public API.
#
# Usage (from PowerShell):
#   $env:N8N_BASE_URL = "https://n8n.example.railway.app"   # NO trailing slash
#   $env:N8N_API_KEY  = "<your n8n API key>"
#   .\scripts\import_legion_n8n.ps1
#
# What it does:
#   1. PUT claude_verification_monitor.json → existing workflow jxnZZwTqJ7naPKc6
#   2. POST claude_trash_purge.json         → creates a new workflow (id returned)
#
# This script prints only workflow names, IDs, and status. It never echoes
# the API key or any header value.

$ErrorActionPreference = 'Stop'

if (-not $env:N8N_BASE_URL) { throw "N8N_BASE_URL not set in shell." }
if (-not $env:N8N_API_KEY)  { throw "N8N_API_KEY not set in shell." }

$Base    = $env:N8N_BASE_URL.TrimEnd('/')
$Headers = @{
    'X-N8N-API-KEY' = $env:N8N_API_KEY
    'Accept'        = 'application/json'
    'Content-Type'  = 'application/json'
}
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Read-Workflow([string]$Name) {
    $path = Join-Path $RepoRoot "n8n\$Name"
    if (-not (Test-Path $path)) { throw "Workflow file missing: $path" }
    $obj = Get-Content $path -Raw | ConvertFrom-Json
    # n8n API refuses unknown read-only properties; strip if present.
    foreach ($prop in @('id','createdAt','updatedAt','versionId','tags','active','triggerCount','pinData')) {
        if ($obj.PSObject.Properties.Name -contains $prop) {
            $obj.PSObject.Properties.Remove($prop) | Out-Null
        }
    }
    return $obj
}

function Update-Workflow([string]$WorkflowId, $Body) {
    $json = $Body | ConvertTo-Json -Depth 50 -Compress
    try {
        $resp = Invoke-RestMethod -Method Put -Uri "$Base/api/v1/workflows/$WorkflowId" `
                                  -Headers $Headers -Body $json
        Write-Host "  updated: $($resp.name) ($($resp.id))"
        return $resp
    } catch {
        Write-Host "  PUT failed: $($_.Exception.Message)" -ForegroundColor Red
        throw
    }
}

function New-Workflow($Body) {
    $json = $Body | ConvertTo-Json -Depth 50 -Compress
    try {
        $resp = Invoke-RestMethod -Method Post -Uri "$Base/api/v1/workflows" `
                                  -Headers $Headers -Body $json
        Write-Host "  created: $($resp.name) ($($resp.id))"
        return $resp
    } catch {
        Write-Host "  POST failed: $($_.Exception.Message)" -ForegroundColor Red
        throw
    }
}

function Set-WorkflowActive([string]$WorkflowId, [bool]$Active) {
    $verb = if ($Active) { 'activate' } else { 'deactivate' }
    try {
        $resp = Invoke-RestMethod -Method Post -Uri "$Base/api/v1/workflows/$WorkflowId/$verb" `
                                  -Headers $Headers
        Write-Host "  $verb`d: $($resp.id)"
    } catch {
        Write-Host "  $verb failed: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

# ─── 1. Update existing Claude-Verification-Monitor ────────────────────────
Write-Host "[1/2] Updating claude_verification_monitor.json → jxnZZwTqJ7naPKc6"
$verif = Read-Workflow 'claude_verification_monitor.json'
Update-Workflow -WorkflowId 'jxnZZwTqJ7naPKc6' -Body $verif

# ─── 2. Create new Claude Inbox Trash Purge ────────────────────────────────
Write-Host "[2/2] Creating claude_trash_purge.json (new workflow)"
$purge   = Read-Workflow 'claude_trash_purge.json'
$created = New-Workflow -Body $purge

# Activate the new trash-purge workflow so the 6h schedule starts firing.
if ($created -and $created.id) {
    Set-WorkflowActive -WorkflowId $created.id -Active $true
}

Write-Host ""
Write-Host "Done. Verify in n8n UI that both workflows look correct before the next"
Write-Host "scheduled execution (every 30s for verification, every 6h for purge)."
