"""Normalized market-data contract (Phase 14 §4A): an AUDIT and adapters, not a new
event bus. Existing producers keep their own shapes; `normalize_*` maps them onto one
contract, adding the missing fields backward-compatibly.

Contract fields:
  exchange_id, market_id, instrument_id, data_type (book / ctx / quote / funding /
  trades), source_ts (s), source_ts_raw, source_unit (ms / s / us / iso / NOT_STATED),
  received_ts (s, local wall clock), sequence, prev_sequence, kind (snapshot / delta /
  full), raw_ref (where the raw answer is kept), evidence (OBSERVED / MOCK / ...).
"""
from v2.paper_obs import source_time

FIELDS = ("exchange_id", "market_id", "instrument_id", "data_type", "source_ts",
          "source_ts_raw", "source_unit", "received_ts", "sequence", "prev_sequence",
          "kind", "raw_ref", "evidence")

# What the existing producers carry today (audited 2026-11-13; "derived" = can be
# computed from what they keep, "missing" = not kept at all).
AUDIT = {
    "v2.ws_readonly.Books.record": dict(
        exchange_id="yes (venue key; hyperliquid covers native + tradeXYZ)",
        market_id="yes (coin / id / channel suffix)", instrument_id="txflow alias only",
        data_type="yes (channel)", source_ts="yes (s, converted from ms)",
        source_unit="derived (ms for txflow/hyperliquid; NOT_STATED for lighter_rh)",
        received_ts="yes (recv_ts)", sequence="txflow seq / lighter nonce; none for HL",
        kind="txflow/lighter snapshot|delta; HL full snapshot", raw_ref="first 2 frames only",
        evidence="implicit (live or MOCK by caller)"),
    "tools.v2_stream answers": dict(
        exchange_id="yes (xid)", market_id="in request body", instrument_id="missing",
        data_type="answer name", source_ts="yes for books/ctx (source_ts), none for ctx "
        "of HL", source_unit="field name only (book.time ms)", received_ts="yes (t0, t1)",
        sequence="missing (REST)", kind="full", raw_ref="stream.jsonl#cycle",
        evidence="OBSERVED (label)"),
    "v2.paper_obs.Observation": dict(
        exchange_id="yes (rows)", market_id="yes", instrument_id="missing",
        data_type="rows/books", source_ts="times[(venue, asset)] (mixed SOURCE / "
        "RECEIVE_TIME, labelled in time_basis)", source_unit="s", received_ts="answers t1",
        sequence="missing", kind="full", raw_ref="origin", evidence="OBSERVED"),
    "v2.collector rows (05:12 snapshot replay)": dict(
        exchange_id="yes", market_id="yes", instrument_id="underlying_id",
        data_type="row", source_ts="MISSING (replay sets every time to the snapshot `at`)",
        source_unit="n/a", received_ts="snapshot at only", sequence="missing",
        kind="full", raw_ref="answers.json.gz", evidence="HISTORICAL"),
}
UNITS = dict(txflow="ms", hyperliquid="ms", lighter_rh="NOT_STATED", variational="iso")


def normalize_ws(rec, venue, evidence="OBSERVED", instrument_id=None):
    """A v2.ws_readonly record -> contract dict (the record itself is unchanged)."""
    unit = UNITS.get(venue, "NOT_STATED")
    return dict(exchange_id=rec.get("venue") or venue, market_id=rec.get("market"),
                instrument_id=instrument_id, data_type=rec.get("channel"),
                source_ts=rec.get("source_ts"),
                source_ts_raw=rec.get("source_raw"), source_unit=unit,
                received_ts=rec.get("recv_ts"), sequence=rec.get("seq"),
                prev_sequence=rec.get("prev_seq"), kind=rec.get("kind") or (
                    "full" if rec.get("channel") == "l2Book" else None),
                raw_ref=None, evidence=evidence)


def normalize_answer(name, ans, raw_ref=None, evidence="OBSERVED"):
    """One saved REST answer (tools.v2_stream / tools.v2_aligned) -> contract dict."""
    st = ans.get("source_ts")
    if st is None and ans.get("ok"):
        st = source_time(name, ans.get("data"))
    return dict(exchange_id=ans.get("xid"), market_id=name.rsplit("_", 1)[-1]
                if "_" in name else None, instrument_id=None,
                data_type="book" if "book" in name else "ctx" if "ctx" in name else
                "stats" if "stats" in name else "history" if "_fh_" in name else name,
                source_ts=st, source_ts_raw=None,
                source_unit="ms" if st is not None else "NOT_PUBLISHED",
                received_ts=ans.get("t1"), sequence=None, prev_sequence=None, kind="full",
                raw_ref=raw_ref, evidence=evidence if ans.get("ok") else "FAILED")
