# Stop the isolated PAPER pilot only. Refuses any process that is not run_pilot.py.
# The venv python.exe is a launcher: the engine runs in its child, whose PID the pilot
# writes to pilot.json. Both are stopped, each after checking its command line.
$ErrorActionPreference = "Stop"
$data = if ($env:NB_PILOT_DATA) { $env:NB_PILOT_DATA } else { "C:\Users\W10\nb-pilot\data" }
$ids = @()
if (Test-Path "$data\pilot.json") { $ids += [int]((Get-Content "$data\pilot.json" -Raw | ConvertFrom-Json).pid) }
if (Test-Path "$data\pilot.pid") { $ids += [int](Get-Content "$data\pilot.pid") }
if (-not $ids) { "No pilot PID recorded."; exit 0 }
foreach ($id in ($ids | Select-Object -Unique)) {
    if ($id -eq 5988) { throw "Refusing: PID 5988 is the live engine." }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
    if (-not $proc) { "PID $id is not running."; continue }
    if ($proc.CommandLine -notmatch 'run_pilot\.py') { throw "Refusing: PID $id is not the pilot ($($proc.CommandLine))" }
    Stop-Process -Id $id
    "Stopped pilot process $id"
}
Remove-Item "$data\pilot.pid" -ErrorAction SilentlyContinue
"PAPER pilot stopped. Its database stays in $data."
