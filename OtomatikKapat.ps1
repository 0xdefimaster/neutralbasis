$ErrorActionPreference = 'Stop'
$taskName = 'neutralbasis paper'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Output 'Otomatik başlatma kapatıldı. Motor çalışıyorsa Durdur.cmd ile durdurabilirsiniz.'
} else { Write-Output 'Otomatik başlatma zaten kapalı.' }
