# Zero-Trade Root Cause

2026-11-10 · Measured from real data only. Full analysis:
`E:\NeutralBasis-Handoff\reports\zero-eligible-root-cause.md` (live candidates) and
`executable-economics-audit.md` (universe economics).

## Verdict

There are three causes, measured separately.

1. **Software and data-source defects** kept most real venues out of evaluation. In the live engine only Extended↔Hyperliquid pairs can reach a deep check. Fixed in the worktree (not deployed):
   - Lighter Core funding and fee (`9ca4467`);
   - Lighter RH parser (`e1ccf7e`);
   - RFQ quota (`cff3b7f`).
2. **Economics.** Every pair that did reach a deep check failed for measured economic or statistical reasons: RFQ spread, fees above funding, below the convergence tier. The offline walk-forward finds no supported opportunity in the 4-venue universe under the engine's fees and reserve.
3. **Evidence not yet accrued** in the fixed build (pilot, from 02:35). The funding-sign gate needs observed settlements. Its outcome is UNKNOWN until then.

No candidate was lost to a still-present software defect in the fixed build, as far as
measured. What the fixed build will find once evidence accrues is not yet known.

## Defects: before and after (real data)

| # | Defect | Before (live engine, fceaf96) | After (worktree) | Evidence |
|---|---|---|---|---|
| 1 | **Lighter Core**: CCXT gives no funding interval, timestamps or history, rate 8× the settled hourly rate, taker fee 0.0 | Universe 0/215 markets ("Funding aralığı kaynakta yok"); interval/sign evidence "0/0"; history "adaptörde yok"; fee priced at an unverified 0 %. HL/Lighter Core (92 assets) never evaluable | Native last settlement (projection) + native history; interval from settlements; Premium taker 0.028 %. Real data: selected 15/15 with a rate; universe 80 after 2 scans (all within ~6); pilot 65 fresh markets, interval gate passed | `9ca4467`, `test_lighter_core_funding` (21), `lighter-core-replay.json`, pilot report |
| 2 | **Lighter RH parser**: one missing optional field dropped the whole venue | market_discovery NOT_VERIFIED, venue blocked | Per-market isolation; pilot discovers 24 markets | `e1ccf7e`, `test_feed_parsing` |
| 3 | **RFQ quota**: unsubscribed RFQ pair used a deep-check slot | 596 / 980 slots (60.8 %) in 98 real cycles; the 60 s wait also pushed the pair down the stream ranking | Skipped without a slot or wait. Would reach 45 more pairs: 36 have another observed outcome (21 price-gap stats, 6 net negative, 6 desync, 2 history, 1 RFQ), 9 UNKNOWN | `cff3b7f`, `test_rfq_not_subscribed` (6), capture replay |
| 4 | RFQ freshness on quiet books | 41 desync + 17 RFQ-unavailable records | **Not changed** (owner ruling: a ping is not price evidence). Liveness is diagnostic only | `3102fa0` |
| 5 | Screen score counts the whole price gap as profit (design, not a bug) | Top of the list = structural Extended–HL basis; CHIP / HBAR priced at −7.87 / −39.36 $ | Shadow ranking (offline, funding-only) | `27c7fc6` |
| 6 | Stream allocation uses the previous cycle's ranking | RFQ pairs only; does not affect normal perps | Not changed (measure first, `d7d00ba` trace) | — |

## Outcome classes (live engine, 98 cycles, 728 deep-check records)

| Class | Records | Reason codes | Type |
|---|---|---|---|
| BOOK_UNAVAILABLE | 472 | RFQ_STREAM_NOT_SUBSCRIBED 455, RFQ_STREAM_UNAVAILABLE 17 | Software (quota, fixed) / data |
| ECONOMICALLY_UNPROFITABLE | 195 | PRICE_GAP_STATS_REJECT 145, NET_EXPECTED_PNL_NEGATIVE 50 | Economic / statistical |
| DATA_STALE | 41 | ORDERBOOK_DESYNC | Data (quiet RFQ books) |
| HISTORY_INSUFFICIENT | 19 | INSUFFICIENT_HISTORY | Data |
| LIQUIDITY_INSUFFICIENT | 4 | INSUFFICIENT_LIQUIDITY | Economic |
| (other) | 1 | RFQ_STREAM_WARMING | Data |
| VERIFIED_ELIGIBLE | **0** | — | — |

Candidates that never reached a deep check:

| Class | Funding | Convergence | Why |
|---|---|---|---|
| FUNDING_SIGN_UNVERIFIED (RISEx) | 44 RISEx pairs | (same) | Projection persistence 66 % |
| FUNDING_MODEL_UNVERIFIED (Paradex) | 67 Paradex pairs | (same) | Continuous accrual |
| UNKNOWN | 11 | 22 | Never assessed in the window; the rejection wait is not visible in the API |
| EXECUTION_MODEL_UNVERIFIED | — | — | Extended RFQ: no proof of the executable price at our size |

UNKNOWN is not counted as an economic rejection.

## Count by stage

| Stage | Live engine (window) | Fixed build |
|---|---|---|
| Deep checks | 728 records, 127 unique pairs | Pilot: 0 so far (evidence accrual) |
| Economic or statistical verdict reached | 199 records (195 unprofitable + 4 liquidity) | — |
| Eligible | 0 | 0 observed |
| Risk-approved | 0 | 0 |
| PAPER opened | 0 | 0 (MOCK E2E: 1, excluded) |

Before → after, measured:

- **F1:** +45 pairs reach a deep check. Of these, 36 have another observed outcome that is economic, statistical or data; 9 are UNKNOWN.
- **Lighter Core:** from 0 evaluable markets to 65 fresh in the pilot (215 listed), waiting for sign evidence.
- **Eligible:** 0 → 0 in all completed evidence.
