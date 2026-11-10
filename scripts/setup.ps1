$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if (Get-Command uv -ErrorAction SilentlyContinue) {
    $UvCommand = 'uv'
    $UvPrefix = @()
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m pip install --user uv
    if ($LASTEXITCODE -ne 0) { throw 'uv 安装失败' }
    $UvCommand = 'py'
    $UvPrefix = @('-3', '-m', 'uv')
} else {
    throw '请先从 python.org 安装 Python 3.11+（包含 py launcher），或安装 uv 后重试。'
}
& $UvCommand @UvPrefix sync --frozen --python 3.12
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
$PythonPath = Join-Path (Get-Location) '.venv\Scripts\python.exe'
$DataPath = Join-Path (Get-Location) 'storage'
& $PythonPath -m personal_search.cli --data-dir $DataPath doctor
if ($LASTEXITCODE -ne 0) { throw '本地检查失败' }
& $PythonPath -m personal_search.cli --data-dir $DataPath config
if ($LASTEXITCODE -ne 0) { throw '配置生成失败' }
Write-Host '安装完成。运行 scripts/login.ps1 扫码；MCP 配置见 config/mcp.local.json。'
