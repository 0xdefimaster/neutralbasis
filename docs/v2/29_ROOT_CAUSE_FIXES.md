# 29 — Kök neden düzeltmeleri (f03eb91 sonrası)

Tarih: 2026-11-12. **PAPER ONLY.**
- E:/D:'ye dokunulmadı.
- Motor, pilot ve kaydedici başlatılmadı.
- Push, merge ve deploy yapılmadı.
- `wide_max_breakeven_hours=24` ve `wide_funding_realization=1.0` değişmedi.
- PAPER evreni 8 mekân, gözlem evreni 11 mekân olarak korundu.

Amaç ret koşullarını gevşetmek değil, eksik kanıtı toplamaktı. Sonuç: **hiçbir ret kaldırılmadı ve yeni PAPER-eligible aday yok.**

Veriler:
- 05:12 kaydı: `v2-rootcause-2026-11-12` (önce: `v2-forensics-2026-11-12`).
- Aynı-zaman damgalı okuma: `v2-aligned-2026-11-12` (07:36 TR, sha256 `9a81c839…`).

## 1. NATGAS referans kontratı (resmî birincil kaynaklar)

Kod: `v2/references.py`. Her alan kaynağının adresiyle birlikte tutuluyor.

| Alan | tradeXYZ | TxFlow | Variational |
|---|---|---|---|
| Dayanak | "1 MMBtu Henry Hub doğalgazının USD fiyatı" | **UNKNOWN** (NATGAS belgede yok; genel tanım "oracle index of the spot asset") | "Henry Hub Natural Gas futures" |
| Borsa (NYMEX/CME) | UNKNOWN | UNKNOWN | UNKNOWN |
| Vade (bugün) | Z26 → F27 roll penceresi (06.11 17:30 ET – 12.11 17:30 ET) | UNKNOWN | UNKNOWN |
| Roll kuralı | **ÇELİŞKİLİ:** 5–10. iş günü (commodities.md) ile 5–9. iş günü (roll-schedules.md) | UNKNOWN | Continuous Month Roll; yakın vadenin son işlem günü ayında 5–9. işlem günü |
| Fiyat kaynağı | Relayer; sağlayıcı adı yok | Validator'ların yayımladığı toplu referans; sağlayıcı adı yok | Omni endeksi (OLP) |
| Kapalı saatler | ±%10 discovery bound; EMA τ = 30 dk | UNKNOWN | Orderbook EWMA modu |
| Uzlaşma | Vadesiz; funding aralığı xyz sayfalarında yok | Vadesiz; saatlik, mark price üzerinden | Vadesiz; NATGAS 4 saatlik; uzlaşma fiyatı UNKNOWN |

Değerlendirme:
- **TxFlow↔tradeXYZ:** 5 alan UNKNOWN, 2 alan farklı → kapı kapalı.
- **tradeXYZ↔Variational:** 3 alan UNKNOWN, 4 alan farklı → kapalı.

Destekleyen gözlem (07:36): aynı anda tradeXYZ fiyatı TxFlow'dan **31.6 bp yüksek**. tradeXYZ bugün roll penceresinin son gününde; vade farkıyla tutarlı bir baz farkı.

Kod değişikliği:
- `Gate.static`, RWA çiftini yalnız `references.compare(...).verified` ise geçiriyor; bunun için tüm alanların bilinmesi ve iki tarafta birebir aynı olması gerekiyor.
- Ret metni eksik ve farklı alanları adıyla yazıyor.
- Tam eşleşen MOCK bir kayıtla kapının açıldığı, tek bir UNKNOWN alanla kapalı kaldığı testle doğrulandı.

**Açmak için:** TxFlow NATGAS spesifikasyonu (yayımlanmamış), iki mekânın açıkça adlandırdığı borsa, bugünkü vade ve tek bir roll kuralı gerekiyor.

## 2. TxFlow funding settlement kanıtı

Kaynak: docs.txflow.com/api/info.md, userFundings.md, trade/funding.md.

- **Kamuya açık piyasa düzeyinde funding geçmişi yok.** `/info` tip listesinde fundingHistory veya benzeri bir tip bulunmuyor.
- **`userFundings`:** bir adresin ödeme satırlarını verir (time, coin, usdc, szi, fundingRate, amount). İmza istemiyor ("Access: By user address").
  - Başkasının hesabını okumak yasak; sahibin yetkilendirdiği bir test adresi yok. Bu yüzden **ENGELLİ**.
- Uygulanan dar istisna (varsayılan kapalı):
  - Yalnız `observation_v3` modunda ve yalnız `NB_TXFLOW_FUNDING_ADDRESS` ile verilen tek adres için geçerli.
  - Başka adres, imza ya da hesap alanı reddediliyor.
  - NB_V2 politikası bu sorguyu her durumda reddediyor.
- `txflow.parse_user_fundings`: satırları `account_payment` olarak işaretliyor. `fundingRate` biriminin yüzde mi kesir mi olduğu belgede yazmadığı için oran türetilmiyor (`rate=None`).
- Yayımlanan oran hiçbir yerde ödeme sayılmıyor. Kapı, oran yayımlayan bir mekânı yalnız **o market için** settled türünde (`settled_public` ya da `account_payment`) noktalar varsa geçiriyor; örneklenmiş oran (`sampled_at_cutoff`) yetmiyor. Test edildi.

## 3. Variational RFQ: doldurulabilirlik modeli gereksinimleri

Kaynak: docs.variational.io. `variational.RFQ_FILL_MODEL_REQUIREMENTS` sonucu: **BLOCKED.**

| Gereksinim | Durum |
|---|---|
| Boyuta göre gösterge kotasyonları (base, 1k, 100k, 1m) | Karşılanıyor |
| Kotasyon yaşı (`updated_at`; en çok 600 s önbellek) | Karşılanıyor |
| Firm ile gösterge fiyat arasındaki fark | Veri yok: firm quote yalnız emir girişinde; "Trading API … not yet available" |
| Last-look ret oranı | Veri yok |
| Firm quote geçerlilik süresi | UNKNOWN |
| Market emirlerinde kısmi dolum | UNKNOWN (limit emirler all-or-nothing) |
| Kamuya açık işlemler ve dolumlar | Yok; yalnız kullanıcının kendi CSV dosyası |
| Settled funding | Yok; yalnız kullanıcının kendi CSV dosyası |

Bu bilgiler kamuya açık verilerle elde edilemiyor. Hesap ve trading API olmadan model kurmak mümkün değil; **EXECUTION_MODEL_UNVERIFIED korunuyor.**

Eklenen: `quote_age()`. 07:36 okumasında INIT kotasyonu okuma anında 30.5 s yaşındaydı.

## 4. Funding kanıt güveni: market, pencere ve gerçek ödeme bazında

Kod: `fe.market_evidence(exchange_id, points, at, interval, window)`. Güven, okunan noktalara göre belirleniyor:

| Durum | Güven |
|---|---|
| Settled ve 7 günlük pencere FULL | HIGH |
| Settled ama pencere PARTIAL | MEDIUM |
| Yalnız oran, örnek ya da çıkarım | LOW |
| Eksik ya da REVISED | NONE |

- En zayıf nokta türü geçerli sayılıyor.
- Huni izinin güveni bacaklardan **en zayıfı**. Yalnız mekân yeteneğine dayanan bacak en çok LOW alabiliyor.

05:12 kaydında (aynı 2,597 iz):

| Güven | Önce | Sonra |
|---|---|---|
| HIGH | 500 | 465 |
| MEDIUM | 696 | 35 |
| LOW | 1,401 | 2,097 |

731 izin güveni değişti. Önceki MEDIUM'ların çoğu yalnız yayımlanan orana dayanıyordu. Bacak kanıt temeli: 2,686 MARKET_POINTS, 2,508 VENUE_PUBLISHES_RATE_ONLY.

## 5. Huni: 2,597 adayın sınıflandırması ve radar görünürlüğü

`test_v2_rootcause.FunnelDataTests` OBSERVED 05:12 kaydı üzerinde şunları doğruluyor:
- 2,597 benzersiz iz; sonuç toplamı 2,597; PAPER_ELIGIBLE 0.
- Her NOT_EVALUATED izde net boş, `failed_at` EXECUTION_MODEL_CHECK ve en az bir ekonomi öncesi ret var.
- Her UNPROFITABLE izdeki retlerin tamamı maliyet kategorisinde.
- Kanonik kodlar listede; tüm bacaklar PAPER evreninde.
- **2,399 NOT_EVALUATED izin tamamı radarda.** 1,026 çift; 16'sı POTENTIAL (hepsi PAPER mekânları). En güçlüleri:
  - NATGAS TxFlow→tradeXYZ: +0.314%/8s, başa baş 4.5 s;
  - BZ ve CL (TxFlow);
  - NVDA (Arcus).
- Radar girdilerinin hiçbiri uygun (eligible) değil.

## 6. Aynı zaman damgalı yeniden oynatılabilir analiz

Araç: `tools/v2_aligned.py`.
- Tek bir okuma patlaması yapılıyor; her cevabın istek ve alım zamanı kaydediliyor.
- Bacak zamanları arasındaki fark 60 s'yi aşarsa sonuç `TIME_MISALIGNED` ve taşıma hesaplanmıyor. Variational için kotasyonun kendi `updated_at` zamanı da sayılıyor.

Önceki analizde gerçek bir karışma vardı: tek bir `at` kullanılmıştı ve Variational kotasyonu "−8 s yaşında" görünüyordu.

07:36 TR patlaması (6.8 s içinde, sha256 `9a81c839…`):

| Çift | Kayma | Oranlar /8s | Carry /8s | $1k | $5k | $10k |
|---|---|---|---|---|---|---|
| NATGAS TxFlow→tradeXYZ | 4.8 s | −0.0126% / +0.367% | **+0.380%** | 20.8 bp, BE 4.4 s | 21.1 bp, BE 4.5 s | 23.9 bp, BE 5.0 s |
| NATGAS Variational→tradeXYZ | 36.6 s | +0.220% / +0.367% | +0.147% | 11.6 bp, BE 6.3 s (RFQ) | yetersiz | yetersiz |
| INIT Variational→Hyperliquid | 37.4 s | −0.326% / −0.407% | **−0.081%** | yetersiz | yetersiz | yetersiz |

Market bazlı kanıt:

| Bacak | Kanıt | Güven |
|---|---|---|
| tradeXYZ NATGAS | MARKET_SETTLEMENT_CONFIRMED, 168/168 | HIGH |
| TxFlow NATGAS | RATE_PUBLISHED | LOW |
| Hyperliquid INIT | MARKET_SETTLEMENT_CONFIRMED, 168/168 (90 gün 2160/2160) | HIGH |
| Variational | RATE_PUBLISHED | LOW |

INIT açıklaması:
- Taşıma tersine döndü.
- HL INIT defterinin ask tarafında ±%0.1 bant içinde yalnız **$702** var; $1k'lik short bacağın kapanışı bile dolmuyor.
- `REGIME_SHIFT` sürüyor.

## 7. Önce/sonra karar izleri

**NATGAS** (iz `nb-18ae793f5eb215f9`, funding TxFlow→tradeXYZ):

| | Önce (f03eb91) | Sonra |
|---|---|---|
| Aşama / sonuç | ECONOMIC_CANDIDATE / NOT_EVALUATED | aynı |
| İlk ret | RWA_REFERENCE_UNVERIFIED | aynı |
| Tüm retler | RWA_REFERENCE_UNVERIFIED, SETTLEMENT_EVIDENCE_MISSING | aynı |
| Ret metni | "venues do not state the same reference contract (commodity)" | Alan düzeyinde: unknown underlying, exchange, contract_month, roll_rule, off_hours; different price_source, settlement |
| Settlement metni | "TxFlow publishes a rate only" | "… no settled payments **for this market**" (market bazlı kontrol) |
| Bacak kanıtı | Mekân yeteneği | long: RATE_PUBLISHED / LOW; short: MARKET_SETTLEMENT_CONFIRMED 168/168 / HIGH |
| Güven | LOW | LOW |
| Radar | Yalnız NATGAS için ayrıca | Tüm NOT_EVALUATED radarında, POTENTIAL |

**INIT** (Variational / Hyperliquid):

| | Önce | Sonra |
|---|---|---|
| Kapsam | CONFIG_EXCLUDED (HL native PAPER evreni dışında) | aynı; evren değiştirilmedi |
| V2 içi | SINGLE_VENUE_ONLY | aynı |
| Carry | +0.693%/8s (07:11; tek `at`, zamanlar karışık) | **−0.081%/8s** (07:36; hizalı, kayma 37 s) |
| $1k | POTENTIAL, BE 6.7 s | INSUFFICIENT_LIQUIDITY (HL ask bandı $702) |
| Variational | EXECUTION_MODEL_UNVERIFIED | aynı; RFQ modeli BLOCKED (6 eksik gereksinim) |

**Yeni PAPER-eligible aday: 0.** Hiçbir ret gevşetilmedi; değişiklikler kanıtı daha kesin ölçüyor.

## 8. Ek kök neden: kaynaklar arası Variational önbellek sızıntısı (düzeltildi)

Tam takım bu sorunu yakaladı: `FunnelDataTests` tek başına 2,597 iz buluyordu, phase3/phase4 testlerinden sonra çalışınca 1,326.

- **Neden:** `variational.stats()`, süreç genelindeki önbelleği yalnız URL ile anahtarlıyordu (TTL 15 s). Bir testin MOCK Variational cevabı, sonraki 05:12 kayıt oynatmasına sızdı; satır sayısı 401'den 297'ye düştü.
- **Etkisi:** aynı süreçte farklı kaynaklardan (canlı, kayıt, MOCK) ya da farklı anlardan gelen okumalar karışabiliyordu. Bu, 6. maddedeki "zamanları karıştırma" sorununun bir başka yolu.
- **Kapsam:** önceki rapor çıktıları (`v2_forensics`, `v2_phase4_eval`) ayrı süreçlerde ve tek kaynakla üretilmişti; etkilenmedi.
- **Düzeltme:**
  - Önbellek anahtarı artık `(URL, kaynak)`.
  - Canlı okuyucular ("live": canlı collector, bağlantı testi, funding gözlemcisi) tek cevabı paylaşmaya devam ediyor.
  - Kayıt oynatan ya da kayıt alan bir collector'ın kendi kaynağı var.
  - Test: `SourceIsolationTests`.

## 9. Testler

- `test_v2_rootcause.py`: 22 test, 4'ü gerçek 05:12 verisiyle. Kayıt klasörü yoksa bu 4 test atlanıyor.
- `test_v2_forensics.py`: 37 test.
- Tam takım: **967 test, OK (2 atlandı)**, 274 s. Önceki 945 testin hepsi geçiyor.
- `git diff f03eb91 -- v2/policy.py v2/network_policy.py wide_paper.py` boş: PAPER evreni, NB_V2 ağ politikası ve risk ayarları değişmedi.
