# ESTIMATED PAPER: operations runbook (Phase 8)

PAPER only. These steps never send an order, sign anything, or read an account. Every
fill is a simulation on observed public data (`ESTIMATED_CLOB_FILL` or
`ESTIMATED_RFQ_FILL`, `verified=false`). The strict PAPER gate (`v2/gates.py`), the
strict desk (`v2/hybrid_desk.py`), the 8-venue PAPER list and the 11-venue observation
list do not change. Risk settings stay as they are: `wide_max_breakeven_hours=24`,
`wide_funding_realization=1.0`.

## 1. Where the data goes

The owner decided on 2026-11-12 that C: is full, so data goes to E:.

- **Root folder:** `E:\nb-v2-phase8`. Override it with `NB_V2_PAPER_DATA_DIR`.
- **Path guard:** `v2/storage.py`. It refuses:
  - D:
  - anything under `E:\NeutralBasis`, where `paper.sqlite3` and its WAL have no backup
  - any other folder on E:
  - database files
- **Format:**
  - append-only JSON lines only;
  - each line carries the sha256 of the previous line, and is flushed and fsync'ed;
  - nothing is deleted or overwritten.
- **On read:**
  - a hash mismatch → `CORRUPT`; appending is then refused;
  - an unfinished last line (crash) → `TORN_TAIL`; on restart it is kept and isolated with a marker.
- **E: is failing.** Every report states the chain status. When a chain is `CORRUPT`, that run is void: rerun it from the saved observations.

## 2. Bounded observation stream (optional, owner-started)

```
python -m tools.v2_stream --observe --cycles 90 --every 20 --out E:\nb-v2-phase8\stream-YYYY-MM-DD-HHMM
```

- **Bounded:** at most 240 cycles, at least 10 s apart. It is not the 24/7 recorder.
- **Guard:** observation_v3. Reads are public only; the guard log is `guard-stream.jsonl` in the output folder.
- **Reads per cycle (7):**
  - Variational stats
  - xyz and native `metaAndAssetCtxs`
  - TxFlow NATGAS ctx and book
  - xyz NATGAS book
  - native INIT book
- **Funding histories:** xyz NATGAS and native INIT, 3 days, read at the start and at the end.
- **Failures:** a venue that fails twice in a row is skipped with an exponential back-off (2–120 s). HTTP 429 pauses the venue for the time it asks.
- **Outputs:** `stream.jsonl` (hash chain) and `stream_manifest.json` (reads, guard counts, chain head).
- **WebSocket is not used.** The guard allows HTTPS reads only, and the venv has no WebSocket client. Adding one is a network-policy change and needs the owner's review.

## 3. Deterministic replay

```
python -m tools.v2_paper_replay --out E:\nb-v2-phase8\replay-NAME ^
    --aligned C:\Users\W10\nb-benchmark\v2-aligned-2026-11-12 ^
              C:\Users\W10\nb-benchmark\v2-bursts-2026-11-12\burst-1 ... ^
    --stream  E:\nb-v2-phase8\stream-...\stream.jsonl
```

- **Inputs:**
  - aligned bursts and streams (read-only);
  - the pilot's hourly closes, opened read-only from `C:\Users\W10\nb-pilot-loose\data\paper.sqlite3`.
- **Runs, one ledger folder each:**

  | Run | What it does |
  |---|---|
  | `estimated` | NATGAS pairs in the PAPER universe |
  | `scenario_init` | INIT long Variational / short Hyperliquid. Hyperliquid is observation-only, so the run is SCENARIO_ONLY and never part of the portfolio. |
  | `stress_last_look` | Last-look rejection injected on the first fill of each leg |
  | `stress_firm_gap` | Firm quote 10 bp worse than the indicative one |
  | `stress_crash_restart` | Crash after the first leg, restart on the same ledger |
  | `determinism_check` | The estimated run again. Its chain head must equal the first one's. |

- **Outputs:**
  - `replay_report.json`: sources, timestamp integrity, the eight acceptance numbers per asset and strategy, open risk, determinism;
  - per run: `ledger.jsonl`, `stories.json` (signal → position → close) and `evaluations.json`.
- **Existing ledgers:** a replay refuses a folder that already holds a ledger. Pick a new `--out`.

## 4. Reading a result

| Field | Meaning |
|---|---|
| `ESTIMATED_ENTRY_SIGNAL` | An entry would be signalled. It is not a position. |
| `PAPER_EXECUTION_ATTEMPT` → `HEDGED_OPEN` → `POSITION_MONITORING` | A virtual position exists only after the simulated fills |
| `PARTIALLY_FILLED`, `LEG_RECOVERY`, `RECOVERY_PENDING` | Unhedged units are shown in `risk()` and in the report's `open_or_recovery` |
| `CLOSED` + `RELIABLE` | Open and close time-aligned; no funding carried forward; no restart or recovery |
| `CLOSED` + `UNRELIABLE:...` | Accounted, but not evidence (reasons listed) |
| funding `SETTLED_RATE` | A public settled rate, valued at the observed mark. The venue's price basis is not in the history, so the value is estimated. |
| funding `PUBLISHED_ESTIMATE` | Rate published in the same funding period, not settled |
| funding `PUBLISHED_CARRIED_FORWARD` | No observation in that period; the last rate was carried. This is an assumption. |
| `observed_part` / `estimated_part` | Price PnL − fees + settled-rate funding / published funding |

## 5. Never

- Run `tools.v2_stream` without `--observe` or unbounded, or schedule it.
- Point `NB_V2_PAPER_DATA_DIR` at `E:\NeutralBasis` or D:.
- Treat an `ESTIMATED_*` fill as an exchange fill, or report its PnL as realized profit.
- Feed the estimated ledger to the strict desk or the runtime.
- Change the 24 h break-even limit, the 1.0 realization factor, or the venue lists.
