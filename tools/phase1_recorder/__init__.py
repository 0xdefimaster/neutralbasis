"""Phase 1 local market evidence recorder.

A standalone, read-only recorder of public market data from Hyperliquid, Lighter Core,
Lighter RH and Extended. It never places orders, never reads accounts, never starts the
paper engine and never writes the engine's database: everything it keeps lives under
its own folder (`phase1-evidence/` by default).
"""

VERSION = "1.0.0"
