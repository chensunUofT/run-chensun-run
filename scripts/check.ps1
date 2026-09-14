. "$PSScriptRoot/runtime.ps1"
Push-Location $ProjectRoot
try {
    Push-Location backend
    try {
        $testTemp = Join-Path $ProjectRoot ('work/pytest-' + [guid]::NewGuid().ToString('N'))
        & "$ProjectRoot/.venv/Scripts/python.exe" -m pytest tests -q -p no:cacheprovider --basetemp $testTemp
        if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed.' }
    } finally { Pop-Location }
    Push-Location frontend
    try { Invoke-ProjectNpm -NpmArgs @('run', 'build') }
    finally { Pop-Location }
    Write-Host 'Backend tests and frontend build passed.'
} finally { Pop-Location }
