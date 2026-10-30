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
import json
import os
import statistics
import time
import urllib.parse

from .clients import LABELS, make_client
from .clients.base import CONFLICT, DECLARED, OBSERVED, UNKNOWN, VERIFIED
from .clients.records import REQUIRED, record_key
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
        seen = set()
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
                        key = (r["venue"], record_key(kind, r))
                        if key in seen:
                            repeated[kind] = repeated.get(kind, 0) + 1
                        seen.add(key)
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
    return (mid,)


def replay(store, cfg):
    """Re-parse every successful stored response offline and check that each stored
    record is reproduced by a response of its own venue. Returns (checked responses,
    problems)."""
    asset_of = {}
    for m in store.records("market"):
        asset_of[str(m["market_id"])] = m["asset"]
    clients = {v: make_client(v, store, cfg) for v in VENUE_DIRS}
    with store.lock:
        rows = store.db.execute(
            "select venue, endpoint, url, params, body, response_at, sha256 from audit"
            " where error is null and sha256 is not null"
        ).fetchall()
    covered, problems, done = set(), [], set()
    for venue, endpoint, url, params, body, at, digest in rows:
        ctx = _context(venue, url, params, body, asset_of)
        key = (venue, endpoint, digest, str(ctx["market_id"]))
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
            covered.update((venue, kind, signature(kind, r)) for r in rs)
    for kind in REQUIRED:
        if kind == "comparison":
            continue
        for r in store.records(kind):
            if (r["venue"], kind, signature(kind, r)) not in covered:
                problems.append(
                    (
                        r.get("raw_sha256"),
                        kind,
                        "%s kaydı ham yanıttan yeniden üretilemedi" % kind,
                    )
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
        client = make_client(v, store, cfg)
        markets = store.checkpoint("markets:" + v) or []
        snaps = store.records("funding_snapshot", v)
        paid = store.latest("settlement", v)
        corrections = store.records("settlement_correction", v)
        candles = store.records("candle", v)
        books = store.records("orderbook", v)
        comps = store.comparisons(v)
        meta = store.records("market", v)
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
            mp = [p for p in paid if str(p["market_id"]) == key]
            # One completed comparison per actual settlement, whatever the index holds.
            mc = list(
                {
                    int(round(c["settled_at"])): c
                    for c in comps
                    if str(c["market_id"]) == key
                    and c.get("settled_at") is not None
                    and c["comparison_status"] in store.FINAL
                }.values()
            )
            hours = {
                int(c["open_time"])
                for c in candles
                if str(c["market_id"]) == key and start <= c["open_time"] < end
            }
            ms = [s for s in snaps if str(s["market_id"]) == key]
            interval, gap = _status_interval(mp, client.declared_interval)
            unit, ratio = _status_unit(mc)
            stats = store.checkpoint("stats:%s:%s" % (v, mid), {})
            last_meta = [x for x in meta if str(x["market_id"]) == key]
            statuses = {}
            for c in mc:
                statuses[c["comparison_status"]] = (
                    statuses.get(c["comparison_status"], 0) + 1
                )
            times = (
                [s["received_at"] for s in ms]
                + [p["settled_at"] for p in mp]
                + [c["open_time"] for c in candles if str(c["market_id"]) == key]
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
                funding_snapshots=len(ms),
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
                orderbook_snapshots=sum(str(b["market_id"]) == key for b in books),
                duplicate_payments=stats.get("duplicates", 0),
                conflicting_payments=sum(
                    p.get("settled_rate_status") == "CONFLICT" for p in mp
                ),
                corrected_payments=sum(str(c["market_id"]) == key for c in corrections),
                jitter_merged=stats.get("jitter_merged", 0),
                out_of_order=stats.get("out_of_order", 0)
                + stats.get("candle_out_of_order", 0),
                bad_timestamps=sum(t > now + 300 or t < 1_420_000_000 for t in times),
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
                last_observation=iso(max(times)) if times else None,
                pending=store.checkpoint("pending:%s:%s" % (v, mid), []),
            )
        venue["unverified_fields"] = unverified_fields(v, client, snaps)
        out["venues"][v] = venue
    return label_source(out, store.checkpoint("data_source"))


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


def unverified_fields(v, client, snaps):
    if not snaps:
        return [
            "Anlık funding kaydı yok: alanlar değerlendirilemedi (önce bağlantı gerekir)"
        ]
    out = [
        "Sözleşme büyüklüğü: DECLARED (bağlayıcı beyanı, API alanı değil)",
        "Funding işaret kuralı: DECLARED; borsa belgesi okunmadı",
        "Normalize saatlik oran: üretilmedi (birim doğrulanmadı)",
    ]
    if not any(s.get("next_funding_at") for s in snaps):
        out.append("Sonraki funding zamanı: API yayımlamıyor (UNKNOWN)")
    if not any(s.get("index_price") for s in snaps):
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
