"""Exchange capability registry: what each connector really supports, in four states,
with the evidence behind every status.

A status is never taken from a connection test. Each venue has a declaration of what
its connector implements (source and known limits); capabilities that live data can
prove are then verified at runtime from what the desk actually received:

  * market discovery, contract metadata, REST data, mark/index price, funding rate,
    open interest, volume, fees: from the fresh market rows of each scan;
  * order book / bid-ask: from every book read (valid, uncrossed, fresh) or its error;
  * settled funding history: from the history keeper's coverage per market;
  * funding interval: the median gap between stored settled payments must match the
    interval the venue publishes;
  * funding sign: on markets whose mark is clearly away from the index, the funding
    rate must point the same way as the premium (mark above index: longs pay). This
    checks the long/short direction of the published sign without an account; a
    venue whose signs run against the premium is marked inverted.

Paper strategies read eligibility from here: a pair whose leg lacks a required
capability is kept as research only, with the reason, and is never executed.
"""

import statistics

FULL = "FULLY_SUPPORTED"
PARTIAL = "PARTIALLY_SUPPORTED"
NONE = "NOT_SUPPORTED"
UNVERIFIED = "NOT_VERIFIED"
STATUSES = (FULL, PARTIAL, NONE, UNVERIFIED)
USABLE = (FULL, PARTIAL)

CAPABILITIES = (
    "market_discovery",
    "contract_metadata",
    "ws_market_data",
    "rest_market_data",
    "bid_ask",
    "order_book",
    "mark_price",
    "index_price",
    "last_price",
    "funding_rate",
    "funding_history",
    "funding_interval",
    "funding_sign",
    "open_interest",
    "volume",
    "fees",
    "leverage_tiers",
    "account_balances",
    "positions",
    "order_placement",
    "order_cancellation",
    "testnet",
)
LABELS = dict(
    market_discovery="Piyasa keşfi",
    contract_metadata="Kontrat bilgisi",
    ws_market_data="WebSocket veri",
    rest_market_data="REST veri",
    bid_ask="Alış / satış",
    order_book="Emir defteri",
    mark_price="Mark fiyatı",
    index_price="Endeks fiyatı",
    last_price="İşlem fiyatı geçmişi",
    funding_rate="Funding oranı",
    funding_history="Gerçekleşmiş funding",
    funding_interval="Funding aralığı",
    funding_sign="Funding işareti",
    open_interest="Açık pozisyon",
    volume="Hacim",
    fees="Ücret",
    leverage_tiers="Kaldıraç / marjin",
    account_balances="Hesap bakiyesi",
    positions="Pozisyonlar",
    order_placement="Emir gönderme",
    order_cancellation="Emir iptali",
    testnet="Testnet",
)

# What a paper strategy needs from each leg's venue. Both settle funding on every
# payment while the position is open, so both need the schedule, settled history and
# sign; both judge prices on traded (not oracle) history. The order book is not in
# the list: every use reads it and refuses a stale, crossed or empty one, so it is
# checked where it is used (the registry shows the last read).
REQUIRED = dict(
    funding=(
        "market_discovery",
        "contract_metadata",
        "last_price",
        "funding_rate",
        "funding_history",
        "funding_interval",
        "funding_sign",
        "fees",
    ),
    convergence=(
        "market_discovery",
        "contract_metadata",
        "last_price",
        "funding_rate",
        "funding_history",
        "funding_interval",
        "funding_sign",
        "fees",
    ),
)

# The initial perp DEX universe. Lighter (the Robinhood Chain instance, own API host
# api.rh.lighter.xyz, USDG collateral) and LighterCore (mainnet.zklighter.elliot.ai via
# CCXT) are separate venues with separate markets and accounts.
PERP_DEXES = (
    "Hyperliquid",
    "Paradex",
    "LighterCore",
    "Lighter",
    "Extended",
    "RISEx",
    "TxFlow",
    "Arcus",
    "Variational",
)
# Venues whose rows come from the desk's own feed (not CCXT).
NATIVE = ("Lighter", "Extended", "RISEx", "TxFlow", "Arcus")
NOT_BUILT = "Bağlayıcıda yok"
ACCOUNT_NOT_BUILT = (
    "Hesap / emir bağlantısı yazılmadı; paper yalnızca public veriyle çalışır"
)


def _ccxt(adapter, **extra):
    caps = dict(
        market_discovery=(UNVERIFIED, "CCXT load_markets + toplu funding", None),
        contract_metadata=(UNVERIFIED, "CCXT market: contractSize, precision", None),
        ws_market_data=(NONE, None, NOT_BUILT),
        rest_market_data=(UNVERIFIED, "CCXT REST", None),
        bid_ask=(UNVERIFIED, "CCXT fetch_order_book", "REST anlık görüntü"),
        order_book=(UNVERIFIED, "CCXT fetch_order_book", "REST anlık görüntü"),
        mark_price=(UNVERIFIED, "CCXT funding/mark", None),
        index_price=(UNVERIFIED, "CCXT funding indexPrice", None),
        last_price=(
            PARTIAL,
            "CCXT fetch_ohlcv 1h işlem mumu",
            "Yalnızca 1 saatlik kapanış",
        ),
        funding_rate=(UNVERIFIED, "CCXT funding rate", None),
        funding_history=(UNVERIFIED, "CCXT fetch_funding_rate_history", None),
        funding_interval=(UNVERIFIED, "CCXT / borsa alanı", None),
        funding_sign=(UNVERIFIED, "CCXT normalize işaret", None),
        open_interest=(UNVERIFIED, "CCXT open interest", None),
        volume=(UNVERIFIED, "CCXT ticker quoteVolume", None),
        fees=(UNVERIFIED, "CCXT public taker tarifesi", "Hesap kademesi okunmuyor"),
        leverage_tiers=(PARTIAL, "CCXT limits.leverage.max", "Marjin kademeleri yok"),
        account_balances=(NONE, None, ACCOUNT_NOT_BUILT),
        positions=(NONE, None, ACCOUNT_NOT_BUILT),
        order_placement=(NONE, None, ACCOUNT_NOT_BUILT),
        order_cancellation=(NONE, None, ACCOUNT_NOT_BUILT),
        testnet=(NONE, None, NOT_BUILT),
    )
    caps.update(extra)
    return dict(connector="CCXT " + adapter, caps=caps)


def _native(source, **extra):
    caps = dict(
        market_discovery=(
            UNVERIFIED,
            source,
            "Yalnızca ayarlardaki varlık listesi taranıyor",
        ),
        contract_metadata=(UNVERIFIED, source, "Miktar taban birimde"),
        ws_market_data=(NONE, None, NOT_BUILT),
        rest_market_data=(UNVERIFIED, source, None),
        bid_ask=(UNVERIFIED, source + " defter", "REST anlık görüntü"),
        order_book=(UNVERIFIED, source + " defter", "REST anlık görüntü"),
        mark_price=(UNVERIFIED, source, None),
        index_price=(UNVERIFIED, source, None),
        last_price=(PARTIAL, source + " 1h işlem mumu", "Yalnızca 1 saatlik kapanış"),
        funding_rate=(UNVERIFIED, source, None),
        funding_history=(UNVERIFIED, source + " funding geçmişi", None),
        funding_interval=(UNVERIFIED, source, None),
        funding_sign=(UNVERIFIED, source, None),
        open_interest=(NONE, None, "Toplanmıyor"),
        volume=(NONE, None, "Toplanmıyor"),
        fees=(UNVERIFIED, "Ayarlar / public tarife", None),
        leverage_tiers=(PARTIAL, source + " azami kaldıraç", "Marjin kademeleri yok"),
        account_balances=(NONE, None, ACCOUNT_NOT_BUILT),
        positions=(NONE, None, ACCOUNT_NOT_BUILT),
        order_placement=(NONE, None, ACCOUNT_NOT_BUILT),
        order_cancellation=(NONE, None, ACCOUNT_NOT_BUILT),
        testnet=(NONE, None, NOT_BUILT),
    )
    caps.update(extra)
    return dict(connector=source, caps=caps)


DECLARED = dict(
    Hyperliquid=_ccxt("hyperliquid"),
    Paradex=_ccxt(
        "paradex",
        funding_interval=(
            UNVERIFIED,
            "CCXT / borsa alanı",
            "Paradex funding'i sürekli işler; ödeme aralığı modeli ödemelerle "
            "doğrulanmadan kullanılmaz",
        ),
    ),
    LighterCore=_ccxt("lighter"),
    Lighter=_native("Lighter RH API (api.rh.lighter.xyz)"),
    Extended=_native(
        "Extended API",
        fees=(
            UNVERIFIED,
            "Ayarlar / salt okunur hesap anahtarı",
            "Hesap ücreti yalnızca anahtar girilirse okunur",
        ),
        ws_market_data=(
            PARTIAL,
            "Extended RFQ WebSocket",
            "Yalnızca RFQ piyasaları; normal defter REST",
        ),
    ),
    RISEx=_native("RISEx API"),
    TxFlow=_native(
        "TxFlow RPC",
        funding_history=(
            NONE,
            None,
            "Public gerçekleşmiş funding geçmişi / pozisyon tahakkuk modeli "
            "doğrulanmadı",
        ),
    ),
    Arcus=_native(
        "Arcus API",
        last_price=(
            NONE,
            "Arcus oracle mumları",
            "Fiyat geçmişi oracle; işlem fiyatı değil, fiyat uyumu ölçülemez",
        ),
    ),
    Variational=dict(
        connector="Katalog kaydı (bağlayıcı yok)",
        caps={c: (NONE, None, "Bağlayıcı yazılmadı") for c in CAPABILITIES},
    ),
)

# Runtime thresholds.
FRESH = 300  # rows older than this do not count as live evidence
BOOK_FRESH = 3600  # a book read proves the capability for this long
SIGN_PREMIUM = 0.0005  # |mark/index - 1| needed for a sign observation (5 bp)
SIGN_MIN = 10  # observations needed to decide the sign
SIGN_AGREE = 0.8  # share that must agree (and at most 1 - this may, for inverted)
SIGN_WINDOW = 3 * 86400
INTERVAL_TOLERANCE = 0.05
INTERVAL_MIN_PAYMENTS = 6
INTERVAL_SAMPLE = 10  # markets re-checked per round once a venue is verified
INTERVAL_BOOTSTRAP = 3  # markets per venue checked first, every round until decided
INTERVAL_RECHECK = 6 * 3600


def median_gap(times):
    ts = sorted(set(times))
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    return statistics.median(gaps) if gaps else None


class Registry:
    """Evidence per venue and the statuses derived from it. `state` is plain JSON so it
    survives restarts; `evaluate` turns declarations plus evidence into the registry."""

    def __init__(self, state=None):
        state = state or {}
        self.books = state.get("books", {})
        self.signs = state.get("signs", {})
        self.intervals = state.get("intervals", {})
        self.rows = {}
        self.history = {}
        self.fee_sources = {}
        self.cursor = state.get("cursor", 0)

    def state(self):
        return dict(
            books=self.books,
            signs=self.signs,
            intervals=self.intervals,
            cursor=self.cursor,
        )

    # --- evidence -----------------------------------------------------------------
    def observe_rows(self, rows, at):
        """Per venue counts from the fresh perp rows of this scan, and one funding-sign
        observation per market and hour where the mark is clearly off the index."""
        by = {}
        for r in rows:
            if r.get("kind", "perp") != "perp":
                continue
            if at - (r.get("received") or 0) > FRESH:
                continue
            v = r["venue"]
            x = by.setdefault(
                v,
                dict(
                    markets=0,
                    contract=0,
                    mark=0,
                    index=0,
                    rate=0,
                    interval=0,
                    fee=0,
                    oi=0,
                    volume=0,
                    at=at,
                ),
            )
            x["markets"] += 1
            x["contract"] += bool(r.get("contract_size"))
            x["mark"] += bool(r.get("mark"))
            x["index"] += bool(r.get("index"))
            x["rate"] += r.get("raw_rate") is not None
            x["interval"] += bool(r.get("interval"))
            x["fee"] += r.get("fee") is not None
            x["oi"] += r.get("oi") is not None
            x["volume"] += r.get("volume") is not None
            if r.get("fee_source"):
                self.fee_sources.setdefault(v, set()).add(r["fee_source"])
            self.observe_sign(v, r, at)
        self.rows = by

    def observe_sign(self, v, r, at):
        mark, index, rate = r.get("mark"), r.get("index"), r.get("raw_rate")
        if not mark or not index or rate is None or rate == 0:
            return
        premium = mark / index - 1
        if abs(premium) < SIGN_PREMIUM:
            return
        key = "%s|%d" % (r["asset"], int(at) // 3600)
        obs = self.signs.setdefault(v, {})
        obs[key] = [at, (rate > 0) == (premium > 0)]
        for k in [k for k, (t, _) in obs.items() if at - t > SIGN_WINDOW]:
            del obs[k]

    def observe_book(self, v, ok, at, error=None):
        self.books[v] = dict(
            ok_at=at if ok else (self.books.get(v) or {}).get("ok_at"),
            error=None if ok else str(error)[:160],
            error_at=None if ok else at,
        )

    def observe_history(self, states):
        """`states`: {(venue, asset): keeper market state}; counts complete markets."""
        by = {}
        for (v, a, *_), s in states.items():
            x = by.setdefault(v, dict(markets=0, complete=0))
            x["markets"] += 1
            x["complete"] += bool(s.get("complete"))
        self.history = by

    def check_market(self, v, a, interval, stored, at, start):
        """Compare one market's published interval with the median gap of its stored
        settled payments; None when there are too few payments to tell."""
        key = "%s|%s" % (v, a)
        try:
            times = [x["ts"] for x in stored(v, a, start)]
        except Exception:
            return None
        gap = median_gap(times) if len(times) >= INTERVAL_MIN_PAYMENTS else None
        match = (
            None
            if gap is None
            else abs(gap - interval) <= INTERVAL_TOLERANCE * interval
        )
        self.intervals[key] = dict(at=at, published=interval, gap=gap, match=match)
        return match

    def due(self, v, a, at):
        x = self.intervals.get("%s|%s" % (v, a))
        return not x or at - x["at"] > INTERVAL_RECHECK

    def check_intervals(self, rows, stored, at, start):
        """Every venue first gets INTERVAL_BOOTSTRAP decided markets (tried each round
        until they have enough payments); then a rotating sample is re-checked."""
        perps = [
            r for r in rows if r.get("kind", "perp") == "perp" and r.get("interval")
        ]
        decided = {}
        for k, x in self.intervals.items():
            if x["gap"] is not None:
                decided[k.split("|")[0]] = decided.get(k.split("|")[0], 0) + 1
        tried = {}
        for r in perps:
            v = r["venue"]
            if decided.get(v, 0) + tried.get(v, 0) >= INTERVAL_BOOTSTRAP:
                continue
            if not self.due(r["venue"], r["asset"], at) and (
                self.intervals["%s|%s" % (v, r["asset"])]["gap"] is not None
            ):
                continue
            tried[v] = tried.get(v, 0) + 1
            self.check_market(v, r["asset"], r["interval"], stored, at, start)
        rest = [r for r in perps if self.due(r["venue"], r["asset"], at)]
        if not rest:
            return
        self.cursor %= len(rest)
        for r in (rest[self.cursor :] + rest[: self.cursor])[:INTERVAL_SAMPLE]:
            self.check_market(r["venue"], r["asset"], r["interval"], stored, at, start)
        self.cursor += INTERVAL_SAMPLE

    def market_ok(self, v, a):
        """False when this market's settled payments contradict its published interval."""
        x = self.intervals.get("%s|%s" % (v, a))
        return not (x and x.get("match") is False)

    # --- statuses -----------------------------------------------------------------
    def evaluate(self, at, venues=None):
        """{venue: {capability: entry}} for the declared venues plus any venue seen in
        the rows (a scanned venue without a declaration gets the generic CCXT one)."""
        names = list(venues or PERP_DEXES)
        for v in self.rows:
            if v not in names:
                names.append(v)
        out = {}
        for v in names:
            decl = DECLARED.get(v) or _ccxt("(genel)")
            caps = {}
            for c in CAPABILITIES:
                status, source, limit = decl["caps"][c]
                caps[c] = dict(
                    status=status,
                    source=source,
                    limitation=limit,
                    verified_at=None,
                    method="Bağlayıcı beyanı",
                    test=TESTS.get(c),
                )
            self.verify(v, caps, at)
            for c, x in caps.items():
                x["eligible"] = x["status"] in USABLE
            out[v] = dict(
                venue=v,
                connector=decl["connector"],
                declared=v in DECLARED,
                perp_dex=v in PERP_DEXES,
                caps=caps,
                strategies={
                    s: [c for c in need if caps[c]["status"] not in USABLE]
                    for s, need in REQUIRED.items()
                },
            )
        return out

    def verify(self, v, caps, at):
        rows = self.rows.get(v)

        def set_(c, status, method, limit=None, when=at):
            # A capability the connector does not implement is never upgraded.
            if caps[c]["status"] == NONE and status != NONE:
                return
            caps[c].update(status=status, method=method, verified_at=when)
            if limit:
                caps[c]["limitation"] = limit

        def share(field, c, method, partial_limit):
            n, k = rows["markets"], rows[field]
            if k == 0:
                set_(c, UNVERIFIED, method + ": canlı veride yok")
            elif k == n:
                set_(c, FULL, method)
            else:
                set_(c, PARTIAL, method, "%d/%d piyasada %s" % (k, n, partial_limit))

        if rows:
            set_(
                "market_discovery",
                FULL if v not in NATIVE else PARTIAL,
                "Canlı tarama: %d taze perp piyasası" % rows["markets"],
                None if v not in NATIVE else caps["market_discovery"]["limitation"],
            )
            set_("rest_market_data", FULL, "Canlı tarama: taze satırlar")
            share("contract", "contract_metadata", "Kontrat büyüklüğü", "var")
            share("mark", "mark_price", "Mark fiyatı", "var")
            share("index", "index_price", "Endeks fiyatı", "var")
            share("rate", "funding_rate", "Funding oranı", "var")
            share("oi", "open_interest", "Açık pozisyon", "var")
            share("volume", "volume", "Hacim", "var")
            if rows["fee"] == 0:
                set_("fees", NONE, "Ücret bilinmiyor; işlem açılmaz")
            else:
                src = self.fee_sources.get(v, set())
                set_(
                    "fees",
                    PARTIAL,
                    "Ücret kaynağı: "
                    + (
                        "kullanıcı girişi"
                        if "user" in src
                        else "public tarife" if "public" in src else "bilinmiyor"
                    ),
                    "Hesap ücret kademesi API'den okunmadı",
                )
        b = self.books.get(v)
        if b and b.get("ok_at") and at - b["ok_at"] <= BOOK_FRESH:
            for c in ("order_book", "bid_ask"):
                set_(
                    c,
                    PARTIAL,
                    "Geçerli defter okundu",
                    "REST anlık görüntü",
                    b["ok_at"],
                )
        elif b and b.get("error"):
            for c in ("order_book", "bid_ask"):
                set_(c, UNVERIFIED, "Son defter okuması hatalı: " + b["error"])
        h = self.history.get(v)
        if caps["funding_history"]["status"] != NONE and h and h["markets"]:
            if h["complete"] == 0:
                set_("funding_history", UNVERIFIED, "Geçmiş henüz dolmadı")
            elif h["complete"] == h["markets"]:
                set_("funding_history", FULL, "30 günlük kayıt tam")
            else:
                set_(
                    "funding_history",
                    PARTIAL,
                    "30 günlük kayıt",
                    "%d/%d piyasada tam" % (h["complete"], h["markets"]),
                )
        checks = [
            x for k, x in self.intervals.items() if k.split("|")[0] == v and x["gap"]
        ]
        if checks:
            good = sum(bool(x["match"]) for x in checks)
            when = max(x["at"] for x in checks)
            method = "Gerçekleşmiş ödeme aralığı = yayımlanan aralık: %d/%d piyasa" % (
                good,
                len(checks),
            )
            if good == len(checks) and good >= 3:
                set_("funding_interval", FULL, method, None, when)
            elif good / len(checks) >= 0.8:
                set_(
                    "funding_interval",
                    PARTIAL,
                    method,
                    "Uyuşmayan piyasalar işlem dışı",
                    when,
                )
            elif len(checks) >= 3 and good / len(checks) < 0.5:
                set_(
                    "funding_interval",
                    NONE,
                    method,
                    "Yayımlanan aralık ödemelerle uyuşmuyor",
                    when,
                )
            else:
                set_("funding_interval", UNVERIFIED, method, None, when)
        obs = list((self.signs.get(v) or {}).values())
        if obs:
            agree = sum(bool(a) for t, a in obs)
            method = "Prim yönüyle uyum: %d/%d gözlem (|mark/endeks-1| ≥ %.0f bp)" % (
                agree,
                len(obs),
                SIGN_PREMIUM * 1e4,
            )
            when = max(t for t, a in obs)
            if len(obs) < SIGN_MIN:
                set_(
                    "funding_sign", UNVERIFIED, method + "; gözlem yetersiz", None, when
                )
            elif agree / len(obs) >= SIGN_AGREE:
                set_(
                    "funding_sign",
                    PARTIAL,
                    method,
                    "Hesaptaki gerçek ödemeyle doğrulanmadı",
                    when,
                )
            elif agree / len(obs) <= 1 - SIGN_AGREE:
                set_(
                    "funding_sign",
                    NONE,
                    method,
                    "İşaret prim yönünün tersine görünüyor; funding kullanılmaz",
                    when,
                )
            else:
                set_("funding_sign", UNVERIFIED, method + "; tutarsız", None, when)


# Where each runtime check is covered by tests.
TESTS = dict(
    market_discovery="test_capabilities.RowEvidenceTests",
    contract_metadata="test_capabilities.RowEvidenceTests",
    order_book="test_capabilities.BookEvidenceTests",
    bid_ask="test_capabilities.BookEvidenceTests",
    funding_history="test_capabilities.HistoryEvidenceTests",
    funding_interval="test_capabilities.IntervalTests",
    funding_sign="test_capabilities.SignTests",
    fees="test_capabilities.RowEvidenceTests",
)


def blockers(registry, strategy, venues):
    """Required capabilities missing on any of `venues` for `strategy`, as reasons."""
    out = []
    for v in venues:
        entry = registry.get(v)
        if entry is None:
            out.append("%s: yetenek kaydı yok" % v)
            continue
        for c in entry["strategies"].get(strategy, []):
            x = entry["caps"][c]
            out.append(
                "%s: %s %s"
                % (v, LABELS[c], "yok" if x["status"] == NONE else "doğrulanmadı")
            )
    return out
