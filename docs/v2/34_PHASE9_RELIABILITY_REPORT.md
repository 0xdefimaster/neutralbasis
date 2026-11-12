# Phase 9: reliability before continuous PAPER

**Base:** `108a643` · **Branch:** `v2/venue-policy` · **Date:** 2026-11-12 · **PAPER only**

No order, signature, key or wallet is involved. STRICT and ESTIMATED results stay apart. The 8-venue PAPER list, the 11-venue observation list and the risk settings (24 h break-even, 1.0 realization) are unchanged. `git diff 108a643` is empty for:
- `v2/policy.py`
- `v2/network_policy.py`
- `wide_paper.py`
- `v2/gates.py`
- `v2/hybrid_desk.py`
- `v2/observation.py`
- `pilot/`
- `extended_rfq.py`

Tests: **1020 OK (2 skipped)**.

## Summary

| Item | Result |
|---|---|
| Reliable closed virtual trades | **0** in every replay (continuous, gapped, Phase 8 set, stress) |
| Disk safety | **NOT safe.** E: and D: are ONE failing disk; E: writes are now refused by default |
| Open risk at the end of the replays | 1–2 hedged positions (0 unhedged units). Crash scenario: 5.2 h unhedged with no data to act on; visible throughout. |
| Funding data coverage | tradeXYZ and Hyperliquid: settled public hourly history. TxFlow and Variational: published rate only. Account payments: none (no account). |
| −$0.42 trade | All 4 fills, 14 payments and the net recompute EXACTLY from the raw answers. The data gaps leave the net anywhere in **[−3.14, −0.42] $**. |
| 24/7 pilot | **Blocked** (list at the end) |

## 1. Disk (read-only inspection)

- **Disks:** `Win32_DiskDrive`: Disk 0 = TEAM T253LE240G SSD (C:); **Disk 1 = TOSHIBA MQ02ABD100H HDD, partitions D: and E:**.
- **Health:** `Get-PhysicalDisk` reports "Healthy". This is **not evidence**: SMART counters (`MSStorageDriver_FailurePredictStatus`, `Get-StorageReliabilityCounter`) are not readable without administrator rights, so the status is UNKNOWN. To verify, run them in an elevated shell, or use CrystalDiskInfo / `smartctl -a`.
- **System log, last 14 days:**

  | Event | Count | Note |
  |---|---|---|
  | `disk` 7 "bad block on \Device\Harddisk1\DR1" | **34,246** | Every day since 3 Nov; 13,842 on 10 Nov; **6,151 today**, 839 of them between 14:00 and 16:00 while the Phase 8 stream was being written; still occurring every few seconds at 16:01 |
  | `Ntfs` 55 (file-system corruption) | 5,158 | All on **D:** |
  | `Microsoft-Windows-Ntfs` 98 | 15 | — |
  | `storahci` 129 (controller reset) | 5 | — |
  | `Kernel-Power` 41 + `EventLog` 6008 (unexpected shutdowns) | 4 | 8 Nov 03:17; 10 Nov 14:20, 15:45, 17:20 |

- **Backup evidence:** `C:\Users\W10\nb-backup\2026-11-10` holds git bundles and a settings file only.
  - `E:\NeutralBasis\data-five-rh\paper.sqlite3` (26.5 GB) and its WAL (3.1 GB) have **no backup**.
  - C: has 13.5 GB free, so they cannot be copied there.
  - They were not touched.
- **Phase 8 data rescued:** 18 files, 18.1 MB, copied read-only to `C:\Users\W10\nb-benchmark\v2-phase8-rescue-2026-11-12`.
  - Every hash chain is OK; the copies are sha256-identical.
  - Manifest: `rescue_manifest.json`.
- **Code (`v2/storage.py`):**
  - E: writes are refused unless `NB_V2_E_WRITE_ACK=1`.
  - The default root is `C:\Users\W10\nb-benchmark\v2-paper-data`, holding small JSON-lines files.
  - `E:\NeutralBasis` and D: are always refused.
  - All Phase 9 outputs (about 30 MB) are on C:.
  - No new writes were made to E:.

## 2. Accounting layers (`v2/paper_ledger.py`)

Phase 8's `observed_part` mixed simulated fills with observed data, so it was misleading. It is replaced by:

| Line | What it rests on |
|---|---|
| `simulated_price_pnl` | SIMULATED fills (IOC on an observed book, or an indicative RFQ). Reproducible from saved public data, but never an exchange fill. |
| `simulated_fees` | Published fee rate × simulated fills |
| funding `SETTLED_PUBLIC_RATE` | The venue's settled public rate. The amount uses the observed mark, because the venue's price basis is not in the history: the rate is data, the amount is an estimate. |
| funding `ACCOUNT_PAYMENT_CONFIRMED` | A real account payment. Always 0 in PAPER, and kept as its own line. |
| funding `PUBLISHED_RATE_ESTIMATE` | A published rate in the same period, not settled |
| funding `ASSUMED_CARRIED_FORWARD` | No observation in the period. An assumption. |
| `mark_to_market` | Open quantity at the last OBSERVED mid |
| layers | `public_data_reproducible` = price − fees + settled-rate funding; `assumption_dependent` = published + carried; `account_verified` = 0 |

Phase 8 ledgers are read under the new names (`ALIASES`).

## 3. The −$0.42 trade, line by line

`tools/v2_verify_trade.py` recomputes everything from the raw answers, independently of the ledger. Output: `C:\Users\W10\nb-benchmark\v2-paper-data\verify-natgas-txflow-tradexyz.json`. Test: `VerifiedTradeTests`.

**Fills** (274.0101 units; every line MATCH):

| Time | Leg | Action | Price | Book mid | Slippage | Fee (rate) |
|---|---|---|---|---|---|---|
| 07:36:28 | TxFlow long | buy | 3.651 | 3.6495 | 4.11 bp | 0.4502 (4.5 bp) |
| 07:36:28 | tradeXYZ short | sell | 3.66065 | 3.66105 | 1.09 bp | 0.0903 (0.9 bp) |
| 14:39:31 | TxFlow long | sell | 3.625 | 3.626 | 2.76 bp | 0.4470 |
| 14:39:31 | tradeXYZ short | buy | 3.6418 | 3.64145 | 0.96 bp | 0.0898 |

- Book source times: 07:36:22–07:36:27 and 14:39:26–14:39:30.
- **Price PnL:** TxFlow −7.124, tradeXYZ +5.166, total **−1.959**.
- **Fees:** **−1.077**.

**Funding** (14 payments, hourly 08:00–14:00 TR; every line MATCH):

| Leg | Payments | Evidence | Amount |
|---|---|---|---|
| tradeXYZ short | 08, 09, 10, 11, 12 (rates 4.56e-4 … 5.76e-4/h, read from the 12:47 history) and 13, 14 (read at 14:39) | SETTLED_PUBLIC_RATE × marks 3.6608 / 3.6508 | **+3.816** |
| TxFlow long | 08 (rate −1.58e-5, seen 07:36) and 13 (6.42e-4, seen 12:52) | PUBLISHED_RATE_ESTIMATE | **−0.624** |
| TxFlow long | 09, 10, 11, 12 (07:36 rate) and 14 (12:52 rate) | ASSUMED_CARRIED_FORWARD | **−0.577** |

**Net:** −1.959 − 1.077 + 3.816 − 0.624 − 0.577 = **−0.4214 $** (ledger: −0.4214, MATCH).

**What the 5.2 h gap (07:36→12:47) and the 1.8 h gap (12:53→14:39) leave unknown.** Only rates and prices actually observed at the gap ends are used; nothing is interpolated.

| Unknown | Band from observed endpoints |
|---|---|
| TxFlow rate at 09–12 (seen −1.6e-5 before, +6.38e-4 after) | carried funding −0.58 … −3.29 $, so **net −0.42 … −3.14 $** |
| tradeXYZ valuation price (oracle, not in the history) | settled amount +3.796 … +3.816 $ |
| Exit time | The reversal was first SEEN at 12:47; it happened at an unknown time inside the gap. The rule needs 30 min of reversed carry, and the next observation after 12:53 was 14:39. The observed "close now" price result was −0.62 / −0.70 / −0.66 at 12:47 / 12:50 / 12:53 and **−1.96 at 14:39**: the 1.8 h gap also cost a later exit. |

## 4. Crash and unhedged risk (`v2/paper_engine.py`)

The 5.2 h came from the data, not from the engine: the replay had no observation between 07:36 and 12:47, so a restarted process could not act sooner.

- **heartbeat:** one event per step.
- **restart:** the first step after a restart reports the downtime and each position's unhedged seconds.
- **priority:** largest unhedged exposure first.
- **reconcile:** legs are recomputed from fills; a mismatch → RECOVERY_PENDING.
- **recovery timeout (60 s):** after that, the missing hedge is no longer completed late. The open leg is unwound first, which reduces the risk.
- **no data:** RECOVERY_PENDING, with `unhedged_units`, `unhedged_seconds` and `unhedged_since` reported every step.

| Crash after the first leg (Phase 8 data) | Result | Unhedged |
|---|---|---|
| Phase 8 rule (complete the hedge 5.2 h later) | −6.75 $ | 18,634 s |
| Phase 9 rule (timeout 60 s → unwind first) | **−4.74 $** | 18,634 s (no observation existed earlier) |

Tests:
- `test_long_downtime_reduces_risk_instead_of_hedging_late`
- `test_restart_without_data_keeps_the_risk_visible`
- `test_reconcile_mismatch_is_never_trusted`
- `test_heartbeat_every_step`
- `test_crash_after_first_leg_and_restart` (a restart within 60 s still completes the hedge)

## 5. Variational requests

- **One request covers every market:** `/metadata/v2/stats` (the stream keeps the two followed listings).
- **Quote refresh** (Phase 8 stream, 76 reads):
  - NATGAS: 45 distinct `updated_at`, median refresh 39 s (10–111 s);
  - INIT: 42 distinct, median 43 s (10–85 s);
  - so about 40% of 20 s polls return the same quote.
  - Answers with an unchanged quote are now marked `quotes_unchanged`.
- **429 (global limit, shared with other clients):**

  | Mode | 429 rate | Time lost |
  |---|---|---|
  | 20 s polling, 3 tries | 14/90 | ~5 s each |
  | 15 s polling, 1 try | 8/15 | — |

  Variational now gets **1 try** (no retry burst), and the venue pause applies. Polling faster than ~20 s brings more 429s for no new quotes. Recommendation: 20–30 s.
- **WebSocket:** proposal and security analysis in doc 35. **Not enabled.**
  - Key finding: the guard cannot see WS frames, so a frame-level allowlist is required first.
  - Variational WS: UNKNOWN.

## 6. INIT 2 s alignment

The INIT freshness limit is the 2 s floor (hourly volatility). It was NOT raised.

| Median skew sources (s) | Sequential (Phase 8, 76 reads) | Parallel (Phase 9 probe, 7 reads) |
|---|---|---|
| Receive spread of a cycle | 6.0 | 3.5 |
| HL ctx vs HL book | 3.6 | 1.8 |
| Variational receive vs HL book receive | 6.3 | 1.7 |
| **Variational quote age** | 20.4 (min 1.4) | 10.6 (min 2.7) |
| Pair skew (min) | 6.5 | 4.1 |
| Reads within 2 s | **0/76** | **0/7** |

- Parallel collection (`--parallel`, tested) removes most of the request-order skew.
- The Variational **indicative quote age** remains: it refreshes about every 40 s. The only fresher price is a firm RFQ, which needs the trading API and is outside PAPER rules.
- **INIT stays blocked.** It cannot be fixed with public REST data. A WS for the Hyperliquid legs (doc 35) would not change the Variational side.

## 7. Gapped vs continuous replay

Output: `C:\Users\W10\nb-benchmark\v2-paper-data\phase9-replays`. Every run is deterministic (same chain head twice).

| Run (NATGAS, ESTIMATED funding) | Obs | Signals / positions | Closed (reliable) | Funding payments: settled / published / carried | public_data_reproducible / assumption_dependent |
|---|---|---|---|---|---|
| continuous 20 s (stream, 30 min) | 90 | 2 / 2 | 0 (0) | 1 / 2 / **0** | −0.30 / 0.00 |
| gapped 5 min (same stream) | 6 | 2 / 2 (different entries, 15:00) | 0 (0) | 0 / 0 / 0 | −3.15 / 0 |
| gapped 15 min | 2 | 0 / 0 | 0 | — | — |
| Phase 8 set (5.2 h + 1.8 h gaps) | 94 | 2 / 2 | 1 (0) | 7 / 6 / **10** | +0.33 / −1.35 |

- Convergence and hybrid: 0 signals in every run.
  - PRICE_HISTORY_MISSING: Variational has no history.
  - GAP_NOT_EXTREME: TxFlow/tradeXYZ.
  - HYBRID_CONVERGENCE_UNAVAILABLE.
- Gaps change WHICH entries are seen (different times, different sizes) and turn funding into carried-forward assumptions. With continuous 20 s data, none of the 3 payments was carried forward. Nothing is generated from a single snapshot: a missing period stays an assumption, labelled.
- **Reliable closed trades stay at 0** because the only continuous window is 30 min. The funding exit needs 30 min of reversed carry after an entry, so a reliable close needs a continuous run of hours, i.e. a recorder.

## 8. Negative worst case with an entry signal

- **Why it happens:** the ESTIMATED entry uses the strict thresholds unchanged:
  - net ≥ 0.5,
  - break-even ≤ 24 h,
  - `combined_moderate` ≥ 0,
  - `rfq_moderate` ≥ 0,
  - reference stress ≥ 0.

  The harsher scenarios lower `net_min` but do not gate: exit spread ×3, +5 bp per fill, funding −50%, half-filled second leg, firm gap 25 bp, last look 20%, and the funding sign flips.
- **Example:** 07:36 TxFlow→tradeXYZ.
  - Gating: combined_moderate +2.02, reference +2.98.
  - `net_min` −0.41 comes from `second_leg_half_filled`.
  - The informational worst case is a funding sign flip at entry: −16.63.
- **Alternative gate:** `WORST_CASE_NONNEGATIVE` (opt-in; test `test_alternative_worst_case_gate_is_opt_in`). The default is unchanged.
  - On the Phase 8 set it would block **both** Phase 8 entries.
  - It enters instead at 14:55 (TxFlow→Variational, $1k, net_min +0.26) and at 15:00 (TxFlow→tradeXYZ, **$5k**, net 56.37, net_min +10.43).
  - Every evaluation now records `alt_gate_blocks` and the gating vs informational stresses.
- **Owner decision:** keep the default, or adopt the worst-case gate.

## Remaining blockers for a 24/7 PAPER pilot

1. **Disk.** E:/D: is failing (bad blocks every day, 6,151 today). The 26.5 GB engine DB has no backup and C: cannot hold it.
   - Needed: a new disk or the planned server move.
   - A 24/7 recorder needs about 26 MB per hour (the Phase 8 stream size), about 630 MB per day.
2. **Continuous data.** Reliable closes need hours of uninterrupted observation. That means a recorder, which needs owner approval and a safe disk.
3. **TxFlow settled funding.** There is no public history. Every TxFlow payment is published or carried. An authorized `userFundings` address is needed, or the TxFlow leg stays assumption-dependent.
4. **Variational.** Indicative quotes only (refresh about every 40 s, cached, 429 under the global limit); firm quote and last look are UNKNOWN. INIT stays blocked.
5. **WebSocket.** It needs the frame allowlist and owner approval (doc 35).
6. **Owner decisions:**
   - E: write acknowledgement (or a new disk);
   - the alternative entry gate;
   - the WebSocket;
   - a recorder.
