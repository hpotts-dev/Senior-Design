$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'run_simulation.cmd') @args
exit $LASTEXITCODE
