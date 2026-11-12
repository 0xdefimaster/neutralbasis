# lighter_rh — Lighter RH (Robinhood Chain)

| Alan | Değer | Durum | Kaynak |
|---|---|---|---|
| Kimlik | Ayrı borsa, Lighter mainnet'in bir piyasası değil; hesap, API anahtarı ve market id'leri ayrı. Eski kodda adı "Lighter" (Core = "LighterCore", V2 dışı) | VD | docs.lighter.xyz/developers/robinhood-chain/overview.md |
| REST / WS | `https://api.rh.lighter.xyz/api/v1/`; `wss://api.rh.lighter.xyz/stream` (`?readonly=true`); zincir id 466324 | VD | aynı |
| Keşif | `GET /api/v1/orderBookDetails`: 71 aktif perp, alanlar `symbol`, `market_id`, `market_type`, `multiplier`, `maker_fee`/`taker_fee`, `mark_price`/`index_price` | VL (smoke 2026-11-12, d6b2063d…) | — |
| Varlık sınıfı | Alan yok, bu yüzden kimlik UNVERIFIED (0 / 71). Hisse ağırlıklı (ör. BE) | U | B5 |
| Funding | Saatlik; pozitifse long öder; index fiyatı; RWA premium çarpanı 1/2 (Core belgesi). `/api/v1/fundings` GET, yanıtta zaman damgası **saniye**; ödeme mi tahmin mi NOT FOUND | VD (kısmi) | …/trading/funding.md, …/api/funding/fundings.md |
| Ücret | Standard 0/0; Plus %0,015 / %0,015; Premium %0,012 / %0,035 | VD | overview.md |
| Rate limit | **Standard 60 istek/dk**; recentTrades ağırlığı 600 | VD | …/reference/rate-limits.md |
| Paper | U: kimlik ve funding semantiği eksik | — | — |
| Sonraki adım | `orderBookDetails` ayrıntısı ve kategori; `orderBookOrders` sayfası; WS read-only sözleşmesi | — | — |
