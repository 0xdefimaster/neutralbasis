# 53 — Production-readiness audit (2026-11-14)

The audit checked existing modules only. There is no new strategy, venue or
architecture. Every fault found was fixed in its own local commit, with tests.
PAPER ONLY; no network in any step. Full suite: 1253 OK (exit 0).

## Real faults found and fixed

| # | fault | effect | fix (commit) |
|---|---|---|---|
| 1 | Doctor crashed on w32tm output in the local code page (Windows Time service stopped today) | the Doctor and the read model build failed; 3 tests failed | bytes decoded with the OEM code page; clock reported UNVERIFIED / confidence NONE (3a017fb) |
| 2 | Under the answer basis, an unknown fetch time made a row look fresh | fail-open | the row is not built (`ANSWER_TIME_UNKNOWN`) (6bceb73) |
| 3 | The funding rate's age was the price answer's age (Lighter rate cached 600 s) | the legacy desk's 120 s rule was blind: the real age reached 659 s while 59 s was shown | per-market funding time; the re-read is real (6bceb73) |
| 4 | Desk `close`, `unwind` and `mark_position` priced from any book | fake realized and unrealized PAPER PnL from a stale, crossed or scaled book | `v2/fill_guard.py` for both engines (2b3430f) |
| 5 | The P6 own-entry 25 % bound applied even with a fresh other-leg price | after a large legitimate move, a position could never close (a loss never realized) | the cross-leg check only when a fresh other-leg price exists (2b3430f) |
| 6 | A running recorder made every panel section LIVE | a 46 h old scan would have been shown as live | per-section labels (fcdb7b7, b8481b5) |
| 7 | No producer wrote `heartbeat-recorder.json` | a running recorder looked NOT_RUNNING; Doctor telemetry was cut off from its producer | the recorder writes its own heartbeat; wired in the V2 engine (60a9666, db8327b) |
| 8 | A crash between a segment's rename and its manifest line | a sealed segment with no checksum on record, never reconciled | reconciliation on start (MANIFEST_MISSING); manifest fsync (60a9666) |

Two of my own process mistakes in this audit, both corrected:
- fcdb7b7 was committed after a failed suite (the check read grep's exit code); b8481b5
  fixes it. From then on, commits were made only on suite exit code 0.
- The 60a9666 message describes wiring that was applied only in db8327b.

## Question: how does NeutralBasis stop a fake arbitrage opportunity, or a fake PAPER profit, that comes from wrong or old data?

| layer | stops | code | proof (tests / measurements) |
|---|---|---|---|
| 1. Identity and scale | a wrong multiplier or a 10x price never becomes a row | `v2/identity.py`, `v2/normalize.py`, `v2/source.build` | `test_v2_phase15` (fake signals: 10x → PRICE_MISMATCH, "5000X" → MULTIPLIER_UNKNOWN); 194 verified groups max/min ≤ 1.013 |
| 2. Units and sign | percent read as fraction (99x carry); inverted sign | `v2/funding_semantics.py`, `fees.to_fraction` | per-adapter sign tests; cross-venue check: 0 of 102 pairs flagged |
| 3. Funding meaning | a published running value taken as a payment; a constant-rate assumption taken as a forecast | `funding_semantics`, `funding_expectation`; STRICT needs SETTLED history | three models; classes; TxFlow / Variational always `ESTIMATED_SETTLEMENT_UNVERIFIED`; NATGAS +4.99 = BASELINE only |
| 4. Time | an old answer shown as fresh; an unknown time taken as fresh; funding age from the wrong answer | `marketdata` (answer basis), collector `answer_times` | `test_v2_readiness` (the nine checks); simulation: 600 / 3,600 hidden-stale TxFlow checks and 79.8 % hidden-old Lighter rates under legacy |
| 5. Freshness and alignment | legs read too far apart; a stale RFQ quote | `opportunity.discovery_freshness`, `pair_time`, `rfq_paper` | harness: slow venue, stale quote |
| 6. Contract | a unit or skew error in source time; an RFQ priced as a book | `v2/control/contract.py` (`validate`, `sequence_check`) | 1,195 markets + 630 answers, 0 problems; violation tests |
| 7. Economics | a positive estimate with negative stress shown as safe | `economics`, `estimated`, read-model `NOT_SAFE` | 0 stress-positive rows; no row is ever SAFE |
| 8. Fill integrity | a fill or mark from a crossed, stale, wide, off-grid or mis-scaled book | `v2/fill_guard.py` in both engines | `test_v2_fill_integrity` (AST proof that every fill / PnL path is guarded, plus behaviour); harness `wrong_price_fill` (unguarded engine filled at 2,997) |
| 9. Storage | corrupted / torn ledger, recorder crash, full disk, a duplicate payment counted twice | `storage.ChainLog`, `recorder`, `paper_ledger` | harness (corrupted JSONL, incomplete write, restart, full disk, duplicate funding); manifest reconciliation test |
| 10. Labels | historical / simulated / estimated shown as live or verified | read model per-section labels, profit label never VERIFIED, EXECUTION_UNVERIFIED on every row | `SectionLabelTests`, `FundingClassTests` |
| 11. Rate and telemetry | 429 storms, silent telemetry loss, memory blow-up | `ratecoord` (off), `bounded`, Doctor | P2 / P4 tests; 147 → 20 MB at 50k markets |

**Still open (not provable from code):**
- what TxFlow and Variational actually PAY;
- the real behaviour of a live venue over a long outage;
- the share of the IP quota used by other clients;
- the clock offset (the Windows Time service is stopped today);
- Linux execution of the P2 locks (no Linux distribution installed here).

## Rate coordinator (P2): verification status

| check | status |
|---|---|
| Windows multi-process | VERIFIED (real subprocesses; the shared bucket bound holds) |
| crash inside the lock; dead-process lease | VERIFIED |
| stale lock | released by the kernel (tested through a crashing subprocess) |
| clock jump back / forward | VERIFIED (the step-back stall was fixed) |
| atomic state | magic + length + SHA-256 record; torn record → conservative reset (tested) |
| per-IP / global budget | per-IP scope = the venue budget. A venue's GLOBAL limit (e.g. Variational 1,000/min across all clients) cannot be coordinated, only its 429s handled |
| circuit-breaker recovery | VERIFIED (half-open probe; a crashed probe is reaped) |
| Linux | **UNVERIFIED**: no Linux distribution on this machine; installing one is a system change |

It stays OFF by default. `NB_V2_SHARED_BUDGET=1` is accepted in principle for approved
live runs.

## Portability (Windows → Ubuntu)

- Paths: environment variables (`NB_V2_BENCH_DIR`, ...) with home-relative Linux
  defaults.
- Windows-only code is all behind `os.name`.
- Gaps:
  - a Linux clock reader (the Doctor says NOT_TESTED there);
  - the P2 `fcntl` path is unexecuted;
  - the UI smoke test knows only Windows browser paths;
  - systemd units are prepared only as docs (docs/v2/42 §8).

## Storage, backup and restore

**Restore drill, today:** `neutralbasis-data-pack-2026-11-13.zip` (5.4 MB):
1. verify: 45 entries, all checksums OK, no unsafe paths;
2. extract: 22 MB into a scratch folder;
3. run the data tests with `NB_V2_BENCH_DIR` pointing there: **63 tests OK, 0
   skipped**.

The drill folder was then deleted.

**Gaps:**
- the pack does not include the Phase 15 reports or any future recording;
- the 29.6 GB engine DB on the failing E: has **no backup** (owner decision);
- no automated restore tool beyond verify + extract.

## A short pilot instead of L1 (around ONE payment boundary)

| item | value |
|---|---|
| Reads | TxFlow NATGAS activeAssetCtx and tradeXYZ metaAndAssetCtxs every 10 s from −120 s to +120 s (25 + 25), plus tradeXYZ fundingHistory before and after (2) = **52 requests** |
| Duration / volume | about 4.5 min; under 0.3 MB; scratch folder capped at 10 MB |
| Budget | through the P2 coordinator (HIGH): TxFlow 0.1 req/s, HL 6 req/min |

**What it can prove:**
1. TxFlow's published value at one more boundary: does it reset, continue or jump?
   With Phase 8 this makes n = 2 boundaries. The analysis uses the venue's own times
   (`blockTime`, `prevFundingTime` / `nextFundingTime`), so it does not depend on the
   local clock.
2. tradeXYZ convergence to its settlement at a second boundary.
3. The end-to-end operational path on live answers: answer-time stamping, contract
   validation, coordinator telemetry, recorder segment, manifest and heartbeat,
   Doctor ingest.

**What it cannot prove:**
- which TxFlow value is PAID;
- anything general (one market, n = 2);
- persistence;
- any latency figure (the clock offset is UNKNOWN: the Windows Time service is
  stopped);
- anything about Variational.

## TxFlow account funding: public options before asking for a wallet

| option | what it would prove | status |
|---|---|---|
| Docs `trade/funding.md` (read 2026-11-12: payment = size x mark x rate, positive = longs pay) | the formula statement; it does NOT say which value of the running rate is paid, nor how it is averaged | a re-read is needed for any averaging / premium detail (a docs read, not an exchange call) |
| `userFundings` needs **no auth** ("the body user is the subject") | account payments for ANY address. A documented PROTOCOL address (vault, insurance fund) with positions would show the paid rate (`usdc / (szi x mark)`) | UNKNOWN whether TxFlow documents such an address. The project rule allows only owner-authorized addresses, so it needs your approval. Never a private person's address. |
| A market-level funding-history endpoint | settled market rates (the strongest public evidence) | none in the docs read so far (Phase 9 / 10); a new docs check is needed |
| A public chain explorer (`blockTime` suggests TxFlow's own chain) | settlement events per market, if published | UNKNOWN; needs a docs check |
| TxFlow support | the formula | external contact; your decision |
| Your own wallet | only helps if it HELD positions, which needs trading | **excluded** (PAPER ONLY) |

## Missing before live observation tests can start safely

1. **Clock:** Windows Time is stopped (0x80070426), so the offset is UNKNOWN. Starting
   the service is a system change and was not done; it is your decision. The pilot
   above avoids local-clock dependence, but no latency or age claim is valid until
   then.
2. **Approval** of a specific plan: the 52-request pilot first.
3. **Storage target and cap** for live recordings (C: folder, size cap, retention), and
   adding new recordings to the backup pack.
4. **A producer heartbeat for WS / scheduler channels.** The recorder heartbeat is
   wired now. The bounded live tools write reports, not heartbeats; the first live run
   should write `channels` / `ingest` heartbeats so the panel shows observed values
   instead of NOT_RUNNING.
5. P2 on Linux is unverified (needed before a VPS run).
6. TxFlow / Variational payment evidence is absent, so their legs stay ESTIMATED,
   never STRICT.
