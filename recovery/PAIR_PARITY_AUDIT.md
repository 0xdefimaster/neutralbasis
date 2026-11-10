# arb.sh Pair Parity Audit (14 reference pairs)

2026-11-10, measured 05:35–05:45 TRT · PAPER ONLY · no gate bypass.

**Sources:**

- `recovery/pair_verdicts.json`, from `pilot/pair_parity.py` (public data, engine endpoints, network guard);
- the pilot's `/api/candidates` (the whole list, not the top 40);
- `pilot/evidence_audit.py`;
- `pilot/candidate_totals.py`.

**How to read the table:**

| Column | Meaning |
|---|---|
| Historical | Funding + basis − 4 fees over 7 days, as a **range** over six consecutive window ends. Backward-looking, not a forecast |
| Forward | Funding contribution − costs at $1000, at the stated price source |
| Price-arb rows | Executable-side entry spread − round-trip cost: an **upper bound** that assumes the gap closes fully, because the "normal" gap is UNKNOWN |

## Per-pair verdicts

| Pair | Discovery | Funding evidence | Historical net (bp) | Forward 48 h / 168 h (bp) | Execution evidence | First blocker | PAPER |
|---|---|---|---|---|---|---|---|
| **AERO** Extended → RISEx | Both listed. **Engine: rank 5** (after the `46300a7` fix; before: not discoverable) | Extended and RISEx venue gates **open**; the RISEx **AERO market** has < 3 comparisons → PENDING_EVIDENCE | +45.6 … +73.9 | −7.7 / +42.2 (Extended **indicative**, not firm) | Extended RFQ: indicative not firm; real book ≈ −340 bp | RESEARCH: market-level funding evidence (RISEx AERO) | No |
| **AERO** Extended → Hyperliquid | Both listed. Engine: rank 12 | Both venue gates **open**; both markets passed → **deep-checked 05:30** | +45.3 … +62.5 | −13.8 / +27.8 (indicative) | Engine fills on the real RFQ book (conservative model) | **DEEP_CHECK: NET_EXPECTED_PNL_NEGATIVE, net −$44.06 < $0.50** | No |
| **SOL** RISEx → Extended | Both listed. Engine: rank 92 | Venue and market gates **open** → **deep-checked** | +7.8 … +35.1 | −9.5 / +8.5 | Order books, both legs (cost assumption) | **DEEP_CHECK: NET_EXPECTED_PNL_NEGATIVE, net −$1.57** | No |
| **XRP** RISEx → Extended (negative control) | Both listed. Engine: rank 138 | Venue gates open; the Extended **XRP market** has < 3 comparisons → PENDING_EVIDENCE | **−0.5 … +69.5** (BASIS_NOISY) | −10.2 / +5.3 | Order books | RESEARCH; then ECONOMICS: 48 h < 5 bp | No |
| **SOXL** Variational → Extended | Both listed (research layer). Engine: no Variational connector | Variational: no public history → **not verifiable** | UNKNOWN | UNKNOWN | Variational indicative quote; Extended SOXL is RFQ | **IDENTITY_UNVERIFIED** (Variational SOXL: default-rule only; Extended RWA) | No |
| **CL/WTI** Variational → Extended | Both listed (research). Variational CL = "WTI Crude Oil futures" (docs) | Not verifiable (Variational) | UNKNOWN | UNKNOWN | Variational indicative; Extended WTI order book | **IDENTITY_UNVERIFIED** (CL vs WTI-USD contracts not matched) | No |
| **CL/WTI** RISEx → Extended | Both listed. RISEx CL is commodity → research layer | — | +3.0 … +12.7 | −17.7 / −14.9 | Order books | **IDENTITY_UNVERIFIED** | No |
| **MSTR** Extended → RISEx | `MSTR_24_5-USD` vs RISEx MSTR (stocks), research layer | — | −28.7 … +17.4 | −16.6 / +0.2 | Order books | **IDENTITY_UNVERIFIED** (24/5 market vs a 24/7 perp) | No |
| **SKHYNIX** RISEx → Extended | Both listed (research; Extended RFQ) | — | −18.9 … +25.1 | −23.0 / −23.0 | RFQ indicative | **IDENTITY_UNVERIFIED** | No |
| **DRAM** RISEx → Variational (price) | Both listed (research). RISEx index ETF; Variational "Roundhill Memory ETF" | — | UNKNOWN (no Variational history) | Entry −16.8, cost 17.3 → **≤ −34.1** | Variational indicative | **IDENTITY_UNVERIFIED**; NO_ENGINE_CONNECTOR | No |
| **SOL** RISEx → Variational (price) | Both listed; crypto, symbol and price verified | — | UNKNOWN | Entry −0.4, cost 9.6 → **≤ −9.9** | Variational indicative | **NO_ENGINE_CONNECTOR** (trading API unavailable) | No |
| **BZ** Variational → RISEx (price, negative control) | Both listed (research) | — | UNKNOWN | Entry +4.5, cost 15.9 → **≤ −11.3** | Variational indicative | IDENTITY_UNVERIFIED; cost exceeds the entry spread | No |
| **VVV** RISEx → Variational (price, negative control) | Both listed | — | UNKNOWN | Entry −1.1, cost 19.8 → **≤ −20.9** | Variational indicative | NO_ENGINE_CONNECTOR; negative after cost | No |
| **XRP** RISEx → Variational (price) | Both listed | — | UNKNOWN | Entry −12.3, cost 13.0 → **≤ −25.3** | Variational indicative | NO_ENGINE_CONNECTOR; negative after cost | No |

**Notes on the table:**

- Both negative controls (BZ, VVV) come out negative after costs, as in arb.sh (−$11, −$48).
- DRAM and SOL, positive on arb.sh (+$7, +$2), are negative here at the executable side at 05:37.
- The price-arb snapshots move quickly. At 04:17, XRP was +6.0 and DRAM −10.4; at 05:37, XRP −12.6 and DRAM −39.2.
- arb.sh's "vs 7d avg" cannot be checked: Variational publishes no price history.

## arb.sh versus NeutralBasis: what differs

| Item | Finding |
|---|---|
| Spread / 8 h (m1) | Matches where the definitions match. AERO Ext→RISEx: 0.0896 % vs arb.sh 0.0826 %; the difference comes from hourly rate moves between snapshots |
| Historical net | Depends on the window end by up to 64 bp (AERO Ext→HL 45–65; XRP −0.5…+66) because the basis term uses unsynchronised 1 h closes. arb.sh's window, price source and fee tier are UNKNOWN |
| Forward | arb.sh shows none. Ours is funding-only (basis risk excluded) and credits only the weakest of 1/3/7-day realized carry |
| Execution | arb.sh's figures carry no execution evidence. Ours: order books (cost assumption), RFQ and Variational indicative (not firm) → RESEARCH_ONLY |

## Funnel totals (real data, pilot, 05:36)

| Metric | Count | Source |
|---|---|---|
| DEX markets listed | 1,840 listed / 1,597 active on the 6 venues counted (HL 465/349, Lighter Core 243/215, Lighter RH 71/71, Extended 419/339, RISEx 54/53, Variational 588/570); Paradex, TxFlow, Arcus counted only via the pilot | `discovery_inventory` |
| In the engine matrix (parsed, with a rate) | 937 (HL 179, Lighter Core 215, Lighter RH 24, Extended 339, RISEx 25, Paradex 66, TxFlow 15, Arcus 74) | pilot funding matrix |
| Research universe, incl. RWA / Variational | 1,746 cross-venue pairs; 877 with PAPER closed (RWA / Variational, identity unverified) | `discovery_inventory` |
| Funding candidates (engine) | 469: research 410 · rejected 47 · pending 12 · PAPER-executable 0 | `candidate_totals` |
| Convergence candidates (engine) | 499: research 439 · rejected 48 · pending 12 · PAPER-executable 0 | `candidate_totals` |
| Held by the identity gate | 68 funding / 71 convergence | `candidate_totals` |
| Deep checks (pilot, after the gates opened) | 114 examined by 05:35. Reference pairs deep-checked: AERO Ext→HL (−$44.06), SOL RISEx→Ext (−$1.57) | pilot `review.examined`, `/api/candidates` |
| Rejected, by first-decisive code (exclusive) | Funding: NET_EXPECTED_PNL_NEGATIVE 36, ORDERBOOK_DESYNC 9, INSUFFICIENT_LIQUIDITY 2. Convergence: PRICE_GAP_STATS_REJECT 40, INSUFFICIENT_HISTORY 5, NET_EXPECTED_PNL_NEGATIVE 2, ORDERBOOK_DESYNC 1 | `candidate_totals` |
| Rejected on economic or statistical grounds | 80 (36 + 2 + 40 + 2) | |
| Rejected for data reasons | 15 (desync 10, history 5) | |
| Risk approval | **0** | |
| PAPER positions opened | **0** | |
| PAPER positions closed | **0** | |
| Realized PAPER PnL | none | |
| UNKNOWN | Variational historical / forward (no public history); arb.sh methodology; RFQ executable price at our size | |

Each class count adds up to its total (research + rejected + pending + executable). `candidate_totals.py` asserts this.

## Status

| Item | Status |
|---|---|
| DISCOVERY_PARITY | **PARTIAL**: all 14 pairs are discovered and shown. Crypto pairs reach the engine; RWA and Variational pairs stay in the research layer by design |
| ECONOMIC_PARITY | **PARTIAL**: metrics are separated and sized. Variational has no history; arb.sh's method is UNKNOWN |
| FUNDING_EVIDENCE | **PARTIAL**: HL, Extended, Lighter RH and RISEx venue gates are open on real comparisons. Lighter Core is pending (2/3). Paradex, TxFlow, Arcus and Variational are blocked |
| RFQ_EXECUTION | **RESEARCH_ONLY** |
| PAPER_ENGINEERING | **PASS** (MOCK E2E, real gates) |
| REAL_DATA_PAPER_TRADES | **0** |
| ECONOMIC_VIABILITY | **UNPROVEN** |
| ZERO_TRADE_ROOT_CAUSE | **PARTIAL**: the pipeline now evaluates real candidates end to end. The zero is economic (net < 0 after costs) plus evidence still pending (market-level comparisons, Lighter Core, RFQ, Variational) |
