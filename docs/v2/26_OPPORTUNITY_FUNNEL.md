# 26 — Fırsat hunisi, funding kanıtı ve Potansiyel Fırsat Radarı

**PAPER ONLY.**

Kod:
- `v2/funnel.py`
- `v2/funding_evidence.py`
- `tools/v2_forensics.py`

Testler: `test_v2_forensics.py`, 37 test.

## 1. Aşamalar ve izler

Aşamalar: `DISCOVERED → MATCHED → OBSERVED → ECONOMIC_CANDIDATE → EXECUTION_MODEL_CHECK → PAPER_ELIGIBLE → PAPER_OPENED`.

Her aday için bir iz (trace) kaydı üretilir:
- **`trace_id`:** `nb-` + sha256(kayıt hash'i, strateji, varlık, long, short)[:16]. Aynı kayıt yeniden oynatılırsa aynı id çıkar.
- **`stage_passed`, `failed_at`:** geçilen son aşama ve takılınan aşama.
- **`outcome`:** aşağıdaki ayrım.
- **`first_rejection`, `all_rejections`:** her ret bacak bazında ve hem kanonik hem iç kodla.
- **`missing_evidence`, `source_hashes`:** kayıt hash'i ve iki bacağın satır hash'i.
- **`equivalence`, `confidence`, `next_action`.**

Sonuç türleri birbirine karışmaz:

| Sonuç | Anlamı |
|---|---|
| `NOT_EVALUATED` | Ekonomi çalışmadı; kimlik, kapsam, kanıt, seans ya da bayat defter yüzünden. Net UNKNOWN; sıfır ya da negatif değil |
| `FAILED` | Ekonomi çalıştı ama girdi eksikti: boyutta likidite, settlement kapsaması, fiyat geçmişi |
| `UNPROFITABLE` | Ekonomi tam girdiyle çalıştı ve sonuç eşiğin altında: başa baş, asgari net, stres |
| `RESEARCH_ONLY` / `ELIGIBLE` | Strateji geçti / PAPER'a uygun |

Kanonik kodlar (belgedeki liste): NOT_DISCOVERED, IDENTITY_UNVERIFIED, RWA_REFERENCE_UNVERIFIED, SINGLE_VENUE_ONLY, MISSING_FUNDING_HISTORY, SETTLEMENT_EVIDENCE_MISSING, SESSION_UNKNOWN, BOOK_STALE, INSUFFICIENT_LIQUIDITY, NET_AFTER_COSTS, NOT_IN_CANDIDATES, CONFIG_EXCLUDED.

Listede karşılığı olmayan iç kodlar başka bir anlama katlanmaz; kendi adlarıyla kalır: EXECUTION_MODEL_UNVERIFIED, FEE_UNKNOWN, SESSION_CLOSED, BOOK_MISSING, PRICE_HISTORY_MISSING, NO_SIGNAL, CONFLICT.

Bacak durumuna göre çevrilen kodlar:
- Geçmiş yayımlayan bir mekânda settlement açığı varsa: `MISSING_FUNDING_HISTORY`.
- Yalnız oran yayımlayan bir mekânda: `SETTLEMENT_EVIDENCE_MISSING`.
- `NOT_A_V2_ROW`: bacak gözlem mekânıysa `CONFIG_EXCLUDED`, değilse `IDENTITY_UNVERIFIED`.

## 2. Denklik sınıfları

| Sınıf | Koşul |
|---|---|
| `EXACT` | Tier A, birden çok mekân, referansı doğrulanmış sınıf |
| `ECONOMICALLY_RELATED_BUT_NOT_IDENTICAL` | Tier A ama emtia, FX, endeks veya karışık RWA; referans kontrat belirtilmemiş (örnek: NATGAS) |
| `UNVERIFIED` | Tier B, tek mekân ya da grup yok |
| `CONFLICT` | Çakışma listesi (QNT, ANTHROPIC, OPENAI, ANT) |

## 3. Funding kanıt durumları ve kapsama pencereleri

Kanıt durumları (zayıftan güçlüye): MISSING < RATE_PUBLISHED < RATE_SAMPLED_AT_CUTOFF < SETTLEMENT_INFERRED_NOT_CONFIRMED < MARKET_SETTLEMENT_CONFIRMED < ACCOUNT_PAYMENT_CONFIRMED.
- Ayrıca `REVISED`: aynı funding zamanı iki farklı oranla görüldüyse.
- PAPER funding ve hibrit için yalnız son iki durum sayılır.

Mekân başına en iyi kanıt `fe.VENUE_BEST` içinde (bkz. 25).

Pencereler 24h, 3d, 7d, 30d ve 90d. Hesap kapsamayı dikkate alır:
- Her pencere için beklenen ve gözlenen nokta, kapsanan gün ve FULL (≥%95) / PARTIAL / EMPTY durumu raporlanır.
- Ortalama yalnız gözlenen noktalar üzerinden alınır.
- 55 günlük bir geçmiş "90d: 1320/2160 nokta (55.0/90 gün), PARTIAL" olarak raporlanır; 90 güne uzatılmaz, sıfırla doldurulmaz.

Anomali etiketleri, yalnız inceleme için:

| Etiket | Koşul |
|---|---|
| EXTREME_RATE | Mutlak değer ≥ %0.1/saat |
| OUTSIDE_HISTORY | Anlık oran geçmiş aralığının dışında |
| SIGN_FLIPS | İşaret saatlerin %20'sinden fazlasında değişiyor |
| SHORT_HISTORY | 90 günlük pencere PARTIAL |
| NO_HISTORY | Hiç geçmiş yok |
| REGIME_SHIFT | 24 saatlik ortalama ≥ 5 × 90 günlük ortalama |

İşaret kuralı tek yerde (`fe.leg_cashflow`): oran pozitifse long öder. Long bacağın nakit akışı −oran, short bacağınki +oran. `pair_carry`, eksik bir oranı asla varsaymaz; sonuç None olur.

## 4. Huni sayıları (05:12 kaydı, sha `f1ff9f2a…`)

| Aşama (en az buraya kadar geçen) | Aday |
|---|---|
| DISCOVERED, MATCHED, OBSERVED, ECONOMIC_CANDIDATE | 2,597 |
| EXECUTION_MODEL_CHECK | 198 |
| PAPER_ELIGIBLE | **0** |
| PAPER_OPENED | 0 |

Varlık düzeyinde: 194 çok-mekânlı grup (119 Tier A), 472 SINGLE_VENUE_ONLY, 119 IDENTITY_UNVERIFIED. Strateji satırı 401 (119 varlık).

| Sonuç | Aday |
|---|---|
| NOT_EVALUATED | **2,399** |
| UNPROFITABLE | 128 |
| FAILED | 70 |

| İlk ret | Aday |
|---|---|
| EXECUTION_MODEL_UNVERIFIED | 1,286 |
| SESSION_CLOSED | 840 |
| SETTLEMENT_EVIDENCE_MISSING | 146 |
| RWA_REFERENCE_UNVERIFIED | 115 |
| NO_SIGNAL | 97 |
| PRICE_HISTORY_MISSING | 55 |
| NET_AFTER_COSTS | 32 |
| INSUFFICIENT_LIQUIDITY | 14 |
| MISSING_FUNDING_HISTORY | 12 |

Tüm izler: `v2-forensics-2026-11-12\funnel.json`.

## 5. Potansiyel Fırsat Radarı

`funnel.radar()` bir çiftin, kapı onu engellese bile ne değerde *olabileceğini* hesaplar:
- yayımlanan anlık oranlar;
- şu anki defter (ya da RFQ kotasyonu);
- $1k, $5k ve $10k boyutları.

Etiket:
- **POTENTIAL:** carry pozitif ve bir boyutta gidiş-dönüş maliyeti 24 saat içinde geri kazanılıyor.
- **RESEARCH_ONLY:** diğer durumlar.

Kurallar:
- `eligible` her zaman False.
- Gözlem mekânları dahildir; `paper_venues` alanı bunu belirtir.
- RFQ kotasyonu `INDICATIVE_RFQ` olarak işaretlenir. Kotasyonun tek fiyatı kova boyutunun tamamı için geçerli: 1k kovası, 5k ve 10k için 100k kovası. Tolerans bandı uygulanmaz ama spread tamamen maliyete yazılır.
- Defterler motorun ±%0.1 dolum toleransıyla yürünür.

## 6. Panel

`v2/dashboard.render` yeni bölümler gösteriyor (`research_sections`):
- huni (gözlenen ↔ uygun);
- sonuç türleri;
- 11 mekân tablosu;
- NATGAS ve INIT kartları;
- radar.

Çıktı: `v2-forensics-2026-11-12\dashboard_research.html`. Dış betik yok.
