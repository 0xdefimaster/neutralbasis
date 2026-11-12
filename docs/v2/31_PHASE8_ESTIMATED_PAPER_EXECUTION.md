# Phase 8: ESTIMATED PAPER, from entry signal to virtual close

**Base:** `a18c732` · **Branch:** `v2/venue-policy` · **Date:** 2026-11-12

**PAPER only.** No order, signature, key, account or wallet connection is involved. Every fill is a SIMULATION on observed public data and is labelled `verified=false`.

**Unchanged:**
- the strict gate, the strict desk, the 8-venue PAPER list and the 11-venue observation list;
- `wide_max_breakeven_hours=24` and `wide_funding_realization=1.0`;
- `git diff a18c732` is empty for `v2/policy.py`, `v2/network_policy.py`, `wide_paper.py`, `v2/gates.py`, `v2/hybrid_desk.py`, `v2/observation.py` and `pilot/`.

**Data location (owner decision, 2026-11-12):** C: is full, so data goes to `E:\nb-v2-phase8`, a new dedicated folder. Nothing is written under `E:\NeutralBasis` or on D:. Files are hash-chained JSONL only; no database is created.

## Summary

- **Strict gate:** still passes nothing (0).
- **NATGAS:**
  - **TxFlow→tradeXYZ (funding):** the full life cycle was booked. Opened at 07:36; the carry reversed (FUNDING_REVERSED); closed at 14:39.
    - Net: −$0.42. Price −$1.96, fees −$1.08, funding +$2.61.
    - Of the funding: +$3.82 is from tradeXYZ's settled public rates; −$1.20 is TxFlow's published rate.
    - The trade is marked **UNRELIABLE**: TxFlow funding was carried forward for 5 hours with no observation.
  - **TxFlow→Variational:** opened at 07:36 and still open at the end of the data.
- **INIT:** never an entry.
  - It runs in scenario-only mode, because one leg is on Hyperliquid.
  - The reverse direction (long Hyperliquid / short Variational) showed a $1k scenario entry 3 times. All 3 were blocked by time alignment: the 2 s volatility limit against a measured 9–39 s.
- **Convergence and hybrid:** no entries for either asset.
  - Variational has no public price history.
  - The TxFlow/tradeXYZ gap was not extreme.
  - Without a convergence component a candidate is not a hybrid.

## A: Fixes

| # | Problem | Fix | Evidence |
|---|---|---|---|
| A1 | The estimated hybrid ran the funding calculation: same net, same cost, same income | `estimated.hybrid()`. The funding component is the published carry × the convergence's expected hold. The convergence component is the capture. Both sit on ONE round trip and ONE reserve, and the net is rebuilt from the components. Without a convergence component the result is `HYBRID_CONVERGENCE_UNAVAILABLE`. | At 05:12 the hybrid "entries" went from 2 to 0 (both had been copies of funding). Test `test_hybrid_components_apart_costs_once`. |
| A2 | `ESTIMATED_PAPER_ENTRY` read like a position had opened | Renamed `ESTIMATED_ENTRY_SIGNAL`. A position exists only in `v2/paper_ledger.py`, after a simulated attempt. | `test_signal_label_is_not_a_position` |
| A3 | A size between tiers was priced on the larger tier, which was called "conservative" | A size between tiers is now unpriced (`BETWEEN_TIERS_UNPRICED`). Only the observed range is reported. `tier_order` reports whether the quotes are actually monotone. | `test_between_tiers_has_no_price...`, `test_larger_tier_is_not_assumed_worse`. Variational NATGAS/INIT are priced only at $1k. |
| A4 | The strict gate stops at its static stage, so the stale-book check was never run when the stop reason was estimable, and the estimate skipped it too | `EstimatedEvaluator.critical()`: book present and fresh. `scenario()` also runs the static checks behind CONFIG_EXCLUDED, with the observation-only identity masked. | `test_estimate_runs_the_checks_strict_never_reached`, `test_scenario_runs_the_static_checks_behind_config_excluded` |
| A5 | The assumption that the published rate stays constant was hidden | `v2/funding_scenarios.py`: constant rate, 24 h / 7 d settled-history mean, half regime, sign flip mid-horizon / at entry, discrete payment schedule, sign persistence | 05:12 NATGAS TxFlow→tradeXYZ (table below) |

**05:12 NATGAS TxFlow→tradeXYZ funding scenarios.** Net after costs, $1k:

| Scenario | Net | Basis |
|---|---|---|
| Constant published rate (assumption) | +4.99 | PUBLISHED_RATE_CONSTANT |
| 24 h settled mean | +15.62 | tradeXYZ settled; TxFlow published (no history) |
| 7 d settled mean | +10.56 | same |
| Half regime | +0.28 | scenario |
| Sign flip at mid-horizon | −4.44 | scenario |
| Sign flip at entry | −13.87 | scenario |
| Payment schedule | +4.99 | TxFlow phase UNKNOWN → worst case 24 payments |
| Persistence | tradeXYZ rate in the same direction for 95.8% of 7 days | PER_LEG_ONLY: TxFlow has no history |

## B: Market data

- **Bounded stream:** `tools/v2_stream.py`, 90 cycles × 20 s, 14:40–15:09 TR, observation_v3 guard.
  - 617 reads succeeded, 14 failed, 0 refused.
  - All 14 failures were Variational HTTP 429. The global limit is shared with other clients, as documented. They were recorded and the reader stayed in its own budget.
  - Chain `1e32a69d…`, status OK.
- **Source timestamps:** book `time` (TxFlow, xyz, native), TxFlow ctx `blockTime`, Variational quote `updated_at`.
  - `metaAndAssetCtxs` publishes no timestamp, so its receive time is used and labelled `RECEIVE_TIME(ctx)`.
  - The decision time is the LAST receive time of the burst. Each book's age is measured from its own source timestamp.
- **Integrity** (`paper_obs.integrity`, 94 observations):

  | Leg | Backwards | Same timestamp repeated | Stale book |
  |---|---|---|---|
  | Variational NATGAS | 0 | 31 of 80 (cached answer, no new data) | — |
  | Variational INIT | 0 | 34 of 80 | — |
  | TxFlow, tradeXYZ, Hyperliquid | 0 | 0 | 0 (max book age 1.9 s) |

  - Sequence numbers are UNKNOWN: the REST answers carry none.
  - Gaps between the sampled points are reported: 07:36→12:47 (5.2 h) and 12:53→14:39 (1.8 h).
- **No look-ahead:** an observation sees settled points and hourly closes only up to its own time.
- **WebSocket is not used** and is blocked by policy. Adding one is a network-policy change for the owner to decide.
  - **Phase 9 correction:** the repo HAS a WS client (`extended_rfq.WebSocket`, used for TxFlow books, off by default). What is missing is a frame-level policy (doc 35).
- **Payment times:**
  - TxFlow: `nodeCtx.nextFundingTime`, 1 h.
  - Variational: `funding.next_timestamp`, 4 h for NATGAS, 1 h for INIT.
  - tradeXYZ / Hyperliquid: derived from the settled history times (hourly; times carry +ms).

## C/D: Ledger and execution models

- **`v2/paper_ledger.py`:**
  - event-sourced; the state is rebuilt from the hash-chained log on restart;
  - states DISCOVERED … CLOSED / RECOVERY_PENDING, as in the prompt;
  - deterministic position ids;
  - every leg's quantity, prices, fills (source times, quote age, tier, slippage), fees, funding (by evidence class), price PnL, total PnL, close reason, restart and recovery flags;
  - `risk()` shows unhedged units and their USD value.
- **`v2/paper_exec.py`:**
  - **CLOB:** marketable IOC; the slippage limit is 10 bp from the decision mid. Only visible depth inside the limit is filled; the rest is a partial fill and is retried.
  - **RFQ:** exact tier only; quote age ≤ 60 s. The firm gap and last-look rejection are SCENARIO inputs, never random.
  - Entry and exit RFQ quotes are separate.
- **`v2/paper_engine.py`:**
  - the RFQ leg goes first; the hedge is sized to its filled quantity;
  - a partial fill is retried up to 2 times, then LEG_RECOVERY, which unwinds the excess;
  - closing: RECOVERY_PENDING until flat;
  - re-entry cooldown: `wide_cooldown_seconds` (3600 s);
  - no opposite-direction position on the same two venues.

## E: Strategies (independent)

| Strategy | Entry | Exit |
|---|---|---|
| funding | ESTIMATED funding, strict thresholds (24 h break-even, 1.0 realization) | FUNDING_REVERSED (≤ 0 for 30 min), RISK_LIMIT (min($50, 10% of size)), MAX_HOLD (168 h) |
| convergence | strict convergence (7 d history, MAD, episodes) | CONVERGED (2 bp), STOP (6 × MAD past the entry), RISK_LIMIT, MAX_HOLD (72 h) |
| hybrid | funding + convergence components, shared costs once | two component exits; closes when BOTH are done, or on STOP / RISK_LIMIT / MAX_HOLD |

Funding is booked by evidence class:

| Class | Meaning |
|---|---|
| `SETTLED_RATE` | A public settled rate. Its value uses the observed mark, because the venue's price basis is not in the history. |
| `PUBLISHED_ESTIMATE` | The rate published in that period |
| `PUBLISHED_CARRIED_FORWARD` | No observation in that period: the last published rate was carried |

`observed_part` = price PnL − fees + SETTLED_RATE funding. `estimated_part` = published funding.

## F: Size

- The sizes tried are $1k, $2.5k, $5k and $10k. The chosen size is the one with the highest worst-scenario net.
- **NATGAS TxFlow→tradeXYZ, 07:36:**

  | Size | Net | Worst scenario |
  |---|---|---|
  | $1k | 6.14 | −0.41 |
  | $2.5k | 15.30 | −1.09 |
  | $5k | 30.55 | −2.22 |
  | $10k | 58.31 | −8.43 |

  $1k was chosen: as size grows, the worst case gets worse.
- **Variational legs:** $2.5k, $5k and $10k are `BETWEEN_TIERS_UNPRICED`, so only $1k is possible. No quantity is invented.

## G: Deterministic replay

- **Data:** 94 observations — 07:36 aligned, three bursts at 12:47 / 12:50 / 12:53, and the 90-cycle stream.
- **Output:** `E:\nb-v2-phase8\replay-2026-11-12-final3`
- **Determinism:** the estimated run, run twice, gave the same chain head (`a4b7732f…`).

**Signal 1: NATGAS funding, long TxFlow / short tradeXYZ, $1k**

| Question | Answer |
|---|---|
| When was the signal produced? | 07:36:28. Legs 4.9 s apart; limit 35.9 s. Expected net +6.14, worst −0.41, break-even 11.1 h. |
| Was a virtual position created? | Yes: 274.01 units, TxFlow 3.651 / xyz 3.66065, CLOB IOC, 4 `ESTIMATED_CLOB_FILL` |
| What happened at the next observation? | 12:47: carry +0.380 → −0.046 %/8h (`funding_changed=true`) |
| Why did it close? | `FUNDING_REVERSED`, at 14:39:31 (negative since 12:47, more than 30 min) |
| Net | −$0.42 = price −1.96 (the entry gap widened), fees −1.08, funding +2.61 |
| Observed vs estimated | observed part +$0.78 (price, fees, 7 tradeXYZ settled payments +3.82); estimated part −$1.20 (TxFlow: 2 published, 5 carried forward) |
| Reliability | **UNRELIABLE: FUNDING_CARRIED_FORWARD** |

**Signal 2: NATGAS funding, long TxFlow / short Variational, $1k**

| Question | Answer |
|---|---|
| When was the signal produced? | 07:36:28, skew 35.3 s (limit 36.3 s) |
| Position | Opened. Variational RFQ $1k at 3.64777 (quote age 37.6 s, `ESTIMATED_RFQ_FILL`); TxFlow 3.651. |
| What happened next? | Carry fell from +0.29 %/8h to about 0. After the 15:00 payment Variational's rate changed and the carry became +0.60 %/8h. |
| End of data | OPEN. The last exit check was skipped because the legs were 41.1 s apart (limit 36.3 s). Realized −$0.59 (fees 0.45, funding −0.14, all estimated); unrealized −$1.27. |

**INIT (scenario):**
- 160 evaluations, 134 economically interesting.
- 3 scenario entries (long Hyperliquid / short Variational, $1k, net 17.87 / 11.56 / 5.24) were blocked by time alignment: skew 9.3 / 39.0 / 23.5 s against the 2 s limit.
- 0 positions.

## H: Acceptance tables

Estimated PAPER, NATGAS (`estimated` run):

| | Funding | Convergence | Hybrid |
|---|---|---|---|
| 1. Candidates evaluated (pairs) | 252 (5) | 508 (6) | 508 (6) |
| 2. Entry signals | 2 | 0 | 0 |
| 3. Virtual positions created | 2 | 0 | 0 |
| 4. Closed | 1 (0 reliable) | 0 | 0 |
| 5. Open / recovery pending | 1 open (unhedged 0) / 0 | 0 | 0 |
| 6. PnL: observed part / estimated part | +0.33 / −1.34 (closed −0.42, open unrealized −1.27) | — | — |
| 7. Components | price −1.96, fees −1.53, funding: settled +3.82, published −0.19, carried forward −1.15 | — | — |
| 8. Fills: total / verified / unverified / not filled | 6 / 0 / 6 / 0 | 0 | 0 |
| First rejections | BETWEEN_TIERS_UNPRICED 469, CARRY_NOT_POSITIVE 271, BREAKEVEN 182 | PRICE_HISTORY_MISSING 960, GAP_NOT_EXTREME 690 | HYBRID_CONVERGENCE_UNAVAILABLE 1,696 |

INIT, scenario only (`scenario_init` run):

| | Funding | Convergence | Hybrid |
|---|---|---|---|
| 1. Candidates (pairs) | 160 (2) | 160 (2) | 160 (2) |
| 2. Entry signals | 0 (3 blocked by time) | 0 | 0 |
| 3–8 | 0 positions, 0 fills | 0 | 0 |
| First rejections | BETWEEN_TIERS_UNPRICED 251, INSUFFICIENT_LIQUIDITY 232, CARRY_NOT_POSITIVE 67 | PRICE_HISTORY_MISSING 640 | HYBRID_CONVERGENCE_UNAVAILABLE 640 |

Stress runs (NATGAS funding):

| Run | Signals | Positions | Not filled | Closed (net) | Note |
|---|---|---|---|---|---|
| `stress_last_look` (every RFQ open refused) | 4 | 1 | 3 | 1 (−0.42) | When the RFQ leg is refused first, the hedge leg is never sent: 0 exposure |
| `stress_firm_gap` (+10 bp) | 2 | 2 | 0 | 1 (−0.42) | The open position's unrealized result went from −1.27 to −2.27 |
| `stress_crash_restart` | 2 | 2 | 0 | 1 (−6.75) | After a crash following the first leg, the TxFlow long leg stayed UNHEDGED for 5.2 h (07:36→12:47). On restart it was reported as RESTART_DURING_ATTEMPT → retry → hedged. The closed trade is UNRELIABLE (FUNDING_CARRIED_FORWARD, RESTARTED, RECOVERY). |

## Still blocked / UNKNOWN

1. **Reliable closed trade: 0.**
   - TxFlow has no settled funding history, so between observations its rate can only be carried forward.
   - A reliable close needs either an authorized `userFundings` address or a continuous observation stream over the whole hold.
   - A 24/7 recorder was not started, by owner rule.
2. **NATGAS reference:** UNKNOWN (TxFlow has no NATGAS documentation). Every position carries basis risk.
3. **Variational firm quote and last look:** no public data, so these stay scenarios only. The indicative answer was a repeat (cached) in 31 of 80 reads.
4. **Venue funding price basis:** the history does not give it, so even `SETTLED_RATE` amounts are value estimates.
5. **WebSocket:** a network-policy change (owner decision).
6. **Convergence:** needs public price history for TxFlow/tradeXYZ beyond the pilot window, and Variational has no history at all.
