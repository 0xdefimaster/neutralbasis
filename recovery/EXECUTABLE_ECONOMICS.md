# Executable Economics

2026-11-10 · PAPER ONLY. Measured on real public data. Per-pair numbers:
`recovery/pair_verdicts.json`; universe audit:
`E:\NeutralBasis-Handoff\reports\executable-economics-audit.md`.

## 1. Metric definitions (never compared across definitions)

| # | Metric | Definition | Where |
|---|---|---|---|
| m1 | Current spread / 8 h | (short − long) current rate × 8, with each leg's semantics | `funding_current` |
| m2 | Realized funding 7 d | Settled payments over 168 h, both legs | `funding_realized_7d` |
| m3 | Historical funding net | m2 − 4 taker fees − round-trip book cost at the size | `by_size[*].historical_funding_net` |
| m4 | Historical total (arb.sh-like) | m2 + basis change from 1 h trade closes − 4 taker fees | `historical_total_estimate` |
| m5 | Historical total at size | m4 − round-trip book cost | `by_size[*].historical_total_at_size` |
| m6 | Executable cost | 4 fees + round-trip book / quote cost at the size | `by_size[*].executable_cost` |
| m7 | Forward | Funding contribution − costs (1/3/7 d agreement, weakest window, 7 d basis-sd reserve) | `forward_24h` … `forward_168h` |
| m8 | Convergence | Entry spread vs normal gap; needs synchronous executable history | `convergence` (UNKNOWN where a leg has no history) |
| m9 | Full position PnL | Not modelled | UNKNOWN |

## 2. The arb.sh "historical net" is window-sensitive

The basis term uses 1-hour trade closes that are not synchronized across venues. Moving the 7-day window's end by 0–5 hours changes m4 by up to:

| Pair | m4 range (bp) | Flag |
|---|---|---|
| AERO Ext → HL | 38.7 – 67.2 | BASIS_NOISY |
| CL/WTI RISEx → Ext | 3.0 – 54.9 | BASIS_NOISY |
| SKHYNIX RISEx → Ext | 17.0 – 113.4 | BASIS_NOISY |
| XRP RISEx → Ext | −28.0 – +5.2 | BASIS_NOISY |
| SOL RISEx → Ext | 7.8 – 13.2 | stable |
| AERO Ext → RISEx | 55.5 – 72.4 | stable |

arb.sh's exact window, price source and fee tier are UNKNOWN. Its single figure and ours agree only within this range, and neither is forward evidence.

## 3. Extended RFQ: three evidence levels, measured together

10 minutes on 4 RFQ markets (`rfq-where-fills.json`).

| Market | Real RFQ book spread | Indicative spread | Trades | Inside the real spread | Print vs indicative mid |
|---|---|---|---|---|---|
| AERO | 234 bp | 3.1 bp | 5 (≈ $22 each) | 5/5 | −4.5 … −12.8 bp (buys) |
| SOXL | 241 bp | 3.6 bp | 1 ($1,498) | 1/1 | +12.4 bp (buy) |
| VVV | 33 bp | 3.2 bp | 2 (≈ $190) | 2/2 | −8.3, −10.7 bp (sells) |
| DRAM | — | 2.9 bp | 0 | — | — |

Checks, per your list:

| Check | Finding |
|---|---|
| Fill location | All 8 prints are inside the real book's spread: fills are market-maker pickups of the quote walk, not resting levels |
| Side | `S` = taker side (docs) |
| Units | Same base-quantity format as the order-book stream (docs) |
| Timestamp | Matched to the book at the trade's own exchange time |
| Quote freshness | Indicative book polled every 15 s, so it is up to 15 s stale. That is enough to explain buys below the mid |
| Sizes | Tiny ($22–$1,498) |
| RFQ flag | No field marks a print as an RFQ fill |
| Firm quotes | Need an account (order submission) |

**Model consequence.**

- Real-book VWAP (225–340 bp) is **not** the execution cost. The engine's RFQ PAPER fill model is therefore very conservative, which is safe but not representative.
- The indicative book and public prints suggest ±15 bp at small sizes. They are **not** evidence for our fills or sizes.
- **RFQ PAPER execution: RESEARCH_ONLY. The gate stays closed; no model change.**

## 4. Variational Omni

Source: official docs, `GET /metadata/v2/stats`.

- Trading fee: 0 %. The cost is the quoted spread.
- Quotes are indicative at the documented sizes and cached up to 600 s.
- Execution is RFQ against the OLP with last look; the trading API is unavailable.
- Funding history and price history are not public.

Consequence: Variational pairs get current funding (`CURRENT_INDICATIVE`) and indicative costs only. Historical and forward funding are UNKNOWN, and PAPER is **closed**.

## 5. Size dependence ($100 / 500 / 1,000 / 5,000 / 10,000)

Every pair in `pair_verdicts.json` carries `by_size` per execution-price source.

- HL books (20 levels via ccxt) often cannot fill $10,000 → `INSUFFICIENT_LIQUIDITY`.
- Real Extended RFQ books cannot fill $5,000.
- A positive value at $10,000 says nothing about $1,000, and the reverse holds too.

## 6. Fee sources

| Venue | Taker fee | Source |
|---|---|---|
| Extended | 0.025 % | docs |
| Hyperliquid | 0.045 % | docs, base tier |
| Lighter Core | 0.028 % | docs, Premium; account type unverified; Standard 0 % never assumed |
| Lighter RH | 0.035 % | docs, Premium |
| RISEx | 0.03 % | owner entry in the live engine; **no venue document** |
| Variational | 0 % | docs; spread cost |

## 7. Shadow vs screen ranking: what the test can and cannot show

Walk-forward test with decision times 48 h apart: top 10 by each ranking, funding contribution − costs.

| Costs | Ranking | 48 h median | ≥ 5 bp | 168 h median | ≥ 5 bp |
|---|---|---|---|---|---|
| Today's book (assumption) | screen | −42.8 bp | 0.0 % | −43.6 bp | 4 % |
| | shadow | −11.3 bp | 3.7 % | −5.1 bp | 16 % |
| Fees only | screen | −14.4 bp | 8.7 % | −13.8 bp | 26 % |
| | shadow | −8.8 bp | 7.5 % | −0.9 bp | 30 % |

- Most of the shadow ranking's advantage comes from the **book-cost assumption**: the screen score favours illiquid pairs. That cost is today's book, **not historical execution evidence**.
- With fees only, the difference at 48 h is small.
- There are 8 independent decision times; the cross-section within a time is correlated; 168 h windows overlap (about 2 independent weeks).

**Conclusion: inconclusive.** A real test needs order books recorded in real time at each decision. PAPER decision policy is unchanged.
