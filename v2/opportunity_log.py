"""Opportunity lifecycle and data-freshness limits (PAPER research).

An opportunity is one (asset, long venue, short venue, strategy). From a time series
of observations (each from ONE aligned read: price and funding of the same moment) it
records:
  first_seen / last_seen / duration_s   over observations where it was present
  max_net_advantage                     the best estimated advantage seen (carry /8h or
                                        estimated net, as given)
  liquid_duration_s                     time between consecutive observations where the
                                        size was fillable on both legs
  estimated_vs_verified                 estimated return minus verified return, where a
                                        verified (settled) return exists; else None
Observations are points in time; durations between points are bounded by the sampling
interval and are reported with the number of points (no interpolation).

freshness_limit(): a market-specific staleness limit. With hourly volatility sigma, a
price that is `age` seconds old has a 1-sigma error of sigma*sqrt(age/3600). The limit
is the age at which that error reaches `tolerance_bp`, clamped to [floor, cap] (cap =
the general 60 s rule). Unknown volatility -> the cap, labelled VOL_UNKNOWN.
"""
import json
import math
from pathlib import Path

GENERAL_CAP_S = 60.0
FLOOR_S = 2.0


def freshness_limit(sigma_hourly, tolerance_bp=5.0, floor=FLOOR_S, cap=GENERAL_CAP_S):
    """(seconds, basis)."""
    if sigma_hourly is None or sigma_hourly <= 0:
        return cap, "VOL_UNKNOWN"
    age = 3600.0 * (tolerance_bp * 1e-4 / sigma_hourly) ** 2
    if age >= cap:
        return cap, "CAP"
    if age <= floor:
        return floor, "FLOOR"
    return age, "VOLATILITY"


def key(o):
    return "%s|%s|%s|%s" % (o.get("strategy", "funding"), o["asset"], o["long"], o["short"])


def lifecycle(observations):
    """{key: summary} from observations [dict(at, asset, long, short, strategy?,
    present, advantage, liquid, estimated_return?, verified_return?)]."""
    by = {}
    for o in sorted(observations, key=lambda x: x["at"]):
        by.setdefault(key(o), []).append(o)
    out = {}
    for k, obs in by.items():
        seen = [o for o in obs if o.get("present")]
        liquid = 0.0
        for a, b in zip(obs, obs[1:]):
            if a.get("present") and a.get("liquid") and b.get("present") and b.get("liquid"):
                liquid += b["at"] - a["at"]
        adv = [o["advantage"] for o in seen if o.get("advantage") is not None]
        diffs = [o["estimated_return"] - o["verified_return"] for o in seen
                 if o.get("estimated_return") is not None
                 and o.get("verified_return") is not None]
        out[k] = dict(
            points=len(obs), points_present=len(seen),
            first_seen=seen[0]["at"] if seen else None,
            last_seen=seen[-1]["at"] if seen else None,
            duration_s=(seen[-1]["at"] - seen[0]["at"]) if seen else 0.0,
            max_net_advantage=max(adv) if adv else None,
            liquid_duration_s=liquid,
            estimated_vs_verified=(sum(diffs) / len(diffs)) if diffs else None,
            verified="NONE" if not diffs else "PARTIAL" if len(diffs) < len(seen) else "ALL",
            sampling="points only; no interpolation between observations")
    return out


class OpportunityLog:
    """Append-only JSONL of observations (one line each) with lifecycle summaries."""

    def __init__(self, path):
        self.path = Path(path)

    def record(self, obs):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obs, default=str) + "\n")

    def observations(self):
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines()
                if x.strip()]

    def summary(self):
        return lifecycle(self.observations())


def sigma_from_returns(returns):
    if len(returns) < 2:
        return None
    m = sum(returns) / len(returns)
    return math.sqrt(sum((r - m) ** 2 for r in returns) / len(returns))
