# install.ps1 - provision the runtime + build the offline .venv (both modes).
#
# Idempotent and mode-agnostic. It is called automatically by start.ps1 on the
# first run, but can be run on its own to pre-provision the environment.
#
#   offline bundle - runtime\python, runtime\uv, pgsql\ and runtime\wheels are
#                    already inside the zip; this just builds the .venv from the
#                    bundled wheels with `pip install --no-index`.
#   online  bundle - those artifacts are absent; runtime\download-manifest.json
#                    pins their URL + sha256. This downloads + verifies each,
#                    then builds the .venv from PyPI wheels (hash-checked).

. (Join-Path $PSScriptRoot '_common.ps1')

$ProgressPreference = 'SilentlyContinue'  # IWR is ~10x faster without the bar

function Write-Step { param([string]$Message) Write-Host "[install] $Message" }

function Get-DownloadManifest {
    if (-not (Test-Path -LiteralPath $Manifest)) { return $null }
    return Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
}

function Save-Verified {
    param([string]$Url, [string]$Sha256, [string]$OutFile)
    $tmp = "$OutFile.part"
    Write-Step "download $Url"
    Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $tmp).Hash
    if ($actual -ne $Sha256.ToUpperInvariant()) {
        Remove-Item -LiteralPath $tmp -Force
        throw "sha256 mismatch for $Url`n  expected $Sha256`n  actual   $actual"
    }
    Move-Item -LiteralPath $tmp -Destination $OutFile -Force
    Write-Step "verified sha256=$($actual.ToLowerInvariant())"
}

function Get-DownloadCache {
    $cache = Join-Path $BundleRoot 'runtime\_downloads'
    New-Item -ItemType Directory -Force -Path $cache | Out-Null
    return $cache
}

function Install-Python {
    param($Manifest)
    if (Test-Path -LiteralPath (Join-Path $PyDir 'python.exe')) {
        Write-Step 'python: bundled runtime present (offline)'
        return
    }
    if (-not $Manifest) { throw 'python runtime missing and no download-manifest.json (bundle incomplete)' }
    $tar = Join-Path (Get-DownloadCache) 'python.tar.gz'
    Save-Verified -Url $Manifest.python.url -Sha256 $Manifest.python.sha256 -OutFile $tar
    Write-Step "python: extracting standalone CPython $($Manifest.python.version)"
    $runtime = Join-Path $BundleRoot 'runtime'
    New-Item -ItemType Directory -Force -Path $runtime | Out-Null
    # install_only tarball roots at python/, so this lands as runtime\python\.
    & tar.exe -xzf $tar -C $runtime
    if ($LASTEXITCODE -ne 0) { throw 'tar extract failed for python runtime' }
    if (-not (Test-Path -LiteralPath (Join-Path $PyDir 'python.exe'))) {
        throw 'python extract did not yield runtime\python\python.exe'
    }
}

function Install-Uv {
    param($Manifest)
    if (Test-Path -LiteralPath $UvExe) {
        Write-Step 'uv: bundled runtime present (offline)'
        return
    }
    if (-not $Manifest) { throw 'uv.exe missing and no download-manifest.json (bundle incomplete)' }
    $zip = Join-Path (Get-DownloadCache) 'uv.zip'
    Save-Verified -Url $Manifest.uv.url -Sha256 $Manifest.uv.sha256 -OutFile $zip
    Write-Step "uv: extracting uv $($Manifest.uv.version)"
    New-Item -ItemType Directory -Force -Path $UvDir | Out-Null
    Expand-Archive -LiteralPath $zip -DestinationPath $UvDir -Force
    if (-not (Test-Path -LiteralPath $UvExe)) { throw 'uv.exe not found after extract' }
}

function Expand-PgSlim {
    param([string]$ZipPath, [string]$Target, [string[]]$Keep)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $keepPrefixes = @($Keep | ForEach-Object { "pgsql/$_/" })
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName
            if ($name.EndsWith('/')) { continue }
            $keepIt = $false
            foreach ($prefix in $keepPrefixes) {
                if ($name.StartsWith($prefix)) { $keepIt = $true; break }
            }
            # keep the server license alongside the slim bin/lib/share set
            if (-not $keepIt -and $name -ne 'pgsql/server_license.txt') { continue }
            $rel = $name.Substring('pgsql/'.Length)
            $out = Join-Path $Target $rel
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $out) | Out-Null
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $out, $true)
        }
    }
    finally {
        $archive.Dispose()
    }
}

function Install-Postgres {
    param($Manifest)
    if (Test-Path -LiteralPath (Join-Path $PgBin 'postgres.exe')) {
        Write-Step 'pg: bundled server present (offline)'
        return
    }
    if (-not $Manifest) { throw 'pgsql missing and no download-manifest.json (bundle incomplete)' }
    $zip = Join-Path (Get-DownloadCache) 'pg16.zip'
    Save-Verified -Url $Manifest.postgres.url -Sha256 $Manifest.postgres.sha256 -OutFile $zip
    Write-Step "pg: extracting slim server set (bin/lib/share) $($Manifest.postgres.version)"
    Expand-PgSlim -ZipPath $zip -Target (Join-Path $BundleRoot 'pgsql') -Keep $Manifest.postgres.keep
}

function Install-Venv {
    if (Test-Path -LiteralPath $VenvPy) {
        Write-Step 'venv: already built'
        return
    }
    Write-Step 'venv: creating with bundled python'
    & (Join-Path $PyDir 'python.exe') -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
    if (Test-Path -LiteralPath $Wheels) {
        Write-Step 'deps: offline install from runtime\wheels (--no-index)'
        & $VenvPy -m pip install --no-index --find-links $Wheels --require-hashes -r $Reqs
    }
    else {
        Write-Step 'deps: online install from PyPI (hash-checked)'
        & $VenvPy -m pip install --require-hashes -r $Reqs
    }
    if ($LASTEXITCODE -ne 0) { throw 'dependency install failed' }
}

$manifest = Get-DownloadManifest
Install-Python -Manifest $manifest
Install-Uv -Manifest $manifest
Install-Postgres -Manifest $manifest
Install-Venv
Write-Step 'environment ready'
