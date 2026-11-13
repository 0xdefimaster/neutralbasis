# Phase 13: Opportunity Intelligence v1 (first slices)

**Base:** `c7f7e51` (local; GitHub `v2/venue-policy` = `74dc319`) · **Date:** 2026-11-13 · **PAPER only**

Nothing was traded, signed or sent. No D:/E: write, no recorder, no live network test in this phase. Outputs are on C:, under 100 KB each. The 8 PAPER / 11 observation venues, the STRICT gate, the 2 s floor, the 60 s RFQ quote limit and the per-venue book limits are unchanged. Diff against `c7f7e51` is empty for `policy.py`, `network_policy.py`, `wide_paper.py`, `gates.py`, `hybrid_desk.py`, `observation.py`, `pilot/`, `extended_rfq.py`, `marketdata.py`, `rfq_paper.py` and `opportunity_log.py`.

## 0. Gap analysis: what already existed

| Phase 13 asks for | Already in the repo | Gap closed in this phase |
|---|---|---|
| Discovery separate from execution | `v2/funnel.py`: one trace per candidate. Stages DISCOVERED → … → PAPER_ELIGIBLE; outcomes NOT_EVALUATED / FAILED / UNPROFITABLE / RESEARCH_ONLY / ELIGIBLE; radar for not-evaluated pairs. | Phase 13 labels as a **view** (`v2/opportunity.py`), derived from funnel + estimated + ledger. **No second state machine**: `test_no_new_state_machine`. |
| Execution eligibility | `v2/gates.py` (STRICT, unchanged); `v2/estimated.py` (ESTIMATED); `v2/paper_engine` / `paper_ledger` (simulated positions) | Entry / exit cost exposed separately in the estimated results |
| Instrument identity | `v2/identity.Instrument`: exchange_id, market_id, symbol, contract_type, quote/settlement asset, underlying_id, multiplier; `v2/references.classify` for RWA reference equivalence | Used as is. `canonical_underlying_id` = `underlying_id`; `reference_index` = `references` registry. |
| Strategy-aware freshness | One limit per pair (`opportunity_log.freshness_limit`, floor 2 s) for ESTIMATED; strict book limits | `discovery_freshness()`: funding (rate in its own period, research price ≤ 120 s), price gap (synchronous legs within the volatility limit, else ASYNCHRONOUS). Strict unchanged. |
| Funding economics on real payment times | `funding_scenarios` (payment counts), `funding_accounting`, `paper_engine` (bookings) | `v2/funding_math.py`: discrete payments per leg, open at payment instant, band when phase unknown, common-horizon view for comparison only, minimum hold to break even, the 7 PnL class names |
| Variational adapter v2 | `variational.stats` (one answer for all listings), `rfq_paper` (tiers, age), 1 try on 429 | `v2/variational_poller.py`: adaptive 30–60 s, cache-repeat back-off, `retry_after`, single flight; five PnL levels, level 5 always NOT_VERIFIED |
| Full-market scanner and report | funnel / radar over the 05:12 snapshot (2,597 traces) | `tools/v2_opportunity_scan.py`: every asset on ≥ 2 venues, every ordered pair, 3 strategies; DISCOVERY / ECONOMIC / EXECUTION READINESS kept apart |
| NATGAS pilot | `tools/v2_pair_snapshot.py` | `tools/v2_natgas_history.py` over all 109 recorded observations |
| HL fast mode and clock | `tools/v2_hl_fast_probe.py` (requires a verified clock) | `--class-a-only` mode that withholds clock-dependent measures (proposal; not run) |

**Mapping of the Phase 13 labels to existing state** (`opportunity.DERIVED_FROM`):

| Label | Derived from |
|---|---|
| STRICT_PAPER_ELIGIBLE | funnel ELIGIBLE |
| ESTIMATED_PAPER_SIMULATED | a ledger position |
| BLOCKED_REFERENCE_MISMATCH | DIFFERENT_UNDERLYING / CONFLICT |
| DATA_STALE | discovery freshness STALE / ASYNCHRONOUS |
| BLOCKED_EXECUTION_MODEL | strict EXECUTION_MODEL_UNVERIFIED and no estimate |
| ESTIMATED_PAPER_CANDIDATE | ESTIMATED_ENTRY_SIGNAL |
| BLOCKED_ECONOMICS | UNPROFITABLE or an estimated cost/stress code |
| WATCHLIST | positive edge, blocked by evidence or scope |
| DISCOVERED | none of the above |

## 1. Slices and evidence

| Slice | Code | Tests (`test_v2_phase13.py`, `test_v2_phase12.py`) |
|---|---|---|
| 1. Labels and discovery freshness | `v2/opportunity.py` | labels from existing state only; funding accepts 40–60 s quotes for research and rejects 335 s; previous-period rate STALE; price gap ASYNCHRONOUS when legs are apart; a known stale leg beats an unknown one; strict limits unchanged |
| 2. Funding economics | `v2/funding_math.py` | 1 h vs 8 h legs counted on real payment times; sign inversion (long receives at a negative rate); close exactly at a payment → not paid; unknown phase → band; break-even on the discrete schedule; no payment counted twice |
| 3. Scanner and report | `tools/v2_opportunity_scan.py`, entry/exit cost in `v2/estimated.py` | OBSERVED 05:12 subset: reports nested (economic ≤ discovered, stress ≤ economic); 0 strict; every label valid; Variational rows with a 335 s quote never a candidate |
| 4. Variational v2 | `v2/variational_poller.py` | 30 s start, ×1.5 on a cached repeat, 429 `retry_after` honoured, single flight, cadence learned within [30, 60] s; five levels ordered, level 5 NOT_VERIFIED |
| 5. NATGAS history | `tools/v2_natgas_history.py` | run on OBSERVED data (below) |
| 6. Clock classes | `tools/v2_hl_fast_probe.py --class-a-only` | clock-dependent measures withheld |

**Full suite: 1076 run, 0 failed, 2 skipped** (before slice 6), then the targeted Phase 12/13 tests OK after slice 6.

## 2. Full-market scan (OBSERVED 05:12 snapshot, $1k)

Output: `C:\Users\W10\nb-benchmark\v2-paper-data\phase13-scan-0512b\` (`opportunity_report.json` 8 KB, `opportunities.jsonl.gz` 88 KB, 3,174 rows).

**Limits of this dataset:**
- It is a single sequential read of ~6 min with no per-answer time, so order-book leg ages are **UNKNOWN**, not 0. Variational quote ages are real (median ~335 s).
- It was taken at 05:12 TR, outside the US session, so equity perps are SESSION_CLOSED.

| Report | Result |
|---|---|
| Instruments matched (assets on ≥ 2 venues) | **399** instruments in **117** assets (5 venues with rows: TxFlow 102, Variational 102, Arcus 73, tradeXYZ 66, Lighter RH 58) |
| **DISCOVERY**: positive edge | **1,290** (price gap 524, funding 481, hybrid 285) |
| **ECONOMIC**: expected net > 0 after estimated costs | **2** (funding: NATGAS and CL, TxFlow→tradeXYZ) |
| ECONOMIC: stress > 0 | **0** |
| Research only (WATCHLIST + DATA_STALE) | **1,161** (DATA_STALE 643, all Variational with ~335 s quotes; WATCHLIST 518) |
| BLOCKED_ECONOMICS | 127 |
| **EXECUTION READINESS**: ESTIMATED PAPER candidates | **2** (NATGAS +4.99 $, stress −0.45; CL +2.37 $, stress −0.39) |
| EXECUTION READINESS: STRICT PAPER eligible | **0** |
| Venues in discovered rows | Variational 643, TxFlow 604, Arcus 470, tradeXYZ 444, Lighter RH 419 |
| Strict first rejection | EXECUTION_MODEL_UNVERIFIED 643, SESSION_CLOSED 443, RWA_REFERENCE_UNVERIFIED 57, SETTLEMENT_EVIDENCE_MISSING 50, NO_SIGNAL 41, PRICE_HISTORY_MISSING 25, NET_AFTER_COSTS 20 |
| Estimated first rejection | RWA_SESSION_CLOSED 837, PRICE_HISTORY_MISSING 153, STALE_QUOTE 85, HYBRID_CONVERGENCE_UNAVAILABLE 68, GAP_NOT_EXTREME 51, BREAKEVEN 50, STRESS_NEGATIVE 22, INSUFFICIENT_LIQUIDITY 18 |

**Risk-adjusted ranking** (ranking only, never a gate):
- Even the two candidates score negative (−0.10, −0.11), because their stress result is negative and their funding evidence is published only.
- The best price-gap rows (DRAM Arcus→tradeXYZ, SOXL TxFlow→tradeXYZ) are negative after costs.

## 3. NATGAS TxFlow ↔ tradeXYZ over 109 recorded observations

Data: 07:36 (aligned), 12:47–12:53 (3 bursts), 14:39–15:09 (90-cycle stream), 16:10–16:15 (15-cycle probe), all 2026-11-12. Output: `C:\Users\W10\nb-benchmark\v2-paper-data\natgas-history-20261113.json`.

**Fixed facts:**
- **Data:** time-aligned in 109/109 observations (skew ≤ 36 s limit).
- **Reference:** **UNKNOWN** (underlying_id missing on the TxFlow side; hedge_ok false).
- **Account funding:** ACCOUNT_PAYMENT_UNVERIFIED.
- **Funding times:** TxFlow publishes its next payment (hourly); tradeXYZ pays hourly on the hour (from its settled history times).

**Results per direction and size (ESTIMATED funding strategy):**

| Direction | Size | Expected net > 0 | Stress > 0 | Entry signals | Median entry / exit cost |
|---|---|---|---|---|---|
| long TxFlow / short tradeXYZ | $1k | 41/109 | 28/109 | 35 | ~$0.5 / ~$0.5 |
| | $2.5k | 41 | 28 | 35 | $2.54 / $2.55 |
| | $5k | 40 | 27 | 34 | $5.27 / $5.43 |
| | $10k | 37 | 24 | 31 (INSUFFICIENT_LIQUIDITY 14×) | — |
| long tradeXYZ / short TxFlow | $1k | 18 | **0** | 0 | $1.00 / $0.99 |
| | $10k | 12 | **0** | 0 | $12.10 / $11.70 |

**When it was positive** (long TxFlow / short tradeXYZ, $1k):

| Window | Observations | Result |
|---|---|---|
| 07:36 | 1 | +6.14 (stress −0.41) |
| 12:47–14:54 | 50 | negative |
| 14:55–14:58 | 12 | positive, stress negative |
| 15:00:14–15:09:20 | 28 | **expected up to +14.6 $, stress +4.6 $** |
| 16:11–16:15 | 15 | negative again (expected −5.2 $) |

- There is no data between 15:09 and 16:11, so the positive window lasted at least 9 min and at most ~1 h.
- **After a positive signal** (evaluation only; decisions used no later data):
  - inside the 20 s stream, the next observation stayed positive 38 of 41 times;
  - the median gap change at the next observation was −0.8 bp;
  - the observations ~1 h later had reversed (expected −5.2 $).
- **Lesson:** the 15:00 estimate assumed the published rates hold for 24 h, and they did not hold one hour. The economic advantage was real only for as long as the published rates lasted. Proving it needs the realized funding of the next payments, and that needs TxFlow settlement evidence, which does not exist publicly.

## 4. Clock (13.6)

- W32Time is Running; last sync 05:09:32; Source time.windows.com; Stratum 5.
- **Measured offset:**

  | Time | Offset (NTP − local) |
  |---|---|
  | 05:24 | +0.30 s |
  | 05:55 | +0.20 s |
  | 06:24 | +0.14 s |
  | 06:36 | +0.135 … +0.139 s |

  The service is slewing slowly. Today's offset was measured, not assumed.
- **Class A (clock-independent)** — reliable now:
  - venue-time differences;
  - push intervals;
  - REST/WS source-time comparison;
  - depth;
  - snapshot integrity.
- **Class B (clock-dependent)** — NOT reliable until the offset is ≤ ~0.05 s:
  - true data age;
  - end-to-end latency;
  - the 2 s freshness share.
- **Fast-mode test:** not started (the condition is not met).
- **Proposal:** `tools/v2_hl_fast_probe.py --owner-approved --class-a-only` runs the same ≤ 300 s comparison and withholds the class B measures. It needs the owner's go-ahead.
- No registry change was made.

## 5. The ten questions

1. **Instruments matched:** 399, in 117 assets listed on ≥ 2 venues (05:12 snapshot).
2. **Opportunities discovered:** 1,290 (price gap 524, funding 481, hybrid 285).
3. **Expected net positive:** 2.
4. **Stress positive:**
   - 0 in the 05:12 snapshot;
   - in the NATGAS history, 28/109 observations (all in the 15:00–15:09 window, long TxFlow / short tradeXYZ).
5. **Research only:** 1,161 (DATA_STALE 643 + WATCHLIST 518).
6. **ESTIMATED PAPER suitable:** 2 (NATGAS and CL, TxFlow→tradeXYZ, funding).
7. **STRICT PAPER suitable:** 0.
8. **Most opportunities seen on:** Variational (643, all DATA_STALE), TxFlow (604), Arcus (470), tradeXYZ (444), Lighter RH (419).
9. **Gates that blocked most:**
   - strict: EXECUTION_MODEL_UNVERIFIED (643), SESSION_CLOSED (443), RWA_REFERENCE_UNVERIFIED (57), SETTLEMENT_EVIDENCE_MISSING (50);
   - estimated: RWA_SESSION_CLOSED (837), PRICE_HISTORY_MISSING (153), STALE_QUOTE (85), HYBRID_CONVERGENCE_UNAVAILABLE (68), GAP_NOT_EXTREME (51), BREAKEVEN (50).
10. **Still missing to prove real profitability:**
    - (a) a full-market snapshot with per-answer source times, taken during the US session (equities);
    - (b) realized funding for TxFlow and Variational (no public settlement; account proof needs an authorized address);
    - (c) RWA reference specs (NATGAS, CL, metals, indices: UNKNOWN on TxFlow);
    - (d) Variational firm quotes (no trading API);
    - (e) price history for Variational and many venue pairs (convergence);
    - (f) continuous observation long enough to see whether published rates persist (a recorder needs safe storage and the owner's approval);
    - (g) a verified clock for class B timing.

## 6. Next smallest slices (not done)

1. **Funding persistence model:** use each venue's settled history (tradeXYZ, Lighter RH, Arcus, RISEx) to measure how long a published rate lasts. Feed it into `funding_scenarios` as the entry expectation instead of "24 h constant" (report only; no gate change without the owner).
2. **A US-session full-market read** with per-answer times (bounded, approved), so order-book ages are known and equities are open.
3. **A recurring bounded scanner run** (approved schedule, C: only, rotated) to build the opportunity history the ranking needs.
