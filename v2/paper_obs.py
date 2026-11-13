"""Observed market moments for the ESTIMATED PAPER engine (offline, read-only).

One `Observation` is one burst of public reads (an aligned folder from tools.v2_aligned
or one cycle of tools.v2_stream). It answers the provider calls the strict gate and the
estimator use (rows_by, book, settled, closes, rth_open) plus rfq_record and leg times.

Time (Phase 8 B): every leg carries the SOURCE timestamps the venue published (book
`time`, TxFlow ctx `blockTime`, Variational quote `updated_at`); metaAndAssetCtxs has
none, so its receive time is used and labelled RECEIVE_TIME. Sequential reads are never
given one shared time: the decision time `at` is the last receive time of the burst,
each book's age is `at` minus its own source time, and `pair_time` reports the spread of
all leg times against the market-specific freshness limit (v2.opportunity_log).

No look-ahead: settled funding points are returned only up to the observation time,
even when the file holds a history read later (the stream's closing history).
"""
import gzip
import hashlib
import json
import statistics
from pathlib import Path

from v2 import estimated, opportunity_log, rfq_paper
from v2 import funding_evidence as fe
from v2 import funding_semantics as fs
from v2.venues import hyperliquid_obs as hlo
from v2.venues import tradexyz, txflow, variational

CORE = ("variational_stats", "xyz_ctx", "native_ctx", "txflow_ctx_NATGAS",
        "txflow_book_NATGAS", "xyz_book_NATGAS", "native_book_INIT")


def source_time(name, data):
    try:
        if name in ("xyz_book_NATGAS", "native_book_INIT"):
            return data["time"] / 1000.0
        if name == "txflow_book_NATGAS":
            return data["data"]["time"] / 1000.0
        if name == "txflow_ctx_NATGAS":
            return data["data"]["blockTime"] / 1000.0
    except (KeyError, TypeError):
        return None
    return None


class Observation:
    def __init__(self, answers, idx, closes=None, histories=None, sha=None, origin=None):
        A = answers
        self.A, self.sha, self.origin = A, sha, origin
        ok = lambda k: bool(A.get(k, {}).get("ok"))  # noqa: E731
        recv = lambda k: A[k]["t1"] if ok(k) else None  # noqa: E731

        def src(k):
            if not ok(k):
                return None, None
            st = A[k].get("source_ts")
            if st is None:
                st = source_time(k, A[k]["data"])
            return (st, "SOURCE") if st is not None else (A[k]["t1"], "RECEIVE_TIME")

        t1s = [recv(k) for k in CORE if recv(k)]
        self.at = max(t1s) if t1s else None
        self.burst_spread_s = (max(t1s) - min(t1s)) if t1s else None
        self.missing = [k for k in CORE if not ok(k)]
        self.cl = closes or {}
        self.rows, self.books, self.times, self.time_basis, self.hist = {}, {}, {}, {}, {}
        self.var = {}
        base = dict(kind="perp", interval=3600)
        if ok("variational_stats"):
            self.var = {r["instrument"].market_id: r
                        for r in variational.parse_markets(A["variational_stats"]["data"])}
            raw = {x.get("ticker"): x for x in A["variational_stats"]["data"].get("listings", [])}
            for mid, asset in (("NATGAS-PERP", "NATGAS"), ("INIT-PERP", "INIT")):
                r = self.var.get(mid)
                if r is None:
                    continue
                nxt = ((raw.get(mid) or {}).get("funding") or {}).get("next_timestamp")
                self.rows[("Variational", asset)] = dict(
                    base, venue="Variational", asset=asset, exchange_id="variational",
                    market_id=mid, rate=fe.per_hour(r["funding"]["raw"],
                                                    r["funding"]["interval_s"]),
                    interval=r["funding"]["interval_s"], fee=r["fee_taker"],
                    mark=r["mark"], next_funding=float(nxt) if nxt else None,
                    fine_class="commodity" if asset == "NATGAS" else "crypto")
                self.books["Variational", asset] = dict(
                    variational.quote_book(r, "size_1k"), model="rfq_indicative",
                    received=recv("variational_stats"))
                qa = rfq_paper.quote_age(r, recv("variational_stats"))
                qt = None if qa is None else recv("variational_stats") - qa
                self.times["Variational", asset] = [t for t in (qt,) if t is not None]
                self.time_basis["Variational", asset] = "QUOTE_UPDATED_AT" if qt else \
                    "QUOTE_TIME_UNKNOWN"
        if idx and ok("txflow_ctx_NATGAS") and ok("txflow_book_NATGAS") and A.get(
                "txflow_meta", {}).get("ok", True):
            meta = (A.get("txflow_meta") or {}).get("data")
            if meta is not None:
                meta = meta if "data" in meta else dict(data=meta)
                recs = txflow.parse_markets(meta, None, {idx: A["txflow_ctx_NATGAS"]["data"]})
                tr = next((r for r in recs if r["instrument"].market_id == idx), None)
                if tr is not None:
                    f = tr["funding"]
                    ctx = A["txflow_ctx_NATGAS"]["data"]["data"]
                    nf = ctx.get("nextFundingTime") or (ctx.get("nodeCtx") or {}).get(
                        "nextFundingTime")
                    self.rows[("TxFlow", "NATGAS")] = dict(
                        base, venue="TxFlow", asset="NATGAS", exchange_id="txflow",
                        market_id=idx, rate=fs.hourly(f, "txflow"),  # one unit path (P0-A)
                        fee=tr["fee_taker"], mark=tr["mark"],
                        next_funding=nf / 1000.0 if nf else None, fine_class="commodity")
                    bt, _ = src("txflow_book_NATGAS")
                    self.books["TxFlow", "NATGAS"] = dict(
                        txflow.parse_book(A["txflow_book_NATGAS"]["data"]), received=bt)
                    self.times["TxFlow", "NATGAS"] = [src("txflow_ctx_NATGAS")[0], bt]
                    self.time_basis["TxFlow", "NATGAS"] = "SOURCE"
        if ok("xyz_ctx") and ok("xyz_book_NATGAS"):
            xyz = {m["instrument"].market_id: m for m in tradexyz.parse_markets(A["xyz_ctx"]["data"])}
            xr = xyz.get("xyz:NATGAS")
            if xr:
                self.rows[("tradeXYZ", "NATGAS")] = dict(
                    base, venue="tradeXYZ", asset="NATGAS", exchange_id="tradexyz",
                    market_id="xyz:NATGAS", rate=xr["funding"]["raw"], fee=xr["fee_taker"],
                    mark=xr["mark"], fine_class="commodity")
                bt, _ = src("xyz_book_NATGAS")
                self.books["tradeXYZ", "NATGAS"] = self.l2(A["xyz_book_NATGAS"]["data"], bt)
                self.times["tradeXYZ", "NATGAS"] = [recv("xyz_ctx"), bt]
                self.time_basis["tradeXYZ", "NATGAS"] = "SOURCE+RECEIVE_TIME(ctx)"
        if ok("native_ctx") and ok("native_book_INIT"):
            nat = {m["base"]: m for m in hlo.parse_markets("hyperliquid_native",
                                                            A["native_ctx"]["data"])}
            nr = nat.get("INIT")
            if nr:
                self.rows[("Hyperliquid", "INIT")] = dict(
                    base, venue="Hyperliquid", asset="INIT", exchange_id="hyperliquid_native",
                    market_id="INIT", rate=nr["funding"]["raw"], fee=nr["fee_taker"],
                    mark=nr["mark"], fine_class="crypto")
                bt, _ = src("native_book_INIT")
                self.books["Hyperliquid", "INIT"] = self.l2(A["native_book_INIT"]["data"], bt)
                self.times["Hyperliquid", "INIT"] = [recv("native_ctx"), bt]
                self.time_basis["Hyperliquid", "INIT"] = "SOURCE+RECEIVE_TIME(ctx)"
        # histories: {answer name: [row lists]} read elsewhere (stream start / end);
        # this burst's own history read is added. Points are merged by time.
        H = {k: list((histories or {}).get(k) or []) for k in ("xyz_fh_NATGAS",
                                                                "native_fh_INIT")}
        for k in H:
            if ok(k):
                H[k].append(A[k]["data"])
        for k, key in (("xyz_fh_NATGAS", ("tradeXYZ", "NATGAS")),
                       ("native_fh_INIT", ("Hyperliquid", "INIT"))):
            pts = {}
            for rows_ in H[k]:
                for x in rows_ or []:
                    pts[x["ts"]] = dict(x, kind="settled_public")
            if pts:
                self.hist[key] = [pts[t] for t in sorted(pts)]

    @staticmethod
    def l2(raw, received):
        lv = (raw or {}).get("levels") or [[], []]
        return dict(bids=[(float(x["px"]), float(x["sz"])) for x in lv[0]],
                    asks=[(float(x["px"]), float(x["sz"])) for x in lv[1]],
                    model="orderbook", empty=not lv[0] or not lv[1], crossed=False,
                    received=received)

    # -- provider interface ------------------------------------------------------------
    def rows_by(self):
        return dict(self.rows)

    def book(self, v, a):
        return self.books.get((v, a))

    def settled(self, v, a, at):
        cut = min(at, self.at) if at is not None and self.at is not None else (at or self.at)
        return [x for x in self.hist.get((v, a)) or [] if x["ts"] <= cut]

    def closes(self, v, a):
        """Hourly closes strictly before this observation (no look-ahead)."""
        c = self.cl.get((v, a))
        if not c or self.at is None:
            return c
        return {t: p for t, p in c.items() if t < self.at}

    def rth_open(self):
        return None

    def rfq_record(self, v, a):
        row = self.rows.get((v, a)) or {}
        return self.var.get(row.get("market_id")) if v == "Variational" else None

    def pair_time(self, lv, sv, asset):
        """(skew seconds, freshness limit, basis) over every leg time of the pair."""
        ts = [x for k in ((lv, asset), (sv, asset)) for x in self.times.get(k, [])
              if x is not None]
        skew = (max(ts) - min(ts)) if ts else None
        sig = [estimated.sigma_hourly(self.cl.get((v, asset))) for v in (lv, sv)]
        sig = [s for s in sig if s is not None]
        lim, basis = opportunity_log.freshness_limit(max(sig) if sig else None)
        return skew, lim, basis


def from_aligned(folder, closes=None):
    raw = gzip.decompress((Path(folder) / "aligned.json.gz").read_bytes())
    blob = json.loads(raw)
    return Observation(blob["answers"], blob.get("txflow_natgas_index"), closes,
                       sha=hashlib.sha256(raw).hexdigest(), origin=str(folder))


def from_stream(path, closes=None):
    """Observations of one bounded stream (hash chain verified first). Funding
    histories from the start and end reads are merged; each observation sees only
    points up to its own time."""
    from v2 import storage

    recs, report = storage.read_chain(path)
    if report["status"] not in ("OK", "TORN_TAIL"):
        raise ValueError("stream chain %s: %s" % (path, report))
    start = next((r for r in recs if r.get("kind") == "start"), {})
    end = next((r for r in recs if r.get("kind") == "end"), {})
    meta = start.get("txflow_meta")
    hist = {k: [h[k]["data"] for h in (start.get("histories"), end.get("histories"))
                if h and h.get(k, {}).get("ok")]
            for k in ("xyz_fh_NATGAS", "native_fh_INIT")}
    out = []
    for r in recs:
        if r.get("kind") != "cycle":
            continue
        A = dict(r["answers"])
        if meta is not None:
            A["txflow_meta"] = meta
        sha = hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()
        out.append(Observation(A, r.get("txflow_natgas_index"), closes, histories=hist,
                               sha=sha, origin="%s#%d" % (path, r["n"])))
    return out, report


def median_or_none(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def integrity(observations, expected_every=None):
    """Timestamp integrity of a sequence of observations, per leg (Phase 8 B).

    Venues publish no sequence numbers on these REST answers (UNKNOWN), so ordering is
    checked on the venues' own timestamps:
      backwards      a source time earlier than the previous one (out of order)
      repeated       the same source time again (no new data: a cached answer)
      gaps           receive-time gaps longer than 2 x the polling interval
      stale_books    order books older than v2.marketdata.MAX_AGE at decision time
      missing        answers that failed (counted per answer name)
    """
    import collections

    from v2 import marketdata

    out = {}
    prev = {}
    missing = collections.Counter()
    gaps = []
    last_at = None
    for o in observations:
        for k in o.missing:
            missing[k] += 1
        if last_at is not None and expected_every and o.at - last_at > 2 * expected_every:
            gaps.append(dict(after=last_at, before=o.at, seconds=o.at - last_at))
        last_at = o.at
        for key, ts in o.times.items():
            st = out.setdefault("%s/%s" % key, dict(points=0, backwards=0, repeated=0,
                                                    stale_books=0, max_book_age_s=0.0,
                                                    basis=o.time_basis.get(key)))
            st["points"] += 1
            src = ts[-1] if ts else None
            p = prev.get(key)
            if src is not None and p is not None:
                if src < p:
                    st["backwards"] += 1
                elif src == p:
                    st["repeated"] += 1
            prev[key] = src
            b = o.books.get(key)
            if b and b.get("model") == "orderbook" and b.get("received") is not None:
                age = o.at - b["received"]
                st["max_book_age_s"] = max(st["max_book_age_s"], age)
                xid = (o.rows.get(key) or {}).get("exchange_id")
                if marketdata.stale(b, xid, o.at):
                    st["stale_books"] += 1
    return dict(legs=out, missing_answers=dict(missing), polling_gaps=gaps,
                sequence_numbers="UNKNOWN (REST answers carry none)")
