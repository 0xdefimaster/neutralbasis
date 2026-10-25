$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskEnv=Join-Path $PSScriptRoot '.venv'
if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv $taskEnv } else { & python -m venv $taskEnv }
if ($LASTEXITCODE -ne 0) { throw 'Python ortamı oluşturulamadı. Python 3.11 veya üzeri kurulumunu kontrol edin.' }
$taskPython=Join-Path $taskEnv 'Scripts\python.exe'
& $taskPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Bağımlılıklar yüklenemedi. İnternet bağlantısını kontrol edin.' }
Write-Output 'Kurulum tamamlandı. Baslat.cmd ile çalıştırabilirsiniz.'
