# Phase 10: data integrity, API coverage and PAPER strategy readiness

- **Base:** `1cdf958`, a clean tree.
- **Branch:** `v2/venue-policy`.
- **Date:** 2026-11-13.
- **Mode:** PAPER only.

Nothing in this phase traded, signed, authenticated or read an account. No write went to D: or E:, and `NB_V2_E_WRITE_ACK` was never set. No live WebSocket was opened, and no recorder was started.

The 8 STRICT PAPER venues, the 11 observation venues and the risk parameters are unchanged. `policy.py`, `network_policy.py`, `wide_paper.py`, `gates.py`, `hybrid_desk.py`, `observation.py`, `pilot/` and `extended_rfq.py` have no diff.

## Status

**ENGINE READY (offline) — DATA PIPELINE READY FOR OFFLINE/MOCK, NOT LIVE-VERIFIED — CONTINUOUS RECORDING PENDING STORAGE**

| Part | Status | Evidence |
|---|---|---|
| ESTIMATED PAPER engine | Ready offline: lifecycle, recovery, accounting layers, replay | Deterministic replays; trade recomputed line by line (Phase 9); tests |
| REST data pipeline | Ready (bounded, observation_v3) | Phase 8/9 streams and probe, hash-chained |
| WebSocket pipeline | MOCK-tested only; **not live-verified** | `v2/ws_readonly.py` + 9 MOCK tests; live use needs owner approval |
| TxFlow account funding | Contract implemented; **ACCOUNT_PAYMENT_UNVERIFIED** | No authorized address; fixture tests only |
| Continuous recording | **Pending storage** | E:/D: failing; C: has 18.9 GB free; no recorder started |
| Reliable closed PAPER trades | **0** | Needs hours of continuous data |

## 0. Environment checked first

- **git:** `1cdf958`, clean.
- **C::** TEAM T253LE240G SSD, "Healthy". 0 disk/NTFS error events for Disk 0 or C: in 14 days. 18.9 GB free.
- **Outputs:** all written to `C:\Users\W10\nb-benchmark\v2-paper-data` and the repo, about 40 MB in total. The data pack is 5.4 MB.

## 1. TxFlow funding

Source: official docs read 2026-11-13 (`docs.txflow.com/api/info/userFundings`, `/api/websocket/userFundings`).

- **REST contract:**
  - Request: `POST /info {"type":"userFundings","user":ADDRESS}`.
  - "No auth — the body `user` is the subject": no signature and no key, so the read is unsigned and read-only.
  - Pages are newest first; limit ≤ 2000; `offset`; `hasMore`.
  - "Served from an index — can briefly lag the latest block."
  - Perp markets only.
  - Row fields: `time` (ms), `coin`, `usdc`, `szi`, `fundingRate`, `amount`.
- **Units and signs:**

  | Field | WS page | REST page |
  |---|---|---|
  | `fundingRate` | "percentage form ("0.01" means 0.01%)" | NOT STATED |
  | `szi` | "Signed position size at settlement time" | — |
  | `usdc` | "Negative = paid, positive = received" | — |
  | `amount` | — | collected > 0 / paid < 0; equality with `usdc` NOT STATED |
  | `coin` (response) | WS example "4" | example "BTC"; the form is NOT STATED |

- **Mainnet host for this type:** NOT STATED (the page's example is testnet).
- **Adapter (`v2/venues/txflow.py`):**
  - Rows are tagged `scope=ACCOUNT` and `status=ACCOUNT_PAYMENT_UNVERIFIED`, with `rate=None`.
  - `verify_payments` marks the unit VERIFIED only when the payments themselves prove it:
    - `usdc == amount`;
    - −usdc / (szi × venue mark at that time) matches `fundingRate` in ONE unit for at least 3 rows.
  - `coin_map` / `resolve_coin` map a numeric id, a name or "BASE-QUOTE" to the V2 `market_id`. Ambiguous keys are never guessed.
  - `market_history_from_account` is **refused**: one account's payments are never the market's history.
  - Market hourly rates (`activeAssetCtx`, kind `observed_published`) and account payments (kind `account_payment`) are stored apart, as before.
- **WS `userFundings`:** allowed only for the owner-authorized address (`NB_TXFLOW_FUNDING_ADDRESS`), like the REST read.
- **Status: ACCOUNT_PAYMENT_UNVERIFIED.**
  - No address was provided, so no real account was read and none was invented.
  - Verification with real payments needs that address and the venue mark at each payment time.

## 2. Read-only WebSocket (`v2/ws_readonly.py`, OFF by default)

| Venue | Docs | URL | Allowed frames | Book semantics | Keep-alive |
|---|---|---|---|---|---|
| TxFlow | docs.txflow.com/api/websocket | wss://rpc.txflow.com/ws | subscribe/unsubscribe (top-level `type` required) to l2BookDepth, l2Book, activeAssetCtx, trades, bbo; ping. userFundings only for the authorized address. | l2BookDepth: snapshot + deltas, `seq`/`prevSeq`; gap → resync (resubscribe; procedure NOT STATED in docs) | ping every 10 s (gateway closes after ~16 s) |
| Hyperliquid + tradeXYZ | Hyperliquid websocket docs | wss://api.hyperliquid.xyz/ws | subscribe/unsubscribe to l2Book, activeAssetCtx, trades, bbo; ping. **`post` refused** (carries signed actions). | l2Book: snapshot feed, no sequence numbers; an older `time` is "stale", never merged; crossed → resync | ping every 30 s (server closes after 60 s) |
| Lighter RH | docs.lighter.xyz/api websocket | wss://api.rh.lighter.xyz/stream?readonly=true | subscribe/unsubscribe to order_book, market_stats, trade, ticker /N; ping. **`jsonapi/sendtx(batch)` and `auth` refused.** | snapshot, then deltas; `begin_nonce` must equal the previous `nonce`, else resync. Time unit of `last_updated_at` NOT STATED, so no latency is computed. | ping every 60 s (limit 2 min) |
| Lighter Core | docs | wss://mainnet.zklighter.elliot.ai/stream | none | — | Not in the 8/11 scope: no adapter |
| Variational | API docs | — | none | No WebSocket documented | — |
| Others (RISEx, Arcus, popdex, veranta, entropy_io, phoenix, qfex) | not re-read in Phase 10 | — | none | REST only; WS UNKNOWN | — |

Every client frame is checked before sending. Forbidden fields anywhere in a frame:
- `signature`, `action`, `nonce`, `vaultAddress`, `auth`;
- `tx_info`/`tx_type`;
- keys and secrets.

A refused frame raises and closes the socket.

Each message is recorded with its source time, receive time, latency, market, `seq`/`nonce`, and a status: ok, duplicate, stale, resync or ignored.
- Duplicates are dropped on (venue, market, time/seq).
- Reconnects use exponential back-off and resubscribe.
- Outgoing frames are rate-limited (5 per second).
- `compare()` checks a WS top-of-book against a REST book. It returns CONSISTENT, INCONSISTENT, or NOT_COMPARABLE when the two are more than 2 s apart (never across moments).

**Open questions, to verify only after approval with one short read-only probe:**
- The subscribe spelling for HIP-3 `xyz:` coins (NOT STATED).
- TxFlow `/ws` connection limits (NOT STATED).

## 3. Variational

Official API page, read 2026-11-13.

- **Resolved:**
  - indicative quotes are kept apart and the strict gate is unchanged (EXECUTION_MODEL_UNVERIFIED);
  - documented tiers only ($1k / $100k / $1m; `base`); sizes between tiers are unpriced;
  - quote age is measured from `updated_at` (docs: "may be cached for up to 600 seconds"; observed refresh about every 40 s);
  - 1 try on HTTP 429 (docs: 10/10 s per IP, 1000/min global, `retry_after_seconds`);
  - last look, firm gap and refusal are scenarios;
  - stale quotes are rejected (60 s) and are never execution-eligible.
- **Not solvable through the API:**
  - "The trading API is still in development, and is not yet available to any users", so a firm quote cannot be obtained and none is imitated.
  - No WebSocket is documented.
  - How long an RFQ answer stays firm: NOT STATED.
- **INIT vs Hyperliquid:** a simultaneous comparison exists (parallel reads + source times, Phase 9). Result below.

## 4. Two entry policies on the same observations

Inputs: 109 observations. Phase 8 set (07:36 aligned, three bursts, 90-cycle stream) plus the Phase 9 parallel probe. Command: `tools/v2_policy_compare.py`. Output: `C:\Users\W10\nb-benchmark\v2-paper-data\phase10-policy-compare\policy_compare.json`.

| NATGAS, ESTIMATED funding | A: STRICT_THRESHOLDS (default) | B: WORST_CASE_NONNEGATIVE |
|---|---|---|
| Signals / positions filled (entry success) | 2 / 2 (100 %) | 2 / 2 (100 %) |
| Simulated fills / verified | 6 / 0 | 4 / 0 |
| Closed (reliable) | 1 (0) | 0 (0) |
| Estimated net (realized + mark-to-market) | **−2.93 $** | **+5.50 $** |
| Worst modelled PnL of the entries (Σ net_min) | −1.02 $ | +10.69 $ |
| Max unhedged | 0 s | 0 s |
| Rejections for missing data | 527 | 796 |
| Evaluations where B would block A's choice | 2 | 16 |

- **NATGAS convergence:** 0 signals under both policies. 1,164 / 1,284 missing-data rejections, mostly PRICE_HISTORY_MISSING on Variational legs.
- **NATGAS hybrid:** 0 signals; first reason HYBRID_CONVERGENCE_UNAVAILABLE.
- **INIT (scenario):** 0 signals under both policies. Funding has 297 missing-data rejections (BETWEEN_TIERS_UNPRICED, STALE_QUOTE). B would block 4 scenario entries that time alignment already blocked.
- **Reading:** B enters later, on entries whose every modelled stress is non-negative (the 15:00 TxFlow→tradeXYZ $5k entry and the 14:55 TxFlow→Variational entry). A entered at 07:36 and lost on the 5.2 h data gap.
  - These are 2 entries per policy over about 8 hours of sparse data. **Not a statistically meaningful difference.**
  - The default stays A; changing it is the owner's decision.
- **Regression tests added** (`test_v2_phase10.RegressionTests`):
  - no look-ahead in funding bookings (a history holding future payments is cut at the observation);
  - the funding sign follows the rate across a flip;
  - a stale book gives no entry;
  - a payment seen in several observations is booked once.

  These are in addition to the existing look-ahead, hybrid cost-once and stale-quote tests.

## 5. Results summary

1. **Tests:** **1037 run, 1035 OK, 0 failed, 2 skipped** (the full suite, after all Phase 10 changes; the 2 skips are the existing platform/data ones). New in Phase 10: `test_v2_phase10.py`, 17 tests.
2. **TxFlow funding:** contract implemented and documented; status **ACCOUNT_PAYMENT_UNVERIFIED** (no authorized address).
3. **REST/WS per venue:**

   | Venue | REST | WS |
   |---|---|---|
   | TxFlow | REST | WS adapter (MOCK-tested) |
   | Hyperliquid, tradeXYZ | REST | WS adapter (MOCK-tested; xyz coin spelling UNVERIFIED) |
   | Lighter RH | REST | WS adapter (MOCK-tested) |
   | Variational | REST only | — |
   | RISEx, Arcus, popdex, veranta, entropy_io | REST | WS UNKNOWN / not built |
   | Phoenix, QFEX | blocked (doc 23–28) | — |

4. **Variational:** see §3.
5. **INIT / NATGAS:**
   - INIT: the 2 s alignment is never met (0/76 sequential, 0/7 parallel reads). The Variational quote age (minimum 1.4–2.7 s, median 10–20 s) blocks it, so INIT stays SCENARIO_ONLY.
   - NATGAS TxFlow/tradeXYZ: aligned (median skew 2.4 s parallel, limit 36 s). Its funding is assumption-dependent on the TxFlow leg. The Phase 8 trade recomputes exactly; the net band is [−3.14, −0.42] $.
6. **Entry policies:** §4.
7. **Technical blockers before 24/7 recording:**
   - a safe disk (E:/D: failing; C: 18.9 GB free; about 630 MB per day);
   - owner approval for the recorder and for live read-only WS;
   - an authorized TxFlow address for payment verification;
   - Variational firm quotes (no trading API);
   - one approved WS probe to verify the xyz coin spelling and TxFlow limits.
8. **Setup package:**
   - `deploy/README.md`, `deploy/setup.ps1`, `deploy/setup.sh` (Linux/macOS not verified);
   - `v2/paths.py`: data locations by environment variable;
   - `tools/v2_export_closes.py`: hourly closes as a 1 MB `json.gz` instead of the 5.3 GB DB; equality tested;
   - `tools/v2_pack_data.py`: data pack on C: at `C:\Users\W10\nb-benchmark\neutralbasis-data-pack-2026-11-13.zip`, 44 files, 22.8 MB of data, 5.4 MB zipped, with a sha256 manifest.
