# start.ps1 - first-run init + start (API + worker + managed PostgreSQL).
#
# First run: provision the runtime (install.ps1), generate bootstrap secrets,
# init/start PG, migrate, create an admin user. Subsequent runs: just `up`.

. (Join-Path $PSScriptRoot '_common.ps1')

function Write-Step { param([string]$Message) Write-Host "[start] $Message" }

# First run: build the offline .venv (offline: bundled wheels; online: download
# runtime + PyPI wheels). install.ps1 is the single shared provisioning path.
if (-not (Test-Path -LiteralPath $VenvPy)) {
    Write-Step 'first run: preparing Python environment'
    & (Join-Path $PSScriptRoot 'install.ps1')
}

$initializedMarker = Join-Path $env:DATAOPS_HOME 'config\.bundle-initialized'
if (-not (Test-Path -LiteralPath $initializedMarker)) {
    Write-Step 'first run: generating bootstrap secrets'
    Invoke-Launcher bootstrap init
    Write-Step 'first run: starting metadata PostgreSQL'
    Invoke-Launcher pg-up
    Write-Step 'first run: applying database migrations'
    Invoke-Launcher alembic-up
    Write-Step "first run: creating admin user $($env:DATAOPS_ADMIN_USER)"
    if ($env:DATAOPS_ADMIN_PASSWORD) {
        Invoke-Launcher admin create --username $env:DATAOPS_ADMIN_USER --password $env:DATAOPS_ADMIN_PASSWORD --update-password
    }
    else {
        Invoke-Launcher admin create --username $env:DATAOPS_ADMIN_USER --update-password
    }
    New-Item -ItemType File -Force -Path $initializedMarker | Out-Null
}

# The one-time password must not reach the long-lived launcher/API/worker tree.
Remove-Item Env:DATAOPS_ADMIN_PASSWORD -ErrorAction SilentlyContinue
Write-Step "launching DataOpsStudio on http://127.0.0.1:$($env:DATAOPS_API_PORT)/"
Write-Step 'press Ctrl+C in this window to stop gracefully.'
Invoke-Launcher up
