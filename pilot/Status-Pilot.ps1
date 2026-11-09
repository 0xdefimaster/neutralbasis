# Pilot health: process, API, mode, guard refusals, disk; and that the live engine is untouched.
$data = if ($env:NB_PILOT_DATA) { $env:NB_PILOT_DATA } else { "C:\Users\W10\nb-pilot\data" }
if (Test-Path "$data\pilot.json") {
    $info = Get-Content "$data\pilot.json" -Raw | ConvertFrom-Json
    $p = Get-Process -Id $info.pid -ErrorAction SilentlyContinue
    if ($p) { "Pilot engine PID $($info.pid) alive since $($p.StartTime) (port $($info.port), data $($info.data))" } else { "Pilot engine PID $($info.pid) NOT running" }
} else { "Pilot not started (no pilot.json)" }
try { "Pilot healthz: " + (Invoke-WebRequest -UseBasicParsing -TimeoutSec 15 http://127.0.0.1:18765/healthz).Content } catch { "Pilot healthz: unreachable" }
try { "Pilot identity: " + ((Invoke-RestMethod -TimeoutSec 15 http://127.0.0.1:18765/api/identity) | ConvertTo-Json -Compress) } catch { "Pilot identity: unreachable" }
$guard = "$data\logs\network-guard.jsonl"
if (Test-Path $guard) { "Guard refusals: " + (Get-Content $guard | Measure-Object -Line).Lines } else { "Guard refusals: 0" }
$size = (Get-ChildItem $data -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
"Pilot data size: {0:N1} MB" -f ($size / 1MB)
if (Test-Path "$data\STOPPED_DISK_LIMIT") { "WARNING: disk limit reached; cycles paused" }
$live = Get-Process -Id 5988 -ErrorAction SilentlyContinue
if ($live) { "Live engine PID 5988 alive since $($live.StartTime) (not managed by the pilot)" } else { "Live engine PID 5988 not running" }
