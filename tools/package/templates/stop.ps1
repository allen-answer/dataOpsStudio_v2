# stop.ps1 - graceful stop (active queries drain). `stop.ps1 --force` for immediate.

. (Join-Path $PSScriptRoot '_common.ps1')

Write-Host '[stop] stopping DataOpsStudio (graceful; active queries drain) ...'
Invoke-Launcher stop @args
