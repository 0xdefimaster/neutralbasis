# Opportunity Visibility: arb.sh pairs in NeutralBasis

2026-11-10 · PAPER ONLY · No gate bypass. Data:

- `pilot/opportunity_audit.py` run at 03:27 TRT on public data, using the engine's own endpoints inside the pilot network guard: `C:\Users\W10\nb-pilot\opportunity-audit.json`;
- live-engine funding matrix (read-only GET) and the 98-cycle capture;
- the pilot's pair view after the fixes (03:38).

Values are **per $1 notional (bp)**:

- **Historical 7d** is backward-looking (arb.sh-like): realized funding + price change − 4 taker fees. It is **not a forecast**.
- **Forward** is funding contribution minus assumed costs (fees, book cost at $1000, 7-day basis sd reserve), credited at the weakest of the 1/3/7-day realized carry. It is not position PnL; the basis move is a risk on top.

## P4 table

| Pair | On both feeds? | Normalized? | Historical net 7d | Forward net 24 / 48 / 72 / 168 h ($1000) | Scanner candidate? | First blocker | PAPER eligible? |
|---|---|---|---|---|---|---|---|
| **AERO** Extended → Hyperliquid | Yes (Extended **RFQ**; HL book) | Yes: price gap 19.9 bp; both hourly; positive rate = long pays | **+41.3 bp** (funding +56.9, price −1.6, fees −14.0) | Indicative Extended book (**not firm**): −21.3 / −13.4 / −5.5 / **+26.3**. Real RFQ book (not a guaranteed fill): −351 / −343 / −335 / −304 | **Yes.** Live: rank 1, 93/98 polls, deep-checked 9×. Pilot: rank 2 at 03:27, rank 60 at 03:38 after the 03:00 rate change | Live: RFQ_STREAM_NOT_SUBSCRIBED 4, ORDERBOOK_DESYNC 3, NET_EXPECTED_PNL_NEGATIVE 1, RFQ_STREAM_UNAVAILABLE 1. Pilot: funding-sign evidence 1/3 | **No.** RFQ execution model unverified; negative on the 48 h engine horizon |
| **AERO** Extended → RISEx | **Before: No.** RISEx AERO was dropped by the legacy 15-symbol filter. **After `46300a7`: Yes** | Yes: gap 7.8 bp; RISEx rate = last settled (projection) | **+0.1 bp** (funding +68.0, price −56.9, fees −11.0) | Indicative (not firm): −19.9 / −10.3 / −0.8 / **+37.4**. Real RFQ book: −350 / −340 / −331 / −292 | **Before: No. After: Yes**, pilot funding rank 3 of 333 (screen 28.7 bp, not a net), convergence rank 5 | DISCOVERY (fixed) → RISEx FUNDING_SIGN_UNVERIFIED → Extended RFQ EXECUTION_MODEL_UNVERIFIED | No |
| **SOL** RISEx → Extended | Yes | Yes: gap −3.3 bp | **+43.8 bp** (funding +24.8, price **+30.0**, fees −11.0) | −13.4 / −10.0 / −6.5 / **+7.3** | Live: rank 4, 88 polls, **research**. Pilot: excluded until the RISEx fee entry was mirrored (fresh database: "taker ücreti bilinmiyor"); now rank 51 of 333 | FUNDING_SIGN_UNVERIFIED (RISEx: "çelişkili"; last-settled projection, 66 % persistence) | No |
| **XRP** RISEx → Extended | Yes | Yes: gap −1.7 bp | **−23.6 bp** (funding +20.8, price −33.4, fees −11.0) | −13.0 / −10.1 / −7.3 / +4.2 (below the 5 bp minimum) | Live: rank 2, 87 polls, research. Pilot: rank 32 | FUNDING_SIGN_UNVERIFIED (RISEx) | No |
| **CL** Variational ↔ **WTI** Extended | Variational lists CL (`metadata/stats`), but there is **no connector**. Extended WTI: yes | **No.** CL ≠ WTI is not verified (contract, underlying, price source). Variational fields are undocumented | UNKNOWN | UNKNOWN | No (Variational is not scanned) | NOT_SUPPORTED (no connector) + not normalized | No |

Every listed pair stays in the table. Missing values are UNKNOWN with their reason.

## Why each pair was or wasn't visible

| Stage | Finding | Type | Fix |
|---|---|---|---|
| Discovery: RISEx (and Arcus) | Native feeds kept only the legacy desk's **15 configured symbols**. 16 of 25 active RISEx crypto markets were silently dropped, including AERO, XMR, ARB, ENA, TAO and ZEC. Live engine: 9 RISEx markets | **Software defect** | `46300a7`: full listed crypto universe from the same single call, per-market error isolation. Pilot after restart: 25 RISEx assets; AERO Ext→RISEx is a candidate |
| Discovery: TxFlow, Lighter RH | Same symbol list, but one request per market | Design limit (load) | Not changed; documented |
| Discovery: Variational | No connector; its only public endpoint has undocumented fields | Not supported | None (would be new integration work) |
| Ranking: fresh pilot | RISEx rows have no fee in a fresh database → excluded as "taker ücreti bilinmiyor" | Pilot configuration | `pilot_api mirror-fees`: the live engine's own owner-entered RISEx fee (0.03 %), labelled |
| Visibility: API | `/api/state` shows the top 40 of 250–333 candidates | UI limit | The pilot report states the count beyond 40; `/api/pair?asset=&long=&short=` shows any pair's rank and decision |
| Evidence | Lighter Core comparisons were overwritten after each hour | **Software defect** | `c49455f` |
| Economics: Extended RFQ | Indicative book 4–30 bp is not firm. The real RFQ book is 225–340 bp. Public trade prints ≈ +4.5 bp vs mark (small sample) | Missing execution evidence | None. RFQ stays RESEARCH_ONLY |
| Economics: horizon | The engine prices at its fixed 48 h horizon. Many pairs are positive only at 168 h | Policy, not a defect | Not changed (owner decision) |

**arb.sh versus NeutralBasis on AERO Ext→HL.**

| | Spread / 8 h | Historical net 7d per $10k |
|---|---|---|
| arb.sh (03:09) | +0.0516 % | +$14 |
| NeutralBasis (engine matrix, 03:21) | +0.0548 % | +$41 |

The method behind arb.sh's 7-day figure (fee tier, price source, window alignment) is **UNKNOWN**. Neither figure says anything about forward profit: with the real RFQ book this pair loses 300+ bp per round trip.

## P3: the XMR Lighter Core → Hyperliquid horizon question

Walk-forward over 16 days of real data, at every decision point (not only selected ones). Values are funding contribution minus costs: fees 14.6 bp + drag 3.9 bp.

| Horizon | Expected median | Realized median | Share > 0 | Realized vs credited carry (bp/h) |
|---|---|---|---|---|
| 24 h | −21.6 | −14.3 | 5 % | 0.18 vs 0.15 |
| 48 h | −18.2 | −5.8 | 23 % | 0.26 vs 0.15 |
| 72 h | −15.1 | −2.5 | 43 % | 0.22 vs 0.14 |
| 168 h | +0.1 | **+22.5** | 100 % | 0.24 vs 0.15 |

Where "48 h negative, 168 h conditionally positive" comes from:

1. **One-time costs.** 18.5 bp of fixed costs need roughly 75–120 h of carry at 0.15–0.25 bp/h to pay back.
2. **Conservative carry.** The weakest window under-credited the realized carry in this period. That is conservative by design.
3. **Basis risk.** The absolute basis move had a median of 7 bp and a 90th percentile of 15.5 bp, the same size as the edge.
4. **Overlap.** The 168 h result covers **39 heavily overlapping windows in 16 days**, which is only about 1–2 independent outcomes.

It is a **conditional** signal, not a proven opportunity. A 168 h horizon would be an entry-policy change and needs your decision. Nothing was changed.
