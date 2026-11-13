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
