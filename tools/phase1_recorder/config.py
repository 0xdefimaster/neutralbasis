"""Recorder settings. Every interval can be changed from a JSON file (--config)."""

import json
import os

# The project's default asset list (bot.DEFAULTS["symbols"]); per-market tasks
# (candles, settlements, books) cover these unless --all-markets is given.
DEFAULT_ASSETS = [
    "BTC",
    "ETH",
    "SOL",
    "HYPE",
    "SUI",
    "XRP",
    "LINK",
    "DOGE",
    "AVAX",
    "BNB",
    "LTC",
    "AAVE",
    "UNI",
    "NEAR",
    "ADA",
]

DEFAULTS = dict(
    root="phase1-evidence",
    venues=["Hyperliquid", "LighterCore", "Lighter", "Extended", "Variational"],
    assets=DEFAULT_ASSETS,
    all_markets=False,
    metadata_seconds=6 * 3600,
    funding_seconds=60,
    settlements_seconds=900,
    candles_seconds=1800,
    orderbook_seconds=60,
    backfill_days=30,
    book_levels=20,
    # Matching a published rate with the payment after it.
    settlement_grace_seconds=2 * 3600,
    timeout_seconds=15,
    retries=4,
    backoff_seconds=2.0,
    # Minimum spacing between two requests to one venue (seconds).
    min_spacing=dict(
        Hyperliquid=0.3, LighterCore=1.0, Lighter=1.0, Extended=0.3, Variational=1.0
    ),
    # Discovery venues (Variational): one raw poll of the public stats endpoint.
    discovery_seconds=60,
    candle_retry_limit=3,
    # Two settlement times of one market closer than this may be one payment. Per
    # venue or per "Venue|ASSET"; the effective value is also capped at 60 s and at
    # 1% of the market's payment interval (records.same_event_tolerance).
    same_event_seconds=dict(Hyperliquid=60, LighterCore=60, Lighter=60, Extended=60),
    # Historical reconciliation: how often, and how far back (capped at backfill_days).
    reconcile_seconds=6 * 3600,
    reconcile_hours=24,
    # A venue whose requests fail this many times in a row with no answer at all
    # (timeout, DNS, refused connection; every retry spent) is skipped without network
    # calls for the cooldown, so one unreachable venue cannot hold up a run.
    unreachable_after=3,
    unreachable_cooldown_seconds=600,
)

# A single --once check is a quick look, not a backfill under a bad connection.
ONCE_OVERRIDES = dict(retries=1, timeout_seconds=10, unreachable_after=2)


def load(path=None, **overrides):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise ValueError("Bilinmeyen ayar: " + ", ".join(sorted(unknown)))
        cfg.update(user)
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    cfg["root"] = os.path.abspath(cfg["root"])
    return cfg
