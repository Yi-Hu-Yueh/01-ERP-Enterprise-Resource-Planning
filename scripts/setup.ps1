$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
New-Item -ItemType Directory -Path '.tmp' -Force | Out-Null
$env:TEMP = (Join-Path $projectRoot '.tmp')
$env:TMP = $env:TEMP
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Project virtual environment creation failed.' }
}
& '.\.venv\Scripts\python.exe' -m pip install --cache-dir '.pip-cache' -r 'backend\requirements-lock.txt'
if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
Push-Location -LiteralPath 'frontend'
try {
    npm.cmd ci --cache '.npm-cache'
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
} finally { Pop-Location }
Write-Output 'Setup complete. Run: .\.venv\Scripts\python.exe scripts\dev.py'

