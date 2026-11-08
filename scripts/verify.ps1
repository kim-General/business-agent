$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $projectPath 'backend')
try {
    & (Join-Path $projectPath '.venv\Scripts\python.exe') -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw '后端测试失败。' }
} finally { Pop-Location }
Push-Location (Join-Path $projectPath 'frontend')
try {
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw '前端检查或构建失败。' }
} finally { Pop-Location }

