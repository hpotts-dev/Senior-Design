$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'setup.cmd') @args
exit $LASTEXITCODE
