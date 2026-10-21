# Backs up the HR Hiring Agent's data (database + uploaded JDs/resumes/interviews/offers)
# to a local backups folder, and - if a cloud-synced or external drive folder exists -
# copies it there too. Safe to run anytime; never touches the live data/ folder, only reads it.
#
# Usage:
#   powershell -File scripts\backup.ps1
#   powershell -File scripts\backup.ps1 -OffsiteRoot "E:\HR-Agent-Backups"
#   powershell -File scripts\backup.ps1 -KeepDaily 30
#
# Recommended: schedule this daily via Windows Task Scheduler.

param(
    [string]$OffsiteRoot = "$env:USERPROFILE\OneDrive\HR-Hiring-Agent-Backups",
    [int]$KeepDaily = 14
)

$ErrorActionPreference = "Stop"
$appDir = Resolve-Path "$PSScriptRoot\.."
$dataDir = Join-Path $appDir "data"
$backupRoot = Join-Path $appDir "backups"

if (-not (Test-Path $dataDir)) {
    Write-Error "No data folder found at $dataDir - nothing to back up."
    exit 1
}

New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$archiveName = "hr-agent-data-$timestamp.zip"
$archivePath = Join-Path $backupRoot $archiveName

Write-Host "Backing up $dataDir ..."
Compress-Archive -Path $dataDir -DestinationPath $archivePath -CompressionLevel Optimal
$sizeMB = [math]::Round((Get-Item $archivePath).Length / 1MB, 1)
Write-Host "Created $archivePath ($sizeMB MB)"

$offsiteParent = Split-Path $OffsiteRoot -Parent
if (Test-Path $offsiteParent) {
    New-Item -ItemType Directory -Force -Path $OffsiteRoot | Out-Null
    Copy-Item $archivePath -Destination $OffsiteRoot -Force
    Write-Host "Copied to offsite/cloud location: $OffsiteRoot"
} else {
    Write-Warning "Offsite folder's parent not found ($offsiteParent) - this backup is LOCAL ONLY, on the same disk as the app. That does not protect against disk failure. Set up OneDrive, another cloud sync, or an external/network drive and pass -OffsiteRoot."
}

# Retention: keep only the most recent $KeepDaily backups in the local backups folder
$old = Get-ChildItem $backupRoot -Filter "hr-agent-data-*.zip" | Sort-Object LastWriteTime -Descending | Select-Object -Skip $KeepDaily
if ($old) {
    Write-Host "Pruning $($old.Count) backup(s) older than the most recent $KeepDaily..."
    $old | Remove-Item -Force
}

Write-Host "Backup complete."
