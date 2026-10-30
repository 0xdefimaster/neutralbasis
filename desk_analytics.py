"""Read-only screener and historical hold simulation; never exchange execution."""

import bisect, copy, csv, io, itertools, math, statistics, threading
from bot import now, hour, number
from risk_scan import RiskEngine, FiveFeed, VENUES


def history_summary(left, right, at):
    end = hour(at)
    times = list(range(end - 168 * 3600, end, 3600))
    if left.get("price_type") != "trade" or right.get("price_type") != "trade":
        return None
    if any(t not in left["prices"] or t not in right["prices"] for t in times):
        return None
    spreads = [math.log(right["prices"][t] / left["prices"][t]) for t in times]
    mean = statistics.mean(spreads)
    sd = statistics.pstdev(spreads)
    deadband = max(0.00002, sd * 0.15)
    sign = 0
    crossings = 0
    for x in spreads:
        current = 1 if x > deadband else -1 if x < -deadband else 0
        if current and sign and current != sign:
            crossings += 1
        if current:
            sign = current
    return dict(
        mean=mean,
        sd=sd,
        crossings=crossings,
        points=[
            dict(
                ts=t + 3600,
                spread=s,
                long_price=left["prices"][t],
                short_price=right["prices"][t],
            )
            for t, s in zip(times, spreads)
        ],
    )


def market_view(engine):
    rows = []
    pairs = []
    meta = engine.feed.meta
    for v in VENUES:
        try:
            fee = engine.fee(v)
        except ValueError:
            fee = None
        for a in engine.cfg["symbols"]:
            m = meta.get(v, {}).get(a)
            if m:
                rows.append(
                    dict(
                        asset=a,
                        venue=v,
                        mark=m["mark"],
                        rate=m.get("rate"),
                        interval=m.get("interval"),
                        next=m.get("next"),
                        received=m["received"],
                        fee=fee,
                        leverage=m["max_leverage"],
                        basis=m.get("rate_basis"),
                        audit_id=m["audit_id"],
                    )
                )
    for a in engine.cfg["symbols"]:
        venues = [v for v in VENUES if a in meta.get(v, {})]
        for x, y in itertools.combinations(venues, 2):
            lv, sv = sorted((x, y), key=lambda v: meta[v][a]["mark"])
            lm, sm = meta[lv][a], meta[sv][a]
            spread = math.log(sm["mark"] / lm["mark"])
            hs = [engine.feed.history.get((v, a)) for v in (lv, sv)]
            summary = history_summary(*hs, now()) if all(hs) else None
            funding = (
                None
                if any(m.get("rate") is None for m in (lm, sm))
                else sm["rate"] - lm["rate"]
            )
            try:
                fee = engine.fee(lv) + engine.fee(sv)
            except ValueError:
                fee = None
            candidate = next(
                (
                    c
                    for c in engine.candidates
                    if c["asset"] == a and {c["long"], c["short"]} == {lv, sv}
                ),
                None,
            )
            pairs.append(
                dict(
                    asset=a,
                    long=lv,
                    short=sv,
                    long_price=lm["mark"],
                    short_price=sm["mark"],
                    spread=spread,
                    funding=funding,
                    fee_roundtrip=fee * 2 if fee is not None else None,
                    history=summary,
                    deep=bool(candidate),
                    eligible=bool(candidate and candidate["eligible"]),
                    reason=(
                        (candidate.get("reasons") or [""])[0]
                        if candidate
                        else "Derin risk analizi sırası bekleniyor"
                    ),
                )
            )
    return dict(markets=rows, pairs=pairs)


def hold_backtest(
    prices,
    funding,
    fees,
    asset,
    lv,
    sv,
    size,
    hours,
    slippage_bps,
    include_price,
    at,
    intervals=None,
):
    """Equal base quantity; closed hourly trades. Historical depth is NOT available."""
    end = hour(at)
    times = list(range(end - (hours + 1) * 3600, end, 3600))
    if hours < 2:
        raise ValueError("En az iki saat gerekli")
    for v in (lv, sv):
        if prices[v].get("price_type") != "trade":
            raise ValueError(
                v + ": oracle mumları perp fiyat backtesti için kullanılamaz"
            )
        if any(t not in prices[v]["prices"] for t in times):
            raise ValueError(v + ": seçilen dönemde kesintisiz işlem mumu yok")
    first = times[0]
    last = times[-1]
    entry_time = first + 3600
    exit_time = last + 3600
    lp, sp = prices[lv]["prices"], prices[sv]["prices"]
    qty = size / ((lp[first] + sp[first]) / 2)
    payments = []
    counts = {}
    estimated = 0
    for v, sign in ((lv, -1), (sv, 1)):
        unique = {}
        for r in funding[v]:
            if not entry_time < r["ts"] <= exit_time:
                continue
            old = unique.get(r["ts"])
            if old and (old["rate"] != r["rate"] or old.get("value") != r.get("value")):
                raise ValueError("Aynı ödeme zamanında çelişen funding kayıtları")
            unique[r["ts"]] = r
        rows = list(unique.values())
        # Every held hourly settlement must be present; never turn absent records into zero.
        interval = (intervals or {}).get(v, 3600)
        if interval is None or interval < 0:
            raise ValueError(v + ": ödeme aralığı doğrulanmadı")
        if interval == 0:
            if rows:
                raise ValueError("Spot funding kaydı olamaz")
            counts[v] = 0
            continue
        if (hours * 3600) % interval:
            raise ValueError("Test dönemi ödeme aralığına bölünmüyor")
        expected = set(range(int(hours * 3600 / interval)))
        seen = {int((r["ts"] - entry_time - 1e-6) // interval) for r in rows}
        if expected - seen:
            raise ValueError(
                v
                + ": funding geçmişinde "
                + str(len(expected - seen))
                + " saat eksik; sonuç hesaplanmadı"
            )
        counts[v] = len(rows)
        for r in rows:
            value = r.get("value")
            valuation_estimated = value is None
            if valuation_estimated:
                # Last CLOSED candle available at this payment, no next-candle look-ahead.
                i = bisect.bisect_right(times, r["ts"] - 3600) - 1
                if i < 0:
                    raise ValueError("Ödeme öncesi fiyat eksik")
                value = r["rate"] * prices[v]["prices"][times[i]]
                estimated += 1
            payments.append(
                dict(
                    ts=r["ts"],
                    venue=v,
                    cash=sign * qty * value,
                    rate=sign * r["rate"],
                    estimated=valuation_estimated,
                    audit_id=r.get("audit_id"),
                )
            )
    entry_fee = qty * (lp[first] * fees[lv] + sp[first] * fees[sv])
    entry_slip = qty * (lp[first] + sp[first]) * slippage_bps / 10000
    points = []
    peak = 0.0
    drawdown = 0.0
    fund = 0.0
    idx = 0
    payments.sort(key=lambda r: r["ts"])
    for t in times:
        close = t + 3600
        paid = 0.0
        while idx < len(payments) and payments[idx]["ts"] <= close:
            fund += payments[idx]["cash"]
            paid += payments[idx]["cash"]
            idx += 1
        pnl = (
            qty * ((lp[t] - lp[first]) + (sp[first] - sp[t])) if include_price else 0.0
        )
        # Each point is hypothetical liquidated equity including both closing orders.
        costs = (
            entry_fee
            + entry_slip
            + qty * (lp[t] * fees[lv] + sp[t] * fees[sv])
            + qty * (lp[t] + sp[t]) * slippage_bps / 10000
        )
        net = fund + pnl - costs
        peak = max(peak, net)
        drawdown = max(drawdown, peak - net)
        points.append(
            dict(
                ts=close,
                funding=fund,
                price=pnl,
                costs=costs,
                net=net,
                paid=paid,
                long_price=lp[t],
                short_price=sp[t],
            )
        )
    result = dict(
        asset=asset,
        long=lv,
        short=sv,
        size=size,
        qty=qty,
        start=entry_time,
        end=exit_time,
        hours=hours,
        points=points,
        payments=payments,
        payment_counts=counts,
        estimated_payments=estimated,
        max_drawdown=drawdown,
        include_price=include_price,
        slippage_bps=slippage_bps,
        fees=fees,
        **{k: points[-1][k] for k in ("funding", "price", "costs", "net")},
        method="Eşit baz miktarıyla sabit tutuş. Gerçek kapanmış işlem mumları ve yayımlanmış funding kayıtları. Ücret tarifesi + emir başına kullanıcı kayma varsayımı; tarihsel emir defteri veya strateji getirisi değildir."
    )
    return result


def report_csv(engine):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(
        [
            "id",
            "asset",
            "strategy",
            "long",
            "short",
            "opened_utc",
            "closed_utc",
            "price_pnl_usd",
            "funding_usd",
            "estimated_funding_usd",
            "fees_usd",
            "net_usd",
            "pending_funding",
            "close_reason",
            "funding_valuation_methods",
            "funding_unverified_hours",
        ]
    )
    for p in engine.closed:
        d = p["decision"]
        writer.writerow(
            [
                p["id"],
                p["asset"],
                d.get("strategy", "funding"),
                d["long"],
                d["short"],
                p["opened"],
                p.get("closed_at"),
                p["price_pnl"],
                p["funding"],
                p.get("estimated_funding", 0),
                p["fees"],
                p.get("net"),
                bool(p.get("pending_funding")),
                p.get("close_reason"),
                " | ".join(
                    sorted({x["method"] for x in p.get("funding_valuations", [])})
                ),
                len(p.get("funding_unverified", [])),
            ]
        )
    return out.getvalue()


class DeskHistorySource:
    """Market list, incremental fetchers and publishing for the background history keeper."""

    def __init__(self, engine):
        self.engine = engine

    def priority(self):
        """Legs of the paper desk's tradeable candidates, rejected-for-history first."""
        return [(v, a, "perp") for v, a in self.engine.wide_paper.history_needs()]

    @staticmethod
    def group(v):
        """The desk's own feed venues share one worker; every scanned venue has its own."""
        return "feed" if v in VENUES else v

    def markets(self):
        e = self.engine
        out = []
        for v in VENUES:
            for a in e.cfg["symbols"]:
                m = e.feed.meta.get(v, {}).get(a)
                if m:
                    out.append((v, a, "perp", 3600, m.get("price_type", "trade")))
        rows, _ = e.wide.snapshot()
        seen = set()
        for r in rows:
            key = (r["venue"], r["asset"], r["kind"])
            if r["venue"] in VENUES or key in seen:
                continue
            seen.add(key)
            out.append(key + (r.get("interval") or None, "trade"))
        # Funding-only history for the widest matrix spreads outside the selected assets.
        from funding_matrix import top_pairs

        try:
            extra = top_pairs(e.matrix.get())
        except Exception:
            extra = []
        for v, a, interval in extra:
            key = (v, a, "perp")
            if v in VENUES or a in e.cfg["symbols"] or key in seen:
                continue
            seen.add(key)
            out.append(key + (interval, "trade"))
        # Every liquid perp the paper desk could trade (its own OI/volume floor), so a
        # deep check finds the history already stored instead of waiting for it.
        from wide_paper import UNIVERSE_MIN_USD

        universe = e.wide.universe_snapshot()
        for v, entries in universe.items():
            if v in VENUES:
                continue
            for a, x in entries.items():
                key = (v, a, "perp")
                depth = x.get("oi") if x.get("oi") is not None else x.get("volume")
                if key in seen or (depth or 0) < UNIVERSE_MIN_USD:
                    continue
                seen.add(key)
                out.append(key + (x.get("interval"), "trade"))
        # Paper candidate legs outside the lists above (any asset) are added, and all
        # candidate legs go first so missing history is filled where it can lead to a trade.
        priority = self.priority()
        rows_by = {(r["venue"], r["asset"]): r for r in rows if r["kind"] == "perp"}
        for key in priority:
            if key in seen:
                continue
            v, a, kind = key
            if v in VENUES:
                if a in e.cfg["symbols"] or not e.feed.meta.get(v, {}).get(a):
                    continue
                interval = e.feed.meta[v][a].get("interval")
                ptype = e.feed.meta[v][a].get("price_type", "trade")
            else:
                x = rows_by.get((v, a)) or universe.get(v, {}).get(a)
                if not x:
                    continue
                interval, ptype = x.get("interval"), "trade"
            seen.add(key)
            out.append(key + (interval, ptype))
        rank = {k: i for i, k in enumerate(priority)}
        return sorted(out, key=lambda m: rank.get(m[:3], len(rank)))

    def candles(self, v, a, kind, start, end):
        # Every listed market gets candles: the paper desk evaluates all assets.
        if v in VENUES:
            return self.engine.feed.candles_range(v, a, start, end)["prices"]
        return self.engine.wide.candles_since(v, a, kind, start, end)

    def funding(self, v, a, start):
        if v in VENUES:
            return self.engine.feed.funding_range(v, a, start, now())
        return self.engine.wide.funding_since(v, a, start)

    def publish(self, v, a, kind, prices, rates, interval, price_type):
        # Only the selected assets' price and funding views read history from memory;
        # every other market (the whole liquid universe) is read from the database, so
        # memory does not grow with the number of stored markets.
        if a not in self.engine.cfg["symbols"]:
            return
        wide = self.engine.wide
        with wide.lock:
            old = wide.histories.get((v, a, kind)) or {}
            wide.histories[(v, a, kind)] = dict(
                prices=dict(
                    prices=prices,
                    price_type=price_type
                    or (old.get("prices") or {}).get("price_type")
                    or "trade",
                    audit_ids=[],
                    end=hour(now()),
                ),
                rates=rates,
                interval=interval or old.get("interval"),
                ts=now(),
            )

    def stored_prices(self, v, a, kind="perp"):
        """Stored 30-day closes when they cover at least 95% of the window, else None."""
        start = hour(now()) - 720 * 3600
        prices = self.engine.history.db.candles(v, a, kind, start)
        if len(prices) < 720 * 0.95:
            return None
        return dict(
            prices=prices,
            price_type=self.price_type(v, a),
            audit_ids=[],
            end=hour(now()),
        )

    def price_type(self, v, a):
        """What the stored closes are: rows keep no label, so it comes from the source.
        A feed venue priced by its oracle (Arcus) stays "oracle", and the history checks
        refuse it as they refuse the live series; an unknown feed source is not taken
        for a trade series. Lighter, Extended and the scanned venues store trade closes.
        """
        if v not in VENUES or v in ("Lighter", "Extended"):
            return "trade"
        meta = self.engine.feed.meta.get(v, {}).get(a)
        return meta.get("price_type", "trade") if meta else "oracle"


class DeskEngine(RiskEngine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.backtest_lock = threading.RLock()
        self.backtest_job = dict(status="idle")
        self.backtest_thread = None
        from wide_markets import WideMarkets

        from wide_paper import WidePaper, EngineSource

        self.wide = WideMarkets(self.store, self.cfg["symbols"], self.stop_event)
        self.wide_paper = WidePaper(self.store, EngineSource(self))
        from history_store import HistoryKeeper

        self.history = HistoryKeeper(
            self.store, DeskHistorySource(self), self.stop_event
        )
        from funding_matrix import MatrixCache

        self.matrix = MatrixCache(self)
        from pair_view import PairViews

        self.pairs = PairViews(self)
        from extended_rfq import ExtendedRfq

        # Real RFQ books stream only once the engine loop runs (not in --once or tests).
        self.rfq = ExtendedRfq(self.store, self.stop_event)
        self.rfq_live = False
        from retention import Retention

        self.retention = Retention(self.store, self.stop_event)
        self.read_lock = threading.RLock()
        self.read_jobs = {}

    def capability_registry(self):
        """The full capability registry: every venue, capability, status and evidence."""
        from capabilities import CAPABILITIES, LABELS, STATUSES, REQUIRED

        return dict(
            venues=self.wide_paper.registry,
            capabilities=list(CAPABILITIES),
            labels=LABELS,
            statuses=list(STATUSES),
            required=REQUIRED,
        )

    def funding_matrix(self):
        return self.matrix.get()

    def asset_view(self, asset):
        return self.pairs.asset_view(asset)

    def pair_view(self, asset, long, short):
        return self.pairs.pair_view(asset, long, short)

    def pair_books(self, body):
        return self.pairs.books(
            body.get("asset"), body.get("long"), body.get("short"), body.get("size")
        )

    def request_pair_sync(self, body):
        return self.pairs.request_sync(
            body.get("asset"), list(body.get("venues") or [])
        )

    def evaluate(self, asset, lv, sv, books, analysis):
        from convergence import qualify

        c = super().evaluate(asset, lv, sv, books, analysis)
        if {lv, sv} != {"Lighter", "Extended"}:
            return c
        hs = [self.feed.history.get((v, asset)) for v in (lv, sv)]
        summary = history_summary(*hs, now()) if all(hs) else None
        return qualify(c, summary, self.cfg)

    def extra_candidates(self):
        from bot import analyze

        out = []
        for a in self.cfg["symbols"]:
            if self.stop_event.is_set():
                break
            if not all(a in self.feed.meta.get(v, {}) for v in ("Lighter", "Extended")):
                continue
            lv, sv = sorted(
                ("Lighter", "Extended"), key=lambda v: self.feed.meta[v][a]["mark"]
            )
            try:
                hs = {v: self.feed.candles(v, a) for v in (lv, sv)}
                ana = analyze(hs[lv], hs[sv], self.cfg["horizon"], now())
                for v in (lv, sv):
                    self.feed.funding_history(v, a)
                if now() - min(self.feed.meta[v][a]["received"] for v in (lv, sv)) > 40:
                    self.feed.markets()
                bs = {v: self.feed.book(v, a) for v in (lv, sv)}
                c = self.evaluate(a, lv, sv, bs, ana)
                c["risk_eligible"] = c["eligible"]
                out.append(c)
            except Exception as ex:
                out.append(
                    dict(
                        asset=a,
                        long=lv,
                        short=sv,
                        strategy="convergence",
                        eligible=False,
                        reasons=[str(ex)],
                        net=None,
                    )
                )
        return out

    def read_status(self):
        with self.read_lock:
            return copy.deepcopy(self.read_jobs)

    def request_read(self, kind, body):
        from wide_markets import CATALOG

        if kind not in ("history", "quote"):
            raise ValueError("Bilinmeyen okuma işi")
        a = body.get("asset")
        lv = body.get("long")
        sv = body.get("short")
        lk = body.get("long_kind", "perp")
        size = number(body.get("size", 1000))
        if (
            a not in self.cfg["symbols"]
            or lv not in CATALOG
            or sv not in CATALOG
            or lk not in ("perp", "spot")
            or not 10 <= size <= 1000000
        ):
            raise ValueError("Piyasa parametreleri geçersiz")
        if lv == sv:
            raise ValueError(
                "Aynı borsanın spot/perp analizi yalnızca taramada; ayrı piyasa kimliği hesabı henüz onaylı değil"
            )
        with self.read_lock:
            if self.read_jobs.get(kind, {}).get("status") == "running":
                raise ValueError("Bu tür analiz zaten sürüyor")
            self.read_jobs[kind] = dict(
                status="running", asset=a, long=lv, short=sv, long_kind=lk
            )

        def run():
            try:
                if not self.lock.acquire(timeout=90):
                    raise ValueError("Tarama bekleme süresi aşıldı")
                try:
                    feed = FiveFeed(self.store)
                    feed.meta = copy.deepcopy(self.feed.meta)
                    feed.arcus_fee = self.feed.arcus_fee
                    extra, _ = self.wide.snapshot()
                    fee_map = {}
                    marks = {}
                    for v, k in ((lv, lk), (sv, "perp")):
                        r = next(
                            (
                                r
                                for r in extra
                                if r["venue"] == v
                                and r["asset"] == a
                                and r["kind"] == k
                            ),
                            None,
                        )
                        marks[v] = (
                            self.feed.meta[v][a]["mark"]
                            if v in VENUES and k == "perp"
                            else r["mark"] if r else None
                        )
                        try:
                            fee_map[v] = self.fee(v)
                        except ValueError:
                            fee_map[v] = r.get("fee") if r else None
                finally:
                    self.lock.release()
                if any(p is None for p in marks.values()):
                    raise ValueError("Güncel piyasa verisi eksik")
                if kind == "history":
                    prices = {}
                    issues = []
                    for v, k in ((lv, lk), (sv, "perp")):
                        if v in VENUES and k == "perp":
                            prices[v] = feed.candles(v, a)
                            try:
                                rates = feed.funding_history(v, a, now() - 7 * 86400)
                            except Exception as ex:
                                rates = []
                                issues.append(str(ex))
                            with self.wide.lock:
                                self.wide.histories[(v, a, k)] = dict(
                                    prices=prices[v],
                                    rates=rates,
                                    interval=feed.meta.get(v, {})
                                    .get(a, {})
                                    .get("interval"),
                                    ts=now(),
                                )
                        else:
                            prices[v], _, _ = self.wide.history(v, a, 7, k)
                    summary = history_summary(prices[lv], prices[sv], now())
                    if summary is None:
                        raise ValueError(
                            "Kesintisiz gerçek perp işlem mumları doğrulanmadı; oracle veya eksik seri olabilir"
                        )
                    result = dict(
                        history=summary,
                        issues=issues,
                        long_kind=lk,
                        **{k: body[k] for k in ("asset", "long", "short")}
                    )
                else:
                    from bot import walk

                    if any(f is None for f in fee_map.values()):
                        raise ValueError(
                            "Dört emir maliyeti için iki borsanın taker tarifesi gerekli"
                        )
                    qty = size / ((marks[lv] + marks[sv]) / 2)
                    books = {
                        v: (
                            feed.book(v, a)
                            if v in VENUES and k == "perp"
                            else self.wide.book(v, a, k)
                        )
                        for v, k in ((lv, lk), (sv, "perp"))
                    }
                    if now() - min(b["received"] for b in books.values()) > 30:
                        raise ValueError("Defterler birlikte güncel değil")
                    buy = {v: walk(b["asks"], qty, True) for v, b in books.items()}
                    sell = {v: walk(b["bids"], qty, False) for v, b in books.items()}
                    fees = qty * sum((buy[v] + sell[v]) * fee_map[v] for v in books)
                    drag = qty * sum(buy[v] - sell[v] for v in books)
                    entry_capture = qty * (sell[sv] - buy[lv])
                    exit_proxy = drag / 2
                    result = dict(
                        asset=a,
                        long=lv,
                        short=sv,
                        long_kind=lk,
                        qty=qty,
                        size=size,
                        fees=fees,
                        roundtrip_book_drag=drag,
                        entry_capture=entry_capture,
                        exit_drag_proxy=exit_proxy,
                        convergence_net_model=entry_capture - exit_proxy - fees,
                        received=now(),
                        audit_ids=[b["audit_id"] for b in books.values()],
                        method="Güncel dört yönün defter VWAP’ı. Sıfır basis hedefi ve güncel yarım spread çıkış vekili; gelecek çıkış likiditesi, funding ve stres dahil değil. Paper giriş onayı değildir.",
                    )
                with self.read_lock:
                    self.read_jobs[kind] = dict(status="done", result=result)
            except Exception as ex:
                with self.read_lock:
                    self.read_jobs[kind] = dict(status="error", error=str(ex))

        threading.Thread(target=run, daemon=True).start()
        return dict(ok=True, message="Public veri analizi sıraya alındı.")

    def cycle(self):
        super().cycle()
        # Multi-venue desk: open positions are always monitored; entries only while running.
        self.wide_paper.latency_ms = self.cfg["latency_ms"]
        self.wide_paper.extra_slippage = self.cfg["extra_slippage_bps"] / 10000
        self.wide_paper.cycle(entries=self.running)
        if self.rfq_live:
            self.rfq.sync(self.feed.meta.get("Extended"))

    def loop(self):
        self.rfq_live = True
        self.wide.start()
        self.history.start()
        self.retention.start()
        return super().loop()

    def update_config(self, changes):
        wide = {k: v for k, v in changes.items() if k.startswith("wide_")}
        rest = {k: v for k, v in changes.items() if k not in wide}
        if wide:
            self.wide_paper.update_config(wide)
        if rest or not wide:
            # Fill latency and extra slippage also price the multi-venue desk's fills.
            shared = ("latency_ms", "extra_slippage_bps")
            before = {k: self.cfg.get(k) for k in shared}
            super().update_config(rest)
            if any(self.cfg.get(k) != before[k] for k in shared):
                self.wide_paper.note_change()
        self.wide.symbols = list(self.cfg["symbols"])

    def snapshot(self):
        from wide_markets import expanded_view, LABELS

        o = super().snapshot()
        o["market_view"] = expanded_view(self, market_view(self))
        o["venue_labels"] = LABELS
        o["wide_paper"] = self.wide_paper.snapshot()
        o["history_status"] = self.history.status()
        o["rfq_streams"] = self.rfq.status()
        o["retention"] = self.retention.status
        o["deployment"] = (
            "Geniş public tarama: DEX + CEX. Lighter Core ve Robinhood ayrı kaynaklar. Paper yürütme: Lighter RH–Extended masası + tüm taranan borsalarda çoklu borsa funding masası."
        )
        return o

    def backtest_status(self):
        with self.backtest_lock:
            return copy.deepcopy(self.backtest_job)

    def request_backtest(self, body):
        asset = body.get("asset", "BTC")
        lv = body.get("long", "Extended")
        sv = body.get("short", "Lighter")
        days = number(body.get("days", 7))
        size = number(body.get("size", 1000))
        slip = number(body.get("slippage_bps", 2))
        from wide_markets import CATALOG

        kind = body.get("long_kind", "perp")
        if (
            days not in (1, 7, 30, 90)
            or (lv == sv and kind != "spot")
            or lv not in CATALOG
            or sv not in CATALOG
            or asset not in self.cfg["symbols"]
            or not 10 <= size <= 1000000
            or not 0 <= slip <= 100
            or kind not in ("perp", "spot")
        ):
            raise ValueError("Backtest parametreleri geçersiz")
        end_at = hour(now())
        hours = int(days) * 24
        if body.get("start_time") is not None or body.get("end_time") is not None:
            start_at = number(body.get("start_time"))
            end_at = number(body.get("end_time"))
            if (
                start_at % 3600
                or end_at % 3600
                or not 2 * 3600 <= end_at - start_at <= 90 * 86400
                or end_at > hour(now())
                or start_at < now() - 91 * 86400
            ):
                raise ValueError(
                    "Tarih aralığı kapanmış saatlere hizalı ve en çok 90 gün olmalı"
                )
            hours = int((end_at - start_at) / 3600)
            days = math.ceil(hours / 24)
        if not isinstance(body.get("include_price", True), bool):
            raise ValueError("Fiyat etkisi seçimi geçersiz")
        extra, _ = self.wide.snapshot()

        def market(v, k):
            return next(
                (
                    r
                    for r in extra
                    if r["venue"] == v and r["asset"] == asset and r["kind"] == k
                ),
                None,
            )

        if any(
            (
                asset not in self.feed.meta.get(v, {})
                if v in VENUES and k == "perp"
                else not market(v, k)
            )
            for v, k in ((lv, kind), (sv, "perp"))
        ):
            raise ValueError("Seçili piyasa için güncel veri doğrulanmadı")
        fee_map = {}
        for v in (lv, sv):
            try:
                fee_map[v] = self.fee(v)
            except ValueError:
                known = market(v, kind if v == lv else "perp")
                if known and known.get("fee") is not None:
                    fee_map[v] = known["fee"]
                    continue
                custom = body.get("fee_bps", {}).get(v)
                if custom is None:
                    raise ValueError(
                        v + " için backtest taker ücreti bp olarak gerekli"
                    )
                fee_map[v] = number(custom) / 10000
            if not 0 <= fee_map[v] <= 0.01:
                raise ValueError("Ücret sınır dışında")
        if any(not 0 <= f <= 0.01 for f in fee_map.values()):
            raise ValueError("Ücret sınır dışında")
        with self.backtest_lock:
            if self.backtest_job["status"] == "running":
                raise ValueError("Bir backtest zaten hesaplanıyor")
            self.backtest_job = dict(
                status="running", requested=now(), asset=asset, long=lv, short=sv
            )
        feed = FiveFeed(self.store)
        feed.meta = copy.deepcopy(self.feed.meta)
        feed.symbols = list(self.cfg["symbols"])
        feed.arcus_fee = self.feed.arcus_fee

        def run():
            try:
                if lv == sv:
                    raise ValueError(
                        "Aynı borsada spot-perp backtesti ayrı piyasa kimliği gerektiriyor; şu an taramada destekleniyor"
                    )
                at = end_at
                prices = {}
                rates = {}
                intervals = {}
                for v, k in ((lv, kind), (sv, "perp")):
                    if v in VENUES and k == "perp":
                        first = hour(at) - (hours + 1) * 3600
                        if first < hour(now()) - 720 * 3600:
                            # Older than the 30-day live cache: page the native APIs.
                            prices[v] = feed.candles_range(v, asset, first, hour(at))
                            rates[v] = feed.funding_range(v, asset, first, at)
                        else:
                            prices[v] = feed.candles(v, asset)
                            rates[v] = feed.funding_history(
                                v, asset, hour(at) - int(days) * 86400
                            )
                        intervals[v] = 3600
                    else:
                        prices[v], rates[v], row = self.wide.history(
                            v, asset, days, k, at
                        )
                        intervals[v] = row["interval"]
                result = hold_backtest(
                    prices,
                    rates,
                    fee_map,
                    asset,
                    lv,
                    sv,
                    size,
                    hours,
                    slip,
                    body.get("include_price", True),
                    at,
                    intervals,
                )
                with self.backtest_lock:
                    self.backtest_job = dict(status="done", result=result)
                self.store.event(
                    "BACKTEST",
                    dict(asset=asset, long=lv, short=sv, net=result["net"], days=days),
                )
            except Exception as ex:
                with self.backtest_lock:
                    self.backtest_job = dict(status="error", error=str(ex))

        self.backtest_thread = threading.Thread(target=run, daemon=True)
        self.backtest_thread.start()
        return dict(ok=True, message="Backtest gerçek geçmiş verilerle hesaplanıyor.")
