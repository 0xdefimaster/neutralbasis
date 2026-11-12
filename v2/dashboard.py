"""V2 dashboard: one self-contained HTML page from a Runtime snapshot (or an
evaluation file). No external scripts or fonts; every number comes from the snapshot.
PAPER figures are labelled as such and never as realized profit."""
import html
import json

CAT_LABEL = dict(ELIGIBLE="Paper'a uygun", DATA="Veri / kanıt eksik",
                 EXECUTION="Simüle edilemiyor", COST="Maliyet", RESEARCH_ONLY="Yalnız araştırma")
STRAT_LABEL = dict(funding="Funding arbitrajı", convergence="Fiyat yakınsaması",
                   hybrid="Hibrit")

CSS = """
:root{--bg:#fafaf8;--fg:#1d1d1b;--mut:#6b6b66;--line:#e2e1dc;--card:#fff;--ok:#1f7a4d;--bad:#a8322d;--warn:#8a6a00}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--mut:#a3a29c;--line:#34332f;--card:#1f1f1d;--ok:#4cc38a;--bad:#ef7b74;--warn:#e0b84a}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0}h2{font-size:16px;margin:24px 0 8px}
.note{color:var(--mut)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
.big{font-size:26px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.wrap{overflow-x:auto}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
"""


def esc(x):
    return html.escape(str(x))


def money(x):
    return "—" if x is None else ("%+.2f $" % x)


def render(snap):
    at = snap.get("at")
    parts = ["<!doctype html><html lang='tr'><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width,initial-scale=1'>",
             "<title>NeutralBasis V2</title><style>%s</style></head><body><main>" % CSS,
             "<h1>NeutralBasis V2 — PAPER fırsat izleme</h1>",
             "<p class='note'>PAPER ONLY. Tahmini net, gerçek defterlerle hesaplanmış bir "
             "beklentidir; gerçekleşmiş kâr değildir. Satır: %s · güvenlik: %s</p>"
             % (esc(snap.get("rows")), "<span class='ok'>tamam</span>" if snap.get("safe", True)
                else "<span class='bad'>%s</span>" % esc("; ".join(snap.get("safety_findings")
                                                                   or [])))]
    parts.append("<div class='grid'>")
    for s, x in (snap.get("strategies") or {}).items():
        cats = x.get("by_category", {})
        parts.append("<div class='card'><div>%s</div><div class='big'>%s aday</div>"
                     % (esc(STRAT_LABEL.get(s, s)), esc(x.get("candidates"))))
        for k in ("ELIGIBLE", "DATA", "EXECUTION", "COST", "RESEARCH_ONLY"):
            if k in cats:
                cls = "ok" if k == "ELIGIBLE" else ""
                parts.append("<div class='%s'>%s: %s</div>" % (cls, esc(CAT_LABEL[k]),
                                                               esc(cats[k])))
        parts.append("</div>")
    parts.append("</div>")
    for s, x in (snap.get("strategies") or {}).items():
        parts.append("<h2>%s — ret nedenleri (ilk neden)</h2><div class='wrap'><table>"
                     "<tr><th>Neden</th><th>Aday</th></tr>" % esc(STRAT_LABEL.get(s, s)))
        for code, n in (x.get("first_reason") or {}).items():
            parts.append("<tr><td>%s</td><td>%s</td></tr>" % (esc(code), esc(n)))
        parts.append("</table></div>")
        rows = (snap.get("top") or {}).get(s) or []
        if rows:
            parts.append("<h2>%s — tahmini net (en yüksek)</h2><div class='wrap'><table>"
                         "<tr><th>Varlık</th><th>Long</th><th>Short</th><th>Net</th>"
                         "<th>Funding</th><th>Baz</th><th>Ücret</th><th>Spread</th>"
                         "<th>Risk payı</th><th>Durum</th></tr>" % esc(STRAT_LABEL.get(s, s)))
            for r in rows:
                c = r.get("components") or {}
                parts.append("<tr><td>%s</td><td>%s</td><td>%s</td><td class='%s'>%s</td>"
                             "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                             % (esc(r["asset"]), esc(r["long"]), esc(r["short"]),
                                "ok" if (r["net"] or 0) > 0 else "bad", money(r["net"]),
                                money(c.get("funding")), money(c.get("basis")),
                                money(-(c.get("fees") or 0)), money(-(c.get("spread") or 0)),
                                money(-(c.get("reserve") or 0)),
                                "uygun" if r["eligible"] else esc(r.get("reason") or "")))
            parts.append("</table></div>")
    parts.append("<h2>Açık hibrit paper pozisyonları</h2>")
    hy = snap.get("open_hybrids") or []
    if not hy:
        parts.append("<p class='note'>Yok.</p>")
    else:
        parts.append("<div class='wrap'><table><tr><th>Varlık</th><th>Durum</th><th>Bacaklar"
                     "</th><th>Fiyat PnL</th><th>Funding</th><th>Ücret</th><th>Net</th></tr>")
        for p in hy:
            pn = p.get("pnl") or {}
            parts.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                         "<td>%s</td><td>%s</td></tr>" % (
                             esc(p["asset"]), esc(p["state"]),
                             esc(", ".join("%s %s" % (k, v["venue"])
                                           for k, v in p["legs"].items())),
                             money(pn.get("price_pnl")), money(pn.get("funding")),
                             money(-(pn.get("fees") or 0)), money(pn.get("net"))))
        parts.append("</table></div>")
    reasons = snap.get("reasons_by_venue") or {}
    if reasons:
        parts.append("<h2>Borsa başına pazar durumu</h2><div class='wrap'><table>"
                     "<tr><th>Borsa</th><th>Durum dağılımı</th></tr>")
        for v, c in sorted(reasons.items()):
            parts.append("<tr><td>%s</td><td>%s</td></tr>" % (esc(v), esc(json.dumps(
                dict(c), ensure_ascii=False))))
        parts.append("</table></div>")
    parts.append("<p class='note'>Zaman: %s · tur: %s</p></main></body></html>"
                 % (esc(at), esc(snap.get("rounds"))))
    return "".join(parts)


def from_evaluation(e):
    """A Runtime-shaped snapshot from tools/v2_phase4_eval's evaluation.json."""
    return dict(at=e.get("at"), rows=e.get("rows"), safe=True,
                strategies={s: dict(candidates=x["candidates"], by_category=x["by_category"],
                                    first_reason=x["first_reason"])
                            for s, x in e.get("summary", {}).items()},
                top={}, open_hybrids=[], reasons_by_venue={})
