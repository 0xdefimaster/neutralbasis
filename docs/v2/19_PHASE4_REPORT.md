# 19 — Phase 4 raporu: motor bağlantısı, strateji modülleri, giriş kapısı, değerlendirme

Tarih: 2026-11-12. Dal: `v2/venue-policy` (yerel; push ve merge yok). **PAPER ONLY.** Motor başlatılmadı; E:'ye dokunulmadı.

Değerlendirme verisi:
- Konum: `C:\Users\W10\nb-benchmark\v2-phase4-2026-11-12\`.
- İçerik: V2 koruması altında 746 salt okunur yanıt (`answers.json.gz`, sha256 `f1ff9f2a…`), alındığı an 2026-11-12 05:12 TR.
- Yeniden üretim: `python -m tools.v2_phase4_eval --dir <dir> [--assume-session-open]` (offline, deterministik).
- Fiyat geçmişi: LOOSE pilot DB'sinden salt okunur saatlik mumlar; son mum 2026-11-11 23:00 TR (yaklaşık 6 saat eski).
- Etiket: OBSERVED public data; kârlılık iddiası değil.

## Sorularının cevabı

### Kaç fırsat üretildi?

| Strateji | Aday | Kaynak |
|---|---|---|
| Funding | **481** | `wide_paper.rank` (401 V2 satırı) |
| Fiyat yakınsaması | **529 çift → 1.058 yön** (her çift iki yönde) | `wide_paper.rank_spreads` |
| Hibrit | **1.058** (yönlü çift birleşimi) | Bu iki listeden |

Phase 3'teki 488 / 528, yeni verinin anında 481 / 529 oldu.

### Kaçı neden reddedildi? (her aday ilk nedeniyle sayıldı)

Gerçek durum (Arcus `isOutsideRth=true`, ABD seansı kapalı):

| | Funding | Yakınsama | Hibrit |
|---|---|---|---|
| **Veri** (kanıt veya girdi eksik) | 66 | 92 | 170 |
| **Yürütme** (simüle edilemez: RFQ, seans, likidite) | 402 | 874 | 864 |
| **Maliyet** (girdiler tamam, işlem kazandırmıyor) | 13 | 92 | 24 |
| **Paper'a uygun** | **0** | **0** | **0** (hibrit zaten yalnız araştırma) |

İlk nedenlerin dağılımı:
- **Funding:**
  - Yürütme: Variational RFQ 234, seans kapalı 168.
  - Veri: settlement kanıtı yok 40, RWA referansı doğrulanmadı 23, fiyat geçmişi yok 3.
  - Maliyet: başa baş süresi 24 saati aşıyor 11, carry kalıcı değil 2.
- **Yakınsama:**
  - Yürütme: RFQ 526, seans 336, likidite 12.
  - Veri: RWA referansı 46, fiyat geçmişi 46.
  - Maliyet: fark uç değil 82, net negatif 10.

Karşı-olgusal senaryo: seans açık kabul edildi, ama fiyatlar seans dışı fiyatlar; yalnız diğer engelleri göstermek için:

| | Funding | Yakınsama | Hibrit |
|---|---|---|---|
| Veri | 194 (settlement 131, fiyat geçmişi 40, RWA referansı 23) | 229 (fiyat geçmişi 182, RWA referansı 46, epizot 1) | 426 |
| Yürütme | 236 (RFQ 234, likidite 2) | 572 (RFQ 526, likidite 46) | 536 |
| Maliyet | 51 (carry kalıcı değil 32, başa baş 19) | 257 (fark uç değil 237, net negatif 20) | 96 |
| **Uygun** | **0** | **0** | **0** |

Maliyet örnekleri (gerçek defterler, $1.000 / bacak):
- **Funding'in en iyisi:** AAPL, tradeXYZ ↔ Lighter RH. 24 saatlik funding $0,31; ücret $0,18 + spread $0,25 + risk payı $0,28. Başa baş **55 saat**. Diğer adaylar 90–800 saat.
- **Yakınsama:** farkı 2,6–4,7 MAD uçta olan çiftler (LIT, AMD, ZEC, META). Maliyetlerden sonra net −$0,8 ile −$2,9 arasında.

### Kaçı gerçekten paper işlem açmaya uygun?

**0.** Kapı hiçbir adayı geçirmedi. Bu bir hata değil: veri ve maliyet kontrolleri tek tek incelendi ve tutarlı. Yanıtlar tekrar oynatılınca aynı sonuç çıkıyor.

### Hangi borsalarda ücret veya funding kanıtı eksik?

| Borsa | Ücret | Settled funding | Diğer |
|---|---|---|---|
| lighter_rh | Pazar alanı 0 (Standard); hesap kademesi doğrulanmadı | Var (`fundings`); `direction` anlamı belgede yok, motor kuralıyla okunuyor | Offline değerlendirmede 152 bacağın fiyat geçmişi yok (pilot doldurmamıştı); motorda V2 geçmiş tutucusu doldurur |
| risex | **UNKNOWN**; 53 doğrulanmış pazar satır üretmiyor | Var (SettleFundingRate) | Mainnet REST host'u belgede yok |
| arcus | Var (ppm, canlı) | **Var** (`/v1/fundingRates?market=&from=&to=`, bu fazda eklendi) | RTH tanımı 04:00–20:00 ET |
| txflow | VIP0 belgesi; hesap kademesi doğrulanmadı | **Yok**: yalnız yayımlanan oran | — |
| variational | 0 (belge); maliyet spread'te | **Yok** | Fiyat geçmişi yok; **RFQ kotası dolum fiyatı değil** |
| tradexyz | Varlık başına canlıdan türetiliyor | Var (`fundingHistory`) | — |
| popdex_perps | — | — | BLOCKED: resmî API yok |
| veranta | — | — | RESEARCH_ONLY |

### Hangi strateji henüz tamamlanmadı?

| Strateji | Değerlendirme modülü | Motorda paper girişi |
|---|---|---|
| Funding (`v2/strategies/funding.py`) | Tamam | V2 kapısıyla bağlı (`WidePaper.assess` + `v2_gate`) |
| Yakınsama (`v2/strategies/convergence.py`) | Tamam (robust medyan / MAD, epizot, iki yön) | V2 kapısıyla bağlı |
| Hibrit (`v2/strategies/hybrid.py`) | Tamam (maliyet ve risk payı tek sefer) | **Tamamlanmadı**: masanın iki çıkış kuralı olan bir pozisyon tipi yok; sonuçlar yalnız araştırma |
| Variational RFQ yürütme modeli | — | **Yok**: Variational bacakları engelli |
| Veranta vault modeli | — | Yok |

### Testler

| Çalıştırma | Sonuç |
|---|---|
| Phase 3 commit'i (fd84ae9) | 849 OK, 2 atlanan |
| `test_v2_phase4.py` (yeni) | 14 / 14 OK |
| Tam paket (Phase 4 sonu) | **863 OK, 2 atlanan, 0 başarısız** |

## İstenen görevler: ne yapıldı

| # | Görev | Uygulama |
|---|---|---|
| 1 | `v2/source`'u motora bağla | `v2/collector.py` (canlı, TTL'li, talebe bağlı) + `v2/engine_source.V2EngineSource` (EngineSource arayüzü). `desk_analytics`: `NB_V2=1` ise V2 kaynağı ve V2 kapısı takılıyor. Canlı çalıştırılmadı (onay gerekir) |
| 2 | 119 Tier A korunuyor; Tier B araştırıldı | Tier B için TxFlow `perpMetaDetail` denendi: `asset.name` yalnız sembol (BTC, ADA, AERO). Bağımsız isim kanıtı yok, Tier B paper'a kapalı |
| 3 | Adayları gerçek defter, ücret ve likiditeyle yeniden değerlendir | `v2/gates.Gate` + `v2/economics` (defter VWAP, %0,1 tolerans, dört taraflı taker ücreti). 481 + 1.058 + 1.058 aday değerlendirildi |
| 4 | Variational RFQ'yu dolum gibi kullanma | Kapı: `EXECUTION_MODEL_UNVERIFIED`. Variational defteri `model=rfq_indicative` işaretli |
| 5 | RISEx UNKNOWN ücret | Satır üretilmiyor (`FEE_UNKNOWN`). Kapı da ayrıca reddediyor |
| 6 | Yayımlanan ve settled funding'i ayır | Funding ve hibrit iki bacakta settled kamu ödemesi istiyor. TxFlow ve Variational "published only" → `SETTLEMENT_EVIDENCE_MISSING`. Yakınsamada yayımlanan oran yalnız tahmini maliyet olarak etiketli |
| 7 | RWA vade, dayanak, seans ve baz | Emtia, FX, endeks ve karışık başlıklar → `RWA_REFERENCE_UNVERIFIED`. Hisse ve ETF → Arcus'un yayımladığı RTH (`RWA_SESSION_UNKNOWN` / `_CLOSED`). Fiyat yakınlığı tek başına yetmiyor |
| 8 | Üç strateji ayrı modül ve ortak net motoru | `v2/strategies/{funding, convergence, hybrid}.py` + `v2/economics.net` |
| 9 | 1000x / 1M normalizasyonu | `v2/normalize.py`: satır ve defter birim başına; `source.build` artık bu kontratları dışlamıyor, normalize ediyor |
| 10 | İki bacak, kısmi dolum, kurtarma, çıkış, PnL | `test_v2_phase4.LifecycleTests`: 1x ↔ 1000x pozisyon, aynı birim miktarları, kapanış neti = fiyat + funding − ücret, defter uzlaşması, LEG_RECOVERY. Kısmi dolum ve `unwind` mevcut `test_wide_paper` testlerinde |
| 11 | CEX ve legacy ağ ile kaynak düzeyi | `CollectorTests`: her istek `network_policy`'den geçiyor; önceki fazların guard / katalog / FiveFeed testleri |
| 12 | Tek borsalı varlık için abonelik ve backfill yok | `Collector.book/settled` V2 satırı olmayan pazarda istek göndermeden hata veriyor. `DeskHistorySource.markets` V2'de yalnız V2 satırlarını döndürüyor (test, legacy farkıyla) |

## Bilinen sınırlar

- Değerlendirme bir anlık görüntü; seans dışında alındı. Seans içinde RWA adayları başka engellere takılabilir (karşı-olgusal tablo).
- Offline fiyat geçmişi pilotun yaklaşık 6 saat eski mumları. Motorda geçmiş tutucu güncel tutar.
- `GAP_NOT_EXTREME` ve `CARRY_NOT_PERSISTENT` o anki piyasa durumu; başka bir saatte farklı olabilir.
- Hibrit pozisyon tipi ve RFQ modeli yok.
- Motorun V2 kipindeki uçtan uca davranışı canlı doğrulanmadı.
