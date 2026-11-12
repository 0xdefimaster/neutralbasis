# Phase 8 test report

**Command:** `python -m unittest discover -p "test_*.py"` (venv: Python 3.13)

**Result:** **1013 tests, OK (2 skipped), 271 s.**
- Before (a18c732): 990.
- New: 23. One RFQ test was replaced and one added in `test_v2_estimated.py`, plus 22 in `test_v2_paper.py`.
- The two skips are the existing ones.

All Phase 8 test data is MOCK unless a test says otherwise. The OBSERVED replay is in doc 31.

## New and changed tests

| File / test | What it proves |
|---|---|
| `test_v2_estimated.RfqQuoteTests.test_between_tiers_has_no_price_only_the_observed_range` | A3: a size between tiers is unpriced; only the observed range is reported |
| `...test_larger_tier_is_not_assumed_worse` | A3: a larger tier tighter than a smaller one is reported (`OBSERVED_NOT_MONOTONE`) and nothing is priced |
| `test_v2_paper.StorageTests.test_paths` | D:, `E:\NeutralBasis`, other E: folders and database files are refused; `E:\nb-v2-phase8` is allowed |
| `...test_chain_detects_corruption_and_isolates_a_torn_tail` | A torn line is isolated with a marker; a tampered line → CORRUPT; appending is refused |
| `ExecTests.test_clob_ioc_partial_inside_the_slippage_limit` | IOC fills only visible depth inside the limit; the rest is partial; unverified |
| `ExecTests.test_rfq_exact_tier_last_look_and_firm_gap` | RFQ: exact tier only; last-look refusal fills nothing; firm gap applied; a matched quantity off-tier is unpriced |
| `EngineTests.test_full_lifecycle_and_accounting` | Signal → position (best worst-case size) → monitoring → FUNDING_REVERSED → CLOSED. The spread is counted once, inside the 4 fill prices. Funding is split SETTLED/PUBLISHED; observed + estimated = total; RELIABLE; no open risk. |
| `...test_partial_fill_retry_then_leg_recovery_never_hides_exposure` | PARTIALLY_FILLED with the unhedged units and USD shown → retries → LEG_RECOVERY → hedged or flat |
| `...test_crash_after_first_leg_and_restart` | Fault injection: crash after the first leg; a new process rebuilds the ledger and shows the exposure; RESTART_DURING_ATTEMPT → hedged |
| `...test_missing_close_data_never_makes_a_closed_trade` | A missing close book → RECOVERY_PENDING, still in `risk()`. Later CLOSED, but UNRELIABLE. |
| `...test_misaligned_data_blocks_the_signal` | Legs more than the freshness limit apart → no signal (`blocked_by_time`) |
| `...test_last_look_on_the_rfq_first_leg_leaves_no_exposure` | RFQ leg first; refused → ENTRY_NOT_FILLED and the hedge is never sent; $2.5k unpriced; cooldown blocks re-signalling |
| `...test_no_opposite_position_on_the_same_venues` | The reverse pair is not opened while one is open |
| `...test_replay_is_deterministic` | Two runs give the same ledger chain head |
| `StrategyTests.test_estimate_runs_the_checks_strict_never_reached` | A4: strict stops at SETTLEMENT_EVIDENCE_MISSING; the estimate still rejects a stale book |
| `...test_signal_label_is_not_a_position` | A2: `ESTIMATED_ENTRY_SIGNAL` |
| `...test_funding_constant_rate_assumption_and_scenarios` | A5: the constant-rate assumption is reported, with all 7 scenarios, the payment counts and their basis, and persistence |
| `...test_hybrid_components_apart_costs_once` | A1: hybrid fees and spread equal ONE round trip; net = funding + convergence − costs − reserve |
| `...test_hybrid_without_convergence_is_not_a_hybrid` | A1: no convergence component → HYBRID_CONVERGENCE_UNAVAILABLE (funding still enters) |
| `...test_scenario_runs_the_static_checks_behind_config_excluded` | A4: an observation-only pair still fails on FEE_UNKNOWN |
| `FundingScenarioTests.test_payment_counts` | Published next payment / unknown phase (worst case) / none within the horizon |
| `ObservationTests.test_source_times_and_no_lookahead` | Decision time = last receive time; source time parsed; no settled point or close after the observation |
| `...test_integrity_flags_repeats_backwards_gaps_and_stale_books` | Timestamp integrity checks |
| `...test_stream_reader_backs_off_a_failing_venue` | Two failures → back-off with skips; a retry after the back-off |

## Fault injection on observed data (replay, doc 31)

| Injection | Result |
|---|---|
| Every RFQ open refused (last look) | 3 attempts not filled, 0 exposure; CLOB pairs unaffected |
| Firm quote 10 bp worse | Open unrealized −$1.27 → −$2.27 |
| Crash after the first leg | 5.2 h unhedged; visible on restart; recovered; trade marked UNRELIABLE |
| Variational HTTP 429 (live, 14 times) | Recorded as failed answers; no tight retry; those observations have no Variational leg |
| Determinism | Same chain head on two runs (`a4b7732f…`) |

## Unchanged invariants

`git diff a18c732` is empty for:
- `v2/policy.py`
- `v2/network_policy.py`
- `wide_paper.py`
- `v2/gates.py`
- `v2/hybrid_desk.py`
- `v2/observation.py`
- `pilot/`
