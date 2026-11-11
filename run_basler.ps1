$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'run_basler.cmd') @args
exit $LASTEXITCODE
