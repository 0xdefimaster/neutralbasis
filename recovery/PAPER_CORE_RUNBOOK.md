# PAPER Pilot Runbook

The isolated PAPER pilot is the worktree engine with its own port, its own database on
C: and a network guard. It does not touch the live engine (PID 5988, port 8765,
`E:\NeutralBasis`). PAPER ONLY: no keys, no account, no orders.

| | Pilot | Live engine (untouched) |
|---|---|---|
| Code | `C:\Users\W10\nb-worktrees\audit-fixes` (branch `local/audit-fixes-2026-11-09`) | `E:\NeutralBasis` @ fceaf96 |
| API | `http://127.0.0.1:18765` | `http://127.0.0.1:8765` |
| Data | `C:\Users\W10\nb-pilot\data\paper.sqlite3` | `E:\NeutralBasis\data-five-rh` |
| Venues | 8 perp DEX venues; reference CEX scanning off | all |

## Start

```powershell
powershell -NoProfile -File C:\Users\W10\nb-worktrees\audit-fixes\pilot\Start-Pilot.ps1
```

First start only, after the API answers (the first cycle can hold the engine lock for
several minutes; the command waits up to 15 minutes):

```powershell
cd C:\Users\W10\nb-worktrees\audit-fixes
$env:PYTHONDONTWRITEBYTECODE="1"; E:\NeutralBasis\.venv\Scripts\python.exe -X utf8 -m pilot.pilot_api configure
```

This sets a workspace on every selectable perp DEX (USD 1000 PAPER collateral each),
both strategies, and the documented Lighter RH Premium taker fee (0.035 %). Zero fees
are never configured: the account type is unverified.

The configuration is stored in the pilot database. A restart keeps it and keeps the
open PAPER positions; nothing is reopened.

## Stop

```powershell
powershell -NoProfile -File C:\Users\W10\nb-worktrees\audit-fixes\pilot\Stop-Pilot.ps1
```

The stop script:

- stops only processes whose command line contains `run_pilot.py`;
- refuses PID 5988 outright.

## Health

```powershell
powershell -NoProfile -File C:\Users\W10\nb-worktrees\audit-fixes\pilot\Status-Pilot.ps1
```

It shows:

- the pilot process and its port;
- `/healthz` (must say `"mode": "PAPER"`);
- guard refusals;
- data size;
- the live engine's PID (only observed, never managed).

## Candidates, positions and PnL

```powershell
E:\NeutralBasis\.venv\Scripts\python.exe -X utf8 -m pilot.pilot_api report
```

The report is written to `C:\Users\W10\nb-pilot\data\reports\pilot-report-*.json`. It contains:

- `candidates`: unique pairs with score and research reasons (repeat scans are one pair);
- `assessed`: the last deep checks, each with `first_code` (the first decisive reason) and `other_codes`;
- `positions`, `closed`: PAPER legs, entry and exit prices, fees, funding, `net`;
- `venues`: what still blocks each venue per strategy;
- `why_no_trade`: the engine's own verdict and reason codes.

The same data is visible in the browser at `http://127.0.0.1:18765`.

## What to expect

The fresh database has to earn its evidence:

1. 30-day history (keeper backfill).
2. At least 3 published-rate → settled-payment comparisons per market and 3 decided markets per venue before the funding-sign gate opens. On hourly venues that is at least about 3–4 settled hours after the start (start 2026-11-10 02:35 TRT, so not before about 06:00).

Until then every pair is research-only, and that is correct behaviour, not a bug.

## When something goes wrong

| Symptom | Where to look | What to do |
|---|---|---|
| API unreachable | `logs\stderr.log`, `logs\faulthandler-*.log` | Stop, then Start. The database and positions survive |
| Guard refusals > 0 | `logs\network-guard.jsonl` (host, method, reason) | A code path tried a non-public call. Keep it refused; report it |
| `STOPPED_DISK_LIMIT` file | `logs\watchdog.jsonl` | New cycles are paused above 5 GB (`NB_PILOT_MAX_GB`). Archive or remove the pilot data folder only after review |
| A venue keeps failing | Report `venues`; `logs\stdout.log` | One venue's error does not stop the others |

## Proving it does not collide with the live engine

1. `Status-Pilot.ps1` shows PID 5988 with its unchanged start time (2026-11-09 18:27:10).
2. The pilot refuses to start with an E: data folder or with port 8765 (`run_pilot.py`).
3. The network guard refuses any connection to `127.0.0.1:8765`. Test: `test_pilot_guard`.
4. `git -C E:\NeutralBasis status` is clean and HEAD is `fceaf96`.
5. The pilot's public request rate is throttled per host (HL ≥ 1 s, others ≥ 0.25 s).
