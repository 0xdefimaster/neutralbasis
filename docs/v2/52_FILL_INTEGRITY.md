# 52 — PAPER fill integrity: one price-quality layer for both engines

`v2/fill_guard.py` checks every simulated fill and every mark of BOTH paper engines:

- **ESTIMATED engine:** `v2.paper_engine.PaperEngine.sim_fill` (open, retry, unwind,
  close).
- **Desk** (the STRICT entry path under NB_V2): `wide_paper.WidePaper.fill` (open,
  tier adds), `unwind`, `close` and `mark_position`.

The 25 % bound from P6 is now only the **last defence**.

| check | rule | applies to |
|---|---|---|
| missing / empty book | refuse | all CLOB fills and marks |
| crossed / locked book | best bid ≥ best ask → refuse | all |
| stale book | `marketdata.stale` under the answer time basis; unknown or future time = stale | all CLOB (RFQ: quote age in `rfq_paper`) |
| spread | > 100 bp of mid → refuse ("not a price") | all |
| tick grid | best prices off the instrument tick → refuse; **unknown tick is reported, not assumed** (no adapter row carries a tick today) | where a tick is known |
| slippage | IOC limits unchanged (ESTIMATED 10 bp, desk FILL_TOLERANCE 0.1 %); walked exits / unwinds / marks > 100 bp from their own book mid → refuse | desk close / unwind |
| cross-leg | > 5 % from the OTHER leg's FRESH current mid (same underlying, per-unit). Catches a wrong scale, a wrong multiplier and a single stale leg, while both legs may move freely | all, when a fresh other-leg price exists |
| own reference | > 25 % from this leg's own entry / fill average; **only when no fresh cross-leg price exists** | last defence |
| no reference | neither price → refuse | all |
| partial fill | allowed; every part is checked | all |

When a fill is refused:
- It is "not filled": nothing is invented or clipped into range.
- The ESTIMATED engine leaves the position at risk and visible (PARTIAL /
  RECOVERY_PENDING).
- The desk's `close` checks every leg first. If one leg is refused, **nothing** is
  closed that round: closing the other leg alone would unhedge. The position goes to
  RECOVERY and is retried.
- A refused `unwind` raises `FillGuardRefused`, and the position goes to recovery.
- A refused mark keeps the last mark and sets `mark_status = UNRELIABLE: …`, so no
  fake unrealized PnL is shown.

## Real faults found in this audit (fixed)

1. **The desk's `close`, `unwind` and `mark_position` priced from the book with no
   check.** They walked any depth, with no staleness, crossed-book or scale test.
   This is the same class of fault P6 found in the ESTIMATED engine.
2. **The P6 own-entry 25 % bound was applied even with a fresh other-leg price.**
   After a legitimate move of more than 25 % over a long hold, the position could
   never be closed, so its loss would never have been realized (tested: +40 % on both
   legs now closes).

## Proof that every fill path is guarded (`test_v2_fill_integrity.py`)

- **AST check, ESTIMATED engine:**
  - `emit("fill")` exists only in `sim_fill`;
  - `fill_guard` is called before the first `emit` there;
  - `clob_ioc` / `rfq_fill` are used only in `sim_fill`.
- **AST check, desk:**
  - every function that books PnL (`credit`) other than funding settlement goes
    through the guard;
  - `ioc` is used only in `fill`;
  - `fill`, `unwind`, `close` and `mark_position` call the guard.
- **Behaviour (MOCK):**
  - a scaled, stale or crossed leg on close → nothing closed, no cash change;
  - a scaled unwind → refused;
  - an entry fill 10 % from the other leg → not filled;
  - a bad mark → last mark kept;
  - a 40 % legitimate move → closed.
- **Recorded data:** the five ESTIMATED replays are byte-identical with the new layer,
  with 0 refusals.

Full suite 1247 OK. The failure harness stays at 17/17. STRICT gates are unchanged;
the layer only refuses fills.
