$ErrorActionPreference = 'Stop'
# Windows Görev Zamanlayıcı görevi: oturum açılınca ve her 5 dakikada bir Baslat.ps1 çalışır.
# Baslat.ps1 motor zaten çalışıyorsa hiçbir şey yapmaz; kapanmış ya da çökmüşse yeniden başlatır.
$taskName = 'neutralbasis paper'
$taskScript = Join-Path $PSScriptRoot 'Baslat.ps1'
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $taskScript + '"') -WorkingDirectory $PSScriptRoot
$taskTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $taskUser),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650))
)
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTriggers -Settings $taskSettings -Description 'neutralbasis paper motorunu açık tutar.' -Force | Out-Null
Write-Output ('Otomatik başlatma açıldı: "' + $taskName + '" görevi oturum açılınca ve her 5 dakikada bir motoru kontrol eder.')
Write-Output 'Bilgisayar yeniden başlarsa motor, oturum açıldığında başlar. Kapatmak için: OtomatikKapat.cmd'
