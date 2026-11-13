# Phase 14: NeutralBasis Doctor and Control Center

PAPER only. LIVE trading DISABLED. Everything below runs offline on saved data, except
the one-time `npm install` of the build tooling, which was approved.

## 1. Commands

| what | command (repo root, venv python) |
|---|---|
| Doctor (offline, socket-guarded) | `python tools/nb_doctor.py` (`--json`, `--venue txflow`, `--market NATGAS`, `--no-save`) |
| last Doctor result, no checks | `python tools/nb_doctor.py --status` |
| live Doctor | `python tools/nb_doctor.py --live`: refused (exit 2); needs the owner's separate approval |
| build the read model | `python tools/nb_control.py build` |
| serve the panel | `python tools/nb_control.py serve` and open http://127.0.0.1:8765/ |
| build, then serve | `python tools/nb_control.py serve --build` |
| periodic offline rebuild | `python tools/nb_control.py serve --rebuild-every 300` (minimum 60 s) |
| funding persistence report | `python tools/v2_funding_persistence.py` |
| UI smoke test (server running) | `python tools/nb_ui_smoke.py --out <dir>` |
| rebuild CSS (only after UI class changes) | `cd v2/control/ui && npm ci && npm run build` |

Exit codes: Doctor 0 ok, 2 live refused, 3 a network attempt was blocked; `--status`
returns 1 when no saved result exists.

## 2. Architecture

```
saved artifacts (C:)          v2.control.readmodel.build()      readmodel/*.json      server.py (GET)      browser
 snapshots, streams,   ──►    offline, netguard: 0 sockets  ──►  small JSON files ──►  reads files only ──► app.js
 probes, stories,             Doctor + scanner + stories +                            127.0.0.1 only       textContent
 heartbeats                   persistence report, redacted
```

- The server never builds, checks or connects. A panel refresh (every 10 s, paused
  when the tab is hidden) reads the cached files only (`MethodAndSideEffectTests.
  test_get_reads_cache_only`).
- The Doctor and the panel read the same modules (`v2.control.doctor`, `capability`,
  scanner report, stories). Neither re-implements strategy, risk or the ledger.
- Legacy `bot.serve` is not used and not changed.

## 3. API contract (GET / HEAD only)

Every JSON body carries `schema`, `generated_at` (epoch s, UTC), `data_mode`
(`HISTORICAL` without a recorder heartbeat, else `LIVE_OBSERVED`), `banner`,
`trading_mode: "PAPER"` and `live_trading: "DISABLED"`. Unknown values are `null`, with a
reason field where one exists. They are never `0`, `HEALTHY` or `VERIFIED`. If a file is
not built, the endpoint returns `status: "NOT_BUILT"` and `data_mode: "UNKNOWN"`.

| endpoint | query | returns |
|---|---|---|
| `/api/meta` | - | server time, read-model age, data mode, banner, file sizes |
| `/api/doctor/overview` | - | system_running (recorder / paper_engine / control_server), system checks, last venue observation, venues (health, problems, last verification age), top opportunities, PAPER position counts, Doctor sections, data kinds (live / historical / simulation) |
| `/api/doctor/exchanges` | - | per venue: role, health, last verification (+ current?), capability levels |
| `/api/doctor/exchanges/{id}` | id in the closed 12-venue list | full capabilities (level, value, source, as_of, current), Doctor records, problems |
| `/api/doctor/markets` | `venue`, `asset`, `page`, `page_size` | per reference asset: venues, strategies, labels, best expected/stress net, top rejection, freshness |
| `/api/doctor/system` | - | system records (python, cpu_ram, disk, failing_disk, recorder, paper_engine, control_server, clock, backup, export) |
| `/api/opportunities` | `strategy`, `label`, `venue`, `asset`, `safety`, `flag`, `freshness`, `sort` (expected_net, stress_net, risk_adjusted_score, gap_bp, carry_8h, asset), `page`, `page_size` ≤ 100 | rows: id, asset, strategy, legs, size, gap_bp, carry_8h, funding_evidence, expected_net, stress_net, breakeven_h, label, freshness, evidence_level, execution_model, reference, strict/estimated first rejection, rejections, flags, safety (`NOT_SAFE` / `RESEARCH_ONLY`, never SAFE). Nulls sort last. |
| `/api/opportunities/{op-NNNNN}` | - | one row |
| `/api/opportunities/summary` | - | snapshot time/age, counts, discovery / economic / readiness layers, first-rejection histograms, top 10 |
| `/api/strategies/readiness` | - | per strategy: rows, raw edge, expected > 0, stress > 0, STRICT eligible, ESTIMATED candidates, rejections, status |
| `/api/paper/positions` | `include_scenarios=1`, paging | ESTIMATED PAPER positions: legs, state, fills (label, verified=false), expected/stress at entry, PnL by evidence class, valuation_reliable + problems |
| `/api/health/history` | - | last 5 Doctor runs (sections), recorder health history (24 h) |
| `/api/funding/persistence` | - | Slice D summary: per-venue validation, 90-day metrics, NATGAS case, TxFlow periods |

Flags on opportunities: `NOT_SAFE_STRESS_NEGATIVE`, `STRESS_UNKNOWN`,
`PUBLISHED_RATE_VALID_ONE_PERIOD`, `HEDGE_REFERENCE_UNVERIFIED`,
`RFQ_INDICATIVE_NO_FIRM_QUOTE`, `FRESHNESS_UNKNOWN`. These are display rules only;
STRICT is unchanged.

Static routes (fixed table): `/`, `/index.html`, `/app.js`, `/app.css`,
`/manifest.webmanifest`, `/icon.svg`. Anything else returns 404.

## 4. Security report

| control | implementation | test |
|---|---|---|
| loopback only | `ControlServer` refuses any non-loopback host; the CLI has no 0.0.0.0 flag | `BindTests` (0.0.0.0, ::, LAN, CGNAT, names) |
| GET/HEAD only | every other method returns 405 `Allow: GET, HEAD`; no handler writes | `test_only_get_and_head` (files unchanged) |
| no action routes | no start/pause/config/orders/keys/rebuild/doctor-run route | `test_no_action_routes_exist` |
| no traversal / uncontrolled reads | fixed route table; no request value becomes a path; `%`, `..`, `\`, `//` give 404; venue ids are whitelisted | `test_traversal_and_unlisted_files` (planted secret file never served) |
| DNS rebinding | Host must be 127.0.0.1 / localhost / [::1] (421 otherwise) | `test_dns_rebinding_host_refused` |
| browser hardening | CSP `default-src 'none'`, script/style/connect self, `frame-ancestors 'none'`, `form-action 'none'`; nosniff; no-referrer; X-Frame-Options DENY; CORP/COOP same-origin; no-store; no CORS | `test_security_headers_and_no_cors`; UI smoke: 0 CSP violations |
| UI injection | app.js uses textContent only; no innerHTML / eval / external URLs; HTML has no inline script or style | `test_ui_code_is_csp_clean_and_text_only` |
| input bounds | values `^[A-Za-z0-9_.:\- ]{1,40}$`, no repeats, ≤ 20 fields, page_size ≤ 100, page ≤ 10000, URL ≤ 2048 | `test_bounds_and_patterns` |
| no leaks | build drops secret-like keys, replaces bench/home paths; env never read into payloads; no tracebacks in responses | `test_redact_…`, `test_no_leaks` (planted env secret absent) |
| no outbound | builder under netguard (0 attempts); server does no name lookup (no getfqdn); smoke tool talks to loopback only | `test_offline_and_labelled`, `test_served_end_to_end_offline` |
| request smuggling | one request per connection (`Connection: close`) | covered by the 405 body test |
| build tooling | Tailwind 3.4.19 exact, lockfile from registry.npmjs.org with sha512, `ignore-scripts`, deps resolved `--before 2026-10-30`, node_modules ignored, `npm audit` 0 | `test_build_tooling_pinned_and_not_committed` |

Code scan since 74dc319: no key material. The only key-like strings are labelled MOCK
values in the redaction test. No trading call, no POST handler, no outbound client in
`v2/control`.

Known limits:
- ThreadingHTTPServer starts one thread per connection. That is fine on loopback, but
  the server is not a public server.
- The Host check is the only cross-site defence. The API is read-only and has no
  cookies or auth, so there is nothing to forge.

## 5. Resources (measured 2026-11-13, this laptop)

| item | value |
|---|---|
| read model build | 0.99-1.13 s, 0 network attempts; 3.4 MB total (opportunities 3.2 MB, others ≤ 105 KB) |
| server process | 42 MB working set, 176 handles; 2.0 s CPU total after build + 80 page loads + API probes |
| request latency (loopback) | 2-9 ms; `/api/opportunities?page_size=100` 110 KB in 8.9 ms |
| heartbeat | `heartbeat-control_server.json` every 15 s (224 B), history rotated at 64 KB |
| UI | app.js 29 KB, app.css 17 KB, no external requests |
| build tooling | node_modules 14 MB (72 packages, gitignored); C: free 19.85 GB before, 19.81 GB after Slice F (npm cache + screenshots) |
| disks | nothing written to D:/E:; read model and outputs refuse D:/E: in code |

## 6. Desktop and mobile testing

`tools/nb_ui_smoke.py` runs headless Edge through the DevTools protocol on loopback.
It uses real device emulation, because a plain `--window-size` cannot go below about
500 px. Coverage: 10 views (overview, exchanges, exchange detail, markets,
opportunities, opportunity detail, PAPER positions, doctor, funding persistence,
history) × 360, 390, 768 and 1280 px. Each check looks for document scrollWidth >
viewport, offending elements, JS exceptions, console errors, CSP violations and an
empty view.

Result: 40 checks, 0 problems. Screenshots were reviewed by eye. One bug was found and
fixed: key/value labels collapsed at 360 px when a value was long.

Layout:
- Mobile: bottom tab bar (Home, Venues, Opps, Paper, Doctor). Markets, funding and
  history open from the Home links.
- Tablet/desktop: a sidebar.
- Tap targets are at least 40 px. The page handles safe areas and uses a dark theme.

## 7. Remote access later (design only; nothing enabled)

- The server keeps binding to 127.0.0.1.
- When the owner approves, `tailscale serve` (tailnet only, HTTPS) proxies to that
  loopback port, so no code change is needed. The Host allowlist must then gain the
  machine's MagicDNS name: a one-line config change with its own review.
- Never: 0.0.0.0, port forwarding, a public IP or Tailscale Funnel.
- No login is needed while the server is loopback-only and read-only. Before
  multi-user access, add tailnet ACLs and consider Tailscale identity headers.

## 8. Linux / HP Pavilion / VPS readiness

Ready:
- Pure Python stdlib server.
- Paths come from `NB_V2_BENCH_DIR`.
- Memory falls back to `/proc` for system and process memory.
- The heartbeat writer is atomic.
- The UI is static, and the CSS is committed: no Node is needed on the target.

Missing:
1. The clock check uses `w32tm` only. On Linux it reports UNKNOWN; add a
   `chronyc tracking` / `timedatectl` reader.
2. No service unit (systemd) for the recorder, engine and control server, and no
   restart policy.
3. The recorder (Phase 1 local recorder) is not running anywhere; nothing produces live
   heartbeats yet.
4. Backups: the 29.6 GB engine DB on E: has no backup; the read model and research
   outputs are small and easy to copy.
5. Rate budgets are per process (section 9).
6. Data transfer: saved artifacts must be copied (data pack zip exists, 2026-11-13).
7. Log rotation for recorder output.

## 9. Rate limits and polling: evaluation

Existing (`v2/ratelimit.py`, `v2/marketdata.Backoff`, `v2/variational_poller.py`,
`Collector` TTLs):
- per-venue sliding-window budgets below the documented limits;
- shared hosts share a budget (Hyperliquid native / entropy_io / tradeXYZ);
- a 429 pauses the whole venue for its `retry_after`;
- a bounded single-flight cache;
- TTLs: meta 60 s, slow meta 1 h, book 5 s, settled 10 min;
- Variational per-listing cadence clamped to 30-60 s, because quotes change every
  ~42 s (median) and 22 of 105 calls hit 429 even under budget.

Gaps:
- Budgets are per process: a recorder, an engine and a scanner would each spend the
  full budget.
- There is no channel scheduler, priority (book > ctx > meta) or global request ledger.
- WebSocket is used only in probes.
- The panel does not show budget use.

All of these are large enough to go to the Phase 15 backlog below.

## 10. Phase 15 backlog (data infrastructure)

| # | item | reason | test plan |
|---|---|---|---|
| 1 | cross-process rate budget (one budget service or a file-lock token bucket per venue) | several processes would multiply requests | 3 processes × burst against a MOCK venue clock: total ≤ limit; 429 pause propagates to all |
| 2 | per-channel scheduler with priorities and TTL-aware polling | books need seconds, meta needs minutes; today callers poll independently | MOCK clock: book freshness ≤ target while meta stays at TTL; budget never exceeded |
| 3 | WebSocket-first market data for TxFlow, Hyperliquid/xyz and Lighter RH (read-only client exists) | REST polling cannot give 2 s freshness; WS probes ran clean (0 resyncs) | 24 h approved run with sequence-gap, resync and reconnect counters in telemetry |
| 4 | recorder with heartbeat + telemetry + bounded storage, and crash recovery (resume, gap marking) | no live heartbeat means the panel can only be HISTORICAL | kill -9 mid-write: restart resumes, gap recorded, no corrupt file |
| 5 | full-market snapshot with per-answer times | 05:12 snapshot has one time for all answers | replay check: every answer has t0/t1; skew report |
| 6 | TxFlow funding: record whole periods for several days | the published value is an intra-period running value (Slice D); mechanism UNKNOWN | per-period curves; compare with any later settled evidence |
| 7 | US-session read for RWA markets | most RWA rows are SESSION_CLOSED in an off-hours snapshot | a snapshot during RTH; compare rejection mix |
| 8 | Linux clock reader + service units + log rotation | VPS move | Doctor on Linux shows clock offset; systemd restart test |
| 9 | budget/telemetry view in the panel | operators need to see 429s and budget use | MOCK telemetry → API → UI smoke |
| 10 | clock: verify offset ≤ 0.05 s before showing any latency (class B) | offset was +0.135 s at 06:36 and still slewing | Doctor reads NTP offset (approved) and gates latency display |
