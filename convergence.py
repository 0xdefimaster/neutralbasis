"""Conservative paper convergence gate; no exchange order endpoints."""

import math


def qualify(c, history, cfg):
    c = dict(c)
    c["strategy"] = "convergence"
    reasons = list(c["reasons"])
    # Funding yield is not the entry thesis. All sizing, depth and risk checks remain.
    reasons = [
        r
        for r in reasons
        if r
        not in (
            "Kalıcı pozitif funding avantajı yok",
            "Funding geliri maliyet + spread stresini karşılamıyor",
        )
    ]
    if not history:
        reasons.append(
            "Yakınsama için kesintisiz 7 günlük işlem fiyatı geçmişi gerekli"
        )
    else:
        points = history["points"]
        daily = []
        for i in range(7):
            values = [x["spread"] for x in points[i * 24 : (i + 1) * 24]]
            daily.append(
                bool(values and min(values) < -0.00002 and max(values) > 0.00002)
            )
        if history["crossings"] < 6 or sum(daily) < 4:
            reasons.append(
                "İstikrarlı kesişim yok: 7 günde en az 6 kesişim ve 4 çift yönlü gün gerekli"
            )
        if abs(history["mean"]) > max(0.0002, history["sd"] * 0.5):
            reasons.append(
                "Kalıcı fiyat bazisi: sıfır çevresinde istikrarlı yakınsama yok"
            )
        gap = math.log(c["entry_short"] / c["entry_long"])
        if gap < max(0.0005, history["mean"] + history["sd"]):
            reasons.append("Fiyat boşluğu geçmiş dağılıma göre yeterli değil")
        c["convergence_history"] = dict(
            crossings=history["crossings"],
            two_sided_days=sum(daily),
            mean=history["mean"],
            sd=history["sd"],
        )
    capture = c["qty"] * (c["entry_short"] - c["entry_long"])
    # Zero-basis target; half of current round-trip book drag proxies future exit drag.
    exit_drag = c["execution_drag"] / 2
    funding_drag = max(0, -c["conservative_funding"])
    net = (
        capture
        - exit_drag
        - c["entry_fee"]
        - c["exit_fee"]
        - funding_drag
        - c["stress_loss"]
    )
    if net < cfg["min_net"]:
        reasons.append(
            "Yakınsama kazancı dört emir maliyeti, funding gideri ve stres sonrası yetersiz"
        )
    c.update(
        entry_capture=capture,
        exit_drag_proxy=exit_drag,
        funding_cost_reserve=funding_drag,
        net=net,
        reasons=reasons,
        eligible=not reasons,
    )
    return c


def exit_reason(p, books, cfg, at):
    d = p["decision"]
    lb = books[d["long"]]
    sb = books[d["short"]]
    # Actual liquidation PnL already contains book VWAP, slippage and closing fees.
    long_exit = next(l["exit_estimate"] for l in p["legs"] if l["side"] == "long")
    short_exit = next(l["exit_estimate"] for l in p["legs"] if l["side"] == "short")
    gap = math.log(short_exit / long_exit)
    p["exit_spread"] = gap
    if at - p["opened"] >= cfg["horizon"] * 3600:
        return "Yakınsama azami bekleme süresi"
    if abs(lb["received"] - sb["received"]) > 5:
        return "Yakınsama defter zamanları uyumsuz"
    if gap <= 0.0002 and p["net_if_closed"] >= cfg["min_net"]:
        return "Fiyatlar yakınsadı; çıkış masrafı sonrası net hedef sağlandı"
    return None
