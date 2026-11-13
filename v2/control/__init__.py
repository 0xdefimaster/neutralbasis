"""NeutralBasis Control Center back end (Phase 14): health model, capability matrix,
doctor checks, telemetry, read model and a GET-only local server. PAPER ONLY.

It READS the existing modules (observation, funding_evidence, ws_readonly, storage,
paper_ledger, opportunity, scanner reports); it never re-implements a strategy, a gate
or a ledger, never opens a venue connection when serving, and has no write path to a
venue, a key or a position.
"""
