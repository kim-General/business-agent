param([string]$Python='python')
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
Push-Location $projectPath
try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        & $Python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 环境创建失败，需要 Python 3.11 或更新版本。' }
    }
    & '.\.venv\Scripts\python.exe' -m pip install -r backend/requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败。' }
    Push-Location frontend
    try {
        & npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
    } finally { Pop-Location }
    Write-Host '安装完成。运行 scripts/start.ps1，然后访问 http://127.0.0.1:8000。'
} finally { Pop-Location }

