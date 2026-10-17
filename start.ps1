param([int]$Port = 8896)
$ErrorActionPreference = 'Stop'
& python -B (Join-Path $PSScriptRoot 'app.py') serve --port $Port
exit $LASTEXITCODE
