# Phase 11: live data readiness audit

**Base:** `f5c6f4c` · **Branch:** `v2/venue-policy` · **Date:** 2026-11-13 · **PAPER only**

No network test was run in this phase. Nothing was written to D:/E:, no recorder or engine was started, no order or account path was touched, and no merge or deploy was done. Push is waiting for the owner.

## 1. WebSocket loop (`v2/ws_readonly.py`)

| Finding (before) | Fix | Test |
|---|---|---|
| `step()` pinged only before a blocking `recv()`. With no incoming data it never pinged, and TxFlow closes a silent client after ~16 s. | `recv_or_tick()`: wait for the first byte of the next frame for at most `POLL_S` = 1 s; return `None` ("tick") if nothing came, then ping when due. Pings follow their own timer (TxFlow 10 s, HL 30 s, Lighter RH 60 s). | `test_ping_keeps_going_while_no_data_arrives` (35 s of silence → pings at 10/20/30 s, no reconnect) |
| `extended_rfq`'s `cancelled` wake-up can fire mid-frame | The wake-up happens only at a frame BOUNDARY (empty buffer); a started frame is read to its end; silence > 30 s → reconnect | `test_wake_up_only_at_a_frame_boundary` (a frame split by a timeout is returned whole) |
| An older TxFlow snapshot reset a newer book | An older snapshot is `stale` and ignored | `test_older_snapshot_never_replaces_a_newer_book` |
| — | Outgoing frames ≤ 5 per second | `test_outgoing_rate_limit` |
| Latency was presented as delay | `latency_s` is labelled APPARENT (receive − source, includes the clock offset); negative values are flagged; `latency_stats()` per venue | `test_latency_is_apparent_and_negative_values_are_flagged` |

Reconnect, resubscribe, sequence gaps, nonce continuity, duplicates, the allowlist (`post`, `sendtx`, `auth`, keys, account channels refused) and the closing of a socket after a refused frame were already covered in Phase 10.

**Clock:**
- The Windows Time service is **not running** (`w32tm` → service not started), so the local clock sync is UNKNOWN.
- 525 recorded REST answers (Phase 8 stream and Phase 9 probe) never showed a negative "receive − source" value; the minimum is 0.078 s.
- So the local clock is at most ~0.08 s AHEAD of the venues' clocks. How far BEHIND it could be is not measurable from these data: the REST round trip is ~1 s.
- Before the INIT 2 s check is trusted, start Windows Time (owner's machine setting) or measure the offset during the live probe.

## 2. Lighter Core scope

| | Lighter RH | Lighter Core |
|---|---|---|
| `exchange_id` | `lighter_rh` (legacy name "Lighter") | — (legacy "LighterCore") |
| REST | `https://api.rh.lighter.xyz/api/v1/` (`v2/venues/lighter_rh.py`) | `https://mainnet.zklighter.elliot.ai/api/v1/` (`marketdata/venues.py`, legacy) |
| WS | `wss://api.rh.lighter.xyz/stream` | `wss://mainnet.zklighter.elliot.ai/stream` |
| In the 8 PAPER venues (`v2/policy.EXCHANGE_IDS`) | yes | **no** |
| In the 11 observation venues (`v2/observation.OBSERVATION_UNIVERSE_V3`) | yes | **no** |

- **Why Core is excluded:** the 8 venues are the owner's closed list of 2026-11-12 (`v2/policy.py`). Lighter docs: "Lighter RH is a separate exchange: accounts, balances, API keys, nonces and account indexes don't carry over." Core is a different exchange that the owner did not list.
- **Status:** not changed; adding it is the owner's decision. Test: `test_lighter_core_is_not_in_either_list`.

## 3. TxFlow payment verification

- **`fetch_user_fundings`:**
  - pages newest-first by `offset` until `hasMore` is false;
  - the page size is clamped to 2000, and offset is capped at 200000 per the docs;
  - duplicates are dropped;
  - an overlapping or out-of-order page → INCONSISTENT; the page limit → TRUNCATED;
  - it still reads only the authorized address (none is set).
- **`parse_ws_user_funding`:** WS rows have no `amount`, `coin` may be an id, and empty strings stay `None`, never 0.
- **Status:** **ACCOUNT_PAYMENT_UNVERIFIED** (no authorized address, no real payment).
- **Tests:** `TxFlowPagingTests` (pages, overlap, duplicates, truncation, clamping, WS/REST fields).

## 4. PnL reporting (`tools/v2_policy_compare.py`)

The fields are now separate and named for what they are:
- `expected_net_at_entry`
- `entry_stress_estimate`: net_min AT ENTRY, a model output. It was called "worst_modelled" before and must not be read as a measured worst result.
- `realized_closed_sim`
- `open_mark_to_market`: valued only with prices that are fresh at the last observation.
- `open_unvalued`: a missing or stale price.
- `funding_by_evidence`, plus `funding_account_verified`.

**Correction to Phase 10:** Phase 10's "estimated net −2.93 / +5.50" valued open positions with the last price seen in ANY observation, which may be old. Same 109 observations, Phase 11 rules:

| NATGAS funding | STRICT_THRESHOLDS (default) | WORST_CASE_NONNEGATIVE |
|---|---|---|
| Signals / filled | 2 / 2 | 2 / 2 |
| Expected net at entry | 10.53 $ | 61.63 $ |
| Entry stress estimate (model) | −1.02 $ | +10.69 $ |
| Closed: simulated realized | −0.42 $ (1 closed, 0 reliable) | — (0 closed) |
| Open: mark-to-market (fresh prices) | 0 valued; **1 unvalued** (stale price at the end) | +7.59 $ (1 valued); **1 unvalued** |
| Funding: settled public rate / published / carried forward / account-verified | +3.82 / −0.17 / −1.15 / **0** | +3.61 / +0.05 / 0 / **0** |

Test: `test_names_and_stale_valuation`. The default policy is unchanged.

## 5. Short live read-only test: plan (NOT RUN; owner approval needed)

**Tool:** `tools/v2_ws_probe.py`.

**It refuses to start unless:**
- `NB_V2_READONLY_WS=1` is set, AND
- `--owner-approved` is given.

**Limits:**
- 300 s maximum, hard-capped in code;
- one thread and socket per venue (single owner);
- observation_v3 egress guard;
- every frame passes the allowlist;
- the output is one small JSON on C:.

| Venue | Subscriptions | REST comparison |
|---|---|---|
| TxFlow | `l2BookDepth` + `activeAssetCtx`, coin `119` (NATGAS) | `l2Book` 119 every 60 s |
| Hyperliquid / tradeXYZ | `l2Book` `INIT` and `xyz:NATGAS` (the xyz spelling is itself one of the things tested) | `l2Book` every 60 s |
| Lighter RH | `order_book/{BTC market id}` (looked up from the REST markets list) | `orderBookOrders` every 60 s |

**Measures:**
- connection success and subscription ack (HL ack, Lighter `subscribed/…`, TxFlow first frame);
- messages by status;
- source timestamps present;
- apparent latency (min/median/max/negatives);
- disconnects and reconnects;
- resyncs, gaps, stale frames and duplicates;
- REST vs WS agreement (only when ≤ 2 s apart);
- API errors and HTTP 429s.

**Command after approval:**

```
set NB_V2_READONLY_WS=1
python -m tools.v2_ws_probe --owner-approved --seconds 300 --out C:\Users\W10\nb-benchmark\v2-paper-data\ws-probe-YYYYMMDD
```

**Offline tests:** `ProbeTests`.
- It refuses without both switches.
- A MOCK run measures connections, resyncs, reconnects and source times.
- The 300 s cap holds.
- Every planned frame passes the allowlist.

## 6. Portability and data safety

- **`deploy/setup.sh`:**
  - **Ubuntu 24.04 ships Python 3.12**, and the old script would fall back to it and then fail.
  - It now requires 3.13 explicitly and explains deadsnakes / `uv`. It checks `venv`/`ensurepip` (`python3.13-venv`) and never uses sudo.
  - `.gitattributes` keeps `*.sh` LF.
- **Code audit for Linux:**
  - the Windows-only calls are guarded (`risk_main`: `os.name` check) or used only by a skipped DPAPI path (`connections.py`);
  - the drive-letter tests skip off Windows;
  - `v2/paths.py` defaults to `~/nb-benchmark` off Windows.
- **Not executed on Ubuntu:** no Linux machine, WSL or container was used. Status: static audit only.
- **Data pack:**
  - `python -m tools.v2_pack_data --verify` re-hashed all **44/44 files OK** (`test_real_pack_hashes`), and tampering is detected (`test_pack_and_verify_detects_tampering`).
  - **The 5.4 MB pack is NOT a backup of the 29.6 GB engine data set** (`paper.sqlite3` 26.5 GB + WAL 3.1 GB on the failing E:). That data set has no backup.

## 7. Acceptance table

- "REST LIVE": a public REST read on the real venue was parsed in Phases 4–9.
- "FUNDING VERIFIED": market-level settled public history read and parsed. NO venue has account-payment verification.
- "EXECUTION VERIFIED" would need real fills; PAPER has none, so it is no everywhere.

| Venue | Scope | REST READY | WS MOCK READY | LIVE VERIFIED (REST / WS) | FUNDING VERIFIED | EXECUTION VERIFIED |
|---|---|---|---|---|---|---|
| lighter_rh | 8 + 11 | ✅ | ✅ | ✅ / ❌ | ✅ market history (`/fundings`) | ❌ |
| risex | 8 + 11 | ✅ | ❌ (no WS adapter) | ✅ / ❌ | ✅ market history | ❌ (fee UNKNOWN) |
| arcus | 8 + 11 | ✅ | ❌ | ✅ / ❌ | ✅ market history | ❌ |
| txflow | 8 + 11 | ✅ | ✅ | ✅ / ❌ | ❌ rate published only; account ACCOUNT_PAYMENT_UNVERIFIED | ❌ |
| variational | 8 + 11 | ✅ (indicative quotes) | ❌ (no WS documented) | ✅ / — | ❌ rate published only | ❌ (no trading API) |
| tradexyz | 8 + 11 | ✅ | ✅ (xyz coin spelling UNVERIFIED) | ✅ / ❌ | ✅ market history | ❌ |
| popdex_perps | 8 + 11 | ❌ (no verified API) | ❌ | ❌ / ❌ | ❌ | ❌ |
| veranta | 8 only | ❌ (no verified API) | ❌ | ❌ / ❌ | ❌ | ❌ |
| hyperliquid_native | 11 only | ✅ | ✅ | ✅ / ❌ | ✅ market history (INIT 90 d) | ❌ (observation only) |
| entropy_io | 11 only | ⚠️ metadata + funding now read; book route allowed, not read | ❌ (not in the WS allowlist test) | ✅ partial / ❌ | ⚠️ route allowed, not read | ❌ |
| phoenix_perps | 11 only | ⚠️ exchange snapshot only | ❌ | ✅ metadata / ❌ | ❌ | ❌ |
| qfex | 11 only | ❌ (routes UNKNOWN) | ❌ | ❌ / ❌ | ❌ | ❌ |
| Lighter Core | out of scope | — | — | — | — | — |

**Result:** the code is ready for a short, controlled, read-only live check. Nothing is LIVE VERIFIED over WebSocket yet.

**What needs the owner:**
- approval of the 5-minute probe;
- Windows Time sync (or an offset measurement);
- an authorized TxFlow address;
- storage for any longer recording.

## 8. Approved actions on 2026-11-13

### 8.1 GitHub

- **Pre-push scan** of `f5c6f4c..61fd8fb`:
  - 10 files, the largest 24 KB;
  - no key, token, personal data, `.env`, database or archive;
  - author e-mail is the GitHub noreply address.
- **Pushed:** `61fd8fb` to `v2/venue-policy` only. `main` is unchanged (`e8a0e7b`); no merge or deploy.

### 8.2 Clock

- **Windows Time service:** `W32Time` is Stopped (StartType Manual), and this session is **not an administrator**, so the service was not started and the clock was **not synchronized**. Starting it needs the owner: an elevated `net start w32time` followed by `w32tm /resync`.
- **Offset measured without changing anything:** `w32tm /stripchart /computer:time.windows.com`:

  | When | Samples | Offset |
  |---|---|---|
  | 04:30 | 5 | +0.261 … +0.279 s |
  | 04:38 (after the probe) | 3 | +0.265 … +0.280 s |

  The offset is stable, and its direction is consistent with the 525 recorded REST answers (never a negative receive − source), so the **local clock is ~0.27 s BEHIND** NTP.
- **Timing labels:** latencies below are labelled *corrected* = apparent + 0.27 s.
  - This is a measured correction, not a synchronized clock.
  - The uncertainty is the NTP jitter (~±0.02 s) plus the venue's own clock (UNKNOWN).

### 8.3 Live read-only WebSocket probe

- **Run:** 04:31:07–04:36:47, 300 s, `tools/v2_ws_probe.py --owner-approved`, `NB_V2_READONLY_WS=1` for that process only.
- **Output:** `C:\Users\W10\nb-benchmark\v2-paper-data\ws-probe-20261113\ws_probe_report.json` (8.7 KB).
- **Guard:** 21 allowed, 0 refused.
- **Never happened:** no order, account or signature frame (0 refused frames), no write to D:/E:.

| Venue / market | Connected | Subscription accepted | Messages (300 s) | Interval (median / max) | Data age = corrected latency (median, range) | REST vs WS | Integrity | Reconnects / errors / 429 | Source timestamp |
|---|---|---|---|---|---|---|---|---|---|
| Hyperliquid INIT | ✅ | ✅ `subscriptionResponse` | 30 `l2Book` | 10.27 s / 10.51 s | 0.42 s (0.39–0.67) | ❌ not compared: REST read 5–8 s after the last WS push (> 2 s rule) | 0 stale / dup / resync (snapshot feed) | 0 / 0 / 0 | ✅ `time` (ms) |
| tradeXYZ NATGAS (`xyz:NATGAS` on the HL socket) | ✅ | ✅ (the spelling `xyz:NATGAS` works) | 30 `l2Book` | 10.27 s / 10.51 s | 0.42 s (0.39–0.78) | ❌ not compared (same reason) | 0 / 0 / 0 | 0 / 0 / 0 | ✅ |
| TxFlow NATGAS (119) `activeAssetCtx` | ✅ | ✅ (data arrived; TxFlow sends no ack by design) | 99 | 3.00 s / 4.41 s | 0.46 s (0.41–1.85) | — | — | 0 / 0 / 0 | ✅ `blockTime` |
| TxFlow NATGAS `l2BookDepth` | ✅ | ⚠️ frames arrived, but **not attributed to market 119** | ~215 (inferred: 314 records with a source time − 99 ctx) | ~1.4 s (inferred) | ~0.20 s corrected (apparent median −0.07 s) | ❌ not compared: WS book not found under "119" | ⚠️ 0 resync / 0 gaps reported, but attribution failed, so **not verified** | 0 / 0 / 0 | ✅ `time` |
| Lighter RH BTC (`order_book/1`) | ✅ | ✅ inferred: deltas are accepted only after a snapshot, and 0 resyncs occurred (the snapshot record itself was pushed out of the 5000-record buffer; fixed) | 5,339 | 0.051 s / 0.87 s (docs: batches every 50 ms) | not measured: the time unit of `last_updated_at` is NOT STATED | ✅ 4 CONSISTENT / 1 INCONSISTENT (0.96 bp vs tolerance 0.46 bp, 0.77 s apart, WS timed by receive time) | ✅ nonce continuity held for every delta (0 resync) | 0 / 0 / 0 | ❌ unit unknown |

**Bugs the live run exposed (fixed, offline-tested; NOT yet re-verified live):**
1. TxFlow `l2BookDepth` frames carry the instrument id in `id` and the name in `coin` (the REST book showed `coin: "NATGAS-USDC"`). Books are now keyed by `id` and the alias is recorded (`test_txflow_book_is_keyed_by_instrument_id_not_coin_name`).
2. Statistics were computed from the bounded 5000-record list, so the first snapshot was lost on Lighter. They now come from never-shrinking totals (`test_totals_survive_the_bounded_record_list`).
3. The REST side was timed by its receive time. It now uses the REST book's own `time` when the venue publishes it, so HL / TxFlow can be compared on the same book state (`test_rest_side_uses_its_own_source_time`).
4. The report now also carries 2 raw frames per channel, unattributed keys and coin aliases.

### 8.4 Freshness: WebSocket vs the earlier REST method

| | REST polling, 20 s (Phase 8 stream) | WS live (this probe) |
|---|---|---|
| Book age at receipt (HL INIT / xyz NATGAS) | 0.50 / 0.52 s apparent (~0.77 / 0.79 s corrected) | 0.42 s corrected |
| Update interval | 20 s (polling) | 10.3 s (HL push cadence observed for these two thin books; both pushed at the same block times) |
| Average age at a random decision moment | ≈ 0.8 + 10 = **~10.8 s** | ≈ 0.42 + 5.1 = **~5.6 s** |
| Worst age between updates | ~20.8 s | ~10.9 s |
| TxFlow NATGAS funding/mark (ctx) | 20 s polling | every 3.0 s, 0.46 s old |

- **NATGAS** (TxFlow / tradeXYZ, limit 36 s): already aligned with REST. WS roughly halves the average age of the tradeXYZ leg and gives TxFlow ctx every 3 s. The TxFlow WS book is not verified yet (bug 1).
- **INIT** (limit 2 s): **not solved.** The WS Hyperliquid leg is up to ~10.9 s old between pushes, and the binding leg is still Variational's REST indicative quote (age median 10–20 s, minimum 1.4–2.7 s; no WebSocket exists).

### 8.5 Verdict

| Venue | LIVE VERIFIED (WS) after this probe |
|---|---|
| Hyperliquid INIT | ✅ connection, subscription, data, source time; ❌ REST agreement not shown |
| tradeXYZ NATGAS | ✅ connection, subscription, data, source time; ❌ REST agreement not shown |
| TxFlow NATGAS | ✅ connection and ctx data; ⚠️ book data received but unattributed: **not verified** |
| Lighter RH BTC | ✅ connection, data, nonce integrity, REST agreement (4/5); ❌ source-time latency (unit unknown) |

A second ≤ 300 s probe would confirm fixes 1–3 live. It needs the owner's approval.
