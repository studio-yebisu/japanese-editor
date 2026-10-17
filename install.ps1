$ErrorActionPreference = 'Stop'
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    throw 'Python 3.12以降を導入してから、もう一度実行してください。'
}
& $pythonCommand.Source -B (Join-Path $PSScriptRoot 'install.py') @args
exit $LASTEXITCODE
