param([switch]$Setup, [ValidateRange(1024, 65535)][int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$RuntimePath = Join-Path $ProjectRoot '.runtime'
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force -Path $RuntimePath | Out-Null

if (-not (Test-Path -LiteralPath $PythonPath)) {
    if (-not $Setup) { throw 'Run .\scripts\start.ps1 -Setup once to prepare the local environment.' }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.11 -m venv (Join-Path $ProjectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { & py -3 -m venv (Join-Path $ProjectRoot '.venv') }
    } else {
        & python -m venv (Join-Path $ProjectRoot '.venv')
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the local Python environment.' }
}
if ($Setup) {
    & $PythonPath -m pip install -r (Join-Path $ProjectRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $PythonPath (Join-Path $PSScriptRoot 'setup_mysql.py')
} else {
    & $PythonPath (Join-Path $PSScriptRoot 'setup_mysql.py') --start-only
}
if ($LASTEXITCODE -ne 0) { throw 'The private MySQL instance could not start.' }

$ApiPidPath = Join-Path $RuntimePath 'api.pid'
if (Test-Path -LiteralPath $ApiPidPath) {
    $PreviousPid = [int](Get-Content -LiteralPath $ApiPidPath -Raw)
    $PreviousProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$PreviousPid" -ErrorAction SilentlyContinue
    if ($PreviousProcess -and $PreviousProcess.ExecutablePath -eq $PythonPath -and $PreviousProcess.CommandLine -like '*gridmind.main:app*') {
        $ExistingPort = $Port
        if ($PreviousProcess.CommandLine -match '--port\s+(\d+)') { $ExistingPort = [int]$Matches[1] }
        Write-Host "GRIDMIND is already running. Open http://127.0.0.1:$ExistingPort"
        exit 0
    }
}
$Listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($Listener) { throw "Port $Port is occupied. Select an unused port with -Port." }
$BackendPath = Join-Path $ProjectRoot 'backend'
$ApiProcess = Start-Process -FilePath $PythonPath -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
    -ArgumentList @('-m', 'uvicorn', 'gridmind.main:app', '--app-dir', "`"$BackendPath`"", '--host', '127.0.0.1', '--port', $Port) `
    -RedirectStandardOutput (Join-Path $RuntimePath 'api-out.log') -RedirectStandardError (Join-Path $RuntimePath 'api-error.log')
Set-Content -LiteralPath $ApiPidPath -Value $ApiProcess.Id
for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
    Start-Sleep -Milliseconds 500
    $ApiProcess.Refresh()
    if ($ApiProcess.HasExited) { throw 'GRIDMIND exited during startup. Inspect .runtime\api-error.log.' }
    $Listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($Listener) {
        try {
            $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3
            if ($Health.status -eq 'ready') {
                Write-Host "GRIDMIND is ready: http://127.0.0.1:$Port"
                Write-Host 'Stop this project with .\scripts\stop.ps1.'
                exit 0
            }
        } catch { }
    }
}
throw 'GRIDMIND is still starting. Inspect .runtime\api-error.log.'
