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
    # The .venv python.exe is a launcher: paper.pid holds the launcher, the engine that
    # answers /api/identity is its child. Either one is the registered engine.
    $taskOurs = $taskOld -and $taskOld.pid -eq $taskOldPid
    if ($taskOld -and -not $taskOurs) {
        $taskChild = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$taskOld.pid) -ErrorAction SilentlyContinue
        $taskOurs = [bool]($taskChild -and $taskChild.ParentProcessId -eq $taskOldPid -and $taskChild.CommandLine -like '*risk_main.py*')
    }
    if ($taskOurs -and $taskOld.root -eq $taskRoot -and $taskOld.mode -eq 'PAPER') {
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
# Her çalıştırma kendi log dosyalarına yazar; önceki çalıştırmaların logları (eski
# motor.log / motor-error.log dahil) hiçbir zaman üzerine yazılmaz. -X faulthandler
# native bir çökmede tüm thread yığınlarını stderr loguna düşürür; risk_main ayrıca
# logs\faulthandler-*.log dosyasına yazar.
$taskLogs = Join-Path $taskData 'logs'
New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
$taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$taskOut = Join-Path $taskLogs ('motor-' + $taskStamp + '.out.log')
$taskErr = Join-Path $taskLogs ('motor-' + $taskStamp + '.err.log')
$taskN = 1
while ((Test-Path -LiteralPath $taskOut) -or (Test-Path -LiteralPath $taskErr)) {
    $taskOut = Join-Path $taskLogs ('motor-' + $taskStamp + '-' + $taskN + '.out.log')
    $taskErr = Join-Path $taskLogs ('motor-' + $taskStamp + '-' + $taskN + '.err.log')
    $taskN++
}
$taskProc = Start-Process -FilePath $taskPython -ArgumentList @('-X', 'faulthandler', '-u', ('"' + $taskBot + '"'), '--autostart') -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $taskOut -RedirectStandardError $taskErr
Set-Content -LiteralPath $taskPidFile -Value $taskProc.Id
Write-Output ('Paper motor başlatıldı. PID: ' + $taskProc.Id)
Write-Output ('Loglar: ' + $taskOut + ' / ' + $taskErr)
Write-Output 'Panel: http://127.0.0.1:8765'
Write-Output 'Yeni girişleri panelden durdurabilirsiniz. Açık pozisyonların izlemesi motor çalıştığı sürece devam eder.'
