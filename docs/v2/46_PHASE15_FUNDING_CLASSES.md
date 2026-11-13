# 46 — Funding opportunity classes: old vs new on the same data

The classes are reported, never used as an entry gate. ESTIMATED and STRICT decisions
are unchanged, and a test checks that their code does not read the classes.

| class | meaning (cumulative) |
|---|---|
| DISCOVERED | a funding or price edge was observed |
| BASELINE_POSITIVE | net > 0 with the published rate held for the horizon (the old rule) |
| PERSISTENCE_POSITIVE | net > 0 under the persistence-adjusted model |
| ADVERSE_POSITIVE | net > 0 under the adverse model |
| INSUFFICIENT_HISTORY | persistence or adverse could not be evaluated (UNKNOWN, never 0) |
| PAPER_ENTRY_READINESS | separate: STRICT_ELIGIBLE / ESTIMATED_CANDIDATE / NOT_READY |

The profit label is never VERIFIED:
- **ESTIMATED** when both legs have public settled history;
- **ESTIMATED_SETTLEMENT_UNVERIFIED** when a leg is TxFlow or Variational.

## Results (`tools/v2_funding_class_compare.py`, `v2-paper-data/phase15-funding-classes/`, 0 network attempts)

**A. ESTIMATED replay entries** (7 unique entries; the old rule entered all of them)

| signal (UTC+3) | pair | old net | baseline / persistence / adverse | new highest | outcome |
|---|---|---|---|---|---|
| 14:44:09 | Variational long / TxFlow short, $1k | +3.01 | +3.01 / −0.64 / −1.75 | BASELINE_POSITIVE | UNKNOWN |
| 14:55:17 | TxFlow long / Variational short, $1k | +5.26 | +5.26 / −4.03 / −7.95 | BASELINE_POSITIVE | UNKNOWN |
| 14:57:12 | TxFlow long / tradeXYZ short, $1k | +5.23 | +5.23 / −11.17 / −12.38 | BASELINE_POSITIVE | UNKNOWN |
| 15:00:14 | TxFlow long / tradeXYZ short, $5k | +56.37 | +56.37 / −36.88 / −51.70 | BASELINE_POSITIVE | UNKNOWN |
| 15:00:14 | TxFlow long / Variational short, $1k | +11.00 | +11.00 / −2.80 / −10.11 | BASELINE_POSITIVE | UNKNOWN |
| 07:36:28 (x2) | TxFlow long vs tradeXYZ / Variational | — | not re-evaluated | — | SIGNAL_OUTSIDE_RECORDED_STREAM |

- Every re-evaluated entry rests on the baseline alone. None reaches
  PERSISTENCE_POSITIVE.
- All of them carry ESTIMATED_SETTLEMENT_UNVERIFIED: each has a TxFlow or Variational
  leg, and what those venues pay is not observable without an authorized address.
- So whether a signal was a FALSE POSITIVE is UNKNOWN. It is never reported as a
  realized profit or loss.

**B. Stream cycles** (90, NATGAS, $1k, 24 h carry cash before costs)

| direction | baseline > 0 | persistence > 0 | adverse > 0 | baseline only |
|---|---|---|---|---|
| TxFlow long → tradeXYZ short | 53 | 18 | 0 | 35 |
| TxFlow long → Variational short | 53 | 37 | 0 | 16 |
| Variational long → tradeXYZ short | 28 | 0 | 1 | 28 |
| Variational long → TxFlow short | 36 | 52 | 38 | 0 |
| tradeXYZ long → TxFlow short | 37 | 72 | 0 | 0 |
| tradeXYZ long → Variational short | 61 | 89 | 0 | 0 |

Here persistence can be MORE positive than the baseline. That happens when the
published value sits at a temporary extreme and the samples revert. These counts are
before costs; the entry table above is after costs.

**C. 05:12 scanner snapshot.**
- Rows: 52 discovered; 2 BASELINE_POSITIVE; 0 PERSISTENCE_POSITIVE; 0
  ADVERSE_POSITIVE; 46 INSUFFICIENT_HISTORY.
- The 46 have one leg without settled history and without recorded samples.
- Profit label: 46 ESTIMATED_SETTLEMENT_UNVERIFIED, 6 ESTIMATED.

## Why UNKNOWN stays UNKNOWN

- A TxFlow or Variational leg has no public settled history. Its persistence can only
  come from published samples, and a single snapshot has none.
- Recording whole funding periods (live plan L1) would supply samples. It would still
  not prove what is PAID.
