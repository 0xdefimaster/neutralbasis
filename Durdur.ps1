$ErrorActionPreference = 'Stop'
$taskPidFile = Join-Path $PSScriptRoot 'data-five-rh\paper.pid'
if (-not (Test-Path -LiteralPath $taskPidFile)) { Write-Output 'Kayıtlı motor yok.'; return }
$taskBot = Join-Path $PSScriptRoot 'risk_main.py'
$taskPid = [int](Get-Content -LiteralPath $taskPidFile)
$taskIdentity = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/identity' -TimeoutSec 3
# The .venv python.exe is a launcher: paper.pid holds the launcher, the engine that
# answers /api/identity is its child. Either one is the registered engine.
$taskOurs = $taskIdentity.pid -eq $taskPid
if (-not $taskOurs) {
    $taskChild = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$taskIdentity.pid) -ErrorAction SilentlyContinue
    $taskOurs = [bool]($taskChild -and $taskChild.ParentProcessId -eq $taskPid -and $taskChild.CommandLine -like '*risk_main.py*')
}
if ($taskOurs -and $taskIdentity.root -eq $PSScriptRoot -and $taskIdentity.mode -eq 'PAPER') {
    $taskHtml = (Invoke-WebRequest -Uri 'http://127.0.0.1:8765/' -UseBasicParsing).Content
    $taskToken = [regex]::Match($taskHtml, "window.paperToken='([^']+)'").Groups[1].Value
    Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/shutdown' -Headers @{'X-Paper-Token'=$taskToken} -ContentType 'application/json' -Body '{}' | Out-Null
    Write-Output 'Motor kapanıyor. Açık paper kayıtları korunur; izleme durur.'
} else { Write-Output 'Kayıtlı işlem çalışmıyor; başka bir işlem durdurulmadı.' }
if (Get-ScheduledTask -TaskName 'neutralbasis paper' -ErrorAction SilentlyContinue) { Write-Output 'Not: otomatik başlatma açık; motor 5 dakika içinde yeniden başlar. Tamamen durdurmak için önce OtomatikKapat.cmd çalıştırın.' }
