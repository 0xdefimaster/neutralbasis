# PAPER Execution Evidence

PAPER ONLY. MOCK test results and real-data results are kept strictly apart.

## A. MOCK TEST (not a market result)

`test_paper_lifecycle_e2e` passes (latest full suite: 753 run, 0 failures, 0 E: writes). It runs the real `WidePaper` code through the **real** capability gate. The two venues are verified only by evidence that the registry's own methods recorded.

| Step | Proven |
|---|---|
| Entry | Two-leg fills at book VWAP + slippage; equal base quantity; fees from fill prices |
| Management | Funding settled per venue schedule; never paid twice |
| Restart | Same position; not reopened; funding not re-paid |
| Exit | Persistent carry flip → both legs closed at the book |
| Accounting | net = price PnL + funding − fees = change in wallet cash |

The MOCK result (−$1.10, an exit after 8.5 h of a planned 48 h hold) is **never** counted as a real trade. Details: `EXECUTION_EVIDENCE.md`.

## B. Real public data: isolated pilot

Port 18765, C: database, network guard (0 refusals).

| Metric | Count | Source |
|---|---|---|
| Real-data PAPER positions opened | **0** | pilot report / `/api/candidates` (no `OPENED` class) |
| Closed | **0** | |
| Realized PAPER PnL | **none** (no position) | |

Why there is no entry: every candidate is still `RESEARCH_CANDIDATE` because the per-venue funding-sign gate needs 3 settled comparisons per market and 3 decided markets per venue. Measured progress is in `ZERO_TRADE_ROOT_CAUSE.md` §Funding evidence.

The PAPER executor is the same code the MOCK test exercised. It opens a position automatically only for a candidate that passes **every** gate. No gate was relaxed to make one appear.
