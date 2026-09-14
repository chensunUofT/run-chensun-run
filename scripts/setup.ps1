. "$PSScriptRoot/runtime.ps1"
Push-Location $ProjectRoot
try {
    $pythonExe = Find-Python
    & $pythonExe -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
    & "$ProjectRoot/.venv/Scripts/python.exe" -m pip install -r backend/requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    Push-Location frontend
    try { Invoke-ProjectNpm -NpmArgs @('ci', '--cache', "$ProjectRoot/work/npm-cache") }
    finally { Pop-Location }
    Write-Host 'Ready. Run: powershell -ExecutionPolicy Bypass -File scripts/start.ps1'
} finally { Pop-Location }
