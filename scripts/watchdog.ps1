# Checks whether the HR Hiring Agent is actually responding, and restarts it if not.
# Meant to run on a schedule (every 5 minutes via Task Scheduler) on the server - this
# is what turns "the process crashed" from a multi-hour outage into a few-minute blip.
#
# Usage:
#   powershell -File scripts\watchdog.ps1                    (dev mode - restarts via python app.py)
#   powershell -File scripts\watchdog.ps1 -UseWaitress        (production - restarts via waitress-serve)
#   powershell -File scripts\watchdog.ps1 -Port 5000 -UseWaitress

param(
    [int]$Port = 5000,
    [switch]$UseWaitress
)

$appDir = Resolve-Path "$PSScriptRoot\.."
$logDir = Join-Path $appDir "backups"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "watchdog.log"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Add-Content -Path $logFile -Value $line
    Write-Host $line
}

function Test-AppHealthy {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/login" -TimeoutSec 5 -UseBasicParsing
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (Test-AppHealthy) {
    exit 0
}

Log "App not responding on port $Port - attempting restart."

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*app.py*" -or $_.CommandLine -like "*waitress-serve*" } |
    ForEach-Object {
        Log "Stopping stale process PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Start-Sleep -Seconds 2

if ($UseWaitress) {
    Log "Starting via waitress-serve..."
    Start-Process -FilePath "waitress-serve" -ArgumentList "--host=0.0.0.0 --port=$Port app:app" -WorkingDirectory $appDir -WindowStyle Hidden
} else {
    Log "Starting via python app.py (dev mode)..."
    Start-Process -FilePath "python" -ArgumentList "app.py" -WorkingDirectory $appDir -WindowStyle Hidden
}

Start-Sleep -Seconds 6

if (Test-AppHealthy) {
    Log "Restart succeeded - app is responding again."
} else {
    Log "Restart attempted but app is STILL not responding - this needs a person, not just a restart."
}
