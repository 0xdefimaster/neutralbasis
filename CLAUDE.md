# Neutralbasis — project rules

## PAPER TRADING ONLY (permanent, non-negotiable)

These rules apply to every session, phase, experiment and test. They change only
through a separate live-trading readiness review and the project owner's explicit
written authorization.

- Default operating mode: PAPER.
- Live trading: DISABLED.
- All development, experiments, strategy evaluations and tests must use simulated
  execution.
- Real public market data may be used.
- All orders, fills, partial fills, fees, slippage, funding payments, balances,
  positions, margin and PnL must be simulated.
- No real exchange orders may ever be submitted during current development.
- No private keys, signing, trading nonces or trading credentials are required.
- Do not request trading permissions.
- Public market data access is the default.
- Optional read-only credentials must never be mandatory when public information is
  sufficient.
- Paper and live execution must remain isolated.
- No implementation or configuration change may enable live execution.
- Any future live-trading transition requires a separate readiness review and the
  project owner's explicit written authorization.

Even when an exchange or an adapter library offers trading calls, Neutralbasis must
not invoke them. Keep observed exchange data and simulated account events clearly
distinguishable in code, storage and reports.

## Working rules

- The repository is the source of truth. Verify a feature in the code before relying
  on it; a passing test is not proof that it works on real exchange data.
- Never invent endpoints, API fields, venue capabilities or funding conventions. Mark
  unknowns as UNKNOWN and say how to verify them.
- Never fabricate, interpolate or silently default missing market data.
- MOCK test data must always be labelled as such and never presented as evidence.
- Do not merge pull requests or push to the main branch without the owner's approval.
- Each phase ends with a report and waits for approval before the next one.

## Reference

- Phase 0 audit, paper-only policy and safety verification:
  `docs/NEUTRALBASIS_2_AUDIT.md` (sections T and Q).
- Market evidence recorder: `PHASE-1-LOCAL-RECORDER.md`.
- Scope and history: `NEUTRALBASIS-KAPSAM.md`.
- Tests: `python3 -m unittest discover -p "test_*.py"`.
