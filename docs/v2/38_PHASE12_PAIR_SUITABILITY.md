# Phase 12: clock, Hyperliquid fast mode, Variational freshness, pair suitability

**Base:** `74dc319` (pushed to `v2/venue-policy`; `main` untouched) · **Date:** 2026-11-13 · **PAPER only**

## 1. GitHub

- **Pushed (approved):** `300af53`, `7285a37`, `74dc319` to `v2/venue-policy`.
- **Checks before the push:**
  - secret / personal-data scan clean;
  - the commits contain code, tests and docs only;
  - no data file, account or `.env`;
  - full suite 1055 run, 0 failed, 2 skipped.
- **Not pushed:** later commits; they need a new approval.

## 2. Windows clock (owner action required)

`W32Time` is Stopped (Manual), and this session is not an administrator. Nothing was elevated.

**Commands for an elevated PowerShell** (Start → "PowerShell" → Run as administrator):

```powershell
# 1. start the time service now (and keep it starting with Windows)
Set-Service -Name W32Time -StartupType Automatic
Start-Service -Name W32Time

# 2. use the Windows NTP pool and sync
w32tm /config /manualpeerlist:"time.windows.com,0x9" /syncfromflags:manual /update
w32tm /resync /force

# 3. check
w32tm /query /source
w32tm /query /status
w32tm /stripchart /computer:time.windows.com /samples:5 /dataonly
```

**Expected result:**
- `/query /source` → `time.windows.com,0x9`;
- `/query /status` → a recent "Last Successful Sync Time";
- `/stripchart` offsets within about ±0.05 s.

**After you run them**, the session verifies with non-admin commands (`w32tm /query /status`, `/stripchart`) before any timing is called reliable.

**If you prefer not to leave the service on:** run step 1 without the `Set-Service` line. The clock will drift again once the service stops; it drifted ~0.02 s per 23 min, and the offset was ~0.27–0.30 s today.

## 3. Hyperliquid fast mode: prepared, NOT run (waiting for the clock)

- **Tool:** `tools/v2_hl_fast_probe.py`.
- **Requirements:** it refuses to start without `--owner-approved`, `--clock-verified` and `NB_V2_READONLY_WS=1`.
- **Design:**
  - two read-only sockets at the same time (`fast: false` and `fast: true`), 150 s each, so the total socket time is ≤ 300 s;
  - subscriptions: INIT and `xyz:NATGAS` l2Book;
  - REST l2Book every 10 s for both coins.
- **Allowlist change:** `fast` is accepted only as a boolean and only on Hyperliquid `l2Book` (test `FastModeAllowlistTests`).
- **Measures:**
  - push interval (median / P95 / max, from the venue's `time`);
  - data age sampled every second;
  - share of seconds ≤ 2 s old;
  - levels and USD depth per side (5 vs 20 levels);
  - stale / duplicate / crossed counts and reconnects;
  - REST − WS book time (positive = REST newer);
  - price agreement only on identical book times;
  - HTTP 429 and errors;
  - data loss (gaps > 3 × median).
- **Decision rule:** `fast: true` becomes the default only if it is measurably fresher AND its 5 levels keep the depth the strategies need ($1k–$10k per leg). Otherwise `fast: false` stays.

## 4. Variational: is a 2 s fresh price possible with the public API?

Tool: `tools/v2_variational_freshness.py`. Data: 105 recorded reads (Phase 8 stream, 20 s polling, and the Phase 9 parallel probe, 15 s), offset +0.27 s. Output: `C:\Users\W10\nb-benchmark\v2-paper-data\variational-freshness-20261113.json`.

| | INIT-PERP | NATGAS-PERP |
|---|---|---|
| Reads / distinct quotes / repeated (cached) | 83 / 48 / 35 | 83 / 49 / 34 |
| Quote update interval: median / P95 / max / min | 42.6 / 71.1 / 85.2 / 10.0 s | 39.0 / 62.1 / 111.2 / 10.1 s |
| Quote age at read: min / median / P95 / max | 1.69 / 19.97 / 50.8 / 56.5 s | 1.25 / 18.8 / 48.9 / 55.2 s |
| Reads with age ≤ 2 s | **1.2 %** | **3.6 %** |
| Best case, reading at every instant: share of time ≤ 2 s old | **4.6 %** | **5.0 %** |
| Reads ≤ 36 s (NATGAS limit) / ≤ 60 s (stale rule) | 81 % / 100 % | 83 % / 100 % |

- **HTTP 429:** 22 of 105 Variational reads (21 %). The global limit is shared with every client.
- **Docs:**
  - "may be cached for up to 600 seconds";
  - no WebSocket;
  - the trading API "is not yet available to any users", so there is no firm quote.

**Conclusion (evidence-based):**
- A Variational price that is ≤ 2 s old exists at most ~5 % of the time, even with perfect polling.
- Polling every second would hit the documented 10/10 s per-IP limit, on top of 21 % global-limit refusals.
- **2 s freshness is not achievable with the public API.**

**What stays as it is:**
- the 2 s floor (`opportunity_log.FLOOR_S`);
- `BLOCKED_EXECUTION_MODEL` / EXECUTION_MODEL_UNVERIFIED for Variational (test `GuardsThatMustNotMove`).

No stale price is shown as current: quotes older than 60 s are rejected; older than the pair limit → TIME_MISALIGNED.

**TxFlow `activeAssetCtx`:** its `funding` is a published market rate (RATE_PUBLISHED). It is never an account payment, which stays ACCOUNT_PAYMENT_UNVERIFIED (test `test_txflow_ctx_funding_is_never_an_account_payment`).

## 5. Which INIT / NATGAS pairs can produce simultaneous, reliable PAPER opportunities

Timing figures depend on the clock. Until §2 is done they are offset-corrected estimates.

| Pair (long / short either way) | Simultaneous data? | Execution model | Funding evidence | Reference | STRICT PAPER | ESTIMATED PAPER |
|---|---|---|---|---|---|---|
| **NATGAS TxFlow ↔ tradeXYZ** | ✅ TxFlow WS book ~0.9 s average age (verified live); tradeXYZ ~5.6 s (WS) or REST; limit 36 s | ✅ both CLOB (IOC on observed books) | ⚠️ tradeXYZ settled public history ✅; TxFlow published rate only (no account proof) | ❌ UNKNOWN (TxFlow undocumented) → basis risk | ❌ RWA_REFERENCE_UNVERIFIED, SETTLEMENT_EVIDENCE_MISSING | ✅ the **only pair suitable** for simultaneous ESTIMATED PAPER, with labelled assumptions (published TxFlow funding, reference basis risk) |
| NATGAS Variational ↔ tradeXYZ | ⚠️ quote ≤ 36 s old in 83 % of reads; quotes repeat (cache) | ❌ indicative RFQ only (no firm quote, no trading API); only the $1k tier priceable | ❌ Variational published only | ⚠️ ECONOMICALLY_RELATED | ❌ EXECUTION_MODEL_UNVERIFIED | ⚠️ $1k only, when the quote is fresh; never verified execution |
| NATGAS Variational ↔ TxFlow | ⚠️ same as above | ❌ (RFQ leg) | ❌ both published only | ❌ UNKNOWN | ❌ | ⚠️ $1k only; both funding legs assumption-dependent |
| **INIT Variational ↔ Hyperliquid** | ❌ 2 s gate met in ≤ 5 % of time (Variational); HL push ~10.3 s (fast mode untested) | ❌ RFQ leg; HL is observation-only | ⚠️ HL settled ✅; Variational published only | n/a (crypto) | ❌ CONFIG_EXCLUDED + EXECUTION_MODEL_UNVERIFIED | ❌ SCENARIO_ONLY; **not suitable** |

**Bottom line:**
- Only **NATGAS TxFlow ↔ tradeXYZ** has simultaneous, verified-live data on both legs and a real order-book execution model.
- Even that pair is ESTIMATED, not STRICT: the reference is UNKNOWN, and TxFlow funding has no settlement or account evidence.
- **INIT is not suitable** with public APIs.

## 6. Still open

| # | Item | Waiting for |
|---|---|---|
| 1 | Clock sync | Owner runs §2 as admin |
| 2 | Hyperliquid fast-mode test | After 1; approved, ≤ 300 s |
| 3 | TxFlow account funding proof | An authorized address |
| 4 | NATGAS reference spec on TxFlow | Venue documentation |
| 5 | Variational firm quotes | Trading API not available |
