# 54 — Live-observation readiness: clock, disks, pilot preflight, backup plan (2026-11-14)

Every check below was **read-only**. Nothing was started, stopped, configured, deleted
or written outside the repository and its C: data folder.

## 1. Windows Time: why it is stopped

| fact (read-only) | value |
|---|---|
| service W32Time | **Stopped**, start type **Manual (DEMAND_START)** |
| start trigger | only "DOMAIN JOINED". Windows 10 Home is never domain-joined, so the service does not start at boot |
| last boot | 2026-11-14 01:33:58: the service has not run since |
| earlier sync | events 35 / 37 on 11-11, 11-12 and 11-13 (it ran after a manual start; last valid sample 13.11 23:21:48) |
| scheduled task `SynchronizeTime` | "On demand only"; last run 13.11 10:07 (result 1056 = service already running) |
| configuration | Type NTP, server `time.windows.com,0x9`, SpecialPollInterval 32768 s (about 9.1 h) |

**Cause.** The service is demand-start with a domain-only trigger, so every reboot
leaves it stopped. Nothing is broken.

**How to fix it safely** (your decision; run in an **administrator** PowerShell):

1. **One-off (until the next reboot):**
   `Start-Service W32Time; w32tm /resync /force`
2. **Persistent (recommended):**
   `Set-Service W32Time -StartupType Automatic; Start-Service W32Time; w32tm /resync /force`
   (or `sc.exe config W32Time start= delayed-auto`)
3. **Verify:** `w32tm /query /status` must show "Last Successful Sync Time". Measure the
   offset with `w32tm /stripchart /computer:time.windows.com /dataonly /samples:5`.
   The Doctor's clock record then reads "synced at …" instead of confidence NONE.
4. **Optional, for observation-grade timing:**
   - add a second server:
     `w32tm /config /manualpeerlist:"time.windows.com,0x9 time.cloudflare.com,0x9" /syncfromflags:manual /update`;
   - poll more often: SpecialPollInterval 1024 s under
     `HKLM\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient`, then
     `w32tm /config /update`.
   - Windows Time typically holds tens of ms. The 0.05 s latency gate (docs/v2/42 §10)
     needs repeated stripchart samples as evidence.

**Effect on NeutralBasis:**
- A resync can STEP the clock. Intervals use `time.monotonic`, the P2 coordinator
  re-anchors on a step, and the pilot judges boundaries on venue times.
- Wall-clock ages measured across a step are flagged by the contract checks.

## 2. C: usage (read-only scan, junctions not followed)

| total | used | free |
|---|---|---|
| 222.8 GB | 203.4 GB | 19.4 GB |

187.4 GB was scanned. About 16 GB sits in areas unreadable without admin rights:
System Volume Information / restore points, `$Recycle.Bin` (2 items), 347 denied
folders.

| largest | GB | what |
|---|---|---|
| `Users\W10\AppData` | 61.2 | Local 43.3, Roaming 17.5 (below) |
| `Windows` | 26.6 | WinSxS 9.6, System32 8.6, MEMORY.DMP 1.2, Installer 1.3 |
| `Program Files` / `(x86)` | 15.9 / 9.0 | Docker 4.1, JetBrains 3.4, Android 2.6, WSL 1.1, Microsoft 4.6 |
| `C:\imx\SpaceNationOnline` | 15.6 | a game install |
| `Users\W10\.gradle` | 10.1 | Gradle caches |
| `Users\W10\nb-pilot-loose` / `nb-pilot` | 5.3 / 5.2 | earlier pilot data (the closes export replaces the 5.3 GB DB for the tools) |
| `C:\src` | 5.2 | flutter 2.6, projeler 2.6 |
| `Users\W10\.git` | 4.1 | a git repository at the home folder root (check that it is intended) |
| `My project (1–4)` | 7.3 | Unity projects (their `Library` folders regenerate) |
| `hiberfil.sys` | 3.2 | hibernation file |

**Safe-to-clean CANDIDATES** (regenerated on demand; nothing was deleted; your
decision):

| item | GB | how |
|---|---|---|
| `AppData\Local\npm-cache` | 6.1 | `npm cache clean --force` |
| `AppData\Roaming\UnityHub\downloads` | 6.8 | installer downloads (after the editors are installed) |
| `.gradle` caches | up to 10.1 | `.gradle\caches` (the next Android build downloads them again) |
| `AppData\Local\Unity\cache` | 1.9 | Unity asset cache |
| browser caches (Chrome / Brave / Opera GX / Firefox) | about 2–3 | browser settings → clear cache (NOT the profiles) |
| `AppData\Local\Temp` | 1.0 | Disk Cleanup ("Temporary files") |
| `Code\CachedExtensionVSIXs` | 0.6 | VS Code extension download cache |
| `Pub\Cache` | 0.9 | Flutter / Dart packages (re-downloaded) |
| `Windows\MEMORY.DMP` | 1.2 | crash dump; keep it if the crashes are still being investigated (likely the failing E: disk) |
| `hiberfil.sys` | 3.2 | `powercfg /h off` (a system setting: disables hibernation and Fast Startup) |
| Windows component store | ? | `Dism /Online /Cleanup-Image /StartComponentCleanup` (admin) |

Total roughly **29–35 GB** without touching projects, games or data.

**Also found: the page file is on the failing disk.** `E:\pagefile.sys`, 11 GB
allocated, 905 MB in use. Windows pages memory to a disk that reports thousands of
bad blocks a day, a plausible contributor to the unclean shutdowns. Moving it to C:
(system-managed, about 4–8 GB, after freeing space) is a system setting and your
decision. Steps: System Properties → Advanced → Performance → Virtual memory.

## 3. Single-boundary pilot: runnable preflight (`tools/v2_boundary_pilot.py`)

**Preflight** (offline, sends nothing):

    python tools/v2_boundary_pilot.py --preflight --out C:\Users\W10\nb-benchmark\v2-paper-data\pilot-<date>

Result today:
- PASS: plan (52 requests), output folder, disk 19.4 GB, TxFlow NATGAS index 119
  (from the saved stream), time basis = answer;
- WARN: shared budget off, clock UNVERIFIED (service stopped);
- BLOCK: timing (it must start 150–1800 s before a whole hour).

**Run** (only with your approval, 2.5–30 min before a whole hour):

    $env:NB_V2_PILOT_APPROVED = "1"; $env:NB_OBSERVATION_V3 = "1"; $env:NB_V2_SHARED_BUDGET = "1"
    Remove-Item Env:NB_V2 -ErrorAction SilentlyContinue
    python tools/v2_boundary_pilot.py --run --owner-approved --out C:\Users\W10\nb-benchmark\v2-paper-data\pilot-<date>
    python tools/v2_boundary_pilot.py --analyze C:\Users\W10\nb-benchmark\v2-paper-data\pilot-<date>

| limit | value |
|---|---|
| requests | 52 (25 TxFlow ctx + 25 tradeXYZ ctx + 2 tradeXYZ funding history); a 53rd is refused |
| duration | from start until B+180 s at most; the sampling itself is B−140 s … B+150 s |
| storage | ≤ 10 MB in one C: folder; never appends; D:/E: refused; stops if C: free < 5 GB |
| failures | a venue stops after 3 consecutive failures; any egress-guard refusal aborts everything |
| egress | `observation_v3` guard (allowlisted hosts and paths only); public info reads only; no account, key or signature |
| success | ≥ 90 % of samples per venue; verdicts: TxFlow CONTINUES / JUMPS at B (venue blockTime), tradeXYZ CONVERGES / DIFFERS vs its settlement |

**Not proven by the pilot:**
- the TxFlow paid rate;
- anything general (n = 2 boundaries, one market);
- latency (until the clock is synced).

## 4. E: database: backup and migration to a new SSD (NOT executed)

**State (read-only):**
- E: and D: are two partitions of ONE TOSHIBA MQ02ABD100H 1 TB HDD.
- Disk events 7 / 51 / 153 over the last 6 days: 13,845 / 1,808 / 2,134 / 9,336 /
  8,918 / 1,133 (today so far); 37,164 bad-block events in 7 days.
- `Get-PhysicalDisk` says "Healthy". **Not evidence**; SMART is unreadable without
  admin rights.
- The engine DB `paper.sqlite3` is 26.5 GB, its WAL 3.1 GB (committed data not yet
  checkpointed). **No backup exists.**
- C: has 19.4 GB free, so the DB does not fit there safely.
- Other disk: TEAM T253LE240G 240 GB SSD (C:).

**Rules until the copy is verified:**
- no write to E:;
- no `chkdsk /r` or defragmentation on E: (it stresses the failing sectors);
- no checkpoint on E:;
- never open the DB on E: writable;
- never `immutable=1` (it would IGNORE the WAL, i.e. drop committed data).

**Hardware needed (yours to buy):**
- a new SSD of **≥ 1 TB** as the data disk (NVMe if the board has a free M.2 slot,
  else SATA);
- a separate **≥ 1 TB external drive** for backups.

**Procedure** (each step needs your go):
1. **Confirm nothing has the DB open.** All engines have been STOPPED since
   2026-11-12. Check the -wal / -shm modification times and the process list.
2. **Image first, read once.** Best practice for a failing disk: a sector image of
   the E: partition onto the new disk with GNU ddrescue from a Linux live USB
   (`ddrescue -n` first pass, then `-r3` retries; a map file records unreadable
   areas). Work only on the image afterwards.
   - Windows-only alternative: `robocopy E:\<db folder> F:\nb-rescue paper.sqlite3 paper.sqlite3-wal paper.sqlite3-shm /J /R:3 /W:5 /LOG:F:\nb-rescue\copy.log`.
   - Copy DB, WAL and SHM TOGETHER, in one pass.
3. **Verify the copy (on the new disk only):**
   - SHA-256 of each copied file;
   - open the COPY writable, so SQLite applies the WAL there;
   - `PRAGMA integrity_check`;
   - row counts per table.
   - If pages are damaged: sqlite3 `.recover` into a fresh DB, and list what was
     lost.
4. **Priority order:**
   1. DB + WAL + SHM (29.6 GB);
   2. `E:\NeutralBasis` worktree (git objects; all branches are on GitHub and in the
      2026-11-10 bundle on C:);
   3. the venv is NOT copied (recreate it).
5. **Migrate:**
   - point every data path at the new disk with the environment variables
     (`NB_V2_BENCH_DIR`, `NB_V2_PAPER_DATA_DIR`, ...);
   - move the page file off E:;
   - retire the TOSHIBA disk after the manufacturer's diagnostic, or replace it.
6. **Back up from then on:**
   - SQLite online backup API from the new disk to the external drive (weekly);
   - `tools/v2_pack_data.py` extended to the Phase 15 outputs and new recordings;
   - a restore drill each time (as done today: pack verified, 63 data tests OK from
     the restored copy).

## 5. Fill guard: hard but valid moves, stuck RECOVERY (evaluated and fixed)

| scenario | before | now |
|---|---|---|
| real persistent divergence between the legs (> 5 %) | every exit refused **forever**; loss never realized; desk marks frozen | after 6 h of refused exits the cross-leg band widens to 50 %; the close is booked (realized loss), flagged `closed_under_relaxed_guard` / "FILLED UNDER RELAXED GUARD" |
| chronically wide book (100–500 bp) | refused forever | closes after 6 h (spread / walk ≤ 500 bp) |
| extreme spread (> 500 bp), scale-sized error (> 50 %, e.g. x2, x10) | refused | **still refused** (a data error, never a price) |
| stale / crossed / locked / off-grid book | refused | **never relaxed** |
| missing reference (other leg's book missing) | own-entry bound 25 %; the desk closes nothing while a leg's venue gives no book | unchanged: a venue without a book cannot be traded in reality either; the position stays visible as `STUCK_IN_RECOVERY` |
| large legitimate move on both legs | closes (cross-leg reference) | unchanged |
| entries | strict | **never relaxed** |

The ESTIMATED engine keeps the stuck clock in memory, so a restart restarts the 6 h:
the delay is conservative. The desk persists it with the position. Tests are in
`test_v2_fill_integrity.StuckPositionTests`.

## 6. Process-global `marketdata.use_basis()`

`use_basis()` swaps a process-global value. Its only production caller is the offline,
single-threaded `tools/v2_identity_snapshot.py`. Every engine thread (runtime, desk,
server) only READS `TIME_BASIS`, and separate processes have separate values.
**No race exists today; no production code was changed.** A test
(`ProcessGlobalBasisTests`) fails if any other module starts switching it.
