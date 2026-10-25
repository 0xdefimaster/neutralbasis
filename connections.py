"""Read-only provider connectivity and Windows-user encrypted optional credentials."""

import base64, ctypes, json, os, time, urllib.request, urllib.error

CATALOG = {
    "Lighter": dict(
        url="https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails",
        docs="https://apidocs.lighter.xyz/reference/orderbookdetails",
        paper=True,
        auth="Anahtarsız piyasa verisi",
        note="Canlı fiyat, geçmiş mum/funding ve defter paper motorunda bağlı.",
    ),
    "Extended": dict(
        url="https://api.starknet.extended.exchange/api/v1/info/markets",
        docs="https://api.docs.extended.exchange/",
        paper=True,
        auth="Piyasa verisi anahtarsız; hesap ücreti için isteğe bağlı salt okunur API anahtarı",
        note="Canlı fiyat, geçmiş mum/funding ve defter paper motorunda bağlı.",
    ),
    "TxFlow": dict(
        url="https://rpc.txflow.com/info",
        docs="https://docs.txflow.com/api/",
        paper=False,
        auth="Salt okunur /info sorguları anahtarsız",
        note="Metadata erişimi test edilir. Funding takvimi, geçmiş ve yürütme adaptörü tamamlanmadı.",
    ),
    "RISEx": dict(
        url="https://api.rise.trade/v1/markets",
        docs="https://developer.rise.trade/reference/marketservice_getmarkets",
        paper=False,
        auth="Public piyasa erişimi testi",
        note="Mainnet piyasa erişimi test edilir; tarih/saat birimleri ve tam adaptör doğrulanmadan paper giriş yok.",
    ),
    "Arcus": dict(
        url="https://api.arcus.xyz/v1/markets",
        docs="https://docs.arcus.xyz",
        paper=False,
        auth="Public piyasa verisi anahtarsız",
        note="Piyasa erişimi test edilir. Geçmiş/fee/funding ve yürütme adaptörü tamamlanmadı.",
    ),
    "Variational": dict(
        url="https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats",
        docs="https://docs.variational.io/technical-documentation/api",
        paper=False,
        auth="Public istatistik verisi anahtarsız",
        note="RFQ kotasyonları 600 saniyeye kadar önbellekli olabilir. Tam geçmiş ve yürütme doğrulanmadan paper giriş yok.",
    ),
}


class Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def protect(raw, decrypt=False):
    if os.name != "nt":
        raise ValueError("Kalıcı anahtar saklama bu sürümde Windows DPAPI gerektirir")
    buf = ctypes.create_string_buffer(raw)
    src = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    api = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    api.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(Blob),
    ]
    api.restype = ctypes.c_int
    args = [ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out)]
    if not api(*args):
        raise ValueError(
            "Windows kullanıcı şifrelemesi kullanılamıyor (kod "
            + str(ctypes.get_last_error())
            + "). Anahtar kaydedilmedi."
        )
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("API yönlendirmesi kabul edilmedi")


class Connections:
    def __init__(self, store):
        self.store = store
        self.results = store.get("connection_results", {})
        self.fees = store.get("verified_extended_fees", {})
        self._memory_key = None

    def view(self):
        encrypted = bool(self.store.get("extended_key_encrypted", None))
        has_key = encrypted or self._memory_key is not None
        return {
            v: {
                **d,
                "test": self.results.get(v),
                "has_key": has_key if v == "Extended" else False,
                "key_storage": (
                    "Windows DPAPI"
                    if encrypted
                    else "Yalnızca motor belleği" if self._memory_key else "Anahtar yok"
                ),
                "account_fee": self.fees if v == "Extended" else None,
            }
            for v, d in CATALOG.items()
        }

    def request(self, url, data=None, key=None):
        headers = {"User-Agent": "FundingDeskPaper/0.4", "Accept": "application/json"}
        if key:
            headers["X-Api-Key"] = key
        if data:
            headers.update(
                {"Content-Type": "application/json", "x-api-version": "2026-08-16"}
            )
        req = urllib.request.Request(
            url, data=json.dumps(data).encode() if data else None, headers=headers
        )
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=12) as r:
            raw = r.read(10_000_000)
            code = r.status
        return json.loads(raw), code

    def test(self, venue):
        if venue not in CATALOG:
            raise ValueError("Bilinmeyen platform")
        try:
            o, code = self.request(
                CATALOG[venue]["url"],
                (
                    {"type": "perpMeta", "exchangeId": "all"}
                    if venue == "TxFlow"
                    else None
                ),
            )
            if venue == "Lighter":
                rows = o["order_book_details"]
            elif venue == "Extended":
                if o.get("status") != "OK":
                    raise ValueError("Platform başarısız yanıt döndürdü")
                rows = o["data"]
            elif venue == "TxFlow":
                if not o.get("success"):
                    raise ValueError("Platform başarısız yanıt döndürdü")
                rows = o["data"]["universe"]
            elif venue == "Arcus":
                rows = o["markets"]
            elif venue == "Variational":
                rows = o["listings"]
            else:
                rows = o.get("markets", o.get("data", []))
                if isinstance(rows, dict):
                    rows = rows.get("markets", [])
            if not isinstance(rows, list) or not rows:
                raise ValueError("Piyasa listesi beklenen biçimde değil")
            result = dict(
                ok=True,
                http=code,
                markets=len(rows),
                ts=time.time(),
                message="Piyasa verisi erişimi doğrulandı. "
                + (
                    "Paper adaptörü bağlı."
                    if CATALOG[venue]["paper"]
                    else "Bu sonuç paper işlem izni değildir; adaptör tamamlanmadı."
                ),
            )
        except Exception as ex:
            result = dict(
                ok=False,
                ts=time.time(),
                message="Bağlantı doğrulanamadı: " + str(ex)[:180],
            )
        self.results[venue] = result
        self.store.put("connection_results", self.results)
        self.store.event("CONNECTION_TEST", dict(venue=venue, **result))
        return result

    def save_extended_key(self, key):
        if (
            not isinstance(key, str)
            or not 8 <= len(key) <= 2048
            or any(c in key for c in "\r\n\x00")
        ):
            raise ValueError("Geçerli bir salt okunur Extended API anahtarı girin")
        try:
            o, _ = self.request(
                "https://api.starknet.extended.exchange/api/v1/user/fees?market=BTC-USD",
                key=key,
            )
        except urllib.error.HTTPError as ex:
            raise ValueError("Anahtar doğrulanmadı: HTTP " + str(ex.code)) from None
        except Exception:
            raise ValueError(
                "Anahtar resmi Extended ücret endpointinde doğrulanamadı"
            ) from None
        if o.get("status") != "OK" or not o.get("data"):
            raise ValueError("Ücret bilgisi doğrulanamadı")
        rows = o["data"] if isinstance(o["data"], list) else [o["data"]]
        row = next((x for x in rows if x.get("market") == "BTC-USD"), rows[0])
        fee = float(row["takerFeeRate"])
        if not 0 <= fee <= 0.01:
            raise ValueError("Beklenmeyen hesap ücret oranı")
        try:
            encrypted = base64.b64encode(protect(key.encode())).decode()
            self._memory_key = None
            mode = "Windows DPAPI ile şifreli kaydedildi"
        except ValueError:
            encrypted = None
            self._memory_key = key
            mode = "Bu ortamda Windows şifrelemesi kullanılamıyor; anahtar yalnızca motor belleğinde tutuldu, diske yazılmadı. Motor kapanınca silinir"
        self.store.put("extended_key_encrypted", encrypted)
        self.fees = dict(
            market="BTC-USD",
            taker=fee,
            builder=float(row.get("builderFeeRate", 0)),
            ts=time.time(),
            note="Doğrulanan hesap ücreti. Paper motoru muhafazakâr yapılandırılmış ücreti kullanmaya devam eder.",
        )
        self.store.put("verified_extended_fees", self.fees)
        self.store.event(
            "CREDENTIAL_SAVED",
            {
                "venue": "Extended",
                "message": "Salt okunur anahtar doğrulandı; "
                + mode
                + ". Anahtar günlüklenmedi.",
            },
        )
        return dict(
            ok=True, message=mode + ". Paper ücret varsayımı otomatik düşürülmedi."
        )

    def delete_extended_key(self):
        self.store.put("extended_key_encrypted", None)
        self.store.put("verified_extended_fees", {})
        self.fees = {}
        self._memory_key = None
        return {"ok": True}

    def retest_key(self):
        encrypted = self.store.get("extended_key_encrypted", None)
        key = self._memory_key or (
            protect(base64.b64decode(encrypted), True).decode() if encrypted else None
        )
        if not key:
            raise ValueError("Kayıtlı anahtar yok; panelden tekrar girin")
        return self.save_extended_key(key)
