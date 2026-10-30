"""Published funding rate against the settlement that followed it.

For each settled payment of a market the snapshot used is the latest one observed
strictly inside that payment's own period: after the previous settlement and before
this one. When a snapshot carries the venue's announced next settlement time, it must
name this settlement. So a snapshot is never linked to an earlier or a later period,
each settlement yields at most one comparison, and a period the recorder did not watch
yields none.

Statuses:
  MATCHED            same sign; the size ratio |published / settled| is kept;
  SIGN_CONFLICT      opposite signs;
  TOO_SMALL          either rate below SIGN_EPS: no sign can be read;
  NO_SIGNED_RATE     the settled rate has no known sign (direction unknown);
  PENDING            the next settlement after the latest snapshot is not recorded yet;
  NOT_RECORDED       its settlement time plus the grace period passed without a record.
The unit of the published rate is not decided here: the reports read the ratios.
"""

import statistics

SIGN_EPS = 1e-9
SLACK = 60


def observed_interval(times):
    ts = sorted(set(int(round(t)) for t in times))
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    return statistics.median(gaps) if gaps else None


def compare(snapshots, settlements, now, grace, interval_hint=None):
    """(final comparisons, pending-or-missing comparisons) for one market."""
    snaps = sorted(
        (s for s in snapshots if s.get("published_rate") is not None),
        key=lambda s: s["received_at"],
    )
    paid = sorted(
        {int(round(s["settled_at"])): s for s in settlements}.values(),
        key=lambda s: s["settled_at"],
    )
    interval = observed_interval([s["settled_at"] for s in paid]) or interval_hint
    out = []
    for i, s in enumerate(paid):
        t = s["settled_at"]
        prev = paid[i - 1]["settled_at"] if i else None
        if prev is None or (interval and t - prev > 1.5 * interval):
            if not interval:
                continue
            prev = t - interval
        inside = [
            x
            for x in snaps
            if prev < x["received_at"] < t
            and (not x.get("next_funding_at") or abs(x["next_funding_at"] - t) <= SLACK)
        ]
        if not inside:
            continue
        x = inside[-1]
        out.append(result(x, s))
    pending = []
    last_paid = paid[-1]["settled_at"] if paid else None
    later = [x for x in snaps if last_paid is None or x["received_at"] > last_paid]
    if later and interval:
        x = later[-1]
        due = x.get("next_funding_at") or None
        expected = due or (last_paid + interval if last_paid else None)
        status = "PENDING"
        if expected and now > expected + grace:
            status = "NOT_RECORDED"
        pending.append(result(x, None, status, expected))
    return out, pending


def result(x, s, status=None, expected=None):
    p = x["published_rate"]
    r = s["settled_rate"] if s else None
    ratio = None
    if s is not None:
        if r is None:
            status = "NO_SIGNED_RATE"
        elif abs(p) < SIGN_EPS or abs(r) < SIGN_EPS:
            status = "TOO_SMALL"
        elif (p > 0) != (r > 0):
            status = "SIGN_CONFLICT"
            ratio = abs(p / r)
        else:
            status = "MATCHED"
            ratio = abs(p / r)
    return dict(
        venue=x["venue"],
        asset=x["asset"],
        market_id=x["market_id"],
        observed_at=x["received_at"],
        expected_settlement_at=x.get("next_funding_at") or expected,
        expected_settlement_source=(
            "API"
            if x.get("next_funding_at")
            else "gözlenen aralık" if expected else None
        ),
        published_rate_raw=x["published_rate_raw"],
        published_rate_unit=x.get("rate_unit"),
        settled_at=s["settled_at"] if s else None,
        settled_rate_raw=s["settled_rate_raw"] if s else None,
        settled_rate=r,
        settled_direction_raw=s.get("direction_raw") if s else None,
        settlement_revision=s.get("revision", 1) if s else None,
        ratio=ratio,
        comparison_status=status,
        snapshot_raw_sha256=x["raw_sha256"],
        settlement_raw_sha256=s["raw_sha256"] if s else None,
        source="public_api",
    )
