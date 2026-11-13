# Phase 14 Slice D: funding persistence (research, ESTIMATED)

PAPER only. Offline, saved data on C: only, socket-guarded (0 network attempts).
Tool: `tools/v2_funding_persistence.py`. Module: `v2/funding_persistence.py`.
Output: `<bench>/v2-paper-data/phase14-funding-persistence/report.{json,md}`.
STRICT is unchanged. Nothing here is a fill or an account payment.

## Question

Why did the positive expected return of the long-TxFlow / short-tradeXYZ NATGAS pair seen
at 15:00-15:09 (UTC+3, 2026-11-12) vanish at the next observation, and how long does a
published funding rate keep its sign and size?

## Inputs and their evidence

| input | what it is | evidence |
|---|---|---|
| 05:12 snapshot settled histories | 7 days, hourly: tradeXYZ 66, Arcus 73, Lighter RH 58 markets | public MARKET settlement history (not account payments) |
| aligned burst | 90 days hourly: tradeXYZ NATGAS, Hyperliquid INIT | public market settlement history |
| Phase 8 rescued stream | TxFlow NATGAS `activeAssetCtx.nodeCtx.funding` (percent/h) every ~20 s, 14:39-15:10, plus `prevFundingTime` / `nextFundingTime` and tradeXYZ published funding | PUBLISHED values; TxFlow has no public settled history; account payments ACCOUNT_PAYMENT_UNVERIFIED |
| natgas-history-20261113 | later pair observations up to 16:15 | PUBLISHED values |

Payment interval, next funding time and realized evidence are kept apart: the interval
and next time come from `nodeCtx` (TxFlow) or the settled history's times (tradeXYZ);
the realized evidence is the settled history only. A published rate is never assumed
to hold for 24 h.

## Finding 1: TxFlow's published funding is an intra-period running value

| period (UTC+3) | samples | first | last | min | max |
|---|---|---|---|---|---|
| 14:00-15:00 | 62 | 14:39:27 7.44e-4/h | 14:59:52 4.20e-5/h | 4.20e-5 | 9.55e-4 |
| 15:00-16:00 | 28 | 15:00:11 4.20e-5/h | 15:09:16 -1.60e-5/h | -1.60e-5 | 4.20e-5 |
| (later, 16:11-16:15) | | 16:11:48 8.04e-4/h | 16:15:17 9.32e-4/h | | |

Inside the 14:00 period the value moved continuously:
- 14:39–14:42: about 7.45e-4/h;
- 14:42–14:44: up to 9.55e-4/h;
- 14:44:28–14:49:08: unchanged (no update seen for 4.5 min);
- 14:49–14:59: an almost linear fall to 4.2e-5/h.

It carried over the 15:00 boundary unchanged and kept falling to -1.6e-5/h by 15:01:53.
It then stayed flat until 15:09. An hour later (16:11–16:15) it was back at 8–9e-4/h.

From 14:59:32 onward, and at 16:11/16:15, every value is 0.00419824 % plus or minus a
multiple of 0.0002 %. The continuous 14:39–14:59 values do not follow this pattern.
This suggests that close to and after a boundary, a different computation, or a reset
to a baseline, is published. TxFlow does not document the formula: **mechanism
UNKNOWN**. Verifying it needs either TxFlow documentation/support, or recorder data
covering whole periods over several days.

The 07:36 sample (-1.575e-5/h) shows the same low level.

Which of these values is actually paid is UNKNOWN (no account payment evidence; the
`userFundings` read needs an owner-authorized address, none is set).

## Finding 2: why the 15:00-15:09 opportunity vanished

Reference: 15:00:11, TxFlow 4.20e-5/h, tradeXYZ 7.36e-4/h, carry +0.555 %/8h.
Hold 24 h at $1,000. 24 hourly payments on each leg (TxFlow: published next payment
16:00, hourly; tradeXYZ: hourly on the hour).

| model | TxFlow leg (long pays) | tradeXYZ leg (short receives) | 24 h carry cash | %/8h |
|---|---|---|---|---|
| baseline (published held 24 h) | 4.2e-5 constant | 7.36e-4 constant | +$16.66 | +0.555 |
| persistence-adjusted | 4.2e-5 for the current period, then the mean of the samples seen up to 15:00:11 (6.4e-4) | AR(1) from the 90-day train fit (phi 0.963) | -$3.02 | -0.101 |
| adverse | current period, then q90 of samples seen (9.55e-4) | published + train q10 residual (-3.1e-5) | -$5.10 | -0.170 |

What followed (published values, not payments): 15:09:20 carry +0.603 %/8h, then
16:11:48 -0.046 %/8h and 16:15:17 -0.153 %/8h. tradeXYZ settled 6.91e-4/h at 15:00
(consistent with its published value). **The unstable leg is TxFlow**: the positive
carry existed only while TxFlow's running value sat at its end-of-period low. Only the
baseline model, which holds that low for 24 h, made the pair positive. Persistence and
adverse models were negative already at 15:00:11, using no future data.

Conclusion: a TxFlow published rate is valid at most for its current period. Any later
hour has to come from a band (sample mean, high quantile), and on this evidence that
band does not support the trade.

## Finding 3: persistence of settled market funding

Chronological 70/30 split. Mean, AR(1) and the adverse quantile are fitted on the train
part only. The forecast at t uses points at or before t, and the target is the mean
realized rate over (t, t+24 h]. Tested: `FundingPersistenceTests.test_validation_has_no_look_ahead`.

| venue (7-day histories) | markets | validated | best model (count) | median sign kept 24 h | median adverse coverage | confidence |
|---|---|---|---|---|---|---|
| arcus | 73 | 68 | baseline 24, persistence 16, local 15, trailing-24h 13 | 0.93 | 0.96 | LOW 68, INSUFFICIENT 5 |
| lighter_rh | 58 | 58 | baseline 22, trailing-24h 15, persistence 12, local 9 | 1.00 | 1.00 | LOW 58 |
| tradexyz | 66 | 65 | trailing-24h 34, persistence 16, local 12, baseline 3 | 1.00 | 1.00 | LOW 65, INSUFFICIENT 1 |

| 90-day series | phi | half-life | sign kept 1/8/24 h | reversals/day | MAE baseline / AR(1) / trailing-24h / local | adverse coverage | confidence |
|---|---|---|---|---|---|---|---|
| tradeXYZ NATGAS | 0.963 | 18.6 h | 0.89 / 0.85 / 0.85 | 2.73 | 9.66e-5 / 1.10e-4 / 1.20e-4 / 9.86e-5 | 0.93 | MEDIUM |
| Hyperliquid INIT | 0.944 | 12.0 h | 0.98 / 0.95 / 0.93 | 0.54 | 1.37e-4 / 2.91e-4 / 1.77e-4 / 2.73e-4 | 0.62 | MEDIUM |

Reading:
- Settled market funding on these venues is persistent in sign: 85-100 % of 24 h windows
  keep it. Half-lives are 12-19 h.
- No single model wins everywhere. On the 90-day series the last settled rate (baseline)
  forecasts the next 24 h best, and global mean reversion is worse because regimes
  shift. On 7-day tradeXYZ the trailing 24 h mean (the engine's `realized_hourly`) wins.
- The INIT adverse bound covers only 62 % (q10 residual = 0, many hours sit at the
  1.25e-5 floor). A train-quantile adverse bound is not a worst case for it.
- Confidence: 7-day histories are LOW (span < 30 days); 90-day are MEDIUM. HIGH is never
  given because no account payment evidence exists.

## Consequences (research, not gate changes)

1. Treat a published-only funding leg (TxFlow, Variational) as valid for one period.
   STRICT already blocks these legs (no settled history); this result explains why.
2. For the scanner's ESTIMATED funding/hybrid rows, show the persistence and adverse
   carry next to the baseline. Mark a row "baseline-only positive" when only the
   baseline model is positive (Slice E/F display).
3. Phase 15: collect TxFlow published funding across whole periods for several days (the
   recorder) to measure the intra-period pattern; settle the mechanism from TxFlow docs
   or support (UNKNOWN today).
