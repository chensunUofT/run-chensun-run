. "$PSScriptRoot/runtime.ps1"
$pythonExe = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path $pythonExe)) { throw 'Run scripts/setup.ps1 first.' }
$backendProcess = $null
Push-Location $ProjectRoot
try {
    foreach ($port in @(8000, 5173)) {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
        try { $listener.Start() } catch { throw "Port $port is in use. Stop the previous Runwise server first." }
        finally { $listener.Stop() }
    }
    New-Item -ItemType Directory -Force work | Out-Null
    $serverArguments = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000')
    if (Test-Path -LiteralPath "$ProjectRoot/backend/.env") { $serverArguments += @('--env-file', '.env') }
    $backendProcess = Start-Process -FilePath $pythonExe -ArgumentList $serverArguments -WorkingDirectory "$ProjectRoot/backend" -WindowStyle Hidden -PassThru -RedirectStandardOutput "$ProjectRoot/work/backend.log" -RedirectStandardError "$ProjectRoot/work/backend-error.log"
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if ($backendProcess.HasExited) { throw 'Backend failed. See work/backend-error.log.' }
        try {
            $null = Invoke-RestMethod 'http://127.0.0.1:8000/api/health'
            $ready = $true
            break
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $ready) { throw 'Backend did not become ready. See work/backend-error.log.' }
    Write-Host 'Open http://localhost:5173 . Press Ctrl+C here to stop.'
    Push-Location frontend
    try { Invoke-ProjectNpm -NpmArgs @('run', 'dev', '--', '--host', '127.0.0.1', '--port', '5173', '--strictPort') }
    finally { Pop-Location }
} finally {
    if ($backendProcess -and -not $backendProcess.HasExited) { Stop-Process -Id $backendProcess.Id }
    Pop-Location
}
