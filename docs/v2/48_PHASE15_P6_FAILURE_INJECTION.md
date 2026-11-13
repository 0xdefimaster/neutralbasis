# 48 — Phase 15 P6: replay and failure injection

`tools/v2_failure_harness.py` holds 17 scenarios. Each one feeds MOCK inputs into the
REAL modules and checks a PAPER-safety invariant. `test_v2_phase15_p6.py` requires
every scenario to pass. Report: `v2-paper-data/phase15-failure-harness/report.json`
(17/17 PASS, 0 network attempts).

| scenario | invariant | covered by |
|---|---|---|
| WS disconnect / reconnect | no book is served between a disconnect and the next snapshot | `marketdata.TxFlowDepthStream` |
| order-book gap | a sequence gap invalidates the book (resync); it is a SUSPECTED gap, never priced | `SeqBook` |
| missing snapshot | a delta before any snapshot is never applied | `SeqBook`, `Books.lighter_rh` |
| REST 429 | repeated 429 ends in RateLimited: no silent empty answer, no tight loop | `base.http_json` (P2 covers the shared path) |
| wrong timestamp unit | a time in ms stored as seconds is never fresh, and the contract flags it | `marketdata.stale` (**fixed**), `contract.validate` |
| clock drift | a time ahead of the local clock is flagged and never fresh | same |
| stale price from a cache | the shadow shows it; it is refused under the answer time basis | docs/v2/45 |
| stale quote | an RFQ quote older than its limit is never filled | `rfq_paper.fill` |
| slow venue | legs further apart than the market's volatility allows are ASYNCHRONOUS (unknown volatility: 60 s cap) | `opportunity.discovery_freshness` |
| corrupted JSONL | reading stops at the last good record; appending is refused | `storage.read_chain` / `ChainLog` |
| incomplete write | a torn last line is isolated on restart; nothing is deleted | `ChainLog` |
| recorder restart | a leftover open segment is sealed as recovered; writing goes on in a new segment | `recorder.Recorder` |
| full disk | below the free-space floor nothing is written and a gap is recorded | `recorder.Recorder` |
| queue overflow | every in-memory buffer is bounded; counts are folded, not lost | P4 |
| duplicate funding | the same payment is booked once; a duplicated settled point counts once | `paper_ledger`, `funding_expectation` |
| look-ahead | data after the decision time never changes a fit | `funding_expectation.fit` |
| **wrong-price fill** | an open position never fills at a crossed, stale or wrongly scaled price | `paper_engine.fill_guard` (**fixed**) |

## Real faults found and fixed

1. **Wrong-price PAPER fills on open positions (critical).**
   - Entry evaluation refused stale books. But the retry of a missing leg, the unwind
     of an excess leg and the close of a position filled from the observation's book
     with no check at all.
   - In the harness, the unguarded engine filled at **2,997** from a book scaled ×1000
     (true price about 3), at 3.006 from a crossed book, and from a book 120 s stale.
   - **Fix:** every simulated fill goes through `PaperEngine.fill_guard`. A CLOB book
     that is crossed or stale is refused before the fill. Every fill (CLOB and RFQ) is
     refused when its price is more than 25 % away from this leg's average fill (else
     the other leg's fills, else the other leg's mid).
   - A refused fill is "not filled": the position stays at risk and is visible
     (RECOVERY_PENDING); no price is invented.
   - **On the recorded stream the guard never triggers.** The five ESTIMATED replay
     runs are byte-identical before and after the fix (same chain heads, same reports,
     determinism check identical). The fault was latent: the path was open, but the
     recorded data never hit it.
2. **A future `received` time counted as fresh.** For example ms stored as seconds:
   the age is negative, so the book was never stale. A time more than 5 s ahead is
   now UNKNOWN, which counts as stale.

## Not fixable offline

- What TxFlow and Variational actually pay (needs account evidence or the formula).
- How each venue behaves under a real long outage. The harness proves our handling,
  not the venue's.
