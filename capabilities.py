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
    is a heuristic about the venue, not evidence of any payment; a venue whose signs
    run against the premium is marked inverted.

A venue status is necessary but never sufficient: it summarizes many markets, so a
venue verified on BTC says nothing about its ETH market. Every leg of a trade must
carry its own market evidence (`Registry.market_blockers`): a fresh row with contract
size, fee, schedule and rate; a periodic funding model; its own settled payments
matching its published interval; its own complete 30-day history; and its own
published rates agreeing in sign and size with the payments that settled after them.
Missing, too little, expired or contradictory evidence blocks the leg.

Paper strategies read eligibility from here: a pair whose leg lacks a required
capability or market evidence is kept as research only, with the reason, and is never
executed.
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
    funding_model="Funding modeli",
)

# What a paper strategy needs from each leg's venue. Both settle funding on every
# payment while the position is open, so both need the schedule, settled history and
# sign; both judge prices on traded (not oracle) history. Convergence needs the same
# funding evidence as the funding strategy: it holds both legs for days and pays the
# carry on every payment, and its entry, add and exit tests charge that carry from the
# signed rate on each leg's own schedule. Without the schedule, the sign or the
# settled history the carry cannot be bounded, so the price signal alone is shown as
# research and never executed. The order book is not in the list: every use reads it
# and refuses a stale, crossed or empty one, so it is checked where it is used (the
# registry shows the last read) and again right before an entry.
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

# --- funding models -----------------------------------------------------------------
# How a venue's funding becomes cash. The paper desk settles discrete payments: at each
# payment time a leg pays or earns rate x quantity x price on its own schedule. Only a
# PERIODIC venue fits that cash flow; a continuous accrual or an exchange-specific epoch
# scheme is not evaluated with the payment-gap rule and stays research until the desk
# implements its cash flow, and an unknown model stays research as well.
PERIODIC = "PERIODIC"
CONTINUOUS = "CONTINUOUS"
EPOCH = "EXCHANGE_SPECIFIC"
UNKNOWN = "UNKNOWN"
MODELS = (PERIODIC, CONTINUOUS, EPOCH, UNKNOWN)
MODEL_LABELS = {
    PERIODIC: "Dönemsel ödeme",
    CONTINUOUS: "Sürekli tahakkuk",
    EPOCH: "Borsaya özel dönem",
    UNKNOWN: "Bilinmiyor",
}
EXECUTABLE_MODELS = (PERIODIC,)
# The official docs could not be fetched from this environment: the conventions below
# are the connector's declaration, labelled as such, and every runtime check still
# applies to them.
DOCS_UNREAD = "Bağlayıcı beyanı; borsa belgesi bu ortamdan okunamadı"
LONG_PAYS = "Pozitif oran: long öder, short alır"


def _model(model, **kw):
    out = dict(
        model=model,
        label=MODEL_LABELS[model],
        settlement=None,
        rate=None,
        direction=None,
        price=None,
        history=None,
        cashflow=None,
        source=DOCS_UNREAD,
        note=None,
    )
    out.update(kw)
    return out


HOURLY = dict(
    settlement="Her saat başı ayrık ödeme",
    rate="Ödeme başına (saatlik) oran",
    direction=LONG_PAYS,
    history="Geçmiş: ödeme başına gerçekleşmiş oran",
    cashflow="Nakit = -yön x miktar x fiyat x ödeme oranı, her ödemede",
)
FUNDING_MODELS = dict(
    Hyperliquid=_model(PERIODIC, price="Oracle fiyatı", **HOURLY),
    LighterCore=_model(PERIODIC, price="Mark fiyatı", **HOURLY),
    Lighter=_model(
        PERIODIC,
        price="Mark fiyatı",
        note="Geçmiş kaydı ödeyen tarafı (direction) ayrıca yayımlıyor. Canlı oran "
        "son gerçekleşen ödemeden alınır; yayımlanan/gerçekleşen karşılaştırması onu "
        "bir sonraki ödemeyle karşılaştırır",
        **HOURLY,
    ),
    Extended=_model(PERIODIC, price="Mark fiyatı", **HOURLY),
    Paradex=_model(
        CONTINUOUS,
        settlement="Ayrık ödeme yok; funding pozisyona sürekli işler",
        rate="Yayımlanan oran bir dönem için; tahakkuk süreklidir",
        direction=LONG_PAYS,
        history="Geçmiş kayıtları ödeme değil oran örneği",
        cashflow="Masanın dönemsel ödeme modeliyle hesaplanamaz",
        note="Sürekli model uygulanana kadar yalnız araştırma",
    ),
    RISEx=_model(UNKNOWN, note="Funding modeli bu masada belgelenmedi"),
    TxFlow=_model(UNKNOWN, note="Funding / tahakkuk modeli doğrulanmadı"),
    Arcus=_model(UNKNOWN, note="Funding modeli bu masada belgelenmedi"),
    Variational=_model(UNKNOWN, note="Bağlayıcı yok"),
)


def funding_model(v):
    """The venue's declared funding model; an undeclared venue's is unknown."""
    return FUNDING_MODELS.get(v) or _model(UNKNOWN, note="Borsa beyanı yok")


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
INTERVAL_RETRY = 900  # an undecided market is re-checked this often
# Market evidence older than this no longer counts: the market is re-checked first.
EVIDENCE_EXPIRY = 24 * 3600
HISTORY_EXPIRY = 24 * 3600  # a history keeper record older than this proves nothing
# Published rate against the payment that settled after it, per market.
PUBLISHED_MIN = 3  # comparisons needed
PUBLISHED_KEEP = 12  # comparisons kept per market
PUBLISHED_WINDOW = 3 * 86400
PREDICTION_KEEP = 2 * 86400  # an observation never matched to a payment is dropped
SIGN_EPS = 1e-6  # |rate| per payment below this is too small to show a sign
RATIO_BAND = (0.25, 4.0)  # median |published / settled| outside it: unit mismatch

# Funding sign evidence, weakest to strongest. The desk has no account connection, so
# the last two are never available and the sign is never fully supported.
SIGN_LEVELS = dict(
    documented="Borsanın belgelediği işaret kuralı",
    heuristic="Prim yönüyle uyum (sezgisel; ödeme kanıtı değil)",
    published="Yayımlanan oran = sonra gerçekleşen ödeme (piyasa bazında)",
    settled_cash="Gerçek ödeme nakdi kaydı",
    account="Hesapta gerçekleşen funding",
)
NO_ACCOUNT = "Hesap bağlantısı yok; gerçek ödeme nakdi okunamıyor"


def median_gap(times):
    ts = sorted(set(times))
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    return statistics.median(gaps) if gaps else None


def market_key(v, a):
    return "%s|%s" % (v, a)


class Registry:
    """Evidence per venue and per market, and the statuses derived from it. `state` is
    plain JSON so it survives restarts; `evaluate` turns declarations plus evidence into
    the venue registry and `market_blockers` judges one market on its own evidence."""

    def __init__(self, state=None):
        state = state or {}
        self.books = state.get("books", {})
        self.signs = state.get("signs", {})
        self.intervals = state.get("intervals", {})
        # {market: [[payment time, sign agrees, |published / settled|], ...]}
        self.published = state.get("published", {})
        # {market: {payment bucket: [observed at, published rate, next payment]}}
        self.predictions = state.get("predictions", {})
        # {market: [keeper record time, complete]}
        self.market_history = state.get("market_history", {})
        self.rows = {}
        self.fee_sources = {}
        self.cursor = state.get("cursor", 0)
        # Each venue's premium-heuristic tally, recounted only after new observations:
        # the gate judges every ranked pair's markets each round.
        self.heuristic_memo = {}

    def state(self):
        return dict(
            books=self.books,
            signs=self.signs,
            intervals=self.intervals,
            published=self.published,
            predictions=self.predictions,
            market_history=self.market_history,
            cursor=self.cursor,
        )

    # --- evidence -----------------------------------------------------------------
    def observe_rows(self, rows, at):
        """Per venue counts from the fresh perp rows of this scan, and one funding-sign
        observation per market and hour where the mark is clearly off the index."""
        by = {}
        self.heuristic_memo = {}
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
            self.observe_prediction(r, at)
        self.rows = by

    def observe_prediction(self, r, at):
        """Keep the latest published rate before each payment of a periodic market, to
        be compared with the payment once the history keeper has recorded it."""
        iv, rate = r.get("interval"), r.get("raw_rate")
        if not iv or rate is None:
            return
        if funding_model(r["venue"])["model"] not in EXECUTABLE_MODELS:
            return
        seen = r.get("received") or at
        nxt = r.get("next")
        bucket = str(int(nxt) if nxt else int(seen // iv + 1) * int(iv))
        obs = self.predictions.setdefault(market_key(r["venue"], r["asset"]), {})
        obs[bucket] = [round(seen), rate, nxt]
        for k in [k for k, x in obs.items() if at - x[0] > PREDICTION_KEEP]:
            del obs[k]
        for k in sorted(obs, key=lambda k: obs[k][0])[:-6]:
            del obs[k]

    def observe_sign(self, v, r, at):
        mark, index, rate = r.get("mark"), r.get("index"), r.get("raw_rate")
        if not mark or not index or rate is None or rate == 0:
            return
        premium = mark / index - 1
        if abs(premium) < SIGN_PREMIUM:
            return
        key = "%s|%d" % (r["asset"], int(at) // 3600)
        self.heuristic_memo.pop(v, None)
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

    def observe_history(self, states, at=None):
        """`states`: {(venue, asset, kind): keeper market state}. Each market keeps the
        time of its keeper record and whether its 30-day history is complete; a record
        survives restarts until it expires."""
        for (v, a, *_), s in states.items():
            when = s.get("ts") or at
            if when is not None:
                self.market_history[market_key(v, a)] = [when, bool(s.get("complete"))]

    def history_counts(self, at):
        """{venue: dict(markets, complete)} over the unexpired market records."""
        by = {}
        for k, (when, done) in self.market_history.items():
            if at - when > HISTORY_EXPIRY:
                continue
            x = by.setdefault(k.split("|")[0], dict(markets=0, complete=0))
            x["markets"] += 1
            x["complete"] += bool(done)
        return by

    def check_market(self, v, a, interval, stored, at, start):
        """Compare one market's published interval with the median gap of its stored
        settled payments (None when there are too few to tell), and its published rates
        with the payments that settled after them. A venue whose funding model is not
        periodic has no payment gap to measure and is left unchecked."""
        if funding_model(v)["model"] not in EXECUTABLE_MODELS:
            return None
        key = market_key(v, a)
        try:
            paid = [(x["ts"], x.get("rate")) for x in stored(v, a, start)]
        except Exception:
            return None
        times = [t for t, _ in paid]
        gap = median_gap(times) if len(times) >= INTERVAL_MIN_PAYMENTS else None
        match = (
            None
            if gap is None
            else abs(gap - interval) <= INTERVAL_TOLERANCE * interval
        )
        self.intervals[key] = dict(
            at=at, published=interval, gap=gap, match=match, n=len(times)
        )
        self.compare_published(key, interval, paid, at)
        return match

    def compare_published(self, key, interval, paid, at):
        """Match each kept observation to the first payment after it (within one
        interval, and at its announced time when it had one) and record whether the
        published rate had the payment's sign and size."""
        obs = self.predictions.get(key)
        paid = sorted((t, r) for t, r in paid if r is not None)
        if not obs or not paid:
            return
        last = paid[-1][0]
        slack = INTERVAL_TOLERANCE * interval + 60
        samples = self.published.setdefault(key, [])
        done = {s[0] for s in samples}
        for bucket, (seen, rate, nxt) in sorted(obs.items(), key=lambda x: x[1][0]):
            if seen >= last:
                continue  # its payment is not recorded yet
            del obs[bucket]
            settled = next(((t, r) for t, r in paid if t > seen), None)
            if settled is None or settled[0] - seen > interval + slack:
                continue
            if nxt and abs(settled[0] - nxt) > slack:
                continue
            t, r = settled
            if t in done or abs(r) < SIGN_EPS or abs(rate) < SIGN_EPS:
                continue
            samples.append([t, (r > 0) == (rate > 0), round(abs(rate / r), 4)])
            done.add(t)
        samples.sort()
        del samples[:-PUBLISHED_KEEP]
        if not obs:
            self.predictions.pop(key, None)

    def published_status(self, v, a, at):
        """(status, n, agreeing, median ratio, latest): consistent, insufficient,
        inconsistent, inverted or unit_mismatch."""
        xs = [
            s
            for s in self.published.get(market_key(v, a), [])
            if at - s[0] <= PUBLISHED_WINDOW
        ]
        n, agree = len(xs), sum(bool(s[1]) for s in xs)
        ratio = statistics.median(s[2] for s in xs) if xs else None
        latest = max((s[0] for s in xs), default=None)
        if n < PUBLISHED_MIN:
            status = "insufficient"
        elif agree / n <= 1 - SIGN_AGREE:
            status = "inverted"
        elif agree / n < SIGN_AGREE:
            status = "inconsistent"
        elif not RATIO_BAND[0] <= ratio <= RATIO_BAND[1]:
            status = "unit_mismatch"
        else:
            status = "consistent"
        return status, n, agree, ratio, latest

    def due(self, v, a, at):
        """A market is re-checked every INTERVAL_RECHECK, every INTERVAL_RETRY while
        its interval or published-rate evidence is still undecided."""
        x = self.intervals.get(market_key(v, a))
        if not x:
            return True
        undecided = (
            x["gap"] is None or self.published_status(v, a, at)[0] == "insufficient"
        )
        return at - x["at"] > (INTERVAL_RETRY if undecided else INTERVAL_RECHECK)

    def check_intervals(self, rows, stored, at, start):
        """Every venue first gets INTERVAL_BOOTSTRAP decided markets (tried each round
        until they have enough payments); then a rotating sample is re-checked. Only
        periodic venues have a payment gap to check."""
        perps = [
            r
            for r in rows
            if r.get("kind", "perp") == "perp"
            and r.get("interval")
            and funding_model(r["venue"])["model"] in EXECUTABLE_MODELS
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
        """True only when this market's own settled payments were counted and match its
        published interval. No record, too few payments or a mismatch are all False."""
        x = self.intervals.get(market_key(v, a))
        return bool(x and x.get("gap") is not None and x.get("match") is True)

    # --- market evidence ----------------------------------------------------------
    def market_evidence(self, v, a, row, at):
        """Everything known about one market's eligibility, with source, time, sample
        size and the reasons it is blocked (`blockers`, empty when it may execute)."""
        key = market_key(v, a)
        model = funding_model(v)
        why = []
        if model["model"] not in EXECUTABLE_MODELS:
            why.append(
                "funding modeli %s; masanın dönemsel ödeme hesabına uymuyor"
                % model["label"].lower()
                if model["model"] != UNKNOWN
                else "funding modeli bilinmiyor"
            )
        # This scan's row: the market is listed, fresh and fully specified.
        r = dict(ok=False, received=None)
        if row is None:
            why.append("piyasa bu taramada yok")
        else:
            r.update(
                received=row.get("received"),
                fee=row.get("fee"),
                contract_size=row.get("contract_size"),
                interval=row.get("interval"),
                raw_rate=row.get("raw_rate"),
            )
            missing = len(why)
            if at - (row.get("received") or 0) > FRESH:
                why.append("piyasa verisi eski")
            if not row.get("contract_size"):
                why.append("kontrat büyüklüğü bilinmiyor")
            if row.get("fee") is None:
                why.append("ücret bilinmiyor")
            if not row.get("interval"):
                why.append("funding aralığı yayımlanmıyor")
            if row.get("raw_rate") is None:
                why.append("funding oranı yok")
            r["ok"] = len(why) == missing
        # The market's own settled payments against its published interval.
        x = self.intervals.get(key)
        iv = dict(status="missing", at=None, n=0, gap=None, published=None)
        if x:
            iv.update(at=x["at"], n=x.get("n"), gap=x["gap"], published=x["published"])
        if model["model"] not in EXECUTABLE_MODELS:
            iv["status"] = "not_applicable"
        elif not x:
            iv["status"] = "missing"
            why.append("funding aralığı bu piyasada doğrulanmadı")
        elif x["gap"] is None:
            iv["status"] = "insufficient"
            why.append(
                "gerçekleşmiş ödeme yetersiz · %s/%d"
                % (x.get("n", "?"), INTERVAL_MIN_PAYMENTS)
            )
        elif x["match"] is False:
            iv["status"] = "mismatch"
            why.append("funding aralığı ödemelerle uyuşmuyor")
        elif row is not None and row.get("interval") != x["published"]:
            iv["status"] = "changed"
            why.append("yayımlanan funding aralığı değişti; yeniden doğrulanmalı")
        elif at - x["at"] > EVIDENCE_EXPIRY:
            iv["status"] = "expired"
            why.append("funding aralığı kanıtının süresi doldu")
        else:
            iv["status"] = "verified"
        # The market's own 30-day candles and funding in the history keeper.
        h = self.market_history.get(key)
        hist = dict(status="missing", at=h[0] if h else None)
        if not h:
            why.append("30 günlük geçmiş kaydı yok")
        elif at - h[0] > HISTORY_EXPIRY:
            hist["status"] = "expired"
            why.append("geçmiş kaydının süresi doldu")
        elif not h[1]:
            hist["status"] = "incomplete"
            why.append("30 günlük geçmiş tamamlanmadı")
        else:
            hist["status"] = "complete"
        # Funding sign, by evidence level.
        heuristic, n_obs, agree, last_obs = self.sign_heuristic(v)
        status, n, ok, ratio, latest = self.published_status(v, a, at)
        sign = dict(
            documented=dict(
                status="declared" if model["direction"] else "missing",
                convention=model["direction"],
                source=model["source"],
            ),
            heuristic=dict(
                status=heuristic,
                scope="borsa",
                n=n_obs,
                agree=agree,
                at=last_obs,
                note=SIGN_LEVELS["heuristic"],
            ),
            published=dict(
                status=status, scope="piyasa", n=n, agree=ok, ratio=ratio, at=latest
            ),
            settled_cash=dict(status="unavailable", note=NO_ACCOUNT),
            account=dict(status="unavailable", note=NO_ACCOUNT),
        )
        if not model["direction"]:
            why.append("funding işaret kuralı belgelenmedi")
        if heuristic == "inverted":
            why.append("funding işareti prim yönünün tersine görünüyor")
        if status == "insufficient":
            why.append(
                "yayımlanan oran ile gerçekleşen ödeme karşılaştırması yetersiz · %d/%d"
                % (n, PUBLISHED_MIN)
            )
        elif status in ("inverted", "inconsistent"):
            why.append(
                "funding işareti çelişkili · yayımlanan oran %d/%d ödemede aynı yönde"
                % (ok, n)
            )
        elif status == "unit_mismatch":
            why.append(
                "yayımlanan oran ödemelerle aynı birimde değil · oran x%.2f" % ratio
            )
        confidence = "yok" if why else "orta" if heuristic == "consistent" else "düşük"
        return dict(
            venue=v,
            asset=a,
            at=at,
            model=dict(
                model=model["model"],
                label=model["label"],
                source=model["source"],
                note=model["note"],
            ),
            row=r,
            interval=iv,
            history=hist,
            sign=sign,
            confidence=confidence,
            blockers=["%s: %s" % (v, w) for w in why],
        )

    def sign_heuristic(self, v):
        """(status, observations, agreeing, latest) of the venue's premium heuristic:
        consistent, inverted, inconsistent or insufficient."""
        obs = self.signs.get(v) or {}
        hit = self.heuristic_memo.get(v)
        if hit and hit[0] is obs and hit[1] == len(obs):
            return hit[2]
        xs = list(obs.values())
        agree = sum(bool(o[1]) for o in xs)
        if len(xs) < SIGN_MIN:
            status = "insufficient"
        elif agree / len(xs) <= 1 - SIGN_AGREE:
            status = "inverted"
        elif agree / len(xs) >= SIGN_AGREE:
            status = "consistent"
        else:
            status = "inconsistent"
        out = (status, len(xs), agree, max((o[0] for o in xs), default=None))
        self.heuristic_memo[v] = (obs, len(obs), out)
        return out

    def market_blockers(self, v, a, row, at):
        """Why this one market may not execute; empty only when its own evidence is
        complete, fresh and consistent. A venue status never stands in for it."""
        return self.market_evidence(v, a, row, at)["blockers"]

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
            model = funding_model(v)
            # A venue whose funding model the desk cannot settle is research only,
            # whatever its other capabilities show.
            bad_model = (
                ["funding_model"] if model["model"] not in EXECUTABLE_MODELS else []
            )
            out[v] = dict(
                venue=v,
                connector=decl["connector"],
                declared=v in DECLARED,
                perp_dex=v in PERP_DEXES,
                funding_model=model,
                caps=caps,
                strategies={
                    s: bad_model + [c for c in need if caps[c]["status"] not in USABLE]
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
        h = self.history_counts(at).get(v)
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
            x
            for k, x in self.intervals.items()
            if k.split("|")[0] == v and x["gap"] and at - x["at"] <= EVIDENCE_EXPIRY
        ]
        model = funding_model(v)
        if model["model"] not in EXECUTABLE_MODELS:
            set_(
                "funding_interval",
                UNVERIFIED,
                "Funding modeli: %s; ödeme aralığı kuralı uygulanmaz" % model["label"],
                "Model masada uygulanmadı; yalnız araştırma",
            )
        elif checks:
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
        # Funding sign: the venue status rests on the premium heuristic, which proves
        # no payment; the per-market published-rate comparison is listed beside it.
        markets = [k for k in self.published if k.split("|")[0] == v]
        consistent = sum(
            self.published_status(v, k.split("|", 1)[1], at)[0] == "consistent"
            for k in markets
        )
        caps["funding_sign"]["evidence"] = dict(
            documented=dict(convention=model["direction"], source=model["source"]),
            published="%d/%d piyasada yayımlanan oran ödemelerle tutarlı"
            % (consistent, len(markets)),
            settled_cash=NO_ACCOUNT,
            account=NO_ACCOUNT,
        )
        obs = list((self.signs.get(v) or {}).values())
        if obs:
            agree = sum(bool(a) for t, a in obs)
            method = (
                "Sezgisel, ödeme kanıtı değil: prim yönüyle uyum %d/%d gözlem "
                "(|mark/endeks-1| ≥ %.0f bp)" % (agree, len(obs), SIGN_PREMIUM * 1e4)
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
    funding_sign="test_capabilities.SignTests, PublishedSignTests",
    fees="test_capabilities.RowEvidenceTests",
)
# Where the per-market evidence and the funding models are covered by tests.
MARKET_TESTS = (
    "test_capabilities.FundingModelTests",
    "test_capabilities.PublishedSignTests",
    "test_capabilities.MarketEvidenceTests",
    "test_capabilities.EveryGapStaysResearchTests",
    "test_wide_paper.CapabilityGateTests",
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
            if c == "funding_model":
                out.append(
                    "%s: Funding modeli %s; yalnız araştırma"
                    % (v, entry["funding_model"]["label"].lower())
                )
                continue
            x = entry["caps"][c]
            out.append(
                "%s: %s %s"
                % (v, LABELS[c], "yok" if x["status"] == NONE else "doğrulanmadı")
            )
    return out


def pair_blockers(reg, registry, strategy, c, rows, at):
    """Every reason the pair `c` may not execute: what its legs' venues lack for the
    strategy, then each leg's own market evidence (`rows`: {(venue, asset): row}).
    Both must be empty; a verified venue never stands in for a market."""
    out = blockers(registry, strategy, (c["long"], c["short"]))
    for v in (c["long"], c["short"]):
        out += reg.market_blockers(v, c["asset"], rows.get((v, c["asset"])), at)
    return list(dict.fromkeys(out))
