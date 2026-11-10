$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
& '.\.venv\Scripts\python.exe' -m ruff check src tests scripts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& '.\.venv\Scripts\python.exe' -m pytest -q
exit $LASTEXITCODE
