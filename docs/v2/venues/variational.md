# variational — Variational Omni

| Alan | Değer | Durum | Kaynak |
|---|---|---|---|
| API | Read-only: `GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats`. **Trading API yok** ("not yet live") | VD + VL (4fadac55…) | docs.variational.io/technical-documentation/api; help 14766804 |
| Rate limit | **10 istek / 10 sn / IP**; global 1.000 / dk; 429 + `retry_after_seconds` | VD | aynı |
| Keşif | 590 listeleme; `ticker` (`-PERP` / `-SWAP`), `name`, `contract_type`, `funding.interval_seconds`, kotalar (`base`, `size_1k`, `size_100k`, `size_1m`) | VL | — |
| Kimlik | Sınıf alanı yok, bu yüzden 0 / 590 doğrulandı. **QNT ve ANTHROPIC çakışma listesinde.** TradFi kuralı: "symbol matches the TradFi symbol" | U | tradfi-perpetuals.md |
| Fiyat | RFQ: tek yapıcı OLP; indicative ≠ firm; last look; bid/ask 600 sn'ye kadar cache'li | VD | trading-via-rfq.md |
| Ücret | Trading ücreti 0; maliyet spread'te; tasfiye ±%0,5; yatırma/çekme $0,1 | VD | omni/trading/fees |
| Funding | Kesir; aralık 1–8 saat (Bybit > Binance > 1 saat kuralı); pozitifse long öder; index fiyatı; tavan %2/saat; pre-IPO %0,005 / 8 saat. **API'de geçmiş yok** | VD | funding-rates |
| Gözlemci | 6/14 tur 429; belgedeki limitle açıklanıyor | V2-0 | B4 |
| Paper | **BLOCKED** (kimlik ve settled kanıtı yok; RFQ yürütme modeli doğrulanmadı) | — | — |
