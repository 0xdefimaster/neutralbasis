# 24 — NATGAS ve INIT adli incelemesi

**PAPER ONLY.** Kaynaklar ve etiketleri:

| Kaynak | Etiket | Bilgi |
|---|---|---|
| arb.sh ekran görüntüleri | SCREENSHOT_UNVERIFIED | `docs/v2/fixtures/arbsh_screenshots_2026-11-12.json` |
| 05:12 TR V2 kaydı | OBSERVED | sha256 `f1ff9f2a…` |
| 07:11 TR gözlem okuması | OBSERVED | `observation_v3`; 19 kamuya açık istek, 0 ret; sha256 `ec14e151…` |

Klasör: `C:\Users\W10\nb-benchmark\v2-forensics-2026-11-12`.

Araç: `python -m tools.v2_forensics --out DIR [--observe]`.

---

## A. NATGAS (arb.sh: long TxFlow / short tradeXYZ, 06:28)

### Bulgu: NATGAS keşfedildi, eşleşti, sıralamada 1. çıktı ve ekonomisi hesaplanmadan kapıda durdu

| Aşama | Sonuç | Kanıt |
|---|---|---|
| DISCOVERED | ✔ | 05:12 kaydında 3 satır: tradeXYZ `xyz:NATGAS`, TxFlow `119` (NATGAS-USDC), Variational `NATGAS-PERP` |
| MATCHED | ✔ Tier A | Grup `rwa:NATGAS`, MULTI_VENUE (tradexyz, txflow, variational). Çapa `perpCategories=commodities` ve TxFlow TradFi sekmesi |
| OBSERVED | ✔ | Oran, fiyat, defter: üçünde de var (Variational yalnız RFQ kotasyonu) |
| ECONOMIC_CANDIDATE | ✔ | `wide_paper.rank` funding sıralaması: **TxFlow→tradeXYZ skor 0.01116** (NATGAS'ın en iyisi) |
| EXECUTION_MODEL_CHECK | ✘ | `v2/gates.py` `Gate.static` |
| PAPER_ELIGIBLE | — | Ulaşılmadı |

Ret ayrıntıları (05:12, `funnel.trace`, iz `nb-18ae793f5eb215f9`):
- **İlk ret: `RWA_REFERENCE_UNVERIFIED`.** `fine_class = commodity`, `gates.REFERENCE_UNVERIFIED` içinde. Mekânlar aynı referans kontratı (Henry Hub vade ayı ya da roll kuralı) belirtmiyor.
- **Tüm retler (funding):** `RWA_REFERENCE_UNVERIFIED`, `SETTLEMENT_EVIDENCE_MISSING`. İkincisi yalnızca **long TxFlow bacağında**: TxFlow yalnız oran yayımlıyor. tradeXYZ ise `fundingHistory` veriyor (90 günün 2160/2160 saati okundu).
- **Yakınsama:** yalnız `RWA_REFERENCE_UNVERIFIED`. **Hibrit:** iki kod birden.
- Variational'lı çiftlere ayrıca `EXECUTION_MODEL_UNVERIFIED` (RFQ) eklenir.
- **NATGAS'ın 15 izinin tamamı NOT_EVALUATED.** Net hiç hesaplanmadı: sıfır değil, negatif değil; UNKNOWN.
- Denklik: `ECONOMICALLY_RELATED_BUT_NOT_IDENTICAL`. Güven: LOW.

### Neden "görünmedi"?

Kapı doğru çalıştı: RWA referansı ve settlement kanıtı gerçekten eksik. Ama sistem bunu hiçbir yerde göstermiyordu:
- panel yalnız ilk-ret sayılarını listeliyordu;
- "en yüksek tahmini net" tablosu yalnız ekonomisi hesaplanmış adayları içeriyordu.

Bu fazda eklenen huni ve radar NATGAS'ı izi, retleri ve potansiyel ekonomisiyle görünür yapıyor.

### Rakamlar

| | Ekran (06:28) | 05:12 OBSERVED |
|---|---|---|
| TxFlow funding /8s | −0.0121% | **−0.01213%** (−1.51614e-05/saat; aynı değer) |
| tradeXYZ funding /8s | +0.3332% | +0.3022% |
| Net carry /8s | +0.3453% | +0.3144% |
| Fiyatlar | 3.6340 / 3.6484 | 3.63 / 3.6402 |

Radar (05:12, gerçek defterler, yayımlanan oranlar; POTENTIAL, PAPER değil):

| Boyut | Gidiş-dönüş | Maliyet | Carry/saat | 24 saat net | Başa baş |
|---|---|---|---|---|---|
| $1k | $1.77 | 17.7 bp | $0.39 | +$7.66 | 4.5 s |
| $5k | $9.23 | 18.5 bp | $1.97 | +$37.9 | 4.7 s |
| $10k | $21.57 | 21.6 bp | $3.93 | +$72.7 | 5.5 s |

Ters yön (long tradeXYZ / short TxFlow) her boyutta negatif: RESEARCH_ONLY.

Bu sayılar **yayımlanan anlık oranla** hesaplandı. TxFlow tarafında ödenmiş funding kanıtı yok; tradeXYZ'nin 90 günlük geçmişi var.

### NATGAS'ı açmak için gerekenler

1. Her mekânın referans kontratını kendi belgesinden kaydetmek. Variational belgesi Henry Hub vadeli işlemini yazıyor; tradeXYZ ve TxFlow için UNKNOWN. Yalnızca yakınsama stratejisi bunu bekliyor.
2. Funding için TxFlow'da ödenmiş ödeme kanıtı (`userFundings`). Bu yalnız yetkili bir salt okunur test adresiyle yapılabilir; adres yok, bu yüzden **ENGELLİ**.

### İsim tuzağı

Hyperliquid native'de `GAS` var (kripto, emtia çapası yok); flx dex'inde `flx:GAS` (emtia). İkisi de NATGAS ile eşlenmez.

---

## B. INIT (arb.sh: long Variational / short Hyperliquid, 06:34–06:37)

### Bulgu: V2'de INIT için ikinci bir mekân yok; Hyperliquid native kapsam dışı

| Aşama | Sonuç | Kanıt |
|---|---|---|
| DISCOVERED | ✔ yalnız Variational | 05:12 V2 kaydı: INIT yalnızca Variational'da (`INIT-PERP`, ad "Initia"). Lighter RH, RISEx, Arcus, TxFlow, tradeXYZ listelemiyor (fikstürler tarandı) |
| MATCHED | ✘ **SINGLE_VENUE_ONLY** | Grup `unverified:variational:INIT-PERP` |
| ECONOMIC_CANDIDATE | ✘ NOT_IN_CANDIDATES | Satır yok, aday yok, iz yok |

Hyperliquid native, `v2/policy.LEGACY_OUT_OF_SCOPE` içinde. 8 mekânlı PAPER evreninde değil: **CONFIG_EXCLUDED**. NB_V2 ağ koruması native sorguları reddeder.

INIT çiftinin tüm retleri (ilk sırada):
1. `CONFIG_EXCLUDED` (Hyperliquid native: yalnız gözlem)
2. `SINGLE_VENUE_ONLY` (PAPER evreni içinde)
3. `IDENTITY_UNVERIFIED`: Variational INIT'in sınıf çapası yok (`asset_class unknown`); HL native kategori ya da ad yayımlamıyor. Elde yalnız fiyat uyumu var.
4. `EXECUTION_MODEL_UNVERIFIED` (Variational RFQ)
5. `SETTLEMENT_EVIDENCE_MISSING` (Variational yalnız oran yayımlıyor)
6. `INSUFFICIENT_LIQUIDITY` ($5k ve $10k'de)
7. Anomali `REGIME_SHIFT`

### Gözlem okuması (07:11 TR, `observation_v3`)

| | Ekran (06:34) | 07:11 OBSERVED |
|---|---|---|
| Variational /8s | −1.6975% | −1.2272% (−0.1534%/saat, 1 saatlik aralık) |
| Hyperliquid /8s | −1.2644% | −0.5346% (−0.06683%/saat) |
| Long Variational alır | +0.2122%/saat | +0.1534%/saat |
| Short Hyperliquid öder | 0.1581%/saat | 0.0668%/saat |
| Net carry /8s | +0.4331% | +0.6926% |

İşaret kuralı ekranla aynı: oran pozitifse long öder; long bacağın nakit akışı −oran, short bacağınki +oran. Birim testleri (`test_init_screenshot_sign`, `test_natgas_screenshot_sign`) ekran rakamlarını 4 ondalığa kadar tutturuyor.

**"90 günlük sekmede yalnız 55 gün" ifadesi Hyperliquid'den gelmiyor.** HL native INIT funding geçmişi:
- 90 gün için **2160/2160 saat, FULL**;
- 5 sayfa (500 satır sınırı);
- ilk kayıt okuma penceresinin başında (2026-08-14).

Variational geçmiş yayımlamıyor (`RATE_PUBLISHED`). 55 günün kaynağı arb.sh'ın kendi kaydı ya da Variational'ın listeleme tarihi olabilir; **UNKNOWN**.

**Rejim:** HL INIT'in saatlik funding ortalamaları:

| Pencere | Ortalama |
|---|---|
| 24 saat | −0.3208%/saat |
| 7 gün | −0.1185%/saat |
| 30 gün | −0.0279%/saat |
| 90 gün | −0.0085%/saat |

- En düşük değer −0.5%/saat.
- Son 24 saatin ortalaması, 90 günlük ortalamanın yaklaşık 38 katı. `REGIME_SHIFT` bayrağı bu durum için eklendi.
- Bugünkü taşıma, varlığın normal düzeyi değil.

Boyut merdiveni (long Variational RFQ kotasyonu / short HL defteri; RESEARCH_ONLY):

| Boyut | Sonuç |
|---|---|
| $1k | Gidiş-dönüş $5.76 (57.6 bp; Variational kotasyon spread'i ≈ 35 bp), carry $0.87/saat, başa baş 6.7 s. **POTENTIAL** |
| $5k | `INSUFFICIENT_LIQUIDITY`. HL defteri ±%0.1 bandı içinde $5k'yi taşımıyor (20 seviyede toplam bid ≈ $13.8k, ask ≈ $29.2k, ama band içi sığ) |
| $10k | `INSUFFICIENT_LIQUIDITY` |

Ekrandaki "thin at this size" uyarısı bizim veriyle doğrulanıyor. Ekrandaki "$188–190 gidiş-dönüş" değerinin hangi boyuta ait olduğu bilinmiyor; mutabakat yapılamadı (UNKNOWN).

### INIT'i açmak için gerekenler

1. Hyperliquid native'in PAPER evrenine alınması. Bu ayrı bir sahip incelemesi gerektirir ve **bu fazda yapılmadı**.
2. Variational için bir RFQ dolum modeli.
3. Variational için ödenmiş funding kanıtı.
4. Kimlik çapası.

Gözlem modu INIT'i artık radarında, araştırma etiketiyle gösteriyor.
