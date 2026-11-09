# Execution Evidence

Two strictly separate parts: (A) a **MOCK** controlled lifecycle test, never counted
as a market result; (B) **real public data**. PAPER ONLY; no live orders in either.

## A. MOCK TEST — controlled full lifecycle (not a market result)

Test: `test_paper_lifecycle_e2e.PaperLifecycleE2E.test_entry_management_exit_and_net_pnl`
(PASS). Venue data is synthetic (flat books 99.99 / 100.01, constant funding). It runs
the real `WidePaper` decision, fill, settlement and close code. **No gate is
bypassed**: the real `capabilities.pair_blockers` runs, and the two venues pass it only
through evidence recorded by the registry's own observation methods.

| Step | Proven by assertion | MOCK value |
|---|---|---|
| Candidate | Not research; evidence gate passed naturally | HYPE-like BTC pair, Hyperliquid (hourly) ↔ Lighter Core (8 h) |
| Economic decision | Expected net ≥ wide_min_net ($0.50) | +$2.10 for $1000 (4.8 conservative funding − 1.90 fees − 0.80 drag) |
| Risk | Size within margin and loss budget | $1000; leverage cap |
| PAPER entry | Both legs; equal base qty (delta neutral); VWAP + 1 bp extra slippage | 10.0 long HL @ 100.020001; 10.0 short LC @ 99.980001 |
| Entry fees | From fill prices × each leg's fee | $0.9500 |
| Management | Funding per venue schedule (8 hourly + one 8 h payment) | +$1.6000 |
| No double count | Repeating the cycle leaves funding unchanged | $1.6000 |
| Restart | New desk on the same store: same position id, not reopened, funding not re-paid | 1 position |
| Exit | Carry flip persisting > 30 min → close both legs at the book | "Funding avantajı tersine döndü" |
| Exit fills | Long sold at bid − slippage; short bought at ask + slippage | 99.980001 / 100.020001 |
| PnL | net = price PnL + funding − all fees | −$0.8000 + $1.6000 − $1.9000 = **−$1.1000** |
| Ledger | Wallet cash change = net | −$1.1000 (difference < 1e-11) |

The MOCK trade lost money: an exit after 8 h instead of the planned 48 h paid the
spread twice for only 8 h of funding. That is the realistic behaviour the chain must
show; it proves the accounting, not profitability.

Other lifecycle cases already covered by existing tests (MOCK):

| Case | Test |
|---|---|
| One-leg / partial fills | `test_entry_without_any_fill_is_not_a_trade`, `test_a_close_never_leaves_one_leg_alone`, `test_thin_books_close_the_matched_part_on_both_legs`, `test_partial_fill_below_a_venue_minimum_is_no_fill` |
| Stale book, thin book, unknown fee | `test_entry_refuses_a_stale_book`, `test_entry_refuses_a_book_too_thin_for_the_order`, `test_entry_refuses_an_unknown_fee` |
| Unverified payment is no hidden gain | `test_unpublished_payment_recorded_unverified` |
| Convergence entry and close | `test_convergence_opens_long_cheap_short_dear_5x`, `test_convergence_closes_when_prices_cross` |

The evidence file is `C:\Users\W10\nb-pilot\e2e-mock-evidence.json`, labelled MOCK.

## B. Real public data

### Real-data PAPER positions: **0**. Closed: **0**. Net real-data PAPER PnL: **none (no position)**.

| Source | What was evaluated | Result |
|---|---|---|
| Live engine (fceaf96), read-only capture 2026-11-10 00:16–02:26 | 98 cycles; 122 funding + 203 convergence unique candidates; 728 deep-check records | 0 eligible. First codes: NOT_SUBSCRIBED 455, PRICE_GAP_STATS 145, NET_NEGATIVE 50, DESYNC 41, HISTORY 19, RFQ_UNAVAILABLE 17, LIQUIDITY 4 |
| Offline walk-forward (real 30-day series, 4 venues) | 27,938 decision points; 458 oriented pairs | Category 1: 0 · Category 2: 22 · Category 3: 436 (funding contribution − assumed costs) |
| Isolated pilot (worktree, from 02:35) | 8 perp DEX venues; Lighter Core 56 markets, Lighter RH 24, HL 109, Extended 339 | Evidence accrual phase: every pair is research until ≥ 3 settled comparisons per market. Deep checks 0 at the latest report (below) |

Why there is no real position, per gate:

1. **Live engine:** only Extended↔Hyperliquid pairs reach the deep check. Lighter Core and Lighter RH are blocked by software defects (fixed in the worktree, not deployed). The Extended↔HL pairs fail on economics: RFQ spread, fees > funding, or below the convergence tier.
2. **Offline audit:** no pair meets the supported rule under engine fees and reserve.
3. **Pilot:** too early. A fresh database must observe settlements before the sign gate opens. This is the gate working as designed, not a defect.

### Pilot snapshot (2026-11-10 02:45:17 TRT; pilot started 02:35)

`C:\Users\W10\nb-pilot\data\reports\pilot-report-20261110-024517.json`:

| Item | Value |
|---|---|
| Rounds | 12 |
| Markets / usable / assets / pairs | 590 / 501 / 101 / 213 |
| Excluded markets | no price 50, taker fee unknown 24, last-trade price only 15 |
| Funding candidates | 208 (18 positive screen; all research) |
| Convergence candidates | 212 (50 positive; all research) |
| Research reasons | funding sign not verified; published-vs-settled comparisons insufficient; settled payments insufficient; no 7-day record |
| Deep checks / eligible / opened / closed | 0 / 0 / 0 / 0 |
| Guard refusals | 0 |

Next check: after about 06:00 TRT, run `pilot_api report`. No result is claimed before then.
