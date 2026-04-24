# Apply Legion migrations to a Postgres database using $env:PG_DSN.
#
# Usage:
#   $env:PG_DSN = "postgres://..."   # from Railway Postgres "Connection" tab
#   .\scripts\apply_migrations.ps1
#
# Requires a psql client on PATH. If you do not have one installed,
# copy the contents of migrations\0001_legion_base.sql into Railway's
# built-in Postgres query editor instead. The migration is idempotent
# (all CREATE IF NOT EXISTS / INSERT ON CONFLICT DO NOTHING).

$ErrorActionPreference = 'Stop'

if (-not $env:PG_DSN) { throw "PG_DSN not set in shell." }

$psql = Get-Command psql -ErrorAction SilentlyContinue
if (-not $psql) {
    Write-Host "psql not found on PATH." -ForegroundColor Yellow
    Write-Host "Options:"
    Write-Host "  1. Install Postgres client: winget install PostgreSQL.PostgreSQL"
    Write-Host "  2. OR paste migrations\0001_legion_base.sql into Railway's Postgres query tab."
    exit 2
}

$repo = Split-Path -Parent $PSScriptRoot
$migrations = Get-ChildItem -Path (Join-Path $repo 'migrations') -Filter '*.sql' | Sort-Object Name

foreach ($m in $migrations) {
    Write-Host "applying: $($m.Name)"
    & $psql.Source $env:PG_DSN -v ON_ERROR_STOP=1 -f $m.FullName
    if ($LASTEXITCODE -ne 0) { throw "migration failed: $($m.Name)" }
}

Write-Host ""
Write-Host "Verifying tables exist..."
& $psql.Source $env:PG_DSN -c "\dt claude_account_state hive_rounds hive_agent_scores agent_quota"
