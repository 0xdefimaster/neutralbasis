# 45 — Time basis: legacy `received` vs `answer_received` (SHADOW, switch prepared)

## Four times, kept apart

| time | where | meaning |
|---|---|---|
| source | book `ts`, ctx `blockTime` | the venue's own time for the data |
| fetch | `answer_received` (P1) | when the answer arrived |
| read | legacy `received` | when the engine read it; a collector cache hit is re-stamped here |
| evaluation | `at` | when the gate decided |

Every book age check goes through `v2.marketdata.stale()`: STRICT `BOOK_STALE`,
ESTIMATED `critical`, and `paper_obs`. RFQ legs are aged from the venue's quote
`updated_at` (source time) in `v2/rfq_paper.py`; they are already honest.

## SHADOW results (`tools/v2_time_shadow.py`, `v2-paper-data/phase15-time-shadow/`, 0 network attempts)

| data | result |
|---|---|
| Rescued Phase 8 stream (90 cycles) and Phase 9 parallel probe (15 cycles), 315 books | Every book is fresh under both bases. Answer age p95 is 4.6 s at most and its maximum is 7.4 s. Source age is 0.5–1 s more than answer age. 0 LEGACY_FRESH_ANSWER_STALE. |
| ESTIMATED replays (14 evaluation sets, 12,639 evaluations) | They measure from SOURCE time, else receive time. That is stricter than the answer basis, so their decisions do not change. 5 evaluations were blocked by time, all under that stricter basis already. |
| 05:12 scanner snapshot | NOT_MEASURABLE: the replay clock is constant, so read = fetch = evaluation. |
| Live V2 collector path | No saved run exists, so only bounds can be given. A cache hit on a book is under 5 s old, and the STRICT limits are 10–30 s: a decision flips only if the gate runs more than limit − 5 s after the read. A funding row's honest age is below the meta TTL (60 s) plus refresh_every (60 s) = 120 s, which is exactly the legacy desk's entry limit. |

**Bug found.** `V2EngineSource.current_funding` "re-reads" a stale rate at entry by
refreshing the collector. Inside the 60 s meta TTL, that refresh serves the SAME cached
answer and re-stamps it. So the legacy desk's re-read of a stale quote fetches nothing
new.

**Which past PAPER decisions change: none.** The saved ESTIMATED replays already use a
stricter time. The scanner snapshot cannot differ. There is no saved live V2 engine run.

## Prepared switch (default OFF: `marketdata.TIME_BASIS = "legacy"`)

With `TIME_BASIS = "answer"`:
- `marketdata.stale` ages a book from `answer_received`. If that key is present but
  None, the fetch time is UNKNOWN and the book counts as stale.
- The collector stamps a book's `received` with its fetch time; a row's `received`
  becomes its venue's oldest answer fetch time. The legacy desk's book-sync (5 s) and
  age checks then become honest too.
- `current_funding` drops only that market's cached answers before refreshing, so the
  re-read is real. For TxFlow it drops one ctx key, never the whole venue.

**Regression tests** (`test_v2_phase15_time.py`):
- The answer basis is never less strict, checked as a property over a grid of read
  ages, cache ages and venues.
- An unknown fetch time is never fresh.
- WS and stream books, which have no cache, give the same result under both bases.
- The STRICT gate decision is unchanged under legacy; `time_shadow` reports the
  difference.
- Under answer, the same input is BOOK_STALE.
- The collector, the rows and `current_funding` behave as described above.

**Recommendation.** Switch to `answer` together with the first approved live run, and
watch the shadow in that run. It only ever makes the gates stricter. It is not
switched here because it changes live gate inputs, so it is the owner's decision.
