# 50 — Phase 15 integrity report and bounded live-verification proposals

Branch `v2/venue-policy`. Local HEAD `c12e0a0`; GitHub is at `b364f52`. These 11
commits are local only. The full suite is **1226 tests, OK, 2 skipped**. PAPER ONLY:
- no order, signature, key or wallet action;
- no D:/E: write;
- no recorder and no live venue test;
- STRICT unchanged.

## 1. What changed, slice by slice

| slice | commit | real bugs fixed | behaviour change |
|---|---|---|---|
| P0-A funding semantics | 000a6b1 | — | none (identical numbers) |
| P0-B three models | 2974cba | — | reported only |
| P0-C sign / unit | 1eeac6b | latent: settled per-period rate in the hourly path | none for hourly venues |
| P1 contract v2 | 48dacd3 | a cached answer re-stamped as fresh (data honesty) | new field `answer_received`; gates unchanged |
| docs | 94b52f5 | — | — |
| P4 data quality | 08d34f0 | callback errors swallowed silently; unbounded metric cardinality; dedupe cleared everything at 200k | metrics bounded; state never evicted by size |
| time-basis shadow | ebc8e6d | the legacy desk's "re-read" did not fetch anything | shadow only; switch prepared, OFF |
| funding classes | 1f731cf | — | reported only |
| P2 rate coordinator | ff9e0cd | clock step back stalled the budget; Windows text-mode fd; stuck half-open probe; CircuitOpen skipped the save | OFF by default |
| P6 failure injection | cd92400 | **wrong-price PAPER fills on retry / unwind / close; a future time counted as fresh** | stricter; recorded replays byte-identical |
| P3 + P5 channels | c12e0a0 | — | plan / view only; not run live |

## 2. Doctor / panel: new indicators

- telemetry callback errors (frequency, last error, source) and cardinality / evictions;
- telemetry key bound;
- rate budget per scope (tokens, breaker, pause, 429, in flight, leases reaped), with
  the external IP share UNKNOWN;
- per-leg `time_shadow` in STRICT results;
- funding semantics per venue;
- three funding models, class and profit label per opportunity;
- data channels per venue (source, cadence, last change, stale, errors, evidence).

## 3. Resource impact (measured on this machine)

| item | value |
|---|---|
| memory, 50k-market stress | 147 MB unbounded → 20 MB bounded |
| rate coordinator | 0.42 ms per acquire + release; a few hundred bytes of state per scope |
| C: written this phase | about 0.2 MB of reports plus the read model (control folder: 4 MB in total); C: free 19 GB |
| network | 0 attempts in every offline tool and test (socket guard) |

## 4. Venue × data type × accuracy (after Phase 15)

| venue | order book | current funding meaning | settled history | sign | what only external evidence settles |
|---|---|---|---|---|---|
| tradexyz | WS / REST, LIVE_OBSERVED | running value converging to the payment (OBSERVED, 1 period) | yes (public) | DOC + cross-venue consistent | more boundaries (L1) |
| hyperliquid_native | WS / REST, LIVE_OBSERVED | converges (OBSERVED, 1 period) | yes | DOC | more boundaries (L1) |
| txflow | WS / REST, LIVE_OBSERVED | running value, payment relation UNKNOWN | none public | DOC | account payments (authorized address) or the formula |
| variational | RFQ indicative only | `last` == `next`, about 5 min updates; payment UNKNOWN | none | DOC | settlement data (none in the API) |
| arcus | REST, LIVE_OBSERVED | UNVERIFIED (never sampled inside a period) | yes | DOC + cross-venue consistent | an intra-period recording (L4) |
| risex | REST, LIVE_OBSERVED | UNVERIFIED | yes | DOC | an intra-period recording (L4) |
| lighter_rh | WS / REST, LIVE_OBSERVED | last settlement reused | yes | ENGINE + cross-venue consistent | the docs or an account payment for `direction` |
| entropy_io | REST | INFERRED (same engine as HL) | yes | INFERRED | an observation |
| popdex, veranta, phoenix, qfex | — | UNKNOWN | — | UNKNOWN | a verified public API |

**Solved in code:**
- one unit path;
- stale next-payment detection;
- per-answer receive times;
- bounded telemetry;
- cross-process budget;
- fill guard;
- time-basis shadow;
- scheduler policy.

**Needs external evidence:**
- what TxFlow and Variational PAY;
- Arcus / RISEx current-rate semantics;
- Lighter `direction` per the docs;
- the share of the IP quota used by other clients;
- Linux execution of the P2 locks.

## 5. L1 re-assessed: what can 3,780 requests actually prove?

**Can prove:**
- TxFlow's published value at every boundary: whether it resets, continues or jumps,
  and whether that is the same on every hour and every market;
- tradeXYZ / HL convergence to the settlement on several boundaries instead of one,
  which would raise their rate-kind evidence from "1 period" to "N periods".

**Cannot prove:**
- which TxFlow value is PAID (only an account payment or the formula settles it);
- anything about Variational payments;
- persistence beyond a few hours.

The old plan sampled every 20 s for 3 h, but the information sits around the
boundaries. **Revised L1 (minimum sufficient):**

| item | value |
|---|---|
| Markets | TxFlow NATGAS, CL, BTC (3); tradeXYZ ctx (one answer covers all its markets) |
| Sampling | every 10 s from −120 s to +120 s around each boundary (25 samples); every 120 s in between (about 28 samples) |
| Duration | 3 h 05 min (4 boundaries) |
| Requests | TxFlow about 3 × 4 × 25 + 3 × 3 × 28 = 552; tradeXYZ about 4 × 25 + 3 × 28 = 184 (**about 740 instead of 3,780**) |
| Rate budget | TxFlow peak 0.3 req/s (budget 40/s); HL peak 0.1 req/s (budget 50/min). Both go through the P2 coordinator (HIGH priority) |
| Volume | about 2–4 MB plain, under 1 MB gzip |
| C: cap | 50 MB hard stop for the folder; stop if C: falls below 5 GB |
| Success | at least 90 % of the boundary windows answered, per venue; verdict per boundary and market |
| Stop | 3 consecutive failures on a venue (it is skipped); breaker OPEN for more than 10 min; any guard refusal |
| After it | TxFlow payment still UNKNOWN; tradeXYZ / HL evidence goes from 1 to N periods |

**L2, L3 and L4** stay as in docs/v2/44 with two additions: every request goes
through the P2 coordinator, and the same 50 MB folder cap and 5 GB C: floor apply.
L4 is reduced to sampling around boundaries (about 80 requests instead of 240).

None of them runs without the owner's approval.

## 6. Push readiness (no push made)

| check | result |
|---|---|
| working tree | clean after each commit |
| tests | full suite 1226 OK, 2 skipped |
| secret scan `b364f52..HEAD` | clean: only the labelled MOCK string in the P4 test and one docstring line |
| data files | none committed (no .env / .db / .jsonl / .gz / .log / .lock) |
| author emails | the GitHub noreply address only |
| size | 44 files changed, +5,924 / −98 lines |
| remote | `v2/venue-policy` at `b364f52` (verified earlier this phase); re-check `git ls-remote` immediately before any push |
| plan | normal push of `v2/venue-policy` only; no force, no main, no PR merge, no deploy |

## 7. Owner decisions pending

1. Push of the 11 local commits.
2. Switch `marketdata.TIME_BASIS` to `answer` (stricter, honest gates). Recommended
   together with the first approved live run.
3. Enable `NB_V2_SHARED_BUDGET=1` for any live run (recommended).
4. Approve revised L1 (about 740 requests, 3 h), then L4, L3, L2.
5. Whether an ESTIMATED entry should require PERSISTENCE_POSITIVE. Not proposed as a
   default; the classes are reported only.
6. An authorized TxFlow address, if account-level funding evidence is wanted.
