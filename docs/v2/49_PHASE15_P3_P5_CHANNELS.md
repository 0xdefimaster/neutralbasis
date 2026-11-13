# 49 — Phase 15 P3 + P5: channel scheduler and channel view

## P3: `v2/scheduler.py`

The scheduler decides WHEN each (venue, channel, key) is read. The reads stay the
existing adapter / collector calls (`run_due(fetchers)`). There is no new event bus
and no second recorder.

| channel | policy |
|---|---|
| orderbook | WS first where the venue has one (txflow, tradexyz, hyperliquid_native, lighter_rh). While the WS is fresh, REST is only a verification read every 5 min, never a duplicate feed. WS stale or absent → REST_FALLBACK at once. Back to WS after 3 fresh checks (hysteresis). |
| public_trades | DISABLED on every venue: no verified public-trades reader exists |
| funding_current | REST from the market answers, adaptive between 30 and 300 s |
| funding_history | Incremental backfill 5 min after each settlement, from a DURABLE cursor. The cursor never moves back and survives a restart. DISABLED where no public settled history exists (TxFlow, Variational). |
| rfq_quote | Variational indicative quotes, adaptive between 15 and 60 s |
| metadata / fees_specs | 1 h / 1 day, LOW priority |

**Adaptive polling** keeps the REQUEST rate apart from the PUBLICATION rate:
- The interval is half the observed time between real data changes.
- It is clamped to [rest_min, min(rest_max, stale_limit / 2)].
- Example (MOCK): Variational quotes changing every 42 s give a 21 s poll, not the
  15 s minimum. An order book that changes once a minute, with a 15 s stale limit, is
  still polled every 7.5 s.

Priorities map to the P2 coordinator (HIGH / LOW). Errors are counted per channel and
never raised. No capability is assumed: the plan is built from the capability matrix,
and a level that is not proven leaves the channel DISABLED or REST-only, with the
reason.

## P5: Doctor / panel

- The read model holds `channels` per venue. Each one carries its source and mode,
  priority, policy interval, observed request interval, observed publication cadence,
  last real data change, status (FRESH / STALE / NO_DATA / DISABLED / NOT_RUNNING),
  error rate and evidence.
- Observed values come ONLY from a running producer's heartbeat (`channels`). There is
  none today, so every channel is NOT_RUNNING with its policy shown. Nothing is
  invented.
- The Control Center venue page has a "Data channels" card. UI smoke: 48 checks at
  360 / 390 / 768 / 1280 px, 0 problems.
- Doctor records added in this phase:
  - P4: telemetry callback errors and cardinality;
  - P2: rate budget per scope, with the external IP share UNKNOWN;
  - the time-basis shadow, in the STRICT gate result.
- A panel refresh never calls a venue; every value comes from files.

## Not run

No producer runs the scheduler live: no recorder, no live test. Wiring it into a
long-running process belongs with the first approved live plan.
