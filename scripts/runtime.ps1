$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BundledRoot = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies'

function Find-Python {
    if ($env:RUNWISE_PYTHON) { return $env:RUNWISE_PYTHON }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notlike '*WindowsApps*') { return $command.Source }
    $bundled = Join-Path $BundledRoot 'python/python.exe'
    if (Test-Path $bundled) { return $bundled }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $resolved = & $launcher.Source -3 -c 'import sys; print(sys.executable)'
        if ($LASTEXITCODE -eq 0) { return $resolved.Trim() }
    }
    throw 'Install Python 3.12+ from python.org, or set RUNWISE_PYTHON to python.exe.'
}

function Find-Node {
    $command = Get-Command node -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $bundled = Join-Path $BundledRoot 'node/bin/node.exe'
    if (Test-Path $bundled) { return $bundled }
    throw 'Install Node.js LTS from nodejs.org.'
}

function Invoke-ProjectNpm {
    param([string[]]$NpmArgs)
    $nodeExe = Find-Node
    $env:PATH = "$(Split-Path $nodeExe);$env:PATH"
    $env:npm_config_cache = Join-Path $ProjectRoot 'work/npm-cache'
    $command = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($command) { & $command.Source @NpmArgs }
    else {
        $npmCli = $env:RUNWISE_NPM_CLI
        if (-not $npmCli) {
            # Codex's temporary downloaded npm; a normal Node installation includes npm.
            $npmCli = Join-Path (Split-Path (Split-Path $ProjectRoot -Parent) -Parent) 'work/npm/package/bin/npm-cli.js'
        }
        if (-not (Test-Path $npmCli)) { throw 'npm is missing. Install Node.js LTS, or set RUNWISE_NPM_CLI.' }
        & $nodeExe $npmCli @NpmArgs
    }
    if ($LASTEXITCODE -ne 0) { throw "npm failed with exit code $LASTEXITCODE" }
}
