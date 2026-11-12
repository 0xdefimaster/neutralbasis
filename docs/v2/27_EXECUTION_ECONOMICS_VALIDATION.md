# 27 — Yürütme ekonomisinin doğrulaması (boyut merdiveni, işaretler, anomaliler)

**PAPER ONLY.** Risk ayarları değişmedi: `wide_max_breakeven_hours` 24, `wide_funding_realization` 1.0.

## 1. Maliyet nasıl hesaplanıyor

Kod: `v2/economics.leg` / `round_trip` ve radar için `funnel.quote_leg`.
- Her bacak gerçek defter üzerinde, verilen boyutta açılış ve kapanış olarak yürünür. Dört taker ücreti (iki bacakta açılış ve kapanış) ve orta fiyata göre spread maliyeti eklenir.
- Dolum toleransı orta fiyattan ±%0.1 (`bot.FILL_TOLERANCE`). Defter bu bant içinde boyutu taşımazsa sonuç `INSUFFICIENT_LIQUIDITY`.
- RFQ kotasyonu (Variational) PAPER'da dolum sayılmaz (`EXECUTION_MODEL_UNVERIFIED`). Radarda `INDICATIVE_RFQ` olarak fiyatlanır: kotasyonun tek fiyatı kendi kovası için geçerli, spread tamamen maliyete yazılır. 5k ve 10k için daha geniş 100k kovası kullanılır (`variational.quote_book_for`).
- Bilinmeyen ücret hiçbir zaman sıfır sayılmaz; sonuç `FEE_UNKNOWN`.

## 2. NATGAS — iki yön, üç boyut (05:12 defterleri, OBSERVED)

| Long → Short | Carry /8s | $1k | $5k | $10k |
|---|---|---|---|---|
| TxFlow → tradeXYZ | **+0.3144%** | 17.7 bp, BE 4.5 s | 18.5 bp, BE 4.7 s | 21.6 bp, BE 5.5 s |
| tradeXYZ → TxFlow | −0.3144% | 17.7 bp, carry negatif | 18.5 bp, negatif | 21.6 bp, negatif |
| TxFlow → Variational | +0.1940% | 26.7 bp, BE 11.0 s (RFQ) | yetersiz likidite* | yetersiz likidite* |
| Variational → tradeXYZ | +0.1204% | 15.3 bp, BE 10.2 s (RFQ) | yetersiz likidite* | yetersiz likidite* |

\* 05:12 kaydında yalnız Variational'ın 1k kotasyon kovası defter olarak saklanmıştı. 5k ve 10k satırları bu yüzden yetersiz likidite görünüyor; bu, kaydın sınırı. Yeni okumalar `quote_book_for` ile 100k kovasını kullanır.

BE = gidiş-dönüş maliyetinin carry ile geri kazanılma süresi.

## 3. INIT — long Variational / short Hyperliquid native (07:11, OBSERVED, RESEARCH_ONLY)

| Boyut | Sonuç |
|---|---|
| $1k | $5.76 (57.6 bp: Variational kotasyon spread'i 35 bp + HL spread $1.35 + HL ücret $0.90), carry $0.87/saat, BE 6.7 s |
| $5k | `INSUFFICIENT_LIQUIDITY` (HL defteri ±%0.1 bandında) |
| $10k | `INSUFFICIENT_LIQUIDITY` |

Ters yön (long HL / short Variational): carry −0.69%/8s, RESEARCH_ONLY.

## 4. İşaret testleri (SCREENSHOT_UNVERIFIED fikstürü)

- `test_natgas_screenshot_sign`:
  - long TxFlow −0.0121%/8s → long alır;
  - short tradeXYZ +0.3332%/8s → short alır;
  - net +0.3453%/8s. ✔
- `test_init_screenshot_sign`:
  - long Variational −1.6975%/8s → +0.2122%/saat alır;
  - short HL −1.2644%/8s → 0.1581%/saat öder;
  - net +0.4331%/8s. ✔
- `test_per_hour_from_four_hour_interval`: Variational NATGAS 4 saatlik oranı 0.0009092 → 0.0002273/saat. ✔
- `test_missing_rate_is_never_defaulted`: eksik oran ya da aralık None döner. ✔

## 5. Anomali ve aykırı değer testleri

| Test | Senaryo |
|---|---|
| `test_coverage_is_coverage_aware_55_of_90_days` | MOCK 55 günlük geçmiş → 90 günlük pencere PARTIAL, 1320/2160, 55.0/90 gün; ortalama yalnız gözlenen noktalardan; `SHORT_HISTORY` |
| `test_anomalies_and_revisions` | EXTREME_RATE, OUTSIDE_HISTORY, NO_HISTORY, SIGN_FLIPS, REGIME_SHIFT (MOCK rejim) ve REVISED (aynı saat, iki farklı oran) |
| Gerçek veri (07:11) | HL INIT → `REGIME_SHIFT` (24 saatlik ortalama −0.32%/saat, 90 günlük −0.0085%/saat) |

## 6. Üç strateji, iki varlık

| | Funding | Yakınsama | Hibrit |
|---|---|---|---|
| NATGAS TxFlow/tradeXYZ | NOT_EVALUATED: RWA_REFERENCE_UNVERIFIED + SETTLEMENT_EVIDENCE_MISSING (TxFlow) | NOT_EVALUATED: RWA_REFERENCE_UNVERIFIED (fiyat geçmişi var: her bacakta 193 saatlik kapanış) | NOT_EVALUATED: iki kod birden |
| NATGAS + Variational | yukarıdakilere ek EXECUTION_MODEL_UNVERIFIED | aynı | aynı |
| INIT Variational/HL | CONFIG_EXCLUDED (aday yok) | CONFIG_EXCLUDED | CONFIG_EXCLUDED |

Referans kontrat doğrulanırsa NATGAS yakınsama stratejisi ekonomi aşamasına geçer; funding stratejisi TxFlow settlement kanıtı gelene kadar kapalı kalır.
