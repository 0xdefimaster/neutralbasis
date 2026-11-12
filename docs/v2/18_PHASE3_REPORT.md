# 18 — Phase 3 raporu: adaptörler, kimlik doğrulaması, V2 kaynak kapatmaları

Tarih: 2026-11-12. Dal: `v2/venue-policy` (yerel; push yok). **PAPER ONLY.** Motor başlatılmadı, E:'ye dokunulmadı.

## Kaynak ve tekrar üretim

- **Canlı veri:** salt okunur, V2 koruması altında, `tools/v2_smoke_phase3.py` ile toplandı. Fixture'lar sha256 ile birlikte `C:\Users\W10\nb-benchmark\v2-smoke-2026-11-12-p3\` altında.
- **Lighter RH funding'i:** talebe bağlı; yalnız doğrulanmış gruptaki 58 RH pazarı için `--fetch-rh-fundings` ile çekildi.
- **Tablo:** `python -m tools.v2_identity_snapshot --fixtures <dir>` komutuyla, `identity_snapshot.json` dosyasından üretiliyor.
- **Etiket:** OBSERVED public metadata. Fiyatlar ve oranlar o anın değerleri; kârlılık iddiası değil.

## 8 borsa özeti

**Kimlik katmanları:**
- **Tier A:** sınıfı borsanın kendi alanından gelen en az iki bağımsız borsa, ya da tek çapa + pozitif isim eşleşmesi; fiyatlar uyumlu.
- **Tier B:** tek kripto çapası + kripto adı + fiyat farkı ≤ %0,5. Raporlanır, **strateji satırı üretmez**.

| Borsa | Pazar (listelenen / aktif perp) | Kimliği doğrulanan, 2+ borsada (Tier A) | Tier B | Strateji satırı | Tier A ortak varlık (borsa başına) | API durumu | Paper uygunluk engeli |
|---|---|---|---|---|---|---|---|
| lighter_rh | 71 / 71 | **58** | 0 | 58 | variational 51, txflow 49, arcus 44, risex 30, tradexyz 26 | Canlı adaptör: `orderBookDetails`, `orderBookOrders`, `fundings` | Funding son ödemeden projeksiyon; `direction` anlamı belgede yok (motor kuralı); Standard 60 istek/dk; taker ücreti 0 ama hesap kademesi doğrulanmadı |
| risex | 54 / 53 | **53** | 0 | **0** | txflow 51, variational 47, arcus 35, lighter_rh 30, tradexyz 22 | Canlı adaptör: `/v1/markets`, `/v1/orderbook`, funding geçmişi | **FEE_UNKNOWN** (ücret yalnız hesap JWT'siyle); mainnet host belgede yok |
| arcus | 86 / 79 | **73** | 0 | 73 | variational 65, txflow 59, lighter_rh 44, tradexyz 36, risex 35 | Canlı adaptör: `/v1/markets`, `/v1/l2OrderBook` | `fundingRates` geçmişi parametre istiyor (400); RWA mesai dışı bantları |
| txflow | 250 / 242 | **102** | 80 | 102 | variational 91, arcus 59, tradexyz 52, risex 51, lighter_rh 49 | Canlı adaptör: `perpMeta`, `marketTags`, `activeAssetCtx`, `l2Book` | **Herkese açık settled funding geçmişi yok** (yalnız `wide_txflow_paper` sahip kararıyla); ücret VIP0 belgesi, hesap kademesi doğrulanmadı |
| variational | 590 / 572 perp | **102** | 80 | 101 | txflow 91, arcus 65, tradexyz 56, lighter_rh 51, risex 47 | Canlı adaptör: `/metadata/v2/stats`; ortak bütçe ve önbellek | **Funding geçmişi yok**; RFQ yürütme modeli doğrulanmadı; 429 riski (global limit) |
| tradexyz | 144 / 122 | **66** | 0 | 66 | variational 56, txflow 52, arcus 36, lighter_rh 26, risex 22 | Canlı adaptör: `metaAndAssetCtxs dex=xyz`, `perpCategories`, `l2Book`, `fundingHistory`; motorda CCXT yerine `XyzClient` | Varlık başına ücret canlıdan türetiliyor; deployer funding çarpanı; vadeli RWA baz riski |
| popdex_perps | — | — | — | — | — | **BLOCKED**: resmî API yok | Resmî API ve belge gerekiyor |
| veranta | — | — | — | — | — | **RESEARCH_ONLY**: read-only API sözleşmesi doğrulanmadı | Ücret, spread, seans ve rate-limit belgeden okunmalı |

**Toplamlar:**

| Ölçüm | Değer |
|---|---|
| 2+ borsada Tier A doğrulanan varlık | **119** (40 kripto, 79 RWA); Phase 2'de 15'ti |
| Ayrıca yalnız Tier B olan | 80 kripto varlık (TxFlow↔Variational) |
| Tier A ortak borsa çiftleri | 6 veri borsası arasındaki **15 çiftin 15'i**. 8 borsa için teorik 28'den, popdex ve veranta'ya ait 13 çift veri olmadığı için 0 |
| Strateji satırı | **400** (119 varlık) |

`wide_paper.rank` bu satırlardan 488 funding adayı, `rank_spreads` 528 fiyat farkı adayı üretti. Bu sayılar yalnız adayların üretildiğini gösteriyor; kârlılık kanıtı değil.

## Eleme nedenleri (6 veri borsası, 1.195 pazar; her pazara tek ilk neden)

| Neden | Pazar | Açıklama |
|---|---|---|
| ROW (strateji satırı) | 400 | Tier A, funding ve ücret bilinen |
| SINGLE_VENUE_ONLY | 467 | Yalnız tek borsada (344'ü Variational); ağır veri ve aday yok |
| IDENTITY_TIER_B | 160 | TxFlow ve Variational'da 80'er kripto; tek çapa |
| FEE_UNKNOWN | 52 | RISEx |
| INACTIVE | 38 | Delist, offline, durdurulmuş |
| SINGLE_ANCHOR_UNCONFIRMED | 38 | Sınıfı olmayan Variational üyesi; tek çapa ve isim eşleşmesi yok (ör. GAS) |
| NOT_PERPETUAL | 18 | Variational swap'ları |
| CONFLICT_LIST | 11 | QNT, ANTHROPIC, OPENAI, ANT |
| PREIPO | 3 | |
| MULTIPLIER_UNKNOWN | 3 | Bilinmeyen sayısal önek |
| MULTIPLIER_NORMALIZATION_PENDING | 2 | 1000x / 1M kontratlar; birim normalizasyonu yazılmadı |
| NAME_CONFLICT | 2 | SPCX: Arcus "Space Exploration Technologies", Variational "SPAC and New Issue ETF" |
| PRICE_MISMATCH | 1 | CVX: Chevron vs "Convex Finance" |
| **Toplam** | **1.195** | |

## Kimlik güvenliği (gevşetilmedi; bkz. `06_MARKET_IDENTITY.md`)

- **Çapa:** sınıf yalnız borsanın kendi belgeli alanından gelir.

| Borsa | Alan |
|---|---|
| RISEx | `category` |
| Arcus | `category` |
| TxFlow | `marketTags` TradFi / Crypto sekmeleri |
| tradeXYZ | `perpCategories` |
| Lighter RH | `funding_premium_multiplier` 100 / 50 / 1 (belgedeki oran ×100) |
| Variational | Yalnız belgedeki 11 TradFi perp |

- **Fiyat:** birim fiyat çapaların medyanından kriptoda en fazla %1, RWA'da en fazla %2 sapabilir. Tier A'daki en büyük sapma %0,47.
- **İsim:** isimler varsa çelişmemeli. Çelişki varsa isimli üyelerin hepsi gruptan çıkar.
- **Sınıfsız üye:** iki bağımsız çapa ya da pozitif isim eşleşmesi olmadan katılamaz. GAS örneğinde TxFlow "Commodities" derken Variational "Gas" diyordu; reddedildi.
- **Yakalanan tuzaklar:** SPCX (isim), CVX (fiyat), SPX-PERP = "SPX6900" token (gruba girmedi), QNT / ANTHROPIC (çakışma listesi).
- **Bilinen risk:** vadeli kontrata dayalı RWA'lar (CL, BZ, NATGAS, COPPER) farklı kontrat aylarını referans alabilir. Fiyat %2 içinde, ama baz riski var. Fiyat yakınsaması stratejisi bu varlıklarda ek kanıt istemeli (Phase 4).

## Yapılan kod işleri

| Görev | Uygulama |
|---|---|
| 1–2 Kimlik | `v2/identity.verify`: grup kimliği, Tier A/B, eleme nedenleri. `v2/discovery.intersect` buna bağlandı |
| 3 FiveFeed | `bot.base_feed_venues()`: V2'de yalnız Lighter RH (Core host'u gelirse hata). `desk_analytics.select_rfq_books`: V2'de `{}`, RFQ soketi açılmaz. `wide_markets.scan_price_discovery`: V2'de istek yok, iş parçacığı başlatılmaz. Lighter Core yardımcıları V2'de ağdan önce hata verir. `connections.test`: kapsam dışına istek göndermez, RH host'unu kullanır |
| 4 Adaptörler | `v2/venues/{lighter_rh, risex, arcus, txflow, variational}.py`: istek kurucular, pazar / defter / funding ayrıştırıcıları, ortak kayıt (`v2/venues/base.py`) |
| 5 Variational | `v2/ratelimit.py`: süreç geneli bütçe (8 / 10 sn) + paylaşılan önbellek (15 sn) + 429'da `retry_after_seconds` kadar tüm borsayı bekletme. Gözlemci, `connections` ve adaptör aynı `variational.stats()` yolundan okur |
| 6 tradeXYZ | `v2/venues/tradexyz_client.XyzClient`: CCXT biçimli, yalnız xyz kapsamlı istekler. `wide_markets.client("tradeXYZ")` V2'de bunu kullanır; denetim, HTTP istatistiği ve 429 kancası korunur |
| 7 PopDEX / Veranta | Değişmedi: BLOCKED / RESEARCH_ONLY; hiçbir endpoint uydurulmadı |
| 8 Strateji entegrasyonu | `v2/source.py`: kayıtlar → kimlik → `wide_paper` satır biçimi (legacy borsa adı + `exchange_id` + `underlying_id`). `rank` ve `rank_spreads` testte bu satırlarla çalışıyor. Hibrit strateji ve yakınsama için fiyat geçmişi bağlantısı Phase 4 |
| 9 Test | `test_v2_phase3.py` (21 test) + güncellenen `test_v2_policy.py`; tam paket 849 OK, 2 atlanan |
| Smoke / araç | `tools/v2_smoke_phase3.py`, `tools/v2_identity_snapshot.py` |

**Ağ izin listesine eklenenler** (belgeli okumalar):

| Borsa | Eklenen |
|---|---|
| TxFlow | `tags`, `marketTags` |
| Hyperliquid | `perpCategories` (BROAD_METADATA; yalnız `xyz:` satırları kullanılır) |
| Arcus | `/v1/l2OrderBook/{m}`, `/v1/bbo/{m}` |

## Henüz yapılmayanlar (Phase 4)

- Motorun V2 kipinde `v2/source` ile uçtan uca çalışması (EngineSource değişimi). Bu bir motor başlatması gerektirmez, ama canlı doğrulama için başlatma ister; onay gerekir.
- 1000x / 1M kontratların birim normalizasyonu.
- RFQ (Variational) ve vault (Veranta) yürütme modelleri.
- Hibrit strateji.
- Arcus funding geçmişi parametreleri.
- WS işçileri.
