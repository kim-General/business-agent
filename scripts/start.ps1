param([int]$Port=8000)
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请先运行 scripts/setup.ps1 创建项目环境。' }
Push-Location (Join-Path $projectPath 'backend')
try { & $pythonPath -m uvicorn app.main:app --host 127.0.0.1 --port $Port }
finally { Pop-Location }

