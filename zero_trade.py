"""Zero-trade diagnostics for the paper desk (PAPER ONLY).

Explains, every cycle, why no paper position was opened: stage counters from the
scan to the opened position, a machine-readable reason code for every rejected or
blocked opportunity, the last rejected opportunities with what was known about them,
and one evidence-backed verdict sentence. It never decides anything itself: it reads
what `wide_paper.WidePaper.cycle` decided and codes it. Nothing here may open a
position, relax a gate or invent an opportunity.
"""

import hashlib
import re

# Stage counters, scan to position. A stage the engine does not have is reported as
# None (unknown), never as 0.
STAGES = (
    "markets_discovered",  # perp markets in the scan
    "instruments_matched",  # assets listed on at least two usable venues
    "data_quality_passed",  # usable markets (fresh price, funding, fee, interval)
    "funding_candidates",  # ranked funding pairs
    "price_gap_candidates",  # ranked convergence pairs
    "combined_candidates",  # unique pairs ranked with signed gap + funding + fees
    "economic_candidates",  # unique pairs with a positive preliminary joint score
    "evidence_blocked",  # positive, but a leg lacks verified evidence (research)
    "simulation_passed",  # deep check (history, book, scenarios) eligible
    "risk_approved",  # passed the entry checks (budget, margin, halts, fresh data)
    "paper_orders_created",  # paper orders sent to the simulated books
    "positions_opened",
    "positions_rejected",  # rejected or skipped at deep check or entry
)

# (code, category, patterns): the first match wins, so specific before generic.
# Categories: DATA (data quality / verification evidence), HISTORY, ECONOMIC, RISK,
# EXECUTION, STRATEGY.
RULES = (
    ("FUNDING_SIGN_CONFLICT", "DATA", ("prim yönünün tersine", "işareti çelişkili")),
    ("FUNDING_SIGN_UNVERIFIED", "DATA", ("funding işaret",)),
    ("FUNDING_UNIT_MISMATCH", "DATA", ("aynı birimde değil",)),
    ("FUNDING_EVIDENCE_INSUFFICIENT", "DATA",
     ("ödeme karşılaştırması yetersiz", "gerçekleşmiş ödeme yetersiz")),
    ("FUNDING_HISTORY_UNVERIFIED", "DATA", ("gerçekleşmiş funding doğrulanmadı",
                                            "gerçekleşmiş funding yok")),
    ("FUNDING_INTERVAL_UNKNOWN", "DATA", ("funding aralığı",)),
    ("FUNDING_MODEL_UNVERIFIED", "DATA", ("funding modeli",)),
    ("FUNDING_UNKNOWN", "DATA", ("funding oranı yok", "funding oranı doğrulanmadı")),
    ("CONTRACT_SPEC_UNKNOWN", "DATA", ("kontrat büyüklüğü", "kontrat bilgisi")),
    ("COST_EXCEEDS_EDGE", "ECONOMIC", ("maliyet sonrası skor negatif",)),
    ("FEE_INCREASED", "ECONOMIC", ("ücret yükseldi",)),
    ("FEE_UNKNOWN", "DATA", ("ücret bilinmiyor", "ücret doğrulanmadı", "ücret yok")),
    ("STALE_ORDERBOOK", "DATA", ("defter eski",)),
    ("INSUFFICIENT_LIQUIDITY", "ECONOMIC", ("derinliği yetersiz",)),
    ("ORDERBOOK_UNAVAILABLE", "DATA", ("defter boş", "defter okunamadı",
                                       "defter kesişik", "emir defteri",
                                       "alış / satış")),
    ("STALE_DATA", "DATA", ("verisi eski", "listede yok", "bu taramada yok",
                            "yenilenemedi")),
    ("INSUFFICIENT_HISTORY", "HISTORY", ("geçmiş kaydı", "geçmiş tamamlanmadı",
                                         "geçmişi eksik", "geçmişi yok",
                                         "geçmişi okunamadı", "fiyat geçmişi yok",
                                         "geçmiş eksik", "mum eksik",
                                         "işlem mumu yok", "kaydının süresi doldu",
                                         "geçmiş dönüş yetersiz",
                                         "geçmiş açılma yok",
                                         "işlem fiyatı geçmişi")),
    ("CAPABILITY_MISSING", "DATA", ("yetenek kaydı yok", "piyasa keşfi",
                                    "mark fiyatı", "endeks fiyatı")),
    ("DRAWDOWN_LIMIT", "RISK", ("kayıp bütçesi", "kayıp limiti")),
    ("MARGIN_RISK", "RISK", ("teminat",)),
    ("POSITION_LIMIT", "RISK", ("pozisyon sınırı",)),
    ("ENTRY_HALTED", "RISK", ("yeni girişler durdu", "durumunda")),
    ("DUPLICATE_EXPOSURE", "RISK", ("açık çoklu borsa pozisyonu",)),
    ("COOLDOWN", "RISK", ("bekleme süresi",)),
    ("PRICE_GAP_STATS_REJECT", "ECONOMIC", ("korelasyon", "hedge beta", "z-score",
                                            "istikrarlı değil", "kesişim yok",
                                            "fiyat farkı kayıyor",
                                            "sınır ileri dönemde",
                                            "en büyük açılmanın üstünde",
                                            "fark kademe")),
    ("NET_EXPECTED_PNL_NEGATIVE", "ECONOMIC", ("karşılamıyor", "avantaj göstermiyor",
                                               "kalıcı avantaj yok",
                                               "tek bir güne dayanıyor",
                                               "birleşik getiri ücret, kayma ve risk payını",
                                               "yakınsama fiyat getirisi + funding",
                                               "sonrası kalmıyor", "sonrası negatif",
                                               "gideri sonrası yetersiz",
                                               "seviyesinin altında")),
    ("EXECUTION_FAILED", "EXECUTION", ("dolmadı", "doldurmadı", "fill")),
    ("STRATEGY_DISABLED", "STRATEGY", ("strateji kapalı",)),
)
CATEGORY = {code: cat for code, cat, _ in RULES}
CATEGORY.update(OTHER="OTHER", NO_COMMON_INSTRUMENT="DATA", COST_EXCEEDS_EDGE="ECONOMIC")
CATEGORY.update(ORDERBOOK_DESYNC="DATA", RFQ_STREAM_UNAVAILABLE="DATA", API_RATE_LIMIT="DATA",
                RFQ_STREAM_WARMING="DATA", VENUE_CIRCUIT_OPEN="DATA",
                RFQ_STREAM_NOT_SUBSCRIBED="DATA")

VERDICTS = dict(
    NO_OPPORTUNITY="Şu anda geçerli fırsat yok.",
    DATA="Veri kalitesi yetersiz.",
    HISTORY="Tarihsel örnek sayısı yetersiz.",
    COST="İşlem maliyeti fırsatı yok ediyor.",
    RISK="Risk limiti nedeniyle reddedildi.",
    EXECUTION="Paper execution başarısız.",
    STOPPED="Strategy engine çalışmıyor.",
    TRADING="Paper pozisyonu açıldı.",
)

KEEP_SECONDS = 24 * 3600
KEEP_REJECTED = 200
WINDOWS = (1, 8, 24)


def code_of(reason):
    """(code, category) of one engine reason text. Unmatched text stays OTHER and is
    kept verbatim next to it, so nothing is hidden behind a code."""
    # A leading venue id ("Hyperliquid: ...") is dropped; venue ids have no spaces.
    t = re.sub(r"^[A-Za-z0-9_.-]{1,30}: ", "", str(reason))
    # Turkish dotted/dotless capitals lower-case to i / ı (str.lower gives "i̇").
    t = t.replace("İ", "i").replace("I", "ı").lower()
    # Book synchronization and RFQ stream failures were previously classified as
    # OTHER, hiding actionable causes behind a generic counter.
    if "defter" in t and "senkron" in t:
        return "ORDERBOOK_DESYNC", "DATA"
    if "429" in t and ("rfq" in t or "websocket" in t):
        return "API_RATE_LIMIT", "DATA"
    # Venue rate limits (ccxt class names or our backoff message) and an open circuit
    # were OTHER or a generic book error before.
    if any(x in t for x in ("istek sınırı", "ratelimitexceeded", "ddosprotection",
                            "too many requests")):
        return "API_RATE_LIMIT", "DATA"
    if "devre açık" in t:
        return "VENUE_CIRCUIT_OPEN", "DATA"
    # A stream that is up (or connecting) but has not received its first snapshot yet:
    # a warming book, not a broken one. It was counted as OTHER before.
    if "rfq" in t and "snapshot bekl" in t:
        return "RFQ_STREAM_WARMING", "DATA"
    # No stream was started for this market (outside the stream limit): nothing is
    # broken, the book is simply not subscribed. Distinct from a failed stream.
    if "rfq" in t and ("başlatılmadı" in t or "baslatilmadi" in t):
        return "RFQ_STREAM_NOT_SUBSCRIBED", "DATA"
    if "rfq" in t and any(x in t for x in (
        "akışı", "akisi", "başlat", "baslat", "sessiz", "bağlı değil", "bagli degil"
    )):
        return "RFQ_STREAM_UNAVAILABLE", "DATA"
    for code, cat, patterns in RULES:
        if any(p in t for p in patterns):
            return code, cat
    return "OTHER", "OTHER"


def opportunity_id(c, at):
    raw = "%s|%s|%s|%s|%d" % (c.get("strategy"), c.get("asset"), c.get("long"),
                              c.get("short"), int(at // 60))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _num(x):
    return x if isinstance(x, (int, float)) and x == x else None


def opportunity(c, at, decision, reasons, stage):
    """What was known about one opportunity when it was decided. Missing values stay
    None. Deep-check money is in USD per position; initial scores are per $1 notional."""
    codes = [code_of(r)[0] for r in reasons] or []
    size = _num(c.get("size"))
    net = _num(c.get("net"))
    deep = size is not None and size > 0 and net is not None
    spread = _num(c.get("entry_gap", c.get("spread")))
    capture = _num(
        c.get("price_capture", c.get("entry_capture", c.get("reversion_cost")))
    )
    funding = _num(c.get("conservative_funding", c.get("funding_horizon")))
    if deep:
        gross = (
            (capture or 0.0) + (funding or 0.0)
        ) / size
        fees = (
            (_num(c.get("entry_fee")) or 0.0)
            + (_num(c.get("exit_fee")) or 0.0)
        ) / size
        score = net / size
    else:
        price_screen = _num(c.get("screen_price_gap", c.get("spread")))
        funding_screen = _num(c.get("screen_funding"))
        gross = (
            (price_screen or 0.0) + (funding_screen or 0.0)
            if price_screen is not None or funding_screen is not None
            else None
        )
        fees = _num(c.get("screen_fees", c.get("fee_roundtrip")))
        score = _num(c.get("screen_score", c.get("score")))
    return dict(
        id=opportunity_id(c, at),
        at=at,
        strategy=c.get("strategy"),
        asset=c.get("asset"),
        long=c.get("long"),
        short=c.get("short"),
        signed_spread_log=spread,  # log(short mark / long mark)
        signed_spread_bp=(
            round(spread * 1e4, 3) if spread is not None else None
        ),
        gross_edge_per_usd=gross,
        cost_breakdown=dict(
            basis="position_usd" if deep else "per_usd",
            price_gap=spread,
            price_target_gap=_num(c.get("target_gap")),
            price_capture=capture,
            price_capture_probability=_num(c.get("price_capture_probability")),
            funding=funding,
            entry_fee=_num(c.get("entry_fee")),
            exit_fee=_num(c.get("exit_fee")),
            execution_drag=_num(
                c.get("exit_drag_proxy")
                if c.get("strategy") == "convergence"
                else c.get("execution_drag")
            ),
            risk_reserve=_num(c.get("risk_reserve", c.get("stress_loss"))),
            net=net if deep else score,
        ),
        funding_gap_per_hour=_num(c.get("funding_gap")),
        funding_forecast_per_usd=(funding / size if deep and funding is not None else _num(c.get("funding_horizon"))),
        trading_costs_per_usd=fees,
        score_per_usd=score,
        expected_net_usd=net,
        size_usd=size,
        profit_probability=None,  # scenario engine not implemented yet (phase B)
        stage=stage,
        decision=decision,
        reason_codes=list(dict.fromkeys(codes)),
        reasons=[str(r) for r in reasons][:6],
    )


class Diagnostics:
    """Cycle records for the last 24 hours and the last rejected opportunities;
    `state()` / `load()` round-trip through the desk's persistence."""

    def __init__(self, state=None):
        state = state or {}
        self.cycles = list(state.get("cycles", []))
        self.rejected = list(state.get("rejected", []))
        self.current = None

    def state(self):
        return dict(cycles=self.cycles, rejected=self.rejected)

    # --- one cycle ------------------------------------------------------------------
    def start(self, at, flags):
        self.current = dict(
            at=at,
            counts={s: None for s in STAGES},
            codes={},
            flags=dict(flags),
            error=None,
        )
        for s in ("economic_candidates", "evidence_blocked", "simulation_passed",
                  "risk_approved", "paper_orders_created", "positions_opened",
                  "positions_rejected"):
            self.current["counts"][s] = 0
        return self.current

    def set(self, stage, n):
        if self.current is not None:
            self.current["counts"][stage] = n

    def add(self, stage, n=1):
        if self.current is not None:
            c = self.current["counts"]
            c[stage] = (c.get(stage) or 0) + n

    def note(self, c, at, decision, reasons, stage, count_codes=True):
        """Code one blocked or rejected opportunity of this cycle."""
        if self.current is None:
            return
        rec = opportunity(c, at, decision, reasons, stage)
        if count_codes:
            for code in rec["reason_codes"] or ["OTHER"]:
                self.current["codes"][code] = self.current["codes"].get(code, 0) + 1
        if decision != "opened":
            self.rejected.append(rec)
            del self.rejected[:-KEEP_REJECTED]

    def finish(self, at, error=None):
        if self.current is None:
            return
        self.current["error"] = error
        self.current["ended"] = at
        self.cycles.append(self.current)
        self.cycles = [x for x in self.cycles if at - x["at"] <= KEEP_SECONDS]
        self.current = None

    # --- reading --------------------------------------------------------------------
    def window(self, hours, now):
        xs = [x for x in self.cycles if now - x["at"] <= hours * 3600]
        totals, codes = {}, {}
        for x in xs:
            for s, n in x["counts"].items():
                if n is not None:
                    totals[s] = totals.get(s, 0) + n
            for k, n in x["codes"].items():
                codes[k] = codes.get(k, 0) + n
        last = xs[-1]["counts"] if xs else {}
        return dict(
            hours=hours,
            cycles=len(xs),
            errors=sum(bool(x.get("error")) for x in xs),
            totals={s: totals.get(s) for s in STAGES},
            last_cycle={s: last.get(s) for s in STAGES},
            reason_codes=dict(sorted(codes.items(), key=lambda kv: -kv[1])),
            categories=_categories(codes),
        )

    def verdict(self, now, stale_after=900):
        """One sentence for the latest state, with the numbers behind it."""
        if not self.cycles or now - self.cycles[-1].get("ended", self.cycles[-1]["at"]) > stale_after:
            last = self.cycles[-1]["at"] if self.cycles else None
            return dict(key="STOPPED", text=VERDICTS["STOPPED"],
                        evidence=dict(last_cycle_at=last, now=now))
        x = self.cycles[-1]
        c, codes, flags = x["counts"], x["codes"], x["flags"]
        evidence = dict(counts=c, reason_codes=codes, error=x.get("error"))
        if x.get("error") and not c.get("markets_discovered"):
            return dict(key="STOPPED", text=VERDICTS["STOPPED"], evidence=evidence)
        if not flags.get("enabled") or not (flags.get("funding") or flags.get("convergence")):
            return dict(key="STOPPED", text=VERDICTS["STOPPED"] + " (strateji kapalı)",
                        evidence=evidence)
        if c.get("positions_opened"):
            return dict(key="TRADING", text=VERDICTS["TRADING"], evidence=evidence)
        if c.get("paper_orders_created"):
            return dict(key="EXECUTION", text=VERDICTS["EXECUTION"], evidence=evidence)
        if not c.get("markets_discovered") or not c.get("data_quality_passed"):
            return dict(key="DATA", text=VERDICTS["DATA"], evidence=evidence)
        cats = _categories(codes)
        if not c.get("economic_candidates"):
            ranked = (c.get("funding_candidates") or 0) + (c.get("price_gap_candidates") or 0)
            key = "COST" if ranked else "NO_OPPORTUNITY"
            return dict(key=key, text=VERDICTS[key], evidence=evidence)
        if c.get("evidence_blocked") and c["evidence_blocked"] >= c["economic_candidates"]:
            key = "HISTORY" if cats.get("HISTORY", 0) > cats.get("DATA", 0) else "DATA"
            return dict(key=key, text=VERDICTS[key], evidence=evidence)
        top = max(cats, key=cats.get) if cats else None
        key = dict(DATA="DATA", HISTORY="HISTORY", ECONOMIC="COST", RISK="RISK",
                   EXECUTION="EXECUTION").get(top, "NO_OPPORTUNITY")
        return dict(key=key, text=VERDICTS[key], evidence=evidence)

    def report(self, now):
        return dict(
            verdict=self.verdict(now),
            windows={"%dh" % h: self.window(h, now) for h in WINDOWS},
            rejected=list(reversed(self.rejected[-20:])),
            stages=list(STAGES),
            not_implemented=dict(
                profit_probability="Senaryo motoru henüz yok",
            ),
        )


def _categories(codes):
    out = {}
    for k, n in codes.items():
        cat = CATEGORY.get(k, "OTHER")
        out[cat] = out.get(cat, 0) + n
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
