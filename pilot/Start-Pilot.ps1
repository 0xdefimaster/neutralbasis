# Start the isolated PAPER pilot (port 18765, data on C:). PAPER ONLY.
$ErrorActionPreference = "Stop"
$py = "E:\NeutralBasis\.venv\Scripts\python.exe"   # interpreter only; nothing is written to E:
$root = Split-Path $PSScriptRoot -Parent
$data = if ($env:NB_PILOT_DATA) { $env:NB_PILOT_DATA } else { "C:\Users\W10\nb-pilot\data" }
if ($data -match '^[Ee]:') { throw "Pilot data must not be on E:" }
New-Item -ItemType Directory -Force "$data\logs" | Out-Null
$pidFile = "$data\pilot.pid"
if (Test-Path $pidFile) {
    $old = Get-Content $pidFile
    if (Get-Process -Id $old -ErrorAction SilentlyContinue) { throw "Pilot already running (PID $old)" }
}
$env:PYTHONDONTWRITEBYTECODE = "1"
$p = Start-Process -FilePath $py -ArgumentList "-X", "utf8", "`"$root\pilot\run_pilot.py`"" `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput "$data\logs\stdout.log" -RedirectStandardError "$data\logs\stderr.log"
$p.Id | Set-Content $pidFile
"PAPER pilot started: PID $($p.Id), http://127.0.0.1:18765, data $data"
