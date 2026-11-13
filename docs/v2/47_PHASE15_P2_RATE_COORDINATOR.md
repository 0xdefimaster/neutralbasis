# 47 — Phase 15 P2: cross-process rate-limit coordinator

`v2/ratecoord.py`. The switch is `NB_V2_SHARED_BUDGET=1`. It is **default OFF**; while
it is off, `v2/venues/base.http_json` uses the existing per-process budget, unchanged.

## What it does

| feature | how |
|---|---|
| Shared budget | One token bucket per scope (`venue.endpoint.ip_scope`); its state is kept in `<bench>/v2-paper-data/control/ratecoord/<scope>.lock` on C:. D:/E: are refused. |
| Cross-process locking | OS advisory lock on byte 0 of that file: `msvcrt.locking` on Windows, `fcntl.flock` on POSIX. |
| Process crash / stale lock | The kernel releases a dead holder's lock. Tested: a subprocess killed inside the lock; the next acquisition takes under 2 s. |
| Lock timeout | Nobody waits forever: `LockTimeout`, which is counted. |
| State integrity | The record is magic + length + SHA-256 + JSON, at offset 16 of the open lock handle. A torn or corrupt record is reset CONSERVATIVELY (0 tokens) and counted. |
| Single-flight + shared cache | `SharedFileCache`: one fetch per key across processes. Tested with 3 processes: 1 fetch, 2 cache hits. It is bounded (oldest entries removed). |
| Retry-After | A 429 pauses the scope for every process (tested: 3 s honoured). |
| Jittered exponential backoff | `backoff_s`, capped; used when no Retry-After is given. |
| Circuit breaker | CLOSED → OPEN after 5 consecutive failures (429 / 5xx / timeout) → HALF_OPEN with one probe → CLOSED on success, OPEN for longer on failure. A crashed probe's lease is reaped, so HALF_OPEN never locks up. |
| Request priority | HIGH may use every token. LOW keeps a 20 % reserve for HIGH (order book and current funding come before metadata and backfill). |
| In-flight limit | 4 leases per scope. A lease of a dead pid, or one older than 120 s, is reaped. |
| Clock steps | A step back adds no tokens and re-anchors, so refilling resumes at once. A jump forward refills at most to capacity. |
| Telemetry / Doctor | `snapshot()` gives tokens, breaker, pause, in-flight and counters. Doctor `rate_budget`: RATE_LIMITED while the breaker is open or a pause is active, DEGRADED after a 429, lock timeout or state reset. |

## Real bugs found and fixed while testing

1. **A clock step back stalled the budget.** The future anchor was kept, so refilling
   waited until the clock caught up: one hour for a one-hour step. The anchor is now
   re-set to `now`.
2. **Windows `os.open` opened files in TEXT mode.** Record bytes 0x0A / 0x1A were
   rewritten, so every record failed its checksum and was reset. The checksum caught
   this; the fix is `O_BINARY`.
3. **The half-open probe flag was set before a token was granted.** If the probe never
   ran, or its process crashed, the scope stayed locked for good. The probe is now
   tied to its lease id and freed when that lease is reaped.
4. **`CircuitOpen` skipped the state save,** so refusals were not counted. The save is
   now in `finally`.

## Measurements (Windows 10, this machine)

| version | acquire + release | why |
|---|---|---|
| first version (a new tmp file per write) | 25.6 ms | each open of a modified file cost about 5–10 ms (most likely the OS file scan) |
| final (one handle per scope per process) | **0.42 ms** | no file is reopened |

Each scope's state is a few hundred bytes. No background thread exists, and no venue
request is made.

**Cross-process test:** 3 processes × 12 requests against 10 per 2 s. Every interval
stayed within capacity + rate × T. The run took more than 4.5 s; three uncoordinated
per-process budgets would have needed about 2.8 s.

## Platform

| platform | status |
|---|---|
| Windows | tested here: 21 tests, including real subprocesses |
| Linux | `fcntl.flock` and `os.kill` liveness: code reviewed, **NOT executed (UNVERIFIED)**. Run `test_v2_phase15_p2.py` on the Linux host before relying on it. |

## What it cannot solve

Other clients behind the same public IP spend the same venue quota invisibly: another
program, another machine on the NAT, or a VPN exit. The coordinator only bounds THIS
machine. A 429 therefore stays possible, and is handled (pause + breaker). The Doctor
states the external share as UNKNOWN.
