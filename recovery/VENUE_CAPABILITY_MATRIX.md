# Venue Capability Matrix

2026-11-10 · PAPER ONLY. One row per execution venue (`exchange_id`).

## Sources

| Kısaltma | Kaynak |
|---|---|
| **L** | Canlı motorun kendi kayıt defteri (`GET /api/capabilities`, PID 5988, kod `fceaf96`; salt okunur) |
| **P** | İzole pilot (worktree, `9ca4467` dahil), 02:35'te boş veritabanıyla başladı. Kanıt birikiyor; işaret kanıtı için en az 3 yerleşmiş saat gerekir |
| **D** | Resmî belgeler (2026-11-10 okundu) |
| **M** | Bu çalışmadaki ölçümler |

Durum etiketleri: `VERIFIED` · `SUPPORTED_UNVERIFIED` · `RESEARCH_ONLY` · `BROKEN` · `UNKNOWN` · `NOT_SUPPORTED`.

| Yetenek | Hyperliquid | Lighter Core | Lighter RH | Extended (normal) | Extended (RFQ) | Paradex | RISEx | TxFlow | Arcus | Variational |
|---|---|---|---|---|---|---|---|---|---|---|
| Kimlik / bağlayıcı | `Hyperliquid`, ccxt | `LighterCore`, ccxt + yerel `fundings` (9ca4467) | `Lighter`, yerel api.rh.lighter.xyz | `Extended`, yerel | `Extended` (`isRfq`) | `Paradex`, ccxt | `RISEx`, yerel | `TxFlow`, yerel RPC | `Arcus`, yerel | katalog kaydı |
| Piyasa keşfi | VERIFIED (L: 109 taze) | L: 15 taze; P: 56 · VERIFIED | **BROKEN** in L (ayrıştırıcı, e1ccf7e ile düzeldi); P: 24 taze | VERIFIED (L: 339, RFQ dahil) | VERIFIED | VERIFIED (15) | SUPPORTED_UNVERIFIED (9) | UNKNOWN | SUPPORTED_UNVERIFIED (14) | NOT_SUPPORTED |
| Public fiyat | VERIFIED | VERIFIED | SUPPORTED_UNVERIFIED | VERIFIED | VERIFIED (mark) | VERIFIED | VERIFIED | UNKNOWN | VERIFIED (oracle) | NOT_SUPPORTED |
| Güncel funding | VERIFIED | L: kullanılamaz (ccxt oranı 8×, aralık yok). P: son yerleşmiş ödeme (projeksiyon) | Son yerleşmiş ödeme (projeksiyon) | VERIFIED | Binance funding'i (D) | VERIFIED | Son yerleşmiş (tahmin değil) | UNKNOWN | VERIFIED | NOT_SUPPORTED |
| Funding geçmişi | VERIFIED | L: **BROKEN** ("adaptörde yok"); P: yerel, 31 gün (M) | VERIFIED (31 gün, M) | VERIFIED | VERIFIED | SUPPORTED_UNVERIFIED | VERIFIED | NOT_SUPPORTED | SUPPORTED_UNVERIFIED | NOT_SUPPORTED |
| Settlement kanıtı | VERIFIED (113/119 piyasa) | L: 0/0; P: birikiyor | L: 0/0; P: birikiyor | VERIFIED (89/89) | VERIFIED | RESEARCH_ONLY (sürekli tahakkuk) | RESEARCH_ONLY (%66 kalıcılık) | NOT_SUPPORTED | UNKNOWN | NOT_SUPPORTED |
| Funding işareti | SUPPORTED_UNVERIFIED (yayımlanan = ödeme; nakit kanıtı yok) | P: birikiyor | P: birikiyor | SUPPORTED_UNVERIFIED | aynı | RESEARCH_ONLY | RESEARCH_ONLY (belge formülü + public tutarlılık) | UNKNOWN (belge yok) | UNKNOWN (belge yok) | NOT_SUPPORTED |
| Funding aralığı | VERIFIED (saatlik, D) | P: gözlenen yerleşme aralığından (3600 s) | VERIFIED | VERIFIED (saatlik, D) | VERIFIED | RESEARCH_ONLY | VERIFIED (saatlik) | UNKNOWN | UNKNOWN | NOT_SUPPORTED |
| Yayın gecikmesi (M, n = 1) | 7,8 s | 11,0 s | 11,9 s | 9,5 s | — | UNKNOWN | ≤ 220 s | UNKNOWN | UNKNOWN | — |
| Defter | REST (ccxt, 20 seviye) | REST (ccxt / `orderBookOrders`) | REST `orderBookOrders` | REST | WS gerçek defter (kullanıcı emirleri); REST indicative | REST (ccxt) | REST | REST (RPC) | REST | — |
| Defter tazeliği | Okumada tazelik kontrolü | aynı | aynı | aynı | Veri çerçevesi ≤ 30 s; ping yalnız teşhis (3102fa0) | aynı | aynı | aynı | aynı | — |
| Derinlik ($1000 gidiş-dönüş, M) | HL/LC medyanı 12,5 bp | 4–12 bp | 3–5 bp | 3–8 bp | 256–387 bp (gerçek defter) | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | — |
| Ücret modeli | Taker 4,5 / maker 1,5 bp (D). Motor: ccxt taker | L: ccxt **0,0 (doğrulanmamış)**; P: 2,8 bp Premium taker (D), hesap türü UNKNOWN | Standard 0 / Premium 3,5 bp (D). P: 3,5 bp | Taker 2,5 / maker 0 bp (D) | Kendi emrin her zaman taker 2,5 bp (D) | ccxt (kaynak bilinmiyor) | Kullanıcı girişi 3 bp | UNKNOWN | ccxt / feetiers | — |
| Piyasa kısıtları | ccxt lot/min | ccxt lot/min | `orderBookDetails` | `tradingConfig` | `tradingConfig`, RFQ min/max (D) | ccxt | yerel | UNKNOWN | yerel | — |
| PAPER giriş/çıkış | SUPPORTED (VERIFIED: MOCK E2E, gerçek kapı) | SUPPORTED; gerçek veride işaret kanıtı bekliyor | SUPPORTED; işaret kanıtı bekliyor | SUPPORTED | RESEARCH_ONLY: dolum modeli doğrulanmadı | RESEARCH_ONLY | RESEARCH_ONLY | NOT_SUPPORTED | RESEARCH_ONLY | NOT_SUPPORTED |
| Yürütme modeli | Taker, defter VWAP | Taker, VWAP | Taker, VWAP | Taker, VWAP | Gerçek defter VWAP (muhafazakâr; MM kotasyonu modellenmedi) | — | — | — | — | — |
| Kanıt güveni | Yüksek | Orta (yerel API + belge; işaret kanıtı bekliyor) | Orta | Yüksek | Düşük (yürütme fiyatı UNKNOWN) | Düşük | Orta-düşük | Düşük | Düşük | Yok |

## Reference-only sources

Bu kaynaklar **yürütme mekânı değildir**:

Binance, Bybit, OKX, Bitget, KuCoin, Kraken Futures, Gate, HTX, MEXC, BingX, Backpack, BloFin, Deribit, Phemex ve DEX referansları (Aster, ApeX, dYdX, Derive, GRVT, Hibachi, Nado, Pacifica). Toplam **63** katalog kaydı ve **30** taranan adaptör var.

- Canlı motorda hepsi `funding_model` UNKNOWN ve işaret/aralık doğrulanmamış. Çalışma alanına alınamazlar (`workspace_venues` = `capabilities.PERP_DEXES`).
- Pilot bu referans adaptörlerini **taramaz**: yük ve hız sınırı için kapalılar.

## Identity checks

| Kontrol | Sonuç | Test |
|---|---|---|
| Lighter Core ≠ Lighter RH | Ayrı `exchange_id` (`LighterCore` / `Lighter`), ayrı host, ayrı ücret senaryosu | `test_wide_markets.test_core_and_robinhood_are_distinct`; `test_lighter_core_funding` |
| Robinhood Chain | Lighter RH ile aynı entegrasyon; tek kez sayıldı | — |
| Aynı ticker, farklı enstrüman | Medyan fiyat oranı %5'ten fazla saparsa çift alınmıyor | `run_audit.py`. Motor: %15 farkta "farklı token" |
| `market_id` / `instrument_id` | Lighter: sayısal `market_id` (ccxt id = yerel id, BTC = 1, ETH = 0, M). Extended: `BTC-USD` adı | Ayrıştırıcı testleri |
