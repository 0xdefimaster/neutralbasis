# 03 — Yetenek matrisi (2026-11-12)

Durum kodları:

| Kod | Anlamı |
|---|---|
| VL | VERIFIED_LIVE: canlı public yanıt görüldü |
| VD | VERIFIED_DOC: resmî belgede var |
| SU | SUPPORTED_UNVERIFIED: eski kod okuyor, belge veya bağımsız kanıt yok |
| RO | RESEARCH_ONLY |
| B | BLOCKED |
| U | UNKNOWN |
| NS | NOT_SUPPORTED |

| Özellik | lighter_rh | risex | arcus | txflow | variational | popdex_perps | veranta | tradexyz |
|---|---|---|---|---|---|---|---|---|
| Piyasa keşfi | VL (`orderBookDetails`, 71 perp) | VL (`/v1/markets`, 53) | VL (`/v1/markets`, 79 online) | VL (`perpMeta`, 241) | VL (`/metadata/v2/stats`, 590) | B | U | VL (`metaAndAssetCtxs dex=xyz`, 122) |
| Varlık sınıfı (kimlik için) | U (alan yok) | VL (`category`) | VL (`category`, `type`) | U (`marketTags` okunmadı) | U (`name`, `-PERP`/`-SWAP`) | U | U | U (`perpCategories` okunmadı) |
| Mark / index | VD + VL | VD (oracle alanları) | VD (mark; oracle sağlayıcısı U) | VD (`markPx`, `oraclePx`) | VD (mark; index Variational oracle) | U | VD (Pyth/Lazer akışları) | VD (oracle deployer'dan) |
| L2 / kota | SU (`orderBookOrders`) | VD (REST + WS checksum) | VD (WS); REST yolu U | VD (`l2Book`, `l2BookDepth` seq) | VD (RFQ kota kovaları, 600 sn'ye kadar cache) | U | VD (risk-v2 spread, POST) | VD (`l2Book` `xyz:`) |
| Public trades | SU | VD | VD (`/v1/trades`) | VD | NS (RFQ) | U | U | U (`recentTrades` belgede bulunamadı) |
| Yayımlanan funding | VD (saatlik) | VD (`current_funding_rate`) | VD (saatlik) | VD (yüzde, saatlik) | VD (1–8 saat değişken) | U | VD (saatlik %) | VD (canlı `funding`) |
| Gerçekleşen / settled funding geçmişi | VD (`/fundings`; ödeme mi tahmin mi U) | VD (SettleFundingRate olayları) | VD (`/v1/fundingRates`) | **NS** (herkese açık geçmiş yok) | **NS** (API'de yok) | U | U | VD (`fundingHistory` `xyz:`) |
| Funding işareti | VD (pozitif: long öder) | VD | VD | VD | VD | U | U | U (bu sayfalarda yok) |
| Funding aralığı | VD 1 saat | VD (`funding_interval` ns), canlı 1 saat | VD 1 saat | VD 1 saat | VD 1–8 saat, market başına | U | VD saatlik | U (belge "8 saatlik" parametre diyor; canlı saatlik ödeme gözlemi eski pilottan) |
| Ücret | VD | **U** | VD + VL | VD | VD (0 + spread) | U | U | VL (türetilmiş) |
| Yürütme modeli (paper) | orderbook_vwap (SU) | orderbook_vwap (SU) | orderbook_vwap (SU) | orderbook_vwap (SU) | rfq_time_limited_quote (U) | B | vault_dynamic_quote (U) | orderbook_vwap (SU) |
| Paper girişi | U | **B (ücret U)** | U | **B (settlement kanıtı yok; yalnız `wide_txflow_paper` sahip kararıyla)** | **B (geçmiş yok, kimlik U)** | **B** | **B** | U (kimlik U) |

Kurallar:
- Bir özelliğin bozuk olması, borsanın geri kalanını gizlemez.
- Uygunluk kapısı, eksik özelliği olan bacağı engeller.
- SU durumundaki bir özellik, bağımsız kanıt olmadan VL'ye yükseltilmez.
