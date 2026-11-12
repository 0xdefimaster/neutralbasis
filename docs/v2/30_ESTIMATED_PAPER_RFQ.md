# 30 — ESTIMATED PAPER, RFQ simülasyonu ve funding kanıtı düzeltmeleri

Tarih: 2026-11-12. Taban `ea1a1d6`. **PAPER ONLY.**
- Gerçek emir, imza, pozisyon ya da cüzdan yok.
- STRICT kapı (`v2/gates.py` mantığı), 8 mekânlı PAPER listesi, 11 mekânlı gözlem listesi ve risk ayarları (24 / 1.0) değişmedi.
- E:/D:'ye dokunulmadı; motor başlatılmadı; push, merge ve deploy yapılmadı.

Veriler: `C:\Users\W10\nb-benchmark\v2-estimated-2026-11-12`.

| Kaynak | Zaman | sha256 |
|---|---|---|
| 05:12 TR kaydı | 05:12 | `f1ff9f2a…` |
| Hizalı okuma | 07:36 TR | `9a81c839…` |
| Okuma 1 | 12:47 TR | `13fa5d22…` |
| Okuma 2 | 12:50 TR | `8c092b9e…` |
| Okuma 3 | 12:53 TR | `90268a5e…` |

Son üç okumada guard 18/20/18 istek izinli, 0 reddedilen istek, 0 CEX.

## 1. Variational RFQ simülasyonu (`v2/rfq_paper.py`)

- **Kotasyon kademeleri:** `base` (asgari emir), `size_1k`, `size_100k`, `size_1m`.
  - Boyut bir kademeye eşitse EXACT_TIER.
  - İki kademe arasındaysa BRACKETED: **bir üst kademeden** (daha geniş, ihtiyatlı) fiyatlanır; alt kademenin fiyatı belirsizlik bandı olarak raporlanır. Ara değer üretilmez.
  - En büyük kademenin üstündeki boyut için fiyat yok (`ABOVE_LARGEST_TIER`); likidite uydurulmaz.
- **Zaman:** her tahmin `updated_at` ile karar anı arasındaki yaşı kontrol eder.
  - En fazla 60 s; belge 600 s'ye kadar önbellek diyor.
  - Gelecekte bir zaman damgası `QUOTE_IN_FUTURE`, zaman bilgisi yoksa `QUOTE_TIME_UNKNOWN`.
- **Spread yalnız bir kez sayılır.** Alış ask'tan, satış bid'den. Fiyat K/Z'si spread'i zaten içerir; `spread_cost` yalnızca dökümdür.
- **Açılış ve kapanış farklı kotasyonlardan** (`simulate`). Durum SIMULATED'dır, hiçbir zaman FILLED değildir. Funding yayımlanan orandan tahmin edilir (RATE_PUBLISHED, ödeme değil).
- **Stres senaryoları** (kamuya açık veriyle ölçülemeyenler):

  | Senaryo | Değerler |
  |---|---|
  | Firm − indicative farkı | 5 / 10 / 25 bp |
  | Last-look reddi | %5 / %20 olasılık; diğer bacak gidiş-dönüş maliyetiyle kapatılır |
  | Kotasyon eskimesi | Kotasyon yaşı boyunca 1σ ters hareket. Oynaklık bilinmiyorsa sonuç None, sıfır sayılmaz |
  | `rfq_moderate` | 10 bp + p = 0.10 + 1σ. Girişte ≥ 0 olmalı |

- **Etiket:** `ESTIMATED_RFQ_FILL`.

## 2. ESTIMATED PAPER modu (`v2/estimated.py`, açıkça açılır)

- **Anahtar:** `NB_V2_ESTIMATED_RFQ=1` (varsayılan kapalı).
- **Davranış:** STRICT kapı aynen çalışır; sonuç ayrı alanlarda saklanır (`strict_eligible`, `status`).
- **Runtime:** sonuçları yalnız anlık görüntüye (`snapshot.estimated`) ekler. Desk'e ya da emir yoluna gitmez; test: `test_runtime_never_hands_estimates_to_the_desk`.
- **Tahminle değerlendirilebilen retler:**

  | Ret | Yöntem |
  |---|---|
  | `EXECUTION_MODEL_UNVERIFIED` | RFQ simülasyonu |
  | `SETTLEMENT_EVIDENCE_MISSING` | Aynı anın yayımlanan oranları: `ESTIMATED_FUNDING_PUBLISHED` |
  | `RWA_REFERENCE_UNVERIFIED` | Yalnız ECONOMICALLY_RELATED ya da UNKNOWN için; baz stresi ile |

- **Kesin retler aynen kalır:** CONFIG_EXCLUDED, FEE_UNKNOWN, oturum kapalı veya bilinmiyor, BOOK_STALE, DIFFERENT_UNDERLYING, fiyat geçmişi yok, boyutta likidite yok.
- **Eşikler STRICT ile aynı:** başa baş ≤ 24 saat, realization 1.0, asgari net ve birleşik stres. RFQ bacağı için ayrıca `rfq_moderate`; referans için ayrıca mevcut fiyat farkının bir kez daha ters açılması.
- **Risk payı:** yalnız oran kanıtı olan çiftlerde 7 günlük fiyat farkı geçmişi yoksa, risk payı olarak **o anki fiyat farkı** kullanılır (`RESERVE_PROXY_CURRENT_GAP`).
- **Gözlem mekânı bacakları** (INIT/Hyperliquid) için `scenario()` kullanılır. Durum SCENARIO_ONLY olur; ESTIMATED PAPER girişi sayılmaz.

## 3. Funding kanıtı

- **TxFlow `userFundings` satırları** `account_payment` olarak işaretlenir, ama oran birimi doğrulanmadığı için `rate=None` kalır. Bu satırlar:
  - `market_evidence` içinde `basis=RATE_UNIT_UNVERIFIED` alır ve yeterli sayılmaz;
  - kapının `market_settled` kontrolünü geçemez;
  - funding stratejisinde sayılmaz ve hata üretmez; strateji `SETTLEMENT_EVIDENCE_MISSING` döndürür.

  Bunların hepsi testle doğrulandı.
- **`txflow.infer_rate_unit`:** birim ancak her satırda ödeme tutarı, pozisyon büyüklüğü ve **o saatteki mark fiyatı** birlikte varsa doğrulanır. En az 3 satır tek bir okumayla (yüzde ya da kesir) %5 içinde tutarlı olmalı. Aksi halde UNVERIFIED olur ve oran türetilmez.
  - Mark geçmişi şu an yok; yetkili bir adres de yok.
- Gerçekleşmiş, örneklenmiş ve yayımlanmış funding ayrı türlerdir. ESTIMATED sonuçlarda funding hep `ESTIMATED_FUNDING_PUBLISHED` olarak geçer.

## 4. NATGAS referans sınıfları (`references.classify`)

Belge metinlerinin birebir aynı olması artık aranmıyor. Her spesifikasyon, kaynaktan elle çıkarılmış normalize alanlara indirgendi:
- underlying_id
- unit
- exchange
- contract_month
- roll_rule_id

Fiyat kaynağı ve kapalı saat yöntemi kimlik alanı değil; `method_differences` olarak raporlanıyor. Ticker ya da fiyat benzerliği kanıt sayılmıyor.

| Çift | Sınıf | Neden |
|---|---|---|
| TxFlow ↔ tradeXYZ | **UNKNOWN** | TxFlow NATGAS'ı hiç belgelemiyor |
| tradeXYZ ↔ Variational | **ECONOMICALLY_RELATED** | Dayanak aynı (Henry Hub); birim, borsa, vade ve roll bilinmiyor |
| TxFlow ↔ Variational | UNKNOWN | — |

Hedge (STRICT) yalnız EXACT_REFERENCE için açılıyor. UNKNOWN ve ECONOMICALLY_RELATED araştırmada ve ESTIMATED senaryoda baz riskiyle yer alıyor.

## 5. Zaman ve veri kalitesi

- **Tazelik sınırı oynaklığa bağlı** (`opportunity_log.freshness_limit`). Saatlik σ ile, fiyat yaşının 1σ hatası 5 bp'ye ulaştığı süre hesaplanır; sonuç [2 s, 60 s] aralığına sınırlanır. Saatlik kapanışlar pilot DB'den, salt okunur.

  | Çift | Sınır | Sonuç |
  |---|---|---|
  | NATGAS (TxFlow, tradeXYZ) | ≈ 36 s | TxFlow↔tradeXYZ okumaları 4.4–4.8 s kayma: ALIGNED |
  | INIT (σ yüksek) | **2 s** | Sıralı okuma (6–7 s, Variational kotasyonu 10–40 s yaşında) INIT için eşzamanlı sayılamaz: tüm INIT sonuçları TIME_MISALIGNED_FOR_VOLATILITY |

- **Yaşam döngüsü:** 4 hizalı nokta; noktalar arası ara değer yok.

  | Fırsat | Görüldüğü noktalar | Sonuç |
  |---|---|---|
  | NATGAS TxFlow→tradeXYZ | 1/4 (yalnız 07:36; en yüksek +0.380%/8s) | 12:47–12:53 arası −0.046 ile −0.049%/8s; **≤ 5 sa 11 dk içinde kapandı** |
  | NATGAS TxFlow→Variational | 4/4 | En yüksek +0.233%/8s, sonra +0.008%/8s (başa baş > 200 sa) |
  | NATGAS Variational→tradeXYZ | 1/4 | Yalnız 07:36 |
  | INIT Variational→Hyperliquid | 0/4 | −0.08 → −0.78%/8s |

  Doğrulanmış (settled) getiri hiçbir çiftte yok: `estimated_vs_verified` = None.

## 6. Kabul: STRICT ile ESTIMATED karşılaştırması

**05:12 kaydı, tüm adaylar:**

| Strateji | Görülen | STRICT geçen | ESTIMATED giriş | Reddedilen | İlk ret (ESTIMATED) |
|---|---|---|---|---|---|
| Funding | 481 | 0 | **2** | 479 | RWA_SESSION_CLOSED 314, STALE_QUOTE 85, BREAKEVEN 50, STRESS_NEGATIVE 11, INSUFFICIENT_LIQUIDITY 10, PRICE_HISTORY_MISSING 7, CARRY_NOT_PERSISTENT 2 |
| Yakınsama | 1,058 | 0 | 0 | 1,058 | RWA_SESSION_CLOSED 628, PRICE_HISTORY_MISSING 296, GAP_NOT_EXTREME 108, INSUFFICIENT_LIQUIDITY 14, STRESS_NEGATIVE 12 |
| Hibrit | 1,058 | 0 | **2** | 1,056 | RWA_SESSION_CLOSED 628, STALE_QUOTE 219, CARRY_NOT_POSITIVE 80, BREAKEVEN 50, … |

- 05:12 kaydı yaklaşık 6 dakika sürmüş; Variational kotasyonlarının yaşı ortanca 335 s, en çok 372 s. Bu yüzden **tüm Variational bacakları STALE_QUOTE**: o kayıt RFQ için zaman açısından geçerli değil.
- ESTIMATED girişler, **funding ve hibrit için aynı iki çift:**

  | Çift | Carry | Net | Başa baş | Stres en düşük |
  |---|---|---|---|---|
  | **NATGAS TxFlow→tradeXYZ** | +0.314%/8s | +$4.99 | 11.3 sa | −$0.45 |
  | **CL TxFlow→tradeXYZ** | +0.146%/8s | +$2.37 | 11.0 sa | −$0.39 |

  NATGAS girişinin döküm ve varsayımları:
  - **Net +$4.99:** funding +$9.43 (yayımlanan oran × 24 sa) − ücret $1.08 − spread $0.69 − risk payı $2.67 (o anki fiyat farkı).
  - **Varsayımlar:** ESTIMATED_FUNDING_PUBLISHED, ESTIMATED_REFERENCE_BASIS_RISK, referans sınıfı UNKNOWN.
  - **Belirsizlik:** stres senaryolarında net −$0.45 ile +$4.99 arasında. `second_leg_half_filled` −0.45, `funding_-50%` +0.28, `combined_moderate` +1.51, `reference_gap_x1` +2.33.
  - **Gerçekleşme belirsizliği:** taşımanın kalıcılığı kanıtsız. 07:36'dan sonra çiftin taşıması gerçekten negatife döndü (bkz. §5).

**NATGAS, hizalı okumalar ($1k / $5k / $10k):**

| Okuma | TxFlow→tradeXYZ | Variational→tradeXYZ | TxFlow→Variational |
|---|---|---|---|
| 07:36 | **Giriş / Giriş / Giriş** (net 6.1 / 30.6 / 58.3 $, BE 11.1–11.7 sa) | Red: NET_BELOW_MIN / BREAKEVEN / BREAKEVEN | **Giriş** / STRESS_NEGATIVE / STRESS_NEGATIVE |
| 12:47, 12:50, 12:53 | Red: CARRY_NOT_POSITIVE | Red: CARRY_NOT_POSITIVE | Red: BREAKEVEN (> 200 sa) |

STRICT: tüm okumalarda 0. Retler RWA_REFERENCE_UNVERIFIED ve SETTLEMENT_EVIDENCE_MISSING; Variational'lı çiftlerde ayrıca EXECUTION_MODEL_UNVERIFIED.

**INIT** (Variational / Hyperliquid native):
- **STRICT:** aday yok. V2 içinde SINGLE_VENUE_ONLY; Hyperliquid CONFIG_EXCLUDED.
- **ESTIMATED:** SCENARIO_ONLY; PAPER'a girmez.

| Okuma | Carry /8s | $1k | $5k / $10k | Zaman |
|---|---|---|---|---|
| 07:36 | −0.081% | INSUFFICIENT_LIQUIDITY | INSUFFICIENT_LIQUIDITY | MISALIGNED |
| 12:47 | −0.787% | CARRY_NOT_POSITIVE (−$29.4) | INSUFFICIENT_LIQUIDITY | MISALIGNED |
| 12:50 | −0.786% | INSUFFICIENT_LIQUIDITY | INSUFFICIENT_LIQUIDITY | MISALIGNED |
| 12:53 | −0.779% | CARRY_NOT_POSITIVE (−$35.1) | INSUFFICIENT_LIQUIDITY | MISALIGNED |

**Farklı zamanlarda açılıp kapanan RFQ simülasyonları** (36 adet; long Variational):
- **NATGAS/tradeXYZ, 18 simülasyon:** hepsi negatif. $1k'de −0.27 ile −1.59 $, $10k'de −27.7 ile −32.7 $. $5k ve $10k, 100k kademesinden BRACKETED fiyatlandı.
- **INIT/Hyperliquid, 18 simülasyon (yalnız senaryo):** 4'ü hesaplanabildi; $1k'de −10.1 ile +7.7 $ arası. 14'ünde diğer bacakta (HL) likidite yetersiz.
  - INIT sonuçlarındaki değişkenliğin kaynağı fiyat gürültüsü; funding değil.
- **36 simülasyonun hiçbiri** iki ucunda da zaman açısından hizalı değil (`trustworthy=False`).

**Sonuç:**
- STRICT PAPER'ı geçen aday: **0**. Hiçbir kapı gevşetilmedi.
- ESTIMATED PAPER girişleri yalnız etiketli tahminlerdir. 05:12'de NATGAS ve CL, 07:36'da NATGAS TxFlow→tradeXYZ ve TxFlow→Variational ($1k) girdi.
- Daha sonraki okumalarda bu girişlerin taşıması tersine döndü veya eridi. Bu, "yayımlanan oran = gerçekleşecek getiri" varsayımının tam olarak neden kanıt gerektirdiğini gösteriyor.

## 7. Testler

- `test_v2_estimated.py`: 23 test, 1'i gerçek 05:12 verisiyle.
- Tam takım: **990 test, OK (2 atlandı)**, 259 s; önceki 967 testin hepsi geçiyor.
- `git diff ea1a1d6 -- v2/policy.py v2/network_policy.py wide_paper.py` boş.
