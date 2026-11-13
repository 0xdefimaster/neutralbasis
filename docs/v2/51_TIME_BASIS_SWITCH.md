# 51 — Time basis switched to `answer` (owner-approved 2026-11-14)

`v2/marketdata.TIME_BASIS = "answer"`. Legacy stays available through
`marketdata.use_basis("legacy")`, for comparison tools and for tests that document the
old behaviour.

## Four times, never mixed

| time | field | used for |
|---|---|---|
| source | book `ts` (venue time) | source age (shadow); a source time > 5 s ahead of the fetch is flagged `SOURCE_AHEAD_OF_FETCH` (clock skew, ours or theirs) |
| fetch | `answer_received` | **the age every gate now measures** (books, rows) |
| read | `read_at` | kept apart; it was the legacy `received` |
| evaluation | gate `at` | the decision time |

A WS book keeps its own arrival time (`received`, no `answer_received`). It is never
re-stamped with a REST fetch time.

## Fail-closed rules

- **A book** whose fetch time is unknown is stale.
- **A row** whose price fetch time is unknown is **not built** (reason
  `ANSWER_TIME_UNKNOWN`). It is never stamped with the build time.
- **A funding rate:**
  - the legacy desk now reads the rate's OWN answer time (`funding_received`);
  - an unknown rate time is `-inf`, so it is never fresh;
  - an old rate makes `current_funding` drop only that market's cached answer and
    fetch it again, so the re-read is real.
- **Price and funding times are per market** where the venue answers per market:
  TxFlow `tx:ctx:<id>`, and Lighter's rate from its own `rh:fund:<id>` answer.
- **A time more than 5 s in the future** is never fresh (P6).

## Measured differences

**Recorded data** (all identical, 0 network attempts):
- scanner on the 05:12 snapshot under `answer`: counts, economics, readiness,
  rejections and ranking identical to legacy;
- ESTIMATED replays of the Phase 8 stream: all 5 chain heads identical. They already
  age books from source time, else receive time.

**Simulated live cadence** (`tools/v2_time_basis_sim.py`: MOCK answers, fake clock,
real Collector code, 3,600 s):

| check | legacy | answer |
|---|---|---|
| book stale, gate 0–2 s after the read (limits 10 / 15 / 30 s) | 0 | 0 |
| book stale, gate 6 s after the read, TxFlow (10 s limit) | 0 | **600 of 3,600** (the legacy basis called them fresh) |
| Lighter rate older than the desk's 120 s entry rule | 0 % (max 59 s shown) | **79.8 %** (max 659 s real) |

**Answers to the directive:**
- **Is funding freshness solved by book time alone? No.** Lighter's rate comes from a
  fundings answer cached 600 s. Under legacy that rate looked at most 59 s old; its
  real age reached 659 s.
- **What fixes it:** the separate per-market funding time and the real re-read.
- **Cost:** one extra request per entry attempt that sees an old rate.

## Never looser

- A fetch precedes its read, so answer age ≥ legacy age. This is tested as a property
  over a grid of read ages, cache ages and venues, and in the simulation (answer-stale
  ⊇ legacy-stale).
- STRICT thresholds are unchanged; only their input time is honest.

## Tests

`test_v2_readiness.TimeBasisSwitchTests` covers the nine checks of the directive:
cache hit, unknown time, WS vs REST, four times, skew, funding rows, difference,
never looser, no future time. Full suite: 1237 OK.
