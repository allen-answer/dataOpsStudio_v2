# _common.ps1 - shared environment for the DataOpsStudio 2.0 Win10 bundle.
#
# Dot-sourced by install.ps1 / start.ps1 / stop.ps1 so the offline and online
# bundles run one identical set of paths + launcher wiring (no drift). No secrets
# and no network side effects happen at dot-source time.
#
# The key anti-batch-quote-hell rule: every path is a single PowerShell variable
# and native tools are invoked with the call operator (&). PowerShell quotes each
# argument itself, so there is never a nested-quote string to get wrong.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# $PSScriptRoot inside a dot-sourced file resolves to that file's own directory,
# which is the bundle root (all scripts live at the bundle top level).
$BundleRoot = $PSScriptRoot
# __DATAOPS_BUILD_IDENTITY__

if (-not $env:DATAOPS_STATE_ROOT) { $env:DATAOPS_STATE_ROOT = $BundleRoot }
if (-not $env:DATAOPS_HOME)       { $env:DATAOPS_HOME = Join-Path $env:DATAOPS_STATE_ROOT 'home' }
if (-not $env:DATAOPS_API_PORT)   { $env:DATAOPS_API_PORT = '8020' }
if (-not $env:DATAOPS_PG_PORT)    { $env:DATAOPS_PG_PORT = '15432' }
if (-not $env:DATAOPS_ADMIN_USER) { $env:DATAOPS_ADMIN_USER = 'admin' }

$PyDir    = Join-Path $BundleRoot 'runtime\python'
$UvDir    = Join-Path $BundleRoot 'runtime\uv'
$UvExe    = Join-Path $UvDir 'uv.exe'
$Venv     = Join-Path $env:DATAOPS_STATE_ROOT '.venv'
$VenvPy   = Join-Path $Venv 'Scripts\python.exe'
$PgBin    = Join-Path $BundleRoot 'pgsql\bin'
$Wheels   = Join-Path $BundleRoot 'runtime\wheels'
$Reqs     = Join-Path $BundleRoot 'runtime\requirements-frozen.txt'
$Manifest = Join-Path $BundleRoot 'runtime\download-manifest.json'

# uv only ever execs the prebuilt project .venv at run time (bundle root holds
# pyproject.toml, so `uv run` resolves it) - never the network, in either mode.
$env:UV_OFFLINE = '1'
$env:UV_NO_SYNC = '1'
$env:UV_FROZEN = '1'
$env:UV_PYTHON_DOWNLOADS = '0'
$env:UV_PROJECT_ENVIRONMENT = $Venv
$env:DATAOPS_FORM = 'portable'
$env:PATH = "$PgBin;$env:PATH"

function Invoke-Launcher {
    $launcherExit = 0
    Push-Location -LiteralPath $BundleRoot
    try {
        & $VenvPy -m updater run --bundle-root $BundleRoot --state-root $env:DATAOPS_STATE_ROOT --python $VenvPy --home $env:DATAOPS_HOME --pg-bin-dir $PgBin --uv $UvExe -- @args
        $launcherExit = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($launcherExit -ne 0) {
        # Never echo launcher arguments here: first-run admin creation carries
        # a password argument and this exception is redirected to gui-start.log.
        throw "launcher failed (exit $launcherExit)"
    }
}
