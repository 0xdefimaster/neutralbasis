$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskPython = (Get-Command python -ErrorAction Stop).Source
}
$taskData = Join-Path $taskRoot 'data-five-rh'
New-Item -ItemType Directory -Path $taskData -Force | Out-Null
$taskPidFile = Join-Path $taskData 'paper.pid'
$taskBot = Join-Path $taskRoot 'risk_main.py'
if (Test-Path -LiteralPath $taskPidFile) {
    $taskOldPid = [int](Get-Content -LiteralPath $taskPidFile)
    $taskOld = $null
    try { $taskOld = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/identity' -TimeoutSec 2 } catch {}
    if ($taskOld -and $taskOld.pid -eq $taskOldPid -and $taskOld.root -eq $taskRoot -and $taskOld.mode -eq 'PAPER') {
        Write-Output 'Paper motor zaten çalışıyor: http://127.0.0.1:8765'
        return
    }
    # Açılış sırasında (ör. ilk veritabanı bakımı) panel henüz cevap vermez; ikinci motor başlatılmaz.
    $taskStarting = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskOldPid) -ErrorAction SilentlyContinue
    if ($taskStarting -and $taskStarting.CommandLine -like '*risk_main.py*') {
        Write-Output 'Paper motor açılıyor; ikinci motor başlatılmadı.'
        return
    }
}
if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) { throw '8765 portu başka bir işlem tarafından kullanılıyor. Yeni motor başlatılmadı.' }
$taskProc = Start-Process -FilePath $taskPython -ArgumentList @('-u', ('"' + $taskBot + '"'), '--autostart') -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskData 'motor.log') -RedirectStandardError (Join-Path $taskData 'motor-error.log')
Set-Content -LiteralPath $taskPidFile -Value $taskProc.Id
Write-Output ('Paper motor başlatıldı. PID: ' + $taskProc.Id)
Write-Output 'Panel: http://127.0.0.1:8765'
Write-Output 'Yeni girişleri panelden durdurabilirsiniz. Açık pozisyonların izlemesi motor çalıştığı sürece devam eder.'
