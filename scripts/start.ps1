$ErrorActionPreference = 'Stop'
$ProjectPath = Split-Path $PSScriptRoot -Parent
& (Join-Path $ProjectPath '.venv\Scripts\python.exe') -m personal_search.cli --data-dir (Join-Path $ProjectPath 'storage') serve
exit $LASTEXITCODE
