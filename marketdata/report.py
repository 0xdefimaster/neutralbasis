"""Capability matrix and canonical market report from a recorder folder."""

import time

from . import canonical, manifest
from .evidence import Evidence
from .venues import VENUES

SHORT = {
    manifest.VERIFIED: "DOĞRULANDI",
    manifest.IMPLEMENTED: "kodda var",
    manifest.NOT_IMPLEMENTED: "—",
    manifest.UNSUPPORTED: "yok",
}


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


def build(root, now=None):
    now = now or time.time()
    ev = Evidence(root, now)
    venues = manifest.evaluate(ev, now)
    recorded = [n for n in manifest.RECORDED.values() if n != "Variational"]
    insts = canonical.instruments(ev, recorded) if not ev.error else []
    marks = {}
    for name in recorded:
        vid = manifest.RECORDED_IDS[name]
        for mid, (mark, _) in ev.latest_marks(name).items():
            marks[(vid, mid)] = mark
    groups = canonical.identities(insts, marks)
    return dict(
        generated_at=now,
        generated_iso=iso(now),
        recorder_root=ev.root,
        data_source=ev.source,
        live=ev.live,
        error=ev.error,
        venues=venues,
        instruments=[i.as_dict() for i in insts],
        identities=groups,
    )


def text(rep, show_instruments=False):
    lines = [
        "VERİ PLATFORMU · YETENEK MATRİSİ",
        "Üretim: %s (UTC) · kanıt klasörü: %s"
        % (rep["generated_iso"], rep["recorder_root"]),
        "Kanıt kaynağı: %s" % (rep["data_source"] or "kayıt yok"),
    ]
    if rep["error"]:
        lines.append("!!! %s" % rep["error"])
    if not rep["live"]:
        lines.append(
            "!!! Bu klasör gerçek borsa verisi içermiyor: hiçbir yetenek DOĞRULANDI sayılmaz."
        )
    lines += [
        "",
        "DOĞRULANDI = kaydedicinin canlı kayıtlarında var · kodda var = adaptör okuyor, "
        "bu klasörde canlı kanıt yok · — = hiçbir adaptör okumuyor",
        "",
    ]
    for vid, v in rep["venues"].items():
        api = v["api"] or {}
        lines.append(
            "%s [%s] — sağlık: %s%s"
            % (
                v["label"],
                vid,
                v["health"],
                (
                    " · istek %d, hata %d, son başarı %s"
                    % (api["requests"], api["errors"], iso(api["last_ok"]))
                    if api
                    else ""
                ),
            )
        )
        lines.append("  " + v["note"])
        for cap in manifest.CAPABILITIES:
            c = v["capabilities"][cap]
            paths = sorted({s["path"] for s in c["sources"]})
            extra = []
            if c["availability"] == manifest.VERIFIED:
                extra.append("son %s, %s" % (iso(c["last_success"]), c["freshness"]))
            if paths:
                extra.append("/".join(paths))
            if c["mock_evidence"]:
                extra.append("yalnız MOCK kanıt")
            if c["gap"]:
                extra.append("not: " + c["gap"])
            lines.append(
                "  %-20s %-11s %s"
                % (manifest.LABELS[cap], SHORT[c["availability"]], " · ".join(extra))
            )
        lines.append("")
    counts = {}
    for g in rep["identities"].values():
        counts[g["status"]] = counts.get(g["status"], 0) + 1
    lines.append(
        "KANONİK PİYASALAR: %d enstrüman, %d kanonik kimlik %s"
        % (len(rep["instruments"]), len(rep["identities"]), counts)
    )
    for cid, g in rep["identities"].items():
        if g["status"] in ("PRICE_CONFLICT", "SETTLEMENT_CONFLICT") or any(
            m["multiplier"] not in (None, 1, 1.0) for m in g["members"]
        ):
            lines.append(
                "  %s %s: %s"
                % (
                    cid,
                    g["status"],
                    ", ".join(
                        "%s %s x%s(%s)"
                        % (
                            m["venue"],
                            m["symbol"],
                            m["multiplier"],
                            m["multiplier_status"],
                        )
                        for m in g["members"]
                    ),
                )
            )
    if show_instruments:
        lines.append("")
        for i in rep["instruments"]:
            unknown = [
                f
                for f in canonical.FIELDS
                if i["provenance"].get(f) == canonical.UNKNOWN
            ]
            lines.append(
                "  %-14s %-12s %-10s %s tick=%s lot=%s min=%s aralık=%s durum=%s · bilinmeyen: %s"
                % (
                    i["canonical_id"],
                    i["venue"],
                    i["venue_symbol"],
                    i["venue_market_id"],
                    i["tick_size"],
                    i["lot_size"],
                    i["min_order_size"],
                    i["funding_interval_seconds"],
                    i["status"],
                    ", ".join(unknown) or "yok",
                )
            )
    return "\n".join(lines) + "\n"


assert set(manifest.RECORDED) <= set(VENUES)
