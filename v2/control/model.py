"""Health model (Phase 14 §4): independent dimensions, explicit statuses, explicit units.

A record describes ONE (exchange, market, channel, dimension). Connectivity, data
freshness and funding evidence are separate dimensions: an open socket says nothing
about a fresh price, a fresh price says nothing about settled funding.

Unknown is never good: a missing input yields NOT_TESTED / UNVERIFIED / HISTORICAL_ONLY,
never HEALTHY. All times are UTC epoch SECONDS (field `time_unit` says so); durations
are seconds.
"""
DIMENSIONS = ("connectivity", "freshness", "integrity", "funding_evidence",
              "execution_model", "market_session", "storage_recorder")
STATUSES = ("HEALTHY", "DEGRADED", "STALE", "DISCONNECTED", "RATE_LIMITED",
            "INTEGRITY_FAILURE", "UNVERIFIED", "NOT_SUPPORTED", "NOT_TESTED",
            "HISTORICAL_ONLY")
GOOD = ("HEALTHY",)
SEVERITY = dict(HEALTHY=0, NOT_SUPPORTED=1, HISTORICAL_ONLY=1, NOT_TESTED=2, UNVERIFIED=2,
                DEGRADED=3, STALE=3, RATE_LIMITED=3, DISCONNECTED=4, INTEGRITY_FAILURE=5)
DATA_SOURCES = ("OBSERVED", "HISTORICAL", "MOCK", "ESTIMATED", "VERIFIED")
FIELDS = ("exchange_id", "market_id", "channel", "dimension", "observed_at",
          "source_timestamp", "received_at", "last_message_at", "last_data_change_at",
          "expected_cadence", "observed_cadence", "health_status", "health_reason",
          "evidence_level", "sample_count", "clock_confidence", "data_source",
          "next_action", "time_unit")


class ModelError(ValueError):
    pass


def record(exchange_id, dimension, health_status, health_reason, *, market_id=None,
           channel=None, data_source="HISTORICAL", evidence_level=None, next_action=None,
           **kw):
    """A validated health record. HEALTHY needs a reason AND evidence; anything not
    proven is downgraded, never upgraded."""
    if dimension not in DIMENSIONS:
        raise ModelError("unknown dimension %r" % dimension)
    if health_status not in STATUSES:
        raise ModelError("unknown status %r" % health_status)
    if data_source not in DATA_SOURCES:
        raise ModelError("unknown data source %r" % data_source)
    if health_status == "HEALTHY" and (not evidence_level or not health_reason):
        health_status, health_reason = "UNVERIFIED", "HEALTHY claimed without evidence"
    if health_status == "HEALTHY" and data_source == "MOCK":
        health_status, health_reason = "UNVERIFIED", "MOCK data cannot prove health"
    out = {f: None for f in FIELDS}
    out.update(kw)
    out.update(exchange_id=exchange_id, market_id=market_id, channel=channel,
               dimension=dimension, health_status=health_status,
               health_reason=health_reason, data_source=data_source,
               evidence_level=evidence_level, next_action=next_action, time_unit="s")
    unknown = set(out) - set(FIELDS)
    if unknown:
        raise ModelError("unknown fields %s" % sorted(unknown))
    return out


def worst(statuses):
    xs = [s for s in statuses if s]
    return max(xs, key=lambda s: SEVERITY[s]) if xs else "NOT_TESTED"
