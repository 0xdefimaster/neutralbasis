"""Offline checks of what was recorded: hashes, schemas, replay of raw responses through
the parsers, coverage and consistency. Nothing here connects to an API.

Field states in the reports:
  VERIFIED  measured from recorded data and agreeing with the declaration;
  OBSERVED  present in the venue's responses or measured, without a declaration to
            confirm it (or the documentation could not be read);
  DECLARED  stated by the project's connector, not checked by data;
  UNKNOWN   too little data, or the venue does not publish it;
  CONFLICT  the data contradicts the declaration or itself.
"""

import gzip
import hashlib
import json
import os
import statistics
import time
import urllib.parse

from .clients import DISCOVERY, LABELS, make_client
from .clients.base import CONFLICT, DECLARED, OBSERVED, UNKNOWN, VERIFIED
from .clients.records import (
    REQUIRED,
    configured_tolerance,
    counted,
    record_key,
    same_event_tolerance,
)
from .matching import observed_interval
from .storage import VENUE_DIRS, iso, sha256

INTERVAL_MIN = 6
RATIO_OK = (0.8, 1.25)
RATIO_CONFLICT = (0.25, 4.0)


# --- hashes and schemas -----------------------------------------------------------
def verify_hashes(store):
    """[(path, problem)] for raw files whose content no longer matches their hash."""
    bad = []
    with store.lock:
        rows = store.db.execute("select sha256, path from raw").fetchall()
    for digest, rel in rows:
        path = os.path.join(store.root, rel)
        try:
            with gzip.open(path, "rb") as f:
                if sha256(f.read()) != digest:
                    bad.append((rel, "hash uyuşmuyor"))
        except OSError as ex:
            bad.append((rel, "okunamadı: %s" % ex))
    return bad, len(rows)


def verify_schemas(store):
    """([(kind, file, line, missing fields)], records, [(kind, file, line)] of lines
    cut off by a crash, {kind: lines repeating a record key}). Repeated keys come from
    a crash between a line and its index row; readers keep one record per key."""
    bad, count, torn, repeated = [], 0, [], {}
    for kind, need in REQUIRED.items():
        base = os.path.join(store.root, "normalized", kind)
        if not os.path.isdir(base):
            continue
        seen = {}  # venue -> keys seen: the same (venue, key) test, smaller
        for d in sorted(os.listdir(base)):
            for name in sorted(os.listdir(os.path.join(base, d))):
                path = os.path.join(base, d, name)
                with open(path, encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if not line.strip():
                            continue
                        count += 1
                        try:
                            r = json.loads(line)
                        except ValueError:
                            # A line cut off by a crash; its record is not used.
                            torn.append((kind, name, i))
                            continue
                        miss = [k for k in need if k not in r]
                        if miss:
                            bad.append((kind, name, i, miss))
                            continue
                        keys = seen.setdefault(r["venue"], set())
                        key = record_key(kind, r)
                        if key in keys:
                            repeated[kind] = repeated.get(kind, 0) + 1
                        keys.add(key)
    return bad, count, torn, repeated


# --- replay -----------------------------------------------------------------------
def _context(venue, url, params, body, asset_of):
    body = json.loads(body) if body else {}
    params = json.loads(params) if params else {}
    mid = None
    if venue == "Hyperliquid":
        mid = body.get("coin") or (body.get("req") or {}).get("coin")
    elif venue in ("Lighter", "LighterCore"):
        mid = params.get("market_id")
        mid = int(mid) if mid is not None and str(mid).isdigit() else mid
    elif venue == "Extended":
        parts = urllib.parse.urlsplit(url).path.split("/")
        for i, p in enumerate(parts):
            if p in ("info", "candles", "markets") and i + 1 < len(parts):
                nxt = parts[i + 1]
                if nxt not in ("candles", "markets", "funding", "trades", "orderbook"):
                    mid = nxt
        mid = mid
    return dict(market_id=mid, asset=asset_of.get(str(mid), mid), body=body)


def signature(kind, r):
    """What the venue published in a record, without receive times (a raw body may be
    stored once and seen again later)."""
    mid = str(r["market_id"])
    if kind == "market":
        return (
            mid,
            r["max_leverage"],
            r["amount_step"],
            r["min_order_size"],
            r["active"],
        )
    if kind == "funding_snapshot":
        return (mid, str(r["published_rate_raw"]))
    if kind == "settlement":
        return (
            mid,
            int(round(r.get("settled_at_api", r["settled_at"]))),
            str(r["settled_rate_raw"]),
            r["direction_raw"],
        )
    if kind == "candle":
        return (mid, int(r["open_time"]), r["close"])
    if kind == "orderbook":
        return (mid, json.dumps(r["bids"]), json.dumps(r["asks"]))
    if kind == "schema":
        return (mid, r["schema_sha256"])
    return (mid,)


def _same(x):
    """A value as Python's == sees it: 1, 1.0 and True are one number."""
    if isinstance(x, (bool, int, float)):
        return float(x)
    return x


def _digest(*parts):
    """16-byte digest of a record identity, kept instead of the identity itself
    (a book's identity holds both sides of the book as text). Equal identities give
    equal digests; a collision of different ones is not a practical concern
    (blake2b, 128 bits)."""
    flat = [_same(p) for p in parts[:2]] + [_same(x) for x in parts[2]]
    raw = json.dumps(flat, ensure_ascii=False, separators=(",", ":"))
    return hashlib.blake2b(raw.encode(), digest_size=16).digest()


AUDIT_PAGE = 5000


def replay(store, cfg):
    """Re-parse every successful stored response offline and check that each stored
    record is reproduced by a response of its own venue. Returns (checked responses,
    problems). Responses are read page by page and records stream past one venue
    and kind at a time; only digests are held, so days of records fit in memory."""
    asset_of = {}
    for m in store.records("market"):
        asset_of[str(m["market_id"])] = m["asset"]
    clients = {v: make_client(v, store, cfg) for v in VENUE_DIRS}
    covered, problems, done = set(), [], set()
    last = -1
    while True:
        with store.lock:
            rows = store.db.execute(
                "select id, venue, endpoint, url, params, body, response_at, sha256"
                " from audit where error is null and sha256 is not null and id > ?"
                " order by id limit ?",
                (last, AUDIT_PAGE),
            ).fetchall()
        if not rows:
            break
        last = rows[-1][0]
        for _, venue, endpoint, url, params, body, at, digest in rows:
            ctx = _context(venue, url, params, body, asset_of)
            key = _digest(venue, endpoint, (digest, str(ctx["market_id"])))
            if key in done:
                continue
            done.add(key)
            try:
                obj = json.loads(store.read_raw(digest))
                out = clients[venue].replay(endpoint, obj, ctx, at, digest)
            except Exception as ex:
                problems.append((digest, endpoint, "yeniden ayrıştırılamadı: %s" % ex))
                continue
            for kind, rs in out.items():
                covered.update(_digest(venue, kind, signature(kind, r)) for r in rs)
    for kind in REQUIRED:
        if kind == "comparison":
            continue
        base = os.path.join(store.root, "normalized", kind)
        if not os.path.isdir(base):
            continue
        for d in sorted(os.listdir(base)):
            # One record per key, the last line winning, in first-seen order: as
            # `Store.records` reads them.
            last_seen = {}
            for r in store.iter_records(kind, d):
                last_seen[(r.get("venue"), record_key(kind, r))] = (
                    r.get("raw_sha256"),
                    _digest(r["venue"], kind, signature(kind, r)),
                )
            for sha, ident in last_seen.values():
                if ident not in covered:
                    problems.append(
                        (sha, kind, "%s kaydı ham yanıttan yeniden üretilemedi" % kind)
                    )
    return len(done), problems


# --- coverage ---------------------------------------------------------------------
def _status_interval(paid, declared):
    gap = observed_interval([p["settled_at"] for p in paid])
    if len(paid) < INTERVAL_MIN or gap is None:
        return UNKNOWN, gap
    ts = sorted({int(round(p["settled_at"])) for p in paid})
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    regular = sum(abs(g - gap) <= 0.05 * gap for g in gaps) / len(gaps)
    if declared and abs(gap - declared) <= 0.05 * declared and regular >= 0.9:
        return VERIFIED, gap
    if declared and abs(gap - declared) > 0.05 * declared:
        return CONFLICT, gap
    return OBSERVED, gap


def _status_unit(comps):
    ratios = [c["ratio"] for c in comps if c["comparison_status"] == "MATCHED"]
    if len(ratios) < 3:
        return UNKNOWN, None
    med = statistics.median(ratios)
    if RATIO_OK[0] <= med <= RATIO_OK[1]:
        # Same size as the settled per-payment rate; the documentation was not read,
        # so this stays an observation.
        return OBSERVED, med
    if not RATIO_CONFLICT[0] <= med <= RATIO_CONFLICT[1]:
        return CONFLICT, med
    return UNKNOWN, med


def _status_direction(paid, comps, client):
    raw = [p.get("direction_raw") for p in paid if p.get("direction_raw") is not None]
    signed = [
        c for c in comps if c["comparison_status"] in ("MATCHED", "SIGN_CONFLICT")
    ]
    conflict = sum(c["comparison_status"] == "SIGN_CONFLICT" for c in signed)
    if len(signed) >= 3 and conflict / len(signed) > 0.2:
        return CONFLICT
    if raw:
        return OBSERVED
    return DECLARED if client.declared_direction else UNKNOWN


def coverage(store, cfg, now=None):
    now = now or time.time()
    end = int(now) // 3600 * 3600
    start = end - cfg["backfill_days"] * 86400
    expected_hours = (end - start) // 3600
    out = dict(
        generated_at=now,
        generated_iso=iso(now),
        window_days=cfg["backfill_days"],
        venues={},
    )
    with store.lock:
        audit = store.db.execute(
            "select venue, count(*), sum(error is null), sum(rate_limited),"
            " max(case when error is null then response_at end),"
            " max(case when error is not null then response_at end)"
            " from audit group by venue"
        ).fetchall()
        last_errors = dict(
            store.db.execute(
                "select venue, error from audit a where error is not null and id ="
                " (select max(id) from audit b where b.venue=a.venue and error is not null)"
            ).fetchall()
        )
    api = {
        v: dict(
            requests=n,
            ok=ok or 0,
            errors=n - (ok or 0),
            rate_limited=rl or 0,
            last_ok=iso(lok),
            last_error=iso(lerr),
            last_error_message=last_errors.get(v),
        )
        for v, n, ok, rl, lok, lerr in audit
    }
    for v in cfg["venues"]:
        if v in DISCOVERY:
            out["venues"][v] = discovery_coverage(store, v, api)
            continue
        client = make_client(v, store, cfg)
        markets = store.checkpoint("markets:" + v) or []
        all_paid = store.latest("settlement", v)
        paid = counted(all_paid)  # AMBIGUOUS payments are never counted
        corrections = store.records("settlement_correction", v)
        withdrawn = store.records("settlement_withdrawn", v)
        comps = store.comparisons(v)
        meta = store.records("market", v)

        def bad(t):
            return t > now + 300 or t < 1_420_000_000

        # Snapshots, books and candles are counted while they stream past (one
        # record per key, as `records` reads them): days of them are never held.
        snap_flags = dict(seen=False, next_at=False, index=False)
        snap_n, snap_bad, snap_max = {}, {}, {}
        for s in _unique(store, "funding_snapshot", v):
            snap_flags["seen"] = True
            snap_flags["next_at"] = snap_flags["next_at"] or bool(
                s.get("next_funding_at")
            )
            snap_flags["index"] = snap_flags["index"] or bool(s.get("index_price"))
            k, t = str(s["market_id"]), s["received_at"]
            snap_n[k] = snap_n.get(k, 0) + 1
            snap_bad[k] = snap_bad.get(k, 0) + bad(t)
            snap_max[k] = max(snap_max.get(k, t), t)
        book_n = {}
        for b in _unique(store, "orderbook", v):
            k = str(b["market_id"])
            book_n[k] = book_n.get(k, 0) + 1
        candle_hours, candle_bad, candle_max = {}, {}, {}
        for c in _unique(store, "candle", v):
            k, t = str(c["market_id"]), c["open_time"]
            if start <= t < end:
                candle_hours.setdefault(k, set()).add(int(t))
            candle_bad[k] = candle_bad.get(k, 0) + bad(t)
            candle_max[k] = max(candle_max.get(k, t), t)
        # Everything else is small: grouped by market once, not scanned per market.
        by = _by_market
        paid_by, all_paid_by = by(paid), by(all_paid)
        corrections_by, withdrawn_by = by(corrections), by(withdrawn)
        comps_by, meta_by = by(comps), by(meta)
        a = api.get(v, dict(requests=0, ok=0, errors=0, rate_limited=0))
        connection = (
            "BAĞLANTI YOK (hiç istek yapılmadı)"
            if a["requests"] == 0
            else "BAĞLANTI KURULAMADI" if a["ok"] == 0 else "BAĞLANDI"
        )
        venue = dict(
            label=LABELS.get(v, v),
            connection=connection,
            api=a,
            markets_listed=len({m["market_id"] for m in meta}),
            markets={},
        )
        for m in markets:
            mid = m["market_id"]
            key = str(mid)
            mp = paid_by.get(key, [])
            # One completed comparison per actual settlement, whatever the index holds.
            mc = list(
                {
                    int(round(c["settled_at"])): c
                    for c in comps_by.get(key, [])
                    if c.get("settled_at") is not None
                    and c["comparison_status"] in store.FINAL
                }.values()
            )
            hours = candle_hours.get(key, set())
            interval, gap = _status_interval(mp, client.declared_interval)
            unit, ratio = _status_unit(mc)
            stats = store.checkpoint("stats:%s:%s" % (v, mid), {})
            last_meta = meta_by.get(key, [])
            statuses = {}
            for c in mc:
                statuses[c["comparison_status"]] = (
                    statuses.get(c["comparison_status"], 0) + 1
                )
            # Snapshot, payment and candle times: how many are impossible, the latest.
            latest = [
                x[key] for x in (snap_max, candle_max) if key in x
            ] + [p["settled_at"] for p in mp]
            bad_times = (
                snap_bad.get(key, 0)
                + candle_bad.get(key, 0)
                + sum(bad(p["settled_at"]) for p in mp)
            )
            funding_expected = (
                (end - start) // client.declared_interval
                if client.declared_interval
                else None
            )
            in_window = [p for p in mp if start <= p["settled_at"] < end]
            venue["markets"][m["asset"]] = dict(
                market_id=mid,
                metadata="OK" if last_meta else "YOK",
                funding_snapshots=snap_n.get(key, 0),
                settled_payments=len(mp),
                comparisons=len(mc),
                comparison_statuses=statuses,
                candles=len(hours),
                candles_expected=expected_hours,
                candle_coverage=round(len(hours) / expected_hours * 100, 1),
                missing_candle_hours=expected_hours - len(hours),
                funding_coverage=(
                    round(len(in_window) / funding_expected * 100, 1)
                    if funding_expected
                    else None
                ),
                orderbook_snapshots=book_n.get(key, 0),
                duplicate_payments=stats.get("duplicates", 0),
                conflicting_payments=sum(
                    p.get("settled_rate_status") == "CONFLICT" for p in mp
                ),
                corrected_payments=len(corrections_by.get(key, [])),
                ambiguous_payments=sum(
                    p.get("identity_status") == "AMBIGUOUS"
                    for p in all_paid_by.get(key, [])
                ),
                withdrawn_payments=len(withdrawn_by.get(key, [])),
                excluded_comparisons=sum(
                    c["comparison_status"] in store.EXCLUDED
                    for c in comps_by.get(key, [])
                ),
                reconcile=store.checkpoint("reconcile:%s:%s" % (v, mid)),
                same_event_tolerance=same_event_tolerance(
                    configured_tolerance(cfg, v, m["asset"]),
                    store.observed_interval(v, mid) or client.declared_interval,
                ),
                jitter_merged=stats.get("jitter_merged", 0),
                out_of_order=stats.get("out_of_order", 0)
                + stats.get("candle_out_of_order", 0),
                bad_timestamps=bad_times,
                funding_interval=interval,
                observed_interval_seconds=gap,
                declared_interval_seconds=client.declared_interval,
                rate_unit=unit,
                published_settled_ratio=ratio,
                direction=_status_direction(mp, mc, client),
                sign_consistency=(
                    round(
                        statuses.get("MATCHED", 0)
                        / max(
                            1,
                            statuses.get("MATCHED", 0)
                            + statuses.get("SIGN_CONFLICT", 0),
                        )
                        * 100,
                        1,
                    )
                    if statuses.get("MATCHED", 0) + statuses.get("SIGN_CONFLICT", 0)
                    else None
                ),
                last_observation=iso(max(latest)) if latest else None,
                pending=store.checkpoint("pending:%s:%s" % (v, mid), []),
            )
        venue["unverified_fields"] = _unverified(v, **snap_flags)
        out["venues"][v] = venue
    return label_source(out, store.checkpoint("data_source"))


def discovery_coverage(store, v, api):
    """A discovery venue: what was polled and what its responses contain. No field is
    given a meaning, so every market-evidence state is UNKNOWN."""
    a = api.get(v, dict(requests=0, ok=0, errors=0, rate_limited=0))
    d = store.checkpoint("discovery:" + v, {})
    schemas = sorted(store.records("schema", v), key=lambda r: r["first_seen_at"])
    with store.lock:
        last = store.db.execute(
            "select headers from audit where venue=? and error is null"
            " order by id desc limit 1",
            (v,),
        ).fetchone()
    headers = json.loads(last[0]) if last and last[0] else {}
    gaps = d.get("change_gaps") or []
    return dict(
        label=LABELS.get(v, v),
        connection=(
            "BAĞLANTI YOK (hiç istek yapılmadı)"
            if a["requests"] == 0
            else "BAĞLANTI KURULAMADI" if a["ok"] == 0 else "BAĞLANDI"
        ),
        api=a,
        markets_listed=d.get("listing_count") or 0,
        markets={},
        discovery=dict(
            polls=d.get("polls", 0),
            first_at=iso(d.get("first_at")),
            last_at=iso(d.get("last_at")),
            content_changes=d.get("content_changes", 0),
            median_change_seconds=(round(statistics.median(gaps), 1) if gaps else None),
            cache_headers={
                k: headers[k] for k in ("age", "cache-control", "date") if k in headers
            },
            listing_count=d.get("listing_count"),
            top_keys=d.get("top_keys"),
            listing_keys=d.get("listing_keys"),
            sample=d.get("sample"),
            schemas=len(schemas),
            schema_changes=d.get("schema_changes", 0),
            paths=schemas[-1]["paths"] if schemas else {},
            paths_truncated=bool(schemas and schemas[-1].get("paths_truncated")),
        ),
        unverified_fields=[
            "Keşif modu: hiçbir alan funding, fiyat veya ödeme olarak yorumlanmadı",
            "Funding aralığı, oran birimi, yön, ödeme geçmişi: UNKNOWN (veri yok)",
            "Alan anlamları belge veya ödeme kanıtı olmadan belirlenmeyecek",
        ],
    )


LIVE = "LIVE_PUBLIC_API"
EVIDENCE = (VERIFIED, OBSERVED, CONFLICT)


def label_source(cov, source):
    """Only data recorded from the real public APIs may carry an evidence status. In
    a folder recorded with a test transport (or with no recorded session) every
    VERIFIED / OBSERVED / CONFLICT is prefixed, so it can never be read as real."""
    cov["data_source"] = source
    cov["live"] = source == LIVE
    if cov["live"]:
        return cov
    prefix = "MOCK:" if source and source.startswith("MOCK") else "KAYNAKSIZ:"
    for x in cov["venues"].values():
        if x["api"]["requests"]:
            x["connection"] += " (%s gerçek borsa bağlantısı değil)" % prefix[:-1]
        for m in x["markets"].values():
            for k in ("funding_interval", "rate_unit", "direction"):
                if m[k] in EVIDENCE:
                    m[k] = prefix + m[k]
    return cov


def _unique(store, kind, v):
    """A venue's records of a kind, streamed, one per key (as `Store.records`)."""
    seen = set()
    for r in store.iter_records(kind, v):
        k = record_key(kind, r)
        if k not in seen:
            seen.add(k)
            yield r


def _by_market(rows):
    out = {}
    for r in rows:
        out.setdefault(str(r["market_id"]), []).append(r)
    return out


def unverified_fields(v, client, snaps):
    return _unverified(
        v,
        seen=bool(snaps),
        next_at=any(s.get("next_funding_at") for s in snaps),
        index=any(s.get("index_price") for s in snaps),
    )


def _unverified(v, seen, next_at, index):
    if not seen:
        return [
            "Anlık funding kaydı yok: alanlar değerlendirilemedi (önce bağlantı gerekir)"
        ]
    out = [
        "Sözleşme büyüklüğü: DECLARED (bağlayıcı beyanı, API alanı değil)",
        "Funding işaret kuralı: DECLARED; borsa belgesi okunmadı",
        "Normalize saatlik oran: üretilmedi (birim doğrulanmadı)",
    ]
    if not next_at:
        out.append("Sonraki funding zamanı: API yayımlamıyor (UNKNOWN)")
    if not index:
        out.append("Endeks fiyatı: yanıtta yok (UNKNOWN)")
    if v in ("Lighter", "LighterCore"):
        out.append(
            "funding-rates oran birimi: belgelenmedi; yalnız ödemelerle karşılaştırılır"
        )
    return out


def comparison_index(store):
    """Problems of the comparison index: rows repeated for one settlement, rows
    without a settlement, and disagreement with the normalized comparison records."""
    rows, distinct = store.comparison_counts()
    problems = []
    if rows != distinct:
        problems.append("aynı ödeme için %d fazla satır" % (rows - distinct))
    with store.lock:
        index = {
            (v, m, t)
            for v, m, t in store.db.execute(
                "select venue, market_id, settled_at from comparisons"
            )
        }
    jsonl = {
        (c["venue"], str(c["market_id"]), int(round(c["settled_at"])))
        for c in store.latest("comparison")
        if c.get("settled_at") is not None
    }
    if any(t is None for _, _, t in index):
        problems.append("ödeme zamanı olmayan satır")
    if index != jsonl:
        problems.append(
            "dizin ile JSONL farklı: dizinde fazla %d, JSONL'de fazla %d"
            % (len(index - jsonl), len(jsonl - index))
        )
    return dict(rows=rows, distinct=distinct, jsonl=len(jsonl), problems=problems)


def consistency(store, cfg):
    bad_hashes, raw_count = verify_hashes(store)
    comp = comparison_index(store)
    bad_schemas, rec_count, torn, repeated = verify_schemas(store)
    replayed, replay_problems = replay(store, cfg)
    settlements = settlement_index(store)
    return dict(
        data_source=store.checkpoint("data_source"),
        torn_lines=[dict(kind=k, file=f, line=i) for k, f, i in torn],
        repeated_keys=repeated,
        settlement_index=settlements,
        raw_files=raw_count,
        raw_hash_failures=[dict(path=p, problem=x) for p, x in bad_hashes],
        records=rec_count,
        schema_failures=[
            dict(kind=k, file=f, line=i, missing=m) for k, f, i, m in bad_schemas
        ],
        replayed_responses=replayed,
        replay_problems=[
            dict(sha256=s, endpoint=e, problem=p) for s, e, p in replay_problems
        ],
        comparison_index=comp,
        repair=store.checkpoint("repair:comparisons"),
        ok=not bad_hashes
        and not bad_schemas
        and not replay_problems
        and not comp["problems"]
        and not settlements["problems"],
    )


def settlement_index(store):
    """The per-payment index must hold exactly the latest revision of every
    normalized settlement record, one row per (venue, market, settled_at)."""
    with store.lock:
        index = {
            (v, m, t): r
            for v, m, t, r in store.db.execute(
                "select venue, market_id, settled_at, revision from settlement_index"
            )
        }
    jsonl = {
        (r["venue"], str(r["market_id"]), int(round(r["settled_at"]))): r.get(
            "revision", 1
        )
        for r in store.latest("settlement")
    }
    problems = []
    if index != jsonl:
        problems.append(
            "dizin ile JSONL farklı: dizinde fazla %d, JSONL'de fazla %d"
            % (len(set(index) - set(jsonl)), len(set(jsonl) - set(index)))
        )
    return dict(rows=len(index), events=len(jsonl), problems=problems)


# --- report -----------------------------------------------------------------------
def text_report(cov, cons=None):
    lines = [
        "PHASE 1 LOCAL DATA REPORT",
        "Üretim: %s (UTC). Yalnızca bu klasördeki kayıtlardan üretildi."
        % cov["generated_iso"],
        "Veri kaynağı: %s" % (cov.get("data_source") or "kayıt oturumu yok"),
    ]
    if not cov.get("live"):
        lines.append(
            "!!! BU KLASÖR GERÇEK BORSA VERİSİ İÇERMİYOR: durumlar kanıt değildir. !!!"
        )
    lines.append("")
    for v, x in cov["venues"].items():
        a = x["api"]
        lines.append("%s — %s" % (x["label"], x["connection"]))
        lines.append(
            "  İstek: %d (başarılı %d, hatalı %d, 429: %d) · son başarı %s · son hata %s"
            % (
                a["requests"],
                a["ok"],
                a["errors"],
                a["rate_limited"],
                a.get("last_ok"),
                a.get("last_error"),
            )
        )
        if a.get("last_error_message"):
            lines.append("  Son hata: %s" % a["last_error_message"])
        lines.append("  Listelenen piyasa: %d" % x["markets_listed"])
        for asset, m in x["markets"].items():
            lines += [
                "  %s (%s)" % (asset, m["market_id"]),
                "  - Market metadata: %s" % m["metadata"],
                "  - Funding snapshots: %d" % m["funding_snapshots"],
                "  - Settled payments: %d (kapsam %s%%)"
                % (m["settled_payments"], m["funding_coverage"]),
                "  - Düzeltilen ödeme: %d · çelişkili ödeme: %d · zaman kayması birleştirilen: %d"
                % (
                    m["corrected_payments"],
                    m["conflicting_payments"],
                    m["jitter_merged"],
                ),
                "  - Belirsiz kimlikli ödeme (sayılmaz): %d · listeden kaybolan: %d · dışlanan karşılaştırma: %d · tolerans %.1f sn"
                % (
                    m["ambiguous_payments"],
                    m["withdrawn_payments"],
                    m["excluded_comparisons"],
                    m["same_event_tolerance"],
                ),
                "  - Son mutabakat: %s"
                % (
                    "yok"
                    if not m["reconcile"]
                    else "%s, son %s saat, %s"
                    % (
                        iso(m["reconcile"]["at"]),
                        m["reconcile"]["window_hours"],
                        {
                            k: x
                            for k, x in m["reconcile"].items()
                            if k not in ("at", "window_hours")
                        },
                    )
                ),
                "  - Published/settled comparisons: %d %s"
                % (m["comparisons"], m["comparison_statuses"] or ""),
                "  - 1h candles: %d / %d (%.1f%%)"
                % (m["candles"], m["candles_expected"], m["candle_coverage"]),
                "  - Missing candle hours: %d" % m["missing_candle_hours"],
                "  - Orderbook snapshots: %d" % m["orderbook_snapshots"],
                "  - Duplicate payments: %d · sırasız kayıt: %d · hatalı zaman: %d"
                % (m["duplicate_payments"], m["out_of_order"], m["bad_timestamps"]),
                "  - Funding interval: %s (gözlenen %s s, beyan %s s)"
                % (
                    m["funding_interval"],
                    m["observed_interval_seconds"],
                    m["declared_interval_seconds"],
                ),
                "  - Rate unit: %s (medyan yayımlanan/gerçekleşen %s)"
                % (m["rate_unit"], m["published_settled_ratio"]),
                "  - Direction: %s · işaret tutarlılığı: %s"
                % (m["direction"], m["sign_consistency"]),
                "  - Last observation: %s" % m["last_observation"],
            ]
            if m["comparison_statuses"].get("MATCHED", 0) >= 3:
                lines.append(
                    "  - En az 3 tutarlı karşılaştırma var; bu tek başına üretim güvenilirliği değildir."
                )
        if x.get("discovery"):
            lines += discovery_lines(x["discovery"])
        lines.append("  Doğrulanmamış alanlar:")
        lines += ["  * " + f for f in x["unverified_fields"]]
        lines.append("")
    if cons is not None:
        lines += [
            "Tutarlılık: %s" % ("TAMAM" if cons["ok"] else "SORUN VAR"),
            "  Ham dosya: %d, hash hatası: %d"
            % (cons["raw_files"], len(cons["raw_hash_failures"])),
            "  Kayıt: %d, şema hatası: %d"
            % (cons["records"], len(cons["schema_failures"])),
            "  Karşılaştırma dizini: %d satır, %d tekil ödeme, JSONL %d%s"
            % (
                cons["comparison_index"]["rows"],
                cons["comparison_index"]["distinct"],
                cons["comparison_index"]["jsonl"],
                (
                    (" · " + "; ".join(cons["comparison_index"]["problems"]))
                    if cons["comparison_index"]["problems"]
                    else ""
                ),
            ),
            "  Ödeme dizini: %d satır, %d ödeme%s"
            % (
                cons["settlement_index"]["rows"],
                cons["settlement_index"]["events"],
                (
                    (" · " + "; ".join(cons["settlement_index"]["problems"]))
                    if cons["settlement_index"]["problems"]
                    else ""
                ),
            ),
            "  Kesik satır (çökme): %d · tekrarlanan anahtar: %s"
            % (len(cons["torn_lines"]), cons["repeated_keys"] or 0),
            "  Yeniden ayrıştırılan yanıt: %d, sorun: %d"
            % (cons["replayed_responses"], len(cons["replay_problems"])),
        ]
    return "\n".join(lines) + "\n"


def discovery_lines(d, limit=60):
    lines = [
        "  KEŞİF (yalnız ham yanıt ve şema; alanlar yorumlanmadı)",
        "  - Yoklama: %d · ilk %s · son %s" % (d["polls"], d["first_at"], d["last_at"]),
        "  - İçerik değişimi: %d kez · medyan aralık %s sn · önbellek başlıkları %s"
        % (
            d["content_changes"],
            d["median_change_seconds"],
            d["cache_headers"] or "yok",
        ),
        "  - Listing sayısı: %s · üst düzey anahtarlar: %s"
        % (d["listing_count"], d["top_keys"]),
        "  - Şema: %d sürüm, %d değişim, %d alan yolu%s"
        % (
            d["schemas"],
            d["schema_changes"],
            len(d["paths"]),
            " (KESİLDİ)" if d["paths_truncated"] else "",
        ),
    ]
    if d["listing_keys"]:
        lines.append("  - Listing alanları (kaç listing'de var):")
        lines += ["      %s: %d" % (k, n) for k, n in sorted(d["listing_keys"].items())]
    if d["sample"]:
        lines.append("  - İlk listing (yayımlandığı gibi, kısaltılmış):")
        lines += [
            "      %s = %s" % (k, json.dumps(v, ensure_ascii=False))
            for k, v in d["sample"].items()
        ]
    shown = list(d["paths"].items())[:limit]
    if shown:
        lines.append("  - Alan yolları ve tipleri:")
        lines += ["      %s: %s" % (p, "|".join(t)) for p, t in shown]
        if len(d["paths"]) > limit:
            lines.append(
                "      ... %d yol daha (coverage_report.json)"
                % (len(d["paths"]) - limit)
            )
    return lines


def write_reports(store, cfg, now=None, with_consistency=True):
    now = now or time.time()
    cov = coverage(store, cfg, now)
    cons = consistency(store, cfg) if with_consistency else None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    folder = os.path.join(store.root, "reports")
    with open(os.path.join(folder, "coverage_report.json"), "w", encoding="utf-8") as f:
        json.dump(cov, f, indent=1, ensure_ascii=False, default=str)
    if cons is not None:
        with open(
            os.path.join(folder, "consistency_report.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(cons, f, indent=1, ensure_ascii=False)
    text = text_report(cov, cons)
    with open(
        os.path.join(folder, "report-%s.txt" % stamp), "w", encoding="utf-8"
    ) as f:
        f.write(text)
    return cov, cons, text
