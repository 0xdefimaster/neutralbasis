"""Exchange capability matrix (Phase 14 §4B): every venue of the 8 PAPER / 11
OBSERVATION universe, every capability, an evidence level each.

Evidence levels:
  DOCUMENTED     the venue's official docs describe it (not read by us)
  MOCK_TESTED    our adapter/parser is tested on MOCK frames or answers only
  LIVE_OBSERVED  we read it from the real venue at `as_of` (a past moment)
  VERIFIED       a live observation cross-checked against an independent source
                 (e.g. a trade recomputed line by line from raw answers)
  UNVERIFIED     claimed or partially seen, not proven
  UNSUPPORTED    the venue does not offer it, or our policy forbids it

A LIVE_OBSERVED entry is HISTORICAL: `current` is true only if `as_of` is within
CURRENT_S of the evaluation time. An old live test is never shown as a current link.
Sources: v2.observation.VENUE_MATRIX (docs/v2/25), v2.funding_evidence.VENUE_BEST,
v2.ws_readonly.VENUES, saved probe / stream / snapshot artifacts (paths below).
"""
import gzip
import json
import time

from v2 import funding_evidence as fe
from v2 import observation, paths, policy, ws_readonly

LEVELS = ("DOCUMENTED", "MOCK_TESTED", "LIVE_OBSERVED", "VERIFIED", "UNVERIFIED",
          "UNSUPPORTED")
CAPABILITIES = ("rest", "websocket", "orderbook", "public_trades", "funding_current",
                "funding_history", "mark_index", "fees", "source_timestamps",
                "instrument_identity", "settlement_evidence", "execution_model")
CURRENT_S = 3600.0
VENUES = tuple(dict.fromkeys(policy.EXCHANGE_IDS + observation.OBSERVATION_UNIVERSE_V3))
WS_VENUE = dict(txflow="txflow", tradexyz="hyperliquid", hyperliquid_native="hyperliquid",
                lighter_rh="lighter_rh")
# source-time fields seen in real answers (Phase 8-11); lighter_rh: unit NOT STATED
SOURCE_TS = dict(txflow="book.time / ctx.blockTime (ms)", tradexyz="l2Book.time (ms)",
                 hyperliquid_native="l2Book.time (ms)",
                 variational="quotes.updated_at (ISO, ns)")


def _matrix_level(v):
    s = str(v or "")
    if s.startswith("OK") or s.startswith("MARKET_SETTLEMENT_CONFIRMED") and \
            "route allowed" not in s:
        return "LIVE_OBSERVED"
    if s.startswith("ROUTE_ALLOWED") or "route allowed" in s:
        return "DOCUMENTED"
    if s.startswith("NOT_ALLOWED"):
        return "UNSUPPORTED"
    if s.startswith("RFQ_INDICATIVE"):
        return "LIVE_OBSERVED"
    if s.startswith(("KNOWN", "PUBLISHED", "DERIVED", "0 (")):
        return "DOCUMENTED"
    return "UNVERIFIED"


def artifact_index(bench=None):
    """{venue: {"rest": [as_of...], "ws": [as_of...]}} from saved artifacts (small
    reads; the 05:12 snapshot is read once)."""
    bench = bench or paths.bench()
    idx = {}

    def add(v, kind, t, src):
        if v and t:
            idx.setdefault(v, {}).setdefault(kind, []).append(dict(as_of=float(t), source=src))

    p4 = bench / "v2-phase4-2026-11-12" / "answers.json.gz"
    if p4.exists():
        b = json.loads(gzip.decompress(p4.read_bytes()))
        for key in (b.get("answers") or {}):
            # keys look like "tradexyz POST https://... {body}": the venue is the first word
            add(str(key).split(" ", 1)[0], "rest", b.get("at"), str(p4))
    ob = bench / "v2-forensics-2026-11-12" / "observation.json.gz"
    if ob.exists():
        b = json.loads(gzip.decompress(ob.read_bytes()))
        for a in (b.get("answers") or {}).values():
            if a.get("ok"):
                add(a.get("xid"), "rest", a.get("at"), str(ob))
    for man in list(bench.glob("v2-phase8-rescue-*/stream-*/stream_manifest.json")) + \
            list(bench.glob("v2-paper-data/probe-*/stream_manifest.json")):
        m = json.loads(man.read_text(encoding="utf-8"))
        for v, n in (m.get("by_venue") or {}).items():
            if n:
                add(v, "rest", m.get("created"), str(man))
    for rep in bench.glob("v2-paper-data/ws-probe-*/ws_probe_report.json"):
        r = json.loads(rep.read_text(encoding="utf-8"))
        for v, x in (r.get("results") or {}).items():
            ok = x.get("connected") and sum((x.get("messages") or {}).values()) > 0
            if not ok:
                continue
            for xid in ([v] if v != "hyperliquid" else ["hyperliquid_native", "tradexyz"]):
                add(xid, "ws", r.get("created"), str(rep))
    for v in idx:
        for k in idx[v]:
            idx[v][k].sort(key=lambda e: e["as_of"])
    return idx


def cap(level, value=None, source=None, as_of=None, now=None):
    assert level in LEVELS, level
    cur = None if as_of is None else (now or time.time()) - as_of <= CURRENT_S
    return dict(level=level, value=value, source=source, as_of=as_of,
                current=bool(cur) if level == "LIVE_OBSERVED" else None)


def matrix(now=None, index=None):
    now = now or time.time()
    index = artifact_index() if index is None else index
    out = {}
    for v in VENUES:
        vm = observation.VENUE_MATRIX.get(v, {})
        live = index.get(v, {})
        last_rest = live.get("rest", [])[-1] if live.get("rest") else None
        last_ws = live.get("ws", [])[-1] if live.get("ws") else None
        best = fe.VENUE_BEST.get(v, fe.MISSING)
        c = {}
        c["rest"] = cap("LIVE_OBSERVED", "public REST", last_rest["source"], last_rest["as_of"],
                        now) if last_rest else cap(_matrix_level(vm.get("metadata")),
                                                   vm.get("metadata"), "docs/v2/25")
        if v in WS_VENUE and last_ws:
            c["websocket"] = cap("LIVE_OBSERVED", ws_readonly.VENUES[WS_VENUE[v]]["url"],
                                 last_ws["source"], last_ws["as_of"], now)
        elif v in WS_VENUE:
            c["websocket"] = cap("MOCK_TESTED", ws_readonly.VENUES[WS_VENUE[v]]["url"],
                                 "test_v2_phase10/11")
        elif v == "variational":
            c["websocket"] = cap("UNSUPPORTED", "no WebSocket documented", "Variational API docs")
        elif v == "qfex":
            c["websocket"] = cap("DOCUMENTED", "market-data WS documented; routes UNKNOWN",
                                 "docs/v2/25")
        else:
            c["websocket"] = cap("UNVERIFIED", "not researched", None)
        c["orderbook"] = cap(_matrix_level(vm.get("book")), vm.get("book"), "docs/v2/25",
                             last_rest["as_of"] if last_rest and
                             _matrix_level(vm.get("book")) == "LIVE_OBSERVED" else None, now)
        c["public_trades"] = cap(_matrix_level(vm.get("trades")), vm.get("trades"), "docs/v2/25")
        c["funding_current"] = cap(_matrix_level(vm.get("funding_now")), vm.get("funding_now"),
                                   "docs/v2/25")
        if best in (fe.RATE_PUBLISHED, fe.RATE_SAMPLED_AT_CUTOFF):
            c["funding_history"] = cap("UNSUPPORTED", "no public settled history (%s)" % best,
                                       "v2.funding_evidence")
        else:
            c["funding_history"] = cap(_matrix_level(vm.get("funding_history")),
                                       vm.get("funding_history"), "docs/v2/25")
        c["mark_index"] = cap("LIVE_OBSERVED" if c["rest"]["level"] == "LIVE_OBSERVED" and
                              vm.get("funding_now") == "OK" else "UNVERIFIED",
                              "mark/oracle in the market context", "adapters",
                              last_rest["as_of"] if last_rest else None, now)
        c["fees"] = cap("UNVERIFIED" if "UNKNOWN" in str(vm.get("fees")) else
                        _matrix_level(vm.get("fees")), vm.get("fees"), "docs/v2/25")
        c["source_timestamps"] = (cap("LIVE_OBSERVED", SOURCE_TS[v], "Phase 8-11 streams/probes",
                                      last_rest["as_of"] if last_rest else None, now)
                                  if v in SOURCE_TS else
                                  cap("UNVERIFIED", "lighter_rh last_updated_at: unit NOT STATED"
                                      if v == "lighter_rh" else "not established", None))
        ident = bool(v in policy.EXCHANGE_IDS and last_rest)
        c["instrument_identity"] = cap("LIVE_OBSERVED" if ident else "UNVERIFIED",
                                       "v2.identity groups (05:12 snapshot)" if ident
                                       else "no verified identity groups for this venue",
                                       "v2/source.py")
        if best == fe.MARKET_SETTLEMENT:
            c["settlement_evidence"] = cap("LIVE_OBSERVED", "public market settlement history; "
                                           "account payments UNVERIFIED", "v2.funding_evidence")
        else:
            c["settlement_evidence"] = cap("UNVERIFIED" if best != fe.MISSING else "UNSUPPORTED",
                                           "%s; account payments UNVERIFIED" % best,
                                           "v2.funding_evidence")
        if v == "variational":
            c["execution_model"] = cap("UNSUPPORTED", "indicative RFQ only; no firm quote "
                                       "(trading API not available)", "Variational API docs")
        elif not observation.is_paper_venue(v):
            c["execution_model"] = cap("UNSUPPORTED", "observation only (not a PAPER venue)",
                                       "v2.observation")
        elif vm.get("book") == "NO_VERIFIED_API" or vm.get("metadata") == "NO_VERIFIED_API":
            c["execution_model"] = cap("UNSUPPORTED", "no verified public API", "docs/v2/25")
        else:
            c["execution_model"] = cap("UNVERIFIED", "order book: SIMULATED fills only; no "
                                       "real execution evidence (PAPER)", "v2.paper_exec")
        last = max([e["as_of"] for k in ("rest", "ws") for e in live.get(k, [])] or [0]) or None
        out[v] = dict(
            exchange_id=v, paper_venue=observation.is_paper_venue(v),
            observation_venue=v in observation.OBSERVATION_UNIVERSE_V3,
            status=vm.get("status", "UNKNOWN"), capabilities=c,
            last_successful_verification=last,
            last_verification_age_s=None if last is None else now - last,
            last_verification_current=None if last is None else now - last <= CURRENT_S)
    return out
