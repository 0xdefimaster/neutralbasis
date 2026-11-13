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

## 5. Results

### P0-A (commit 000a6b1)

`v2/funding_semantics.py`: rate kind and evidence per venue (`matrix()`), observation /
settlement contract, stale next-payment detection, one unit path. The TxFlow
hard-coded `/100` paths and the PAPER engine's settled-rate path (G5) changed; the
values are identical for every hourly venue. Full suite 1147 OK.

### P0-B: models reported separately

`v2/funding_expectation.py` (baseline / persistence / adverse, side-aware adverse, no
look-ahead, confidence never above MEDIUM). Attached to every ESTIMATED funding result
(`funding_models`) and to the scanner rows. It is reported only: a failure in the
models is reported as `MODELS_FAILED` and can never change an evaluation.

**NATGAS 15:00:11 local, $1,000, 24 h (OBSERVED inputs, ESTIMATED result)**

| model | P0-B general module | Phase 14 research (docs/v2/41) |
|---|---|---|
| baseline | +$16.66 (+0.555 %/8h) | +$16.66 |
| persistence | -$1.99 (-0.066 %/8h) | -$3.02 |
| adverse | -$4.96 (-0.165 %/8h) | -$5.10 |

The difference comes from method. The general module keeps the published tradeXYZ
value for the current period, and takes its residual quantiles from the whole history
up to the reference time (Phase 14 used a 70 % training split). The conclusion is the
same: only the baseline is positive.

**Scanner, 05:12 snapshot** (`v2-paper-data/phase15-scan-0512`, 0 network attempts).
Decisions and counts are identical to Phase 13: 1290 discovered, 2 expected-positive,
0 stress-positive, 0 STRICT.

| funding / hybrid rows reaching the funding economics: 52 | net > 0 | UNKNOWN |
|---|---|---|
| baseline (published held 24 h) | 2 | 0 |
| persistence-adjusted | 0 | 46 |
| adverse | 0 | 46 |

46 rows have an UNKNOWN persistence or adverse result because one leg (TxFlow or
Variational) has no settled history and no recorded samples in this snapshot. They
are shown as UNKNOWN, never as 0. 51 rows carry `BASELINE_ONLY_POSITIVE` (positive
baseline carry cash, persistence cash <= 0 or UNKNOWN). Both expected-positive rows
(NATGAS +4.99 and CL +2.37, long TxFlow / short tradeXYZ) are BASELINE_ONLY_POSITIVE,
PUBLISHED_PAYMENT_RELATION_UNKNOWN and NOT_SAFE.

Control Center: the opportunity detail shows the three nets and the model confidence.
UI smoke: 44 checks (10 views plus the NATGAS detail, at 360 / 390 / 768 / 1280 px),
0 problems.

Owner decision, not taken here: whether the ESTIMATED entry rule should require a
positive persistence-adjusted net. Today it is reported only; STRICT is unchanged.

### P0-C: sign and unit verification

**Tests (MOCK fixtures, end to end).** A positive published rate makes the LONG pay
on every PAPER adapter: Arcus, RISEx, TxFlow (percent), Variational (per 4 h), tradeXYZ,
HL native and Lighter RH (`direction`). Lighter's `short` gives a negative rate, and an
unknown direction is dropped. A TxFlow percent value read as a fraction would invent a
99x carry; the single unit path makes that impossible.

**Recorded data** (`tools/v2_funding_semantics_check.py`, report in
`v2-paper-data/phase15-funding-semantics/`, 0 network attempts).
- Boundary convergence. tradeXYZ NATGAS: 0.0048 % from the 12:00 UTC settlement
  (11 s before). HL native INIT: 0.25 % (10 s before). Both CONVERGE. TxFlow has no
  settled history (48 distinct values in 62 samples).
- Variational. `last` == `next` in 152 / 152 listings; the value changes about every
  298 s; `next_timestamp` was in the past in 28 reads (max 285 s).
- Cross-venue settled histories (05:12 snapshot: 66 assets on 2 or more venues, 102
  venue pairs). 0 sign-inversion or scale suspicions.

  | venue pair | pairs | median sign agreement | median abs. ratio |
  |---|---|---|---|
  | arcus / lighter_rh | 43 | 0.86 | 1.14 |
  | arcus / tradexyz | 33 | 0.79 | 0.82 |
  | lighter_rh / tradexyz | 26 | 0.93 | 0.64 |

  This supports Lighter's engine-convention sign and its percent unit, but does not
  verify them. RISEx, TxFlow and Variational were not in the comparison (no settled
  history in the snapshot).

**Registry.** `sign_cross_check` was added to `v2/funding_semantics`. The Control
Center funding view shows what each venue's field means. UI smoke: 44 checks, 0
problems.

**What only external evidence can settle.**
- The TxFlow and Variational payment rate needs account payments (an authorized
  address) or the venue's formula documentation.
- The Arcus and RISEx current-rate semantics need an intra-period recording, which
  is in the live test plan.
- Lighter's `direction` needs the docs or an account payment.

### P1: multi-venue data contract v2

`v2/control/contract.py` -> `md-contract/2`. This is backward-compatible: the 13 v1
fields keep their meaning. Nine fields are added:

- `schema_version`
- `canonical_underlying_id`
- `channel`
- `monotonic_receive_ts`
- `integrity`
- `funding_evidence`
- `quote_type`
- `execution_model`
- `multiplier`

New functions:
- `normalize_market` (adapter records with their funding evidence);
- `normalize_book`;
- `validate` (reports problems, never repairs);
- `sequence_check` (duplicates, regressions, gaps as a SUSPICION only, out-of-order
  source times);
- `coverage`.

Per-answer receive time:
- The collector keeps each answer's own fetch time (wall clock and monotonic).
- A cached book used to be re-stamped "now", so its age was understated by up to the
  5 s cache TTL. Rows were stamped with the refresh time while their ctx answers can be
  up to 60 s old.
- The honest time is now carried as `answer_received` (for a row: the oldest input of
  the venue) and in the contract's `received_ts`.
- The legacy `received`, which the engines' age gates read (STRICT BOOK_STALE, the
  legacy desk's 120 s funding age), is **unchanged**. Moving those gates to the answer
  time would make them stricter, so it is an owner decision.
- WS records carry `recv_mono`.

**Audit** (`tools/v2_contract_audit.py`, `v2-paper-data/phase15-contract-audit/`,
0 network attempts):
- 1195 snapshot markets mapped and validated: 0 problems.
- 630 Phase 8 stream answers: 0 problems. Receive-minus-source is positive everywhere
  (median 0.35–0.52 s, minimum 0.10 s); this is apparent latency, clock unverified.
  Variational had 14 failed answers (HTTP 429).
- What each venue's funding row can say:

  | venue | rate kind | funding rate known | underlying id |
  |---|---|---|---|
  | arcus | UNVERIFIED | 100 % | 85 % |
  | lighter_rh | LAST_SETTLED | 82 % | 86 % |
  | risex | UNVERIFIED | 100 % | 98 % |
  | tradexyz | RUNNING_CONVERGES (next payment UNKNOWN in the answer) | 100 % | 57 % |
  | txflow | RUNNING_PAYMENT_UNKNOWN | 97 % | 81 % |
  | variational | RUNNING_PAYMENT_UNKNOWN | 97 % | 30 % |

  The underlying id is set only for identity-verified markets.

**Fake-signal guards:**
- Identity refused 3 MULTIPLIER_UNKNOWN and 1 PRICE_MISMATCH market (Variational),
  plus single-venue, conflict and inactive markets. None of them can become a row.
- Over 194 verified multi-venue groups, the per-unit price max/min has a median of
  1.0020 and a maximum of 1.0129; 0 groups exceed 1.5x.
- MOCK tests: a 10x price gives PRICE_MISMATCH and no row; an unknown multiplier
  ("5000X") gives MULTIPLIER_UNKNOWN; a percent rate read as a fraction would create
  a 99x carry, and the contract path makes that impossible.

The scanner output is byte-identical before and after P1 (replay clock), and STRICT
is unchanged. The full suite is 1169 OK.

## 6. Re-assessment of P2–P6 after P0 / P1

| priority | what P0 / P1 showed | what exists | recommendation |
|---|---|---|---|
| P4 data quality | `v2/ws_readonly.Books.record` swallows `on_record` errors with no counter (the data path survives, but errors vanish silently). Telemetry (`v2/control/telemetry.py`) bounds each key's buckets, but not the NUMBER of (venue, market, channel) keys; `Books.per_market` is the same. `contract.sequence_check` now exists (gaps are a suspicion). | bounded deques, rotated history file, heartbeat | **Next (first).** Small and isolated. (a) A callback error counter, the last error and a Doctor warning. (b) A key cap with LRU eviction and an eviction counter. (c) Recorder / JSONL corruption detection and restart after an incomplete write, offline fixtures only. |
| P2 rate coordinator | Variational: 14 of 90 Phase 8 answers failed (global 429) with ONE process. `ratelimit.BUDGETS` are per process, so two tools at once could double the share. | per-process sliding window, a 429 pause, single-flight cache per process | **Second.** A file-locked token bucket per (venue, endpoint class, IP) on C:, plus a cross-process single-flight cache, Retry-After, jittered backoff, a circuit breaker and telemetry. Offline / MOCK with several processes; behind a switch, default off. The global Variational limit is shared with other clients and stays a risk. |
| P6 replay acceptance | Already covered by P0 / P1 tests: sign inversion, wrong multiplier, wrong unit, sequence gap / duplicate / out of order, stale next payment. | `tools/v2_paper_replay.py`, fixtures | **Third.** One fixture harness with the remaining scenarios: REST timeout, 429, WS disconnect / reconnect, snapshot loss, duplicate funding payment, clock drift, stale quote, a slow venue, recorder crash, queue overflow, full disk, corrupted JSONL, restart after an incomplete write. |
| P3 channel scheduler | WS probes: 0 resync over 2 x 300 s (TxFlow, HL, Lighter). REST polling repeats the same data where WS exists. | `marketdata.PollWorker`, `TxFlowDepthStream`, `ws_readonly` | **After P2.** It needs the shared budget to schedule against. WS-first with REST fallback and recovery. Publication rate is kept apart from request rate (Variational: values change about every 298 s, so polling every 20 s mostly re-reads). |
| P5 Doctor | the funding semantics matrix is already in the read model / panel | Doctor, read model, telemetry | **Incrementally,** with each of P2 / P3 / P4: budget use, throttling, queue depth, checkpoint age, recorder lag, funding evidence coverage. A refresh never calls a venue. |

Owner decisions collected so far (none taken here):
1. Whether the ESTIMATED entry rule should require a positive persistence-adjusted net.
2. Whether the engines' age gates should read `answer_received` (stricter, honest)
   instead of the legacy `received`.
3. Approval of any live plan in docs/v2/44 (L1 first).
