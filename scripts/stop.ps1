$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$RuntimePath = Join-Path $ProjectRoot '.runtime'
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$ApiPidPath = Join-Path $RuntimePath 'api.pid'
if (Test-Path -LiteralPath $ApiPidPath) {
    $ApiProcessId = [int](Get-Content -LiteralPath $ApiPidPath -Raw)
    $ApiProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$ApiProcessId" -ErrorAction SilentlyContinue
    if ($ApiProcess) {
        if ($ApiProcess.ExecutablePath -ne $PythonPath -or $ApiProcess.CommandLine -notlike '*gridmind.main:app*') {
            throw 'Refusing to stop an API process whose project ownership cannot be verified.'
        }
        Stop-Process -Id $ApiProcessId
        Write-Host 'The project API stopped.'
    }
    Remove-Item -LiteralPath $ApiPidPath -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath $PythonPath) {
    & $PythonPath (Join-Path $PSScriptRoot 'setup_mysql.py') --stop
    if ($LASTEXITCODE -ne 0) { throw 'Private MySQL shutdown failed; inspect the message above.' }
}
