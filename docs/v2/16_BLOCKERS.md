# 16 — Açık engeller

| # | Engel | Etki | Nasıl çözülür | Kimin kararı |
|---|---|---|---|---|
| B1 | **popdex_perps:** resmî belge sayfası boş; API, zincir, ücret ve funding NOT FOUND. DefiLlama 403. Bilgiler yalnız üçüncü taraftan | Paper ve veri toplama BLOCKED | Resmî API / belge bağlantısı (sahip ya da PopDEX ekibinden), zincir id'si, ücret sayfası | Sahip |
| B2 | **veranta:** SDK v2 belgeli, ama ücret değerleri, rate-limit, seans takvimi ve funding yönü okunmadı. Spread uç noktası POST | RESEARCH_ONLY; ağ izni yok | SDK'nın read-only yüzeyini (`/v2/trading`, `/v2/open-interests`, pair data akışı) belgeden doğrulayıp izin listesine ekle; SDK kurulumu yalnız izole ortamda, anahtarsız | Mühendislik (sonraki faz) |
| B3 | **risex ücreti UNKNOWN** (JWT ile kullanıcıya özel); mainnet REST host'u belgede yok | risex bacaklı her aday `FEE_UNKNOWN` | Resmî ücret sayfası ya da RISEx ekibinden kaynak; host'un belgede yer alması | Sahip / mühendislik |
| B4 | **variational limiti 10 istek / 10 sn / IP.** Motor ve gözlemci aynı IP'den aşıyordu (6/14 tur 429) | Veri kaybı | Borsa genelinde tek bir token bucket, `retry_after_seconds` uyumu, tek bir paylaşılan okuyucu | Mühendislik (Phase 3) |
| B5 | **Kimlik sınıfı:** TxFlow, Lighter RH, tradeXYZ ve Variational metadata'da varlık sınıfı yok. Katı kesişim yalnız arcus↔risex (15 varlık) | Fırsat kapsamı çok dar | `marketTags`, `perpCategories`, RH kategori alanları ve küratörlü RWA registry'si | Mühendislik + sahibin RWA eşleme onayı |
| B6 | **Eski `FiveFeed`** V2 kipinde Extended ve LighterCore'a gitmeye çalışıyor (koruma reddediyor, hata kaydı oluşuyor) | Gürültü; motor V2'de temiz başlamaz | Borsa adaptörlerini `v2` katmanına almak (Phase 3) | Mühendislik |
| B7 | **CCXT'nin tradeXYZ yüklemesi V2 ile uyumsuz.** 2026-11-12 izole testte CCXT hyperliquid `load_markets` önce `spotMeta` istedi; V2 koruması reddetti ve yükleme durdu (fail-closed) | Eski CCXT tradeXYZ yolu V2'de çalışmaz | Doğrudan, kapsamlı adaptör yazıldı: `v2/venues/tradexyz.py` (istek kurucuları ve ayrıştırıcı). Motorun bu adaptöre bağlanması Phase 3 işi | Mühendislik; kısmen çözüldü |
| B8 | **TxFlow / Variational settled funding kanıtı yok** | Funding uygunluğu BLOCKED (TxFlow istisnası yalnız `wide_txflow_paper`) | Herkese açık settlement kaynağı yok; sahibin PAPER kararı | Sahip |
| B9 | **E: DB'nin yedeği yok**; disk arızalı | Veri kaybı riski | `14_OPERATION_BACKUP_RUNBOOK.md` | Sahip (disk ve onay) |
| B10 | **Lighter RH Standard 60 istek/dk** | Ağır REST taraması imkânsız | WS (`/stream?readonly=true`) ya da daha yüksek kademe; WS sözleşmesi doğrulanmalı | Mühendislik |
| B11 | **HIP-3 funding işareti ve formülü** okunan sayfalarda yok; Hyperliquid rate-limit ağırlıkları okunmadı | tradexyz funding kanıtı kısmi | Hyperliquid funding ve rate-limit sayfalarını okumak; eski pilotun saatlik xyz geçmişiyle karşılaştırmak | Mühendislik |
| B12 | **Risk ayarları:** 24 saat / 1,0 korunuyor; alternatifler onay bekliyor | İşlem sayısı çok düşük | `09_FUNDING_MODEL.md` seçenekleri | Sahip |

## Phase 3 güncellemesi (2026-11-12)

| # | Yeni durum |
|---|---|
| B4 | **Çözüldü (kod):** `v2/ratelimit` süreç geneli bütçe + paylaşılan önbellek + `retry_after_seconds`. Canlıda tek isteğe bile 429 geldi ("Service is busy"; global limit), bu yüzden 429 tamamen önlenemez; yeniden deneme ve önbellek bunu karşılıyor |
| B5 | **Büyük ölçüde çözüldü:** TxFlow `marketTags`, tradeXYZ `perpCategories`, Lighter RH `funding_premium_multiplier` ve Variational belge tablosu çapa olarak kullanılıyor. Tier A 119 varlık. Kalan: Tier B (80) için bağımsız ikinci kaynak |
| B6 | **Çözüldü:** V2'de Extended ve Core istekleri kaynakta kesildi (`bot.base_feed_venues`, `select_rfq_books`, `scan_price_discovery`, Core yardımcıları, `connections.test`) |
| B7 | **Çözüldü:** `XyzClient` motora bağlı (`wide_markets.client`) |
| B11 | **Çözüldü (belge):** Hyperliquid funding sayfası: saatlik, pozitifse long öder, oracle fiyatı, HIP-3 için de geçerli |
| B13 (yeni) | Vadeli kontrata dayalı RWA'larda (CL, BZ, NATGAS, COPPER) borsalar farklı kontrat ayı referans alabilir; fiyat yakınsamasında baz riski |
| B14 (yeni) | 1000x / 1M kontratlar için birim normalizasyonu yok (2 pazar) |
| B15 (yeni) | Arcus `fundingRates` geçmişi parametre istiyor (HTTP 400); belge parametreleri okunmadı |
