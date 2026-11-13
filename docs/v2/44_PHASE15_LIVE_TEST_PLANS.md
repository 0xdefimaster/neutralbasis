# 44 — Phase 15 bounded live test plans (NOT RUN; each needs the owner's approval)

Every plan follows the same rules:
- PAPER ONLY; public GET / info reads only; no keys, no signing, no `/exchange`.
- Bounded duration; the process stops on its own; this is not the 24/7 recorder.
- Output on C: under the benchmark data folder only; D:/E: are refused.
- Gzip at the end; the run stops if C: free space falls below 5 GB.
- Before start: clock offset measured (`w32tm /stripchart`, read only; no change to
  the clock or the registry). Latency is reported as APPARENT unless the offset is
  within 0.05 s.
- Every answer keeps its request, receive and source times (`md-contract/2`).

Each plan answers one open item of docs/v2/43. Size estimates come from the Phase 8
stream: about 146 KB of plain JSON per cycle, mostly the full `metaAndAssetCtxs` and
Variational stats.

## L1. TxFlow whole-period funding behaviour (highest value)

| item | value |
|---|---|
| Question | Does TxFlow's running `nodeCtx.funding` reset or continue at every boundary? Is the end-of-period value stable across hours and markets? Does tradeXYZ converge to its settlement at every boundary, not just one? |
| Endpoints | TxFlow `POST rpc.txflow.com/info` activeAssetCtx (NATGAS, CL, XAU, BTC, ETH); HL `POST api.hyperliquid.xyz/info` metaAndAssetCtxs dex "xyz" and native; fundingHistory xyz:NATGAS and xyz:CL at start and end |
| Tool | `tools/v2_stream.py --observe --parallel` with a slim answer set (Variational and book reads off). A `--answers` switch has to be added and tested offline first. |
| Requests | per 20 s cycle: 5 TxFlow + 2 HL = 7; 3 h = 540 cycles = 3,780 requests, plus 4 history reads |
| Rate limits | TxFlow /info 50/s, budget 40/s: uses 0.25/s. HL 1,200 weight/min, budget 50 req/min: uses about 6 req/min (about 44 weight/min). |
| Duration | 3 h 05 min, starting 5 min before a whole hour, so 3 full periods and 4 boundaries are covered |
| Volume | about 25–35 MB plain, about 3–5 MB gzip (no Variational / books) |
| Location | `C:\Users\W10\nb-benchmark\v2-paper-data\l1-txflow-period-<date>\` |
| Safety | observation_v3 egress guard (allowlisted hosts and paths only); no WS; one try per read; a 429 pauses the venue |
| Success | 3 complete periods with at least 90 % of cycles answered per venue. Report: TxFlow value at -60 s / -10 s / +10 s / +60 s of each boundary; tradeXYZ convergence at each boundary |
| Stop | 3 consecutive failures on a venue (that venue is skipped for the rest); C: below 5 GB; any guard refusal (exit, report) |
| Still UNKNOWN afterwards | which TxFlow value is PAID (needs an authorized address or the formula) |

## L2. Hyperliquid l2Book `fast` mode (tool exists, never run)

| item | value |
|---|---|
| Question | Is the native INIT / xyz NATGAS book fresh enough for a 2 s gate, and does `fast: true` change the update interval? |
| Endpoints | `wss://api.hyperliquid.xyz/ws` l2Book INIT and xyz:NATGAS, two sockets (fast false / true); REST l2Book every 10 s |
| Tool | `tools/v2_hl_fast_probe.py` (refuses to start without NB_V2_READONLY_WS=1, --owner-approved and --clock-verified) |
| Requests | 2 sockets x 150 s; REST: 2 coins x 15 = 30 requests |
| Rate limits | HL WS: 2 connections / 4 subscriptions, far below limits; REST about 60 weight |
| Duration | 150 s (both sockets in parallel) |
| Volume | under 1 MB (one JSON summary) |
| Location | `...\v2-paper-data\hl-fast-probe-<date>\` |
| Success | interval median / P95 per mode and coin; share of seconds the book was 2 s old or less; REST vs WS agreement at the same book time |
| Stop | 150 s cap per mode; any integrity failure is recorded, not retried in a loop |

## L3. US regular-session snapshot

| item | value |
|---|---|
| Question | During US hours (RTH), how many RWA rows pass the session checks? This is the main cause of the ESTIMATED rejection RWA_SESSION_CLOSED (837 rows in the 05:12 snapshot). |
| Endpoints | the eight-venue collector (markets, books and settled history of the verified multi-venue rows only), as in the Phase 4 snapshot |
| Tool | `tools/v2_phase4_eval.py` record mode (one replayable `answers.json.gz`) |
| Requests | about 6 market lists + up to 200 books + up to 200 settled histories, inside each venue's budget (Lighter 50/min is the slowest: about 4–5 min) |
| Duration | one snapshot, 5–10 min, started between 15:00 and 20:00 UTC on a US trading day |
| Volume | about 1–2 MB gzip (the 05:12 snapshot was 0.64 MB) |
| Location | `...\v2-phase4-<date>-rth\` |
| Success | a replayable snapshot; the scanner rerun offline (`tools/v2_opportunity_scan.py`) |
| Stop | per-venue 429: pause as asked; a venue failing twice is left out and labelled |

## L4. Arcus / RISEx current-rate semantics

| item | value |
|---|---|
| Question | Is Arcus `fundingRate` / RISEx `current_funding_rate` a running value of the current period that converges to the settled rate (like tradeXYZ), a last settled value, or something else? Today both are PUBLISHED_SEMANTICS_UNVERIFIED. |
| Endpoints | Arcus `GET /v1/markets` and `/v1/fundingRates`; RISEx `GET /v1/markets` and `/markets/id/{id}/funding-rate-history` |
| Tool | new, small: samples the market lists every 60 s and reads the histories at start and end. It has to be written and MOCK-tested offline first. |
| Requests | Arcus 1/min (weight 20 of 1,500/min), RISEx 1/min; 2 h = 240 requests + about 8 history reads |
| Duration | 2 h 05 min (2 boundaries) |
| Volume | about 15–25 MB plain, about 2 MB gzip |
| Location | `...\v2-paper-data\l4-current-rate-<date>\` |
| Success | per market, the last value before each boundary vs the settled rate. A verdict is drawn only where at least 20 markets agree. |
| Stop | 2 consecutive failures per venue; C: below 5 GB |

## L5. TxFlow account payments (blocked)

`userFundings` needs an owner-authorized address; none is configured. No plan is
proposed until the owner names an address in writing (CLAUDE.md). Without it, TxFlow
and Variational payments stay UNKNOWN and their funding legs stay out of STRICT.

## Order proposed

1. L1: it settles the main P0 unknown.
2. L4.
3. L3.
4. L2.

The tool changes for L1 and L4 are offline work (MOCK-tested) and can be done before
any approval; running a plan needs the owner's approval of that plan.
