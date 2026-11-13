# 43 — Phase 15: plan, backlog re-evaluation and P0 funding audit

Phase 15 goal: **Reliable Market Data Infrastructure v1**. The existing collector,
adapters, Doctor, Control Center, funnel, gates (STRICT unchanged), risk engine and PAPER
ledger stay. Phase 15 adds no new venue and no second data engine; it extends the
contract that the existing producers already map onto (`v2/control/contract.py`).

Constraints for this phase: PAPER ONLY, live trading DISABLED, no recorder start, no
live venue test, no D:/E: writes, no change to STRICT gates or venue lists. Every new
result is labelled VERIFIED, ESTIMATED, SIMULATED or UNKNOWN.

## 1. Backlog re-evaluation (docs/v2/42 §10 against the owner's P0–P6)

| # | Phase 14 backlog item | Phase 15 priority | Change |
|---|---|---|---|
| — | (new) funding semantics, rate kinds, model separation | **P0** | Highest. The Phase 14 NATGAS finding showed that published rates are running values whose relation to the payment differs by venue. |
| — | (new) multi-venue data contract incl. funding fields, scale / multiplier / unit fake-signal tests | **P1** | Extends `v2/control/contract.py` instead of building a second engine. |
| 1 | cross-process rate budget | **P2** | Unchanged; offline / MOCK first. |
| 2 | channel scheduler | **P3** | Merged with "WS-first" (item 3) into one scheduler with a REST fallback. |
| 3 | WS-first market data | **P3** | Merged into P3. |
| 4 | recorder heartbeat + crash recovery | **P4** | Offline code and fixtures only; no recorder start (owner: not approved). |
| 5 | per-answer snapshot times | **P1** | Part of the contract (`source_ts` / `received_ts` per answer). |
| 6 | TxFlow whole-period recording | **live test plan** | Needs a live read: prepared as a bounded plan, not run. |
| 7 | US-session read | **live test plan** | Same. |
| 8 | Linux clock reader, systemd, log rotation | **P5 / server prep** | Configuration and offline tests only; nothing installed or started. |
| 9 | budget view in the panel | **P5** | After P2 exists. |
| 10 | clock gate before showing latency | **P5** | Doctor rule. |
| — | (new) telemetry `on_record` error accounting, bounded cardinality | **P4** | From the owner's P4 list. |
| — | (new) replay of degradation scenarios | **P6** | Offline fixtures. |

Order: P0 → P1 (implemented first), then the remaining priorities are re-assessed with
the P0/P1 test results before any of them starts.

## 2. P0 audit: what the code did with funding before this phase

What already existed and is kept:
- `v2/venues/*`: every adapter emits `funding = dict(raw, unit, interval_s, next_ts,
  kind, sign, source)`; units are explicit and converted only through
  `v2/fees.to_fraction` (unknown unit -> error). Lighter RH's current value is labelled
  `last_settled_projection`.
- `v2/funding_evidence.py`: evidence statuses (RATE_PUBLISHED < ... <
  ACCOUNT_PAYMENT_CONFIRMED), the single sign convention, coverage-aware windows.
- `v2/funding_math.py`: discrete payment times, unknown phase -> band.
- STRICT funding strategy (`v2/strategies/funding.py`): needs SETTLED public payments
  on both legs; TxFlow / Variational legs are refused (SETTLEMENT_EVIDENCE_MISSING).
- PAPER ledger: funding events carry SETTLED_PUBLIC_RATE / PUBLISHED_RATE_ESTIMATE /
  ASSUMED_CARRIED_FORWARD; assumption-dependent funding is reported separately.

Gaps found:

| # | Gap | Effect | Severity |
|---|---|---|---|
| G1 | `kind="observed_published"` for every current rate. Nothing says whether the published value is a running estimate of the current period, a last settled value, or something else. | The ESTIMATED evaluator and the scanner hold the published value for 24 h (`PUBLISHED_RATE_CONSTANT`); the NATGAS +4.99 came from that. | High (economic) |
| G2 | The headline expected net of ESTIMATED / scanner rows is the baseline model only; persistence-adjusted and adverse results exist only in the Phase 14 research report. | A row can look positive when only the baseline is positive. | High (economic) |
| G3 | `next_ts` may be in the past at read time (Variational: 28 of 152 listings, up to 285 s after the boundary, Phase 8 stream). Consumers walk it forward silently. | Payment-time phase can be wrong for a few minutes after each boundary. | Medium |
| G4 | TxFlow conversion is hard-coded `/100` with `interval_s or 3600` in `v2/paper_obs.py` and `tools/v2_estimated.py` (correct today, unit check bypassed). | A unit change would pass silently. | Low (latent) |
| G5 | The PAPER engine books a settled point's rate (per PERIOD) through the hourly-rate path (`x interval/1h`). | Correct only because every settled venue is hourly today. | Low (latent) |
| G6 | Funding sign: documented for Arcus, TxFlow, Variational, HL/tradeXYZ; Lighter RH `direction` follows the engine convention (docs do not define it); no cross-venue check exists. | An inverted sign would make a fake carry. | Medium |
| G7 | The contract audit (`v2/control/contract.py`) has no funding fields (rate kind, interval source, next payment, projected / settled, valid-for). | Downstream code cannot tell a projection from a settlement. | High (contract) |

## 3. P0 evidence (offline, recorded data only, 0 network attempts)

Source: the rescued Phase 8 stream (2026-11-12 11:39–12:09 UTC, about every 20 s) and
its embedded settled histories.

| venue | field | behaviour inside a funding period | relation to the payment |
|---|---|---|---|
| tradexyz | `ctx.funding` | moves every sample (62 distinct in 62), smoothly | last value before 12:00 UTC 6.905777e-4 vs settled 12:00 6.906106e-4 (0.005 %): **converges to the settlement** (1 market, 1 period) |
| hyperliquid_native | `ctx.funding` | moves every sample | INIT last value -8.87029e-5 vs settled -8.89231e-5 (0.25 %): **converges** (1 market, 1 period) |
| txflow | `nodeCtx.funding` | continuous fall to the period end, carried over the boundary, then a different pattern (docs/v2/41) | **UNKNOWN** (no settled history; account payments need an authorized address) |
| variational | `funding.last` / `funding.next` | identical in 152 / 152 listings; change about every 5 min | **UNKNOWN** (no settled history in the API); `last` is not a settled value |

## 4. P0 / P1 slices

- **P0-A** funding semantics registry + funding observation / settlement contract
  (`v2/funding_semantics.py`): rate kind per venue with its evidence, interval and
  next-payment sources, `valid_for = CURRENT_PERIOD_ONLY` for every published value,
  stale next-payment detection, one unit path. Fixes G1, G3, G4, G7 (funding part).
- **P0-B** model separation (`v2/funding_expectation.py`): baseline / persistence-
  adjusted / adverse per leg and pair, side-aware adverse, no look-ahead; attached to
  ESTIMATED results and scanner rows as reported fields (decisions unchanged);
  `BASELINE_ONLY_POSITIVE` flag in the read model. Fixes G2.
- **P0-C** sign and unit verification: MOCK end-to-end sign tests per adapter;
  offline cross-venue settled-rate sign / scale check and boundary-convergence check
  (`tools/v2_funding_semantics_check.py`); G5 fix. Fixes G5, G6 as far as public data
  allows.
- **P1** multi-venue data contract v2 and fake-signal tests (scale, multiplier, unit).

Results and test counts are added to this document when each slice is committed.
