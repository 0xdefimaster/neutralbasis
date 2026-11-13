# Phase 14, Slice A: architecture decision record and gap matrix

**Date:** 2026-11-13 · **Mode:** PAPER only · **Branch:** `v2/venue-policy`

## 0. Verified state (not taken on trust)

| Claim in the directive | Check | Result |
|---|---|---|
| Branch `v2/venue-policy` | `git rev-parse --abbrev-ref HEAD` | ✅ |
| Last local commit `1143125`; earlier local `c9bf8f3`, `c7f7e51` | `git log` | ✅ (`1143125`, `c7f7e51`, `c9bf8f3` on top of `74dc319`) |
| GitHub at `74dc319` | `git ls-remote github` | ✅ `74dc31960cd9…` |
| Tree clean | `git status` | ✅ before Slice A |
| 1076 tests, 0 failed, 2 skipped | last full run | ✅; after Slice A: **1084 run, 0 failed, 2 skipped** |
| D:/E: not a write target; C: limited | `v2/storage.py` guard | ✅ enforced in code |

## 1. Architecture map (what exists and how it connects)

```
venue adapters (v2/venues/*)  ──►  v2/source.py (catalog → identity check → strategy rows)
        │                                  │  rows only for VERIFIED multi-venue groups,
        │                                  │  tier A, multiplier normalized (v2/normalize)
        ▼                                  ▼
v2/collector.py (REST reads, budgets)   v2/identity.py (Instrument, underlying_id)
v2/marketdata.py (books, SeqBook)       v2/references.py (RWA reference equivalence)
v2/ws_readonly.py (read-only WS, OFF)   v2/observation.py (8 PAPER / 11 OBSERVE, guard)
        │
        ▼
v2/gates.py  STRICT gate (unchanged)  ──►  v2/funnel.py  trace / outcome / radar
v2/estimated.py  ESTIMATED evaluator  ──►  v2/opportunity.py  Phase 13 label (a VIEW)
v2/strategies/{funding,convergence,hybrid}.py  (independent evaluators)
v2/economics.py, v2/funding_math.py, v2/funding_scenarios.py, v2/funding_evidence.py
        │
        ▼
v2/paper_engine.py + v2/paper_ledger.py (ESTIMATED positions, event-sourced, C: only)
v2/hybrid_desk.py (strict desk; untouched)
        │
tools/: v2_opportunity_scan, v2_natgas_history, v2_pair_snapshot, v2_stream,
        v2_ws_probe, v2_hl_fast_probe, v2_variational_freshness, v2_verify_trade, ...
UI today: v2/dashboard.py (static HTML render of a Runtime snapshot);
          bot.serve (LEGACY engine server: GET + POST, incl. engine start/pause,
          config and API-key endpoints)
```

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Keep** funnel, estimated, gates, identity, references, observation, paper engine/ledger, storage, ws_readonly, funding_math/evidence/scenarios as the single sources. Doctor and Control Center READ them; they never re-implement them. | The directive forbids duplicates. Each already has tests and evidence. |
| D2 | **Do not extend `bot.serve`** for the Control Center. Build a separate **GET-only** server (`v2/control/`) bound to `127.0.0.1`, with no POST handler at all. | `bot.serve` carries engine start/pause, config writes and API-key storage (`/api/connections/extended-key`). A read-only panel must not share a process or handler with them. |
| D3 | **One read model** (`v2/control/readmodel.py`, Slice C/E) that turns stored artifacts (scanner report, ledgers, probe reports, stream manifests, doctor records) into small JSON. GET requests read only this cache, never a venue. | Hard rule: refreshing the dashboard must not create venue traffic. |
| D4 | **Doctor** = `tools/nb_doctor.py` + `v2/doctor/` checks. `--offline` imports nothing that opens a socket and runs under a socket guard that fails the run on any connect attempt. `--live` stays refused unless explicitly approved (same pattern as `v2_ws_probe`). | Proof that offline means offline. |
| D5 | **Health model:** 7 independent dimensions × 10 statuses per (exchange, market, channel). Unknown → NOT_TESTED / UNVERIFIED, never HEALTHY. | Directive §4. A single HEALTHY flag hides which part failed. |
| D6 | **Frontend:** server-rendered HTML + a small vanilla JS fetch layer; Tailwind compiled locally to one static CSS file; no CDN at runtime; no SPA framework. | Lightest stack. Node 24.14 / npx 11.9 are present. **Installing the pinned Tailwind CLI from npm is a network download and a new dev dependency: it needs the owner's OK** (Slice F). Until then the panel ships with the existing hand-written CSS. |
| D7 | **Platform-neutral paths** via `v2/paths.py` and `NB_V2_*` variables; nothing Windows-specific in core logic. | Linux move (Phase 10/11 work). |
| D8 | Mobile remote access (Tailscale Serve etc.) only designed, not enabled. | Directive §11. |

## 3. Phase 13 independent audit (directive §2): findings and fixes

| Area | Finding | Severity | Fix / evidence |
|---|---|---|---|
| Scanner counts (§2.1) | The report mixed scope numbers; the directive's distinct counts were missing | Medium | `report()["counts"]`: unique instruments 399, unique reference assets 117, unique venue pairs 10, strategy-direction rows 3,174, raw positive 1,290, expected positive 2, stress positive 0, ESTIMATED candidates 2, simulated positions `null` (scanner never simulates; ledger is separate), STRICT 0; snapshot time kept (`at`) |
| Identity (§2.2) | The scanner grouped by the ticker string. Rows only exist for VERIFIED multi-venue groups (`v2/source.build`), and the 05:12 data had 0 ticker→underlying conflicts and a correctly normalized 1000PEPE (multiplier 1000 → per-unit price 4.55e-6 vs TxFlow 4.546e-6), but the rule was unsafe. | Medium | `pairable()` groups by `underlying_id` and drops a ticker carried by two underlyings or without an underlying (`test_same_ticker_different_underlying_never_pairs`) |
| Funding math (§2.3): unknown-phase band | For a hold that is an exact multiple of the interval the band was too high: 2 h at 1 h gave {2, 3}, correct is {1, 2} | **High** (overstated income band) | `possible_counts()`: q integer → {k−1, k}, else {⌊q⌋, ⌊q⌋+1}; verified by brute force over phases (`test_unknown_phase_counts_on_integer_boundaries`) |
| Funding math: boundary instants | A payment exactly at the open or close instant was always "not paid". That is NOT conservative for a paying leg. | **High** | Such payments are now UNCERTAIN: left out of `total`, and the band covers both cases (`phase BOUNDARY`; `test_open_exactly_at_a_payment_is_uncertain_both_ways`) |
| Funding math | Schedule shift, close just before a payment, duplicates | — | `test_schedule_shift_and_close_just_before_payment`; `pair_cash` still asserts no payment counted twice |
| Funding math | Missing or late settlement | — | Not funding_math's job: the ledger books settled points when they appear and labels the rest ASSUMED_CARRIED_FORWARD (Phase 8/9 tests) |
| Variational poller (§2.4): cadence | `updated_at` of all listings went into ONE sorted list, so two listings refreshing every 60 s offset by 30 s looked like a 30 s cadence | **High** | Per-listing histories; the interval is the median of the per-listing medians, clamped to [30, 60] s (`test_poller_cadence_is_per_listing`) |
| Variational poller: repeat | A funding change with an unchanged quote counted as a cached repeat | Medium | A repeat needs every listing's quote time AND funding unchanged (`test_poller_repeat_needs_quote_and_funding_unchanged`) |
| Variational poller: robustness | Missing quote, negative `Retry-After`, rate limit | Low | No cadence sample from a missing quote; a nonsense `Retry-After` falls back to ≥ 30 s; MIN 30 s ≫ 10/10 s per IP (`test_poller_never_faster_than_documented_limits`) |
| Variational poller: clock skew | Cadence uses only differences of the venue's own timestamps | — | Local clock offset cannot bias it (documented in `on_answer`) |

**Effect on the published numbers:**
- Phase 13's scan results are unchanged (no identity conflicts in the data).
- The NATGAS history used `paper_engine`/`estimated`, not `funding_math`'s unknown-phase band, so it is unaffected.
- **`funding_math` was not yet used by any published figure.** The fixes land before the Funding Persistence slice uses it.

## 4. Gap matrix (directive → existing → to build)

| Directive § | Exists | Gap (slice) |
|---|---|---|
| 3 Doctor CLI (offline/status/live/json/venue/market) | pieces: `observation.VENUE_MATRIX`, `funding_evidence.VENUE_BEST`, `storage`, probe reports, `paper_ledger.risk()` | CLI, check registry, socket-guarded offline mode (**B**) |
| 4 Health model (7 dims × 10 statuses, record fields, units) | statuses scattered (ws_readonly totals, stream integrity, probe reports) | `v2/doctor/model.py` and a mapping from evidence to dimensions (**B/C**) |
| 5 Continuous health metrics (1/5/60 min, 24 h windows, bounded) | `ws_readonly.Books.totals/per_market`, stream manifests, `runtime_metrics.py` (legacy) | Lightweight rolling counters, one small heartbeat file, rotation (**C**) |
| 6 Opportunity read model (funding / gap / hybrid fields) | scanner rows, labels, pair snapshot, NATGAS history | Read model + paging (**E**); funding persistence column (**D**) |
| 7 Funding persistence (sign/magnitude persistence, half-life, reversals; baseline / persistence-adjusted / worst case; time-ordered validation) | settled histories (tradeXYZ, HL, Lighter RH, Arcus, RISEx) via adapters; 90 d tradeXYZ NATGAS and HL INIT already saved; `funding_scenarios` | Persistence engine on saved histories, chronological train/validate split (**D**); more venues need an approved history read |
| 8–9 Control Center + mobile | static `v2/dashboard.py` | GET-only server, pages, mobile layout, Tailwind build (**E/F**; Tailwind install needs OK) |
| 10 Read API | none for V2 (legacy `bot.serve` excluded by D2) | `/api/doctor/*`, `/api/opportunities*`, `/api/strategies/readiness`, `/api/paper/positions`, `/api/health/history` (**E**) |
| 11 Remote access | design notes only | ADR section in G; nothing enabled |
| 12 Storage policy | `v2/storage.py`, `paths.py` | Disk-threshold check, log rotation for doctor/health files (**B/C**) |
| 13 Invariants | tests in phases 8–13 | One invariant test module the panel and doctor import (**B**) |
| 14 Tests | broad | Dashboard XSS / leak / no-POST / no-venue-traffic tests (**E/F**) |
| 16 Q10/Q11 Linux and mobile readiness | Phase 10/11 docs | Re-check in **G** |

## 5. Slice plan

| Slice | Content | Needs approval? |
|---|---|---|
| A | This audit, fixes, ADR | — (done) |
| B | Doctor CLI offline/status/json, venue registry view, system checks, socket guard | no |
| C | Health telemetry read model, rolling windows, rotation | no |
| D | Funding persistence on SAVED histories | no; wider history reads: yes |
| E | GET-only read API on 127.0.0.1 | no |
| F | Control Center pages, mobile-first | Tailwind npm install: yes |
| G | Integration, security scan, Linux review | no |
| H | Live validation plan only | live runs: yes |
