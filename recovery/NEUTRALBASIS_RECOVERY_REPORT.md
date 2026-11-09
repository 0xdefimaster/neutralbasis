# NeutralBasis Recovery Report

| Status | Result |
|---|---|
| ENGINEERING | **PASS** (isolated worktree; not deployed) |
| ALL-EXCHANGE COVERAGE | **PARTIAL** |
| FUNDING STRATEGY | **PARTIAL** |
| CONVERGENCE STRATEGY | **PARTIAL** |
| REAL-MARKET EVALUATION | **FAIL** (the fixed build has not finished a real evaluation yet; see Q1) |
| PAPER EXECUTION TEST | **PASS** (MOCK, real gates) |
| REAL-DATA PAPER POSITIONS | **0** |
| CLOSED PAPER POSITIONS | **0** (real data). MOCK test: 1, excluded |
| ECONOMIC VIABILITY | **UNPROVEN** |
| ZERO-TRADE ROOT CAUSE | **PARTIALLY RESOLVED** |
| LIVE ORDERS | **0** |
| SAFETY GATE BYPASSES | **0** |

2026-11-10 · PAPER ONLY. Live engine PID 5988 (started 2026-11-09 18:27:10, PAPER,
`E:\NeutralBasis` @ `fceaf96`, clean tree): verified read-only at the start and still
untouched. No restart, deploy, push, merge, SQLite/WAL access or gate change.

## 1. Which level do data collection and opportunity evaluation reach across all venues?

**Live engine (fceaf96).** Only Hyperliquid and Extended can execute.

- **Lighter Core:** 0 of 215 markets in the universe. Its funding interval and sign can never be verified: CCXT gives no interval and no history, and a zero fee.
- **Lighter RH:** blocked at market discovery by the parser defect.
- **Paradex** (continuous accrual) and **RISEx** (sign/projection) are research.
- **TxFlow / Arcus:** model and sign undocumented.
- **Variational:** has no connector.

The candidate list is therefore Extended↔Hyperliquid only. That universe is uneconomic: RFQ books cost 256–387 bp round trip, and normal-market funding is below the 14 bp of four-fill fees.

**Fixed build (worktree; pilot running since 02:35 on its own port and C: database).**

| Venue | Pilot markets |
|---|---|
| Lighter Core | 65 fresh markets (live: 15, no funding) |
| Lighter RH | 24 |
| Hyperliquid | 109 |
| Extended | 339 |
| Paradex | 15 |
| RISEx | 9 |
| TxFlow | 15 |
| Arcus | 14 |

The pilot scans 590 markets across 101 assets and 213 pairs, forming 208 funding and 212 convergence candidates. All are still research. A fresh database must observe at least 3 published-rate → settled-payment comparisons per market (and 3 decided markets per venue) before the funding-sign gate opens: not before about 06:00 TRT. **No deep check has run in the pilot yet.** That is why REAL-MARKET EVALUATION is FAIL: the fixed build's real evaluation is not complete.

Per-venue detail: `VENUE_CAPABILITY_MATRIX.md`.

## 2. Which concrete opportunities did funding and convergence find in real market data?

None that is economically supported.

| Source | Funding | Convergence |
|---|---|---|
| Live engine, 98 cycles (00:16–02:26) | 122 unique candidates (65 research), 325 deep checks, 0 eligible | 203 unique (100 research), 403 deep checks, 0 eligible |
| Offline walk-forward, 4 venues, 30 days | Cat. 1: 0 · Cat. 2: 22 · Cat. 3: 436 | Not evaluable: no synchronous executable price history |

The walk-forward values are funding contribution minus assumed costs, not position PnL.

Category 2 is promising but missing evidence:

- tokenized equities Lighter RH → Lighter Core (SNDK, GOOGL, META, AMD …), mostly only with Standard-account (0 %) fees;
- commodities WTI and XCU on Extended ↔ Lighter Core;
- XMR Lighter Core → HL (1–2 independent episodes).

The missing evidence: account type and fee, basis risk, and more than 1–2 independent episodes.

Extended RFQ is RESEARCH_ONLY:

- real book: 256–325 bp;
- indicative book: 4–30 bp, not firm;
- public trade prints: +4.5 bp versus mark, small, and not a fill at our size.

## 3. If a candidate passes every condition, can a PAPER position be opened and managed?

**Yes, proven with MOCK data, through the real gates.** `test_paper_lifecycle_e2e` checks:

- entry on both legs at book VWAP;
- fees from the fill prices;
- funding per venue schedule, never paid twice;
- restart without reopening;
- exit on a persistent carry flip;
- net = price PnL + funding − fees = the change in the wallet.

Partial and one-leg fills, stale or thin books and unknown fees are covered by existing tests. Figures: `EXECUTION_EVIDENCE.md`.

The pilot runs the same entry code automatically whenever a real candidate passes. **It has not opened one; none qualified.**

## 4. With still zero real PAPER trades: economics, missing data, or a software defect?

All three, now measured apart (`ZERO_TRADE_ROOT_CAUSE.md`):

- **Software:** Lighter Core and Lighter RH data paths, and the RFQ quota. Fixed in the worktree, not live. In the live engine they still hide the largest real universe.
- **Economics:** everything that was evaluated failed on measured costs (RFQ spread, fees > funding, below the convergence tier). The walk-forward finds no supported pair under the engine's fees.
- **Missing data/evidence:**
  - the fixed build's funding-sign evidence (accruing);
  - account type and fee (Standard vs Premium);
  - executable synchronous prices for basis/convergence;
  - RFQ fills at our size;
  - maker fill behaviour.

## 5. Commits that can safely go to the live engine, and what still needs approval

Branch `local/audit-fixes-2026-11-09`, base `fceaf96`. Local only, not pushed.

| Commit | Content | Decision effect | Recommendation |
|---|---|---|---|
| `e1ccf7e` | Lighter RH / Extended parser isolation | Fixes a dropped venue | Transfer (approval) |
| `d7d00ba`, `2a0559b` | RFQ trace, recorder, replay tools | None (observation) | Transfer (approval) |
| `2929e0a` | Funding observation model | None (not wired) | Optional |
| `9ca4467` | Lighter Core native funding + documented fee | Makes Lighter Core evaluable; replaces an unverified 0 % fee with 0.028 % | Transfer (approval); restart needed |
| `cff3b7f` | RFQ quota (F1) | Quota rule change | **Owner approval** (previously deferred) |
| `e2e25c8` → `3102fa0` | RFQ liveness, final form: ping is diagnostic only | None on decisions | Transfer with `3102fa0` only together |
| `27c7fc6` | Shadow ranking (offline) | None | Analysis tool |
| `66e7cac` | Pilot + network guard + MOCK E2E test | None on the engine | Stays isolated |

Still requiring approval:

- the transfer itself and the restart. The 72 h observation ends 2026-11-12 18:27:10, and its end does not lift any rule;
- the F1 quota rule;
- any account-type fee decision (Standard 0 % is never assumed);
- RISEx/Lighter acceptance (the persistence test treats last-settled venues alike);
- an RFQ scheduler or ranking change;
- wiring the shadow ranking into decisions.

## Changes, commits and tests

- Start: `local/audit-fixes-2026-11-09` @ `27c7fc6` (earlier this session `fceaf96`). End HEAD: `66e7cac` + this report's commit.
- This phase:
  - `9ca4467` Lighter Core;
  - `66e7cac` pilot, guard and E2E;
  - earlier today `cff3b7f`, `3102fa0`, `27c7fc6`.
- Tests: full suite under an E:-write audit hook: **724 run, 0 failures, 0 errors, 2 optional skips, E: writes 0.**

New tests this phase:

| Test file | Tests | Covers |
|---|---|---|
| `test_lighter_core_funding` | 21 | Identity, settled interval, stale rate, zero-fee refusal, budget, history, scan integration |
| `test_pilot_guard` | 7 | Private and order endpoints blocked, unknown hosts, live port, hook in a child process |
| `test_paper_lifecycle_e2e` | 1 | Full MOCK lifecycle |
| `test_shadow_ranking` | 17 | Lookahead, publication lag, candle close, overlap |

Behaviour verified with real data:

- Lighter Core rows from native settlements (15/15 selected, universe growing);
- publication lag 8–12 s;
- RFQ book levels versus trade prints;
- pilot guard 0 refusals in normal running.

## Remaining problems

1. Pilot evidence: check after about 06:00 TRT (`pilot_api report`). Until then the fixed build has made no deep check.
2. 33 live candidates were never assessed (UNKNOWN). The rejection wait is not exposed; the trace (`d7d00ba`) will show it once deployed.
3. Basis and convergence lack executable synchronous prices. Recording two-leg bid/ask is needed before any convergence or total-PnL claim.
4. The account type decides whether Lighter Category 2 candidates are viable.
5. TxFlow, Arcus and Variational stay research or not supported (no documented funding model / no connector). Paradex continuous accrual is unsupported by design.
