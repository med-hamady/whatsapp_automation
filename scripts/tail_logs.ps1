# Ouvre 4 fenetres PowerShell, une par log, en mode "live, only new lines".
# Utilisation : powershell -ExecutionPolicy Bypass -File scripts\tail_logs.ps1
# Ctrl+C dans une fenetre arrete son tail sans toucher au service.

$logs = "C:\Users\Administrator\whatsapp_automation\data\logs"

# Auto-detection du worker-<PID>.log courant (change a chaque Restart-Service)
$workerLog = Get-ChildItem "$logs\worker-*.log" |
    Where-Object { $_.Name -notmatch 'stdout|stderr' } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if (-not $workerLog) {
    Write-Warning "Aucun worker-*.log trouve dans $logs"
    $workerPath = "$logs\worker-stderr.log"  # fallback
} else {
    $workerPath = $workerLog.FullName
}

$panes = @(
    @{ Title = "WEBHOOK";        Path = "$logs\webhook.log" },
    @{ Title = "AI_OCR (anomalies)"; Path = "$logs\ai_ocr.log" },
    @{ Title = "AI_OCR (HTTP)";  Path = "$logs\ai_ocr-stdout.log" },
    @{ Title = "WORKER";         Path = $workerPath }
)

foreach ($p in $panes) {
    $cmd = "`$Host.UI.RawUI.WindowTitle='$($p.Title)'; Write-Host 'Tail live de: $($p.Path)' -ForegroundColor Cyan; Write-Host 'Ctrl+C pour arreter (n arrete PAS le service).' -ForegroundColor DarkGray; Write-Host ''; Get-Content '$($p.Path)' -Wait -Tail 0"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
}

Write-Host "4 fenetres ouvertes. Worker suivi : $workerPath" -ForegroundColor Green
