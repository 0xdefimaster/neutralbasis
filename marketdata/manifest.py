"""Capability manifests: what each venue's adapter code reads, from where, and how.

A manifest only lists sources the repository already reads; every endpoint named
here appears in the module it cites (test_marketdata checks this), so nothing is
invented. A capability with no source is NOT_IMPLEMENTED: the venue may publish it,
the project does not read it. Availability is then decided from recorded evidence
(evaluate):

  VERIFIED         live public-API records in the recorder folder prove the
                   recorder path for this capability (a MOCK folder never does);
  IMPLEMENTED      adapter code reads it, no live evidence in this folder yet
                   (paper-engine and CCXT paths are not checked by the recorder);
  NOT_IMPLEMENTED  no adapter reads it;
  UNSUPPORTED      the venue does not publish it, with the evidence that says so.
                   Nothing is marked this way without that evidence.

Health is separate from availability: the venue's request history in the recorder
(OK, DEGRADED, DOWN, NO_DATA, NO_COLLECTOR) and, per capability, STALE when the
newest evidence is older than three update periods.
"""

from dataclasses import asdict, dataclass

from .venues import VENUES

CAPABILITIES = (
    "market_discovery",
    "ticker",
    "bid_ask",
    "mark_price",
    "index_price",
    "orderbook_l2",
    "trades",
    "ohlcv",
    "funding_current",
    "funding_predicted",
    "funding_history",
    "funding_settlements",
    "open_interest",
    "volume",
    "instrument_metadata",
    "trading_constraints",
)
LABELS = dict(
    market_discovery="Piyasa keşfi",
    ticker="Ticker",
    bid_ask="Alış/satış",
    mark_price="Mark fiyatı",
    index_price="Endeks fiyatı",
    orderbook_l2="L2 emir defteri",
    trades="İşlemler",
    ohlcv="OHLCV",
    funding_current="Anlık funding",
    funding_predicted="Tahmini funding",
    funding_history="Funding geçmişi",
    funding_settlements="Funding ödemeleri",
    open_interest="Açık pozisyon",
    volume="Hacim",
    instrument_metadata="Enstrüman bilgisi",
    trading_constraints="İşlem kısıtları",
)

VERIFIED = "VERIFIED"
IMPLEMENTED = "IMPLEMENTED"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
UNSUPPORTED = "UNSUPPORTED"
AVAILABILITY = (VERIFIED, IMPLEMENTED, NOT_IMPLEMENTED, UNSUPPORTED)

# Collection paths, and the module each one's endpoints are written in.
RECORDER = "recorder"
PAPER_FEED = "paper_feed"
CCXT = "ccxt"
RFQ_WS = "rfq_ws"
PATHS = dict(
    recorder="tools/phase1_recorder (bağımsız kanıt kaydedicisi)",
    paper_feed="risk_scan.FiveFeed / bot.Feed (paper motoru)",
    ccxt="wide_markets.WideMarkets üzerinden CCXT (paper motoru tarayıcısı)",
    rfq_ws="extended_rfq.ExtendedRfq (paper motoru)",
)
CCXT_LIMIT = (
    "CCXT bağdaştırıcısının `has` bayrağına bağlı; borsa başına desteği çalışma "
    "anında belirlenir, burada doğrulanmadı"
)


@dataclass(frozen=True)
class Source:
    path: str
    module: str
    endpoint: str
    transport: str = "REST"
    frequency_seconds: int = None
    timestamp: str = None
    rate_limit: str = None
    limitations: str = None

    def as_dict(self):
        return asdict(self)


def _rec(venue_module, endpoint, freq, timestamp, limitations=None, rate=None):
    return Source(
        RECORDER,
        "tools/phase1_recorder/clients/%s.py" % venue_module,
        endpoint,
        "REST",
        freq,
        timestamp,
        rate
        or "Kaydedici: borsa başına asgari aralık (min_spacing), 429'da Retry-After",
        limitations,
    )


def _feed(module, endpoint, freq, timestamp, limitations=None):
    return Source(
        PAPER_FEED,
        module,
        endpoint,
        "REST",
        freq,
        timestamp,
        "Paper beslemesi: yanıt önbelleği (max_age); açık hız sınırı yok",
        limitations,
    )


def _ccxt(method, freq=60, timestamp=None, limitations=None):
    return Source(
        CCXT,
        "wide_markets.py",
        method,
        "REST",
        freq,
        timestamp or "CCXT normalize zaman damgası; borsa alanı CCXT'ye bağlı",
        "CCXT enableRateLimit; tarama turları arasında 60 sn",
        limitations or CCXT_LIMIT,
    )


# Recorder task periods (tools/phase1_recorder/config.py defaults).
META, FUND, SETTLE, CANDLE, BOOK = 6 * 3600, 60, 900, 1800, 60
RECV = "Yanıtta borsa zaman damgası yok; yerel alım zamanı kullanılır"

HL = "hyperliquid"
HL_META = _rec(HL, "metaAndAssetCtxs", META, RECV)
HL_CTX = _rec(HL, "metaAndAssetCtxs", FUND, RECV)
HL_HIST = _rec(
    HL,
    "fundingHistory",
    SETTLE,
    "time: ödeme anı, ms",
    "Piyasa düzeyinde ödeme oranı; hesap nakdi değil; sayfa başı 500 satır",
)
HL_BOOK = _rec(HL, "l2Book", BOOK, "time: borsa zamanı, ms")
HL_CANDLE = _rec(HL, "candleSnapshot", CANDLE, "t: mum açılışı, ms")

LT = "lighter"
LT_META = _rec(LT, "orderBookDetails", META, RECV)
LT_CTX = _rec(LT, "orderBookDetails", FUND, RECV)
LT_RATES = _rec(
    LT,
    "funding-rates",
    FUND,
    RECV,
    "Oran birimi belgelenmedi (UNKNOWN); yanıt başka borsaların oranlarını da içerir",
)
LT_HIST = _rec(
    LT,
    "fundings",
    SETTLE,
    "timestamp: ödeme anı, sn",
    "Yüzde oran + ayrı direction alanı; okuma kuralı DECLARED",
)
LT_BOOK = _rec(LT, "orderBookOrders", BOOK, RECV + " (defterde zaman damgası yok)")
LT_CANDLE = _rec(LT, "candles", CANDLE, "t: mum açılışı, ms")

EX = "extended"
EX_META = _rec(EX, "info/markets", META, RECV)
EX_CTX = _rec(EX, "info/markets", FUND, RECV + "; nextFundingRate alanı sonraki ödeme")
EX_HIST = _rec(
    EX,
    "info/<market>/funding",
    SETTLE,
    "T: ödeme anı, ms",
    "Ödeyen taraf alanı yok (UNKNOWN); yanıt yeniden eskiye sıralı",
)
EX_BOOK = _rec(EX, "info/markets/<market>/orderbook", BOOK, RECV)
EX_CANDLE = _rec(
    EX, "info/candles/<market>/trades", CANDLE, "T: mum açılışı, ms (yeniden eskiye)"
)

TX = "risk_scan.py"
TX_META = _feed(TX, "perpMeta", 60, RECV)
TX_CTX = _feed(
    TX,
    "activeAssetCtx",
    60,
    "nodeCtx.blockTime; 90 sn'den eskiyse reddedilir",
    "Oran yüzde; aralık prev/nextFundingTime farkından",
)
TX_BOOK = _feed(TX, "l2Book", 30, RECV)
TX_CANDLE = _feed(TX, "candleSnapshot", 1800, "Mum açılışı")

AR = "risk_scan.py"
AR_MKT = _feed(AR, "https://api.arcus.xyz/v1/markets", 60, RECV)
AR_HIST = _feed(AR, "https://api.arcus.xyz/v1/fundingRates?", 1800, "Ödeme anı")
AR_BOOK = _feed(AR, "https://api.arcus.xyz/v1/l2OrderBook/", 30, RECV)
AR_CANDLE = _feed(
    AR,
    "https://api.arcus.xyz/v1/candles?",
    1800,
    "Mum açılışı",
    "Oracle fiyat mumu; işlem fiyatı değil (price_type=oracle)",
)

RX = "risk_scan.py"
RX_MKT = _feed(RX, "https://api.rise.trade/v1/markets", 60, RECV)
RX_ID = _feed(RX, "https://api.rise.trade/v1/markets/id/", 1800, "Borsa alanı")
RX_BOOK = _feed(RX, "https://api.rise.trade/v1/orderbook?market_id=", 30, RECV)

RH_FEED = _feed(
    "bot.py",
    "orderBookDetails",
    60,
    RECV,
    "Paper beslemesi RH host'unu risk_scan.RH ile kullanır",
)

MANIFESTS = dict(
    hyperliquid=dict(
        note="Paper motoru CCXT ile, kaydedici POST /info ile okur.",
        caps=dict(
            market_discovery=[HL_META, _ccxt("fetch_tickers")],
            ticker=[_ccxt("fetch_tickers")],
            bid_ask=[HL_BOOK, _ccxt("fetch_order_book")],
            mark_price=[HL_CTX, _ccxt("fetch_mark_prices")],
            index_price=[],
            orderbook_l2=[HL_BOOK, _ccxt("fetch_order_book")],
            trades=[],
            ohlcv=[HL_CANDLE, _ccxt("fetch_ohlcv", 1800)],
            funding_current=[HL_CTX, _ccxt("fetch_funding_rates")],
            funding_predicted=[],
            funding_history=[HL_HIST, _ccxt("fetch_funding_rate_history", 1800)],
            funding_settlements=[HL_HIST],
            open_interest=[_ccxt("fetch_open_interests")],
            volume=[_ccxt("fetch_tickers")],
            instrument_metadata=[HL_META],
            trading_constraints=[HL_META],
        ),
        gaps=dict(
            index_price="metaAndAssetCtxs oraclePx kaydediliyor (oracle_price); "
            "endeks olduğu belgelenmedi",
            trading_constraints="Fiyat adımı yanıtta yok; miktar adımı szDecimals'tan",
        ),
    ),
    paradex=dict(
        note="Yalnız CCXT tarayıcısı. Funding modeli CONTINUOUS (DECLARED): ayrık "
        "ödeme yok, paper işlem açılmaz.",
        caps=dict(
            market_discovery=[_ccxt("fetch_tickers")],
            ticker=[_ccxt("fetch_tickers")],
            bid_ask=[_ccxt("fetch_order_book")],
            mark_price=[_ccxt("fetch_mark_prices")],
            index_price=[],
            orderbook_l2=[_ccxt("fetch_order_book")],
            trades=[],
            ohlcv=[_ccxt("fetch_ohlcv", 1800)],
            funding_current=[_ccxt("fetch_funding_rates")],
            funding_predicted=[],
            funding_history=[
                _ccxt(
                    "fetch_funding_rate_history",
                    1800,
                    limitations="Oran örnekleri, ödeme değil (sürekli model). "
                    + CCXT_LIMIT,
                )
            ],
            funding_settlements=[],
            open_interest=[_ccxt("fetch_open_interests")],
            volume=[_ccxt("fetch_tickers")],
            instrument_metadata=[],
            trading_constraints=[],
        ),
        gaps=dict(
            funding_settlements="Sürekli tahakkuk (DECLARED); ayrık ödeme kaydı okunmuyor",
            instrument_metadata="CCXT piyasa bilgisi paper satırına alınıyor, ayrı "
            "kaynak olarak doğrulanmadı",
        ),
    ),
    txflow=dict(
        note="Paper beslemesi POST https://rpc.txflow.com/info ile okur.",
        caps=dict(
            market_discovery=[TX_META],
            ticker=[],
            bid_ask=[TX_BOOK],
            mark_price=[TX_CTX],
            index_price=[],
            orderbook_l2=[TX_BOOK],
            trades=[],
            ohlcv=[TX_CANDLE],
            funding_current=[TX_CTX],
            funding_predicted=[],
            funding_history=[],
            funding_settlements=[],
            open_interest=[],
            volume=[],
            instrument_metadata=[TX_META],
            trading_constraints=[TX_META],
        ),
        gaps=dict(
            funding_history="Public gerçekleşmiş funding geçmişi doğrulanmadı; kod "
            "bilerek hata verir (risk_scan.FiveFeed.funding_history)",
            index_price="activeAssetCtx oraclePx okunuyor; endeks olduğu belgelenmedi",
        ),
    ),
    arcus=dict(
        note="Paper beslemesi. Fiyat geçmişi oracle; fiyat uyumu ölçülemez.",
        caps=dict(
            market_discovery=[AR_MKT],
            ticker=[],
            bid_ask=[AR_BOOK],
            mark_price=[AR_MKT],
            index_price=[],
            orderbook_l2=[AR_BOOK],
            trades=[],
            ohlcv=[AR_CANDLE],
            funding_current=[AR_MKT],
            funding_predicted=[],
            funding_history=[AR_HIST],
            funding_settlements=[],
            open_interest=[],
            volume=[],
            instrument_metadata=[AR_MKT],
            trading_constraints=[AR_MKT],
        ),
        gaps=dict(
            index_price="markets oraclePrice okunuyor; endeks olduğu belgelenmedi",
            funding_settlements="fundingRates ödeme mi oran örneği mi, doğrulanmadı",
        ),
    ),
    extended=dict(
        note="Paper beslemesi ve kaydedici REST; RFQ piyasalarının gerçek defteri "
        "WebSocket ile (extended_rfq).",
        caps=dict(
            market_discovery=[EX_META],
            ticker=[],
            bid_ask=[EX_BOOK],
            mark_price=[EX_CTX],
            index_price=[EX_CTX],
            orderbook_l2=[
                EX_BOOK,
                Source(
                    RFQ_WS,
                    "extended_rfq.py",
                    "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks/rfq/",
                    "WebSocket",
                    0,
                    "Mesajda borsa zamanı + yerel alım zamanı + sıra numarası",
                    "Akış; sıra boşluğunda defter düşürülür, SNAPSHOT beklenir",
                    "Yalnız RFQ piyasaları; gösterge defterden ayrı tutulur",
                ),
            ],
            trades=[],
            ohlcv=[EX_CANDLE],
            funding_current=[EX_CTX],
            funding_predicted=[],
            funding_history=[EX_HIST],
            funding_settlements=[EX_HIST],
            open_interest=[],
            volume=[],
            instrument_metadata=[EX_META],
            trading_constraints=[EX_META],
        ),
        gaps={},
    ),
    variational=dict(
        note="Yalnız keşif: kaydedici metadata/stats ham yanıtını ve şemasını saklar; "
        "hiçbir alan yorumlanmaz.",
        caps={c: [] for c in CAPABILITIES},
        gaps=dict(
            market_discovery="metadata/stats `listings` içeriyor; alan anlamı "
            "doğrulanmadan piyasa keşfi sayılmaz"
        ),
    ),
    risex=dict(
        note="Paper beslemesi.",
        caps=dict(
            market_discovery=[RX_MKT],
            ticker=[],
            bid_ask=[RX_BOOK],
            mark_price=[RX_MKT],
            index_price=[RX_MKT],
            orderbook_l2=[RX_BOOK],
            trades=[],
            ohlcv=[RX_ID],
            funding_current=[RX_MKT],
            funding_predicted=[],
            funding_history=[RX_ID],
            funding_settlements=[],
            open_interest=[],
            volume=[],
            instrument_metadata=[RX_MKT],
            trading_constraints=[RX_MKT],
        ),
        gaps=dict(
            funding_settlements="Funding geçmişi ödeme mi oran örneği mi, doğrulanmadı"
        ),
    ),
    lighter_core=dict(
        note="Paper motoru CCXT `lighter` ile, kaydedici Core host'unu doğrudan okur.",
        caps=dict(
            market_discovery=[LT_META, _ccxt("fetch_tickers")],
            ticker=[_ccxt("fetch_tickers")],
            bid_ask=[LT_BOOK, _ccxt("fetch_order_book")],
            mark_price=[LT_CTX],
            index_price=[LT_CTX],
            orderbook_l2=[LT_BOOK, _ccxt("fetch_order_book")],
            trades=[],
            ohlcv=[LT_CANDLE, _ccxt("fetch_ohlcv", 1800)],
            funding_current=[LT_RATES, _ccxt("fetch_funding_rates")],
            funding_predicted=[],
            funding_history=[LT_HIST, _ccxt("fetch_funding_rate_history", 1800)],
            funding_settlements=[LT_HIST],
            open_interest=[_ccxt("fetch_open_interests")],
            volume=[_ccxt("fetch_tickers")],
            instrument_metadata=[LT_META],
            trading_constraints=[LT_META],
        ),
        gaps={},
    ),
    lighter_rh=dict(
        note="Robinhood dağıtımı (api.rh.lighter.xyz). Paper beslemesi ve kaydedici.",
        caps=dict(
            market_discovery=[LT_META, RH_FEED],
            ticker=[],
            bid_ask=[LT_BOOK],
            mark_price=[LT_CTX],
            index_price=[LT_CTX],
            orderbook_l2=[LT_BOOK],
            trades=[],
            ohlcv=[LT_CANDLE],
            funding_current=[LT_RATES],
            funding_predicted=[],
            funding_history=[LT_HIST],
            funding_settlements=[LT_HIST],
            open_interest=[],
            volume=[],
            instrument_metadata=[LT_META],
            trading_constraints=[LT_META],
        ),
        gaps=dict(
            funding_current="Paper beslemesi canlı oranı son gerçekleşen ödemeden alır"
        ),
    ),
)
assert set(MANIFESTS) == set(VENUES)
assert all(set(m["caps"]) == set(CAPABILITIES) for m in MANIFESTS.values())

# Venues whose recorder records count as evidence, by recorder venue name.
RECORDED = dict(
    hyperliquid="Hyperliquid",
    lighter_core="LighterCore",
    lighter_rh="Lighter",
    extended="Extended",
    variational="Variational",
)
RECORDED_IDS = {v: k for k, v in RECORDED.items()}
HEALTH_WINDOW = 600  # a venue with no successful request for this long is DOWN
STALE_PERIODS = 3


def venue_health(api, now):
    """OK / DEGRADED / DOWN / NO_DATA from the recorder's request history."""
    if not api or not api.get("requests"):
        return "NO_DATA"
    last_ok = api.get("last_ok")
    if last_ok is None or now - last_ok > HEALTH_WINDOW:
        return "DOWN"
    if api.get("errors_last_hour"):
        return "DEGRADED"
    return "OK"


def evaluate(evidence, now):
    """Per venue: health, and per capability its availability, sources and, from
    live recorder evidence only, last success and freshness."""
    live = evidence.live
    out = {}
    for vid, m in MANIFESTS.items():
        rec_name = RECORDED.get(vid)
        api = evidence.api.get(rec_name) if rec_name else None
        caps = {}
        for cap in CAPABILITIES:
            sources = m["caps"][cap]
            seen = evidence.capability(rec_name, cap) if rec_name else None
            on_recorder = [s for s in sources if s.path == RECORDER]
            if not sources:
                availability = NOT_IMPLEMENTED
            elif on_recorder and seen and live:
                availability = VERIFIED
            else:
                availability = IMPLEMENTED
            fresh = None
            if availability == VERIFIED:
                period = max(s.frequency_seconds or 0 for s in on_recorder)
                fresh = (
                    "STALE"
                    if period and now - seen["last_at"] > STALE_PERIODS * period
                    else "FRESH"
                )
            caps[cap] = dict(
                availability=availability,
                sources=[s.as_dict() for s in sources],
                verified_path=RECORDER if availability == VERIFIED else None,
                last_success=(
                    seen["last_at"] if seen and availability == VERIFIED else None
                ),
                records=seen["count"] if seen and availability == VERIFIED else 0,
                freshness=fresh,
                mock_evidence=bool(seen) and not live,
                gap=m["gaps"].get(cap),
            )
        out[vid] = dict(
            label=VENUES[vid]["label"],
            note=m["note"],
            health=venue_health(api, now) if rec_name else "NO_COLLECTOR",
            api=api,
            capabilities=caps,
        )
    return out
