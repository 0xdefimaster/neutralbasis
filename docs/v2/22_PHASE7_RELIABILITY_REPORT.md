# 22 — Phase 7: PAPER güvenilirliği ve doğrulama

Tarih: 2026-11-12. Dal: `v2/venue-policy`, `2cafe02` üzerine (yerel; push, merge ve deploy yok). **PAPER ONLY.** Motor, pilot ve kaydedici **başlatılmadı**; E:'ye dokunulmadı. Risk ayarları değişmedi (24 saat, 1,0); kapsam ve kurallar aynı.

**Yöntem:** önce hataları gösteren karakterizasyon testleri yazıldı (`test_v2_phase7.py`). Düzeltmelerden önceki çalıştırmada **13 testin 13'ü de başarısızdı** (7 yanlış sonuç, 6 eksik davranış). Sonra kod düzeltildi ve testler yeniden çalıştırıldı.

## Görev 1 — Hibrit kurtarma ve bakiye güvenliği

**Kök neden:**
- `_recover()`, defter bacağın tamamını almasa bile durumu `LEG_RECOVERY` yapıyordu. Açık miktar kalan bir pozisyon "kurtarıldı" görünüyordu.
- `_unwind()`, defter yoksa `None["asks"]` ile çöküyordu. Kısmi unwind'da fazla miktar sessizce kalıyor ve durum `HEDGED_OPEN` oluyordu (gizli tek yönlü risk).
- `_close()` / `_close_leg()`, defter okuma hatasında döngüyü düşürüyordu. Bacaklar farklı miktarda kapanırsa dengesizlik kaydedilmiyordu.
- Kaydedilen dengesiz miktarı raporlayan bir alan yoktu.

**Değişiklik:** `v2/hybrid_desk.py`.
- Yeni durumlar:
  - `PARTIAL_EXPOSURE`: bacaklar eşit değil; fazla her turda yeniden unwind edilir.
  - `RECOVERY_PENDING`: başarısız giriş düzleştiriliyor.
- Hiçbir bacakta miktar kalmadıkça pozisyon `CLOSED` ya da `LEG_RECOVERY` olmaz.
- Tüm defter okumaları `_book()` üzerinden geçer; hata ya da eksik defter pozisyonu bekletir, çökertmez. Hata pozisyona kaydedilir.
- `exposure(p)` ve `risk()` açık ve dengesiz miktarı raporlar. Kapanışta önce fazla, sonra iki bacak aynı miktarla kapatılır; tek bacak hiçbir zaman yalnız kapanmaz.
- Yeniden başlatmada her ara durum kaldığı yerden devam eder.
- **Windows'a özgü ek hata:** `os.replace`, dosyayı başka bir süreç (antivirüs ya da dizinleyici) tutarken `PermissionError` (WinError 5) veriyor ve durum dosyası yazımı düşüyordu. Test sırasında gerçekten oluştu. `v2/ops.atomic_write` / `replace_retry` yeniden deniyor; deneme hakkı biterse önceki dosya bozulmadan kalıyor ve hata yükseltiliyor. Kaydedicinin segment mühürleme adımı da aynı yolu kullanıyor.

| Test | Senaryo | Sonuç |
|---|---|---|
| `test_recovery_on_a_thin_book_stays_pending_with_its_residual` | İkinci bacak dolmuyor, birinci bacağın defteri sığ | RECOVERY_PENDING, kalan miktar ve risk raporda; likidite dönünce LEG_RECOVERY, risk 0 |
| `test_unwind_without_a_book_keeps_the_exposure_instead_of_crashing` | Unwind sırasında defter yok | PARTIAL_EXPOSURE; sonraki turda HEDGED_OPEN, risk 0 |
| `test_book_errors_do_not_break_the_cycle_or_close_anything` | Defter okuması hata veriyor | EXIT_PENDING'de kalıyor; döngü düşmüyor |
| `test_uneven_close_records_the_imbalance` | Kısa bacağın defteri yarım | İki bacak eşit miktarda kapanıyor; risk 0 |
| `test_restart_keeps_a_pending_recovery` | Yeniden başlatma | Durum korunuyor; `risk()` açık riski gösteriyor |
| `AtomicWriteTests` | Geçici ve kalıcı WinError 5 | Geçici olan yeniden deneniyor; kalıcıda önceki dosya bozulmuyor |

**Kalan risk:** kurtarma işlemi PAPER'da defter likiditesine bağlı. Gerçek piyasada likidite uzun süre dönmezse pozisyon RECOVERY_PENDING'de kalır. Dashboard ve `risk()` bunu gösteriyor; insan müdahalesi kuralı runbook'ta.

## Görev 2 — Seçici veri kaydedici

**Kök neden:** kaydedici yalnız strateji satırlarını yazıyordu. Ham defter ve funding yanıtları, API hataları ve kaynak bilgisi yoktu. Tekrar oynatma aracı yoktu, yani bozuk ya da eksik segment fark edilemezdi.

**Değişiklik:**
- `v2/collector.py`: `call()`, her ham yanıtı ve her API hatasını kaydediciye aktarır. Kaynak bilgisi: borsa, metot, URL, gövde hash'i, alınma zamanı, gecikme.
- `v2/recorder.py`: `sink()` borsa düzeyindeki kayıtları yalnız 8 V2 borsası için kabul eder. `record()` pazar kimliği, sequence ve kaynak alanlarını saklar.
- `v2/marketdata.py`: TxFlow akışı her snapshot ve delta çerçevesini seq, prevSeq, kaynak zamanı ve bütünlük sonucuyla (ok / resync / ignored) yazar.
- `v2/runtime.py`: yukarıdaki kancaları bağlar.
- `v2/replay.py` (yeni):
  - `verify()` her segmentin varlığını, sha256'sını, gzip ve JSON okunabilirliğini ve satır sayısını kontrol eder;
  - manifest dışı, açık ve "recovered" segmentleri ve delta sequence boşluklarını raporlar;
  - `iterate()` sorun varsa `ReplayError` fırlatır.

**Testler:** `test_collector_records_raw_answers_and_errors`, `test_replay_refuses_corrupt_and_missing_segments`. Bozuk segment `corrupt`, silinen segment `missing` olarak yakalanıyor ve okuma reddediliyor.

**Veri türleri (dürüst durum):**

| Veri türü | Kaydediliyor mu |
|---|---|
| L2 snapshot | ✅ V2 satırlarının REST defter yanıtları ham olarak (Lighter RH, Arcus, TxFlow, tradeXYZ). RISEx'in satırı yok (ücret UNKNOWN), bu yüzden defteri de çekilmiyor |
| L2 delta | ⚠️ Yalnız TxFlow WS (varsayılan kapalı). Diğer borsaların WS mesaj biçimleri belgeden okunmadı: elde edilemiyor |
| Public trades | ❌ **Kaydedilmiyor.** Belgede var (TxFlow `trades`, RISEx `trade-history`, Arcus `/v1/trades`, Lighter RH `recentTrades`), ama toplayıcıya bağlanmadı. Lighter RH'de ağırlık 600 ve dakikada 60 istek limiti var; bütçe planı gerekiyor |
| Mark / index | ✅ Metadata yanıtları ve satırlar |
| Yayımlanan funding | ✅ Metadata yanıtları ve satırlar |
| Settled funding | ✅ Lighter RH, RISEx, Arcus, tradeXYZ geçmiş yanıtları (talebe bağlı). ❌ TxFlow ve Variational yayımlamıyor |
| Variational kotası | ✅ Ham `/stats` yanıtı (RFQ; defter değil) |
| API hataları | ✅ `api_error` |

## Görev 3 — Funding muhasebesi

**Kök neden:**
- Hibrit masa, ödemeyi giriş fiyatıyla değerleyip `funding` (gerçekleşmiş) olarak yazıyordu.
- Ödeme anahtarı `(bacak, ts)` olduğu için aynı saniyedeki farklı ödemeler birleşiyordu.
- Kapanıştan sonraki uzlaştırma bacak miktarını 0 görüyordu. Bu hata, düzeltme sırasında yeni bir testle bulundu.

**Değişiklik:**
- `v2/funding_accounting.py` (yeni):
  - borsa başına uzlaşma fiyat tabanı;
  - yalnız borsanın kendi uzlaşma fiyatını taşıyan ödeme `funding` sayılır; giriş fiyatıyla değerleme `funding_estimated` olarak gerçekleşmiş sonuçtan ayrı tutulur;
  - ödeme kimliği `(bacak, zaman, oran)`.
- `HybridDesk.pnl()`: `net` yalnız gerçekleşenleri içerir; `net_with_estimates` ayrı.
- Ödeme, ödeme anındaki miktarla değerlenir (bacak miktar geçmişi).
- **RISEx uzlaşma fiyatı canlı veriyle doğrulandı:** `accumulated_funding`, ardışık 99 kaydın 99'unda `oran × index_price` kadar değişiyor (göreli hata < 2·10⁻¹²). RISEx ödemeleri bu yüzden kendi `index_price` değeriyle gerçekleşmiş sayılıyor (`v2/venues/risex.py`).
- Lighter RH, Arcus ve tradeXYZ geçmişinde ödeme anı fiyatı yok; bu borsalarda ödemeler **tahmini** kalıyor.

**Testler:**

| Test | Doğruladığı |
|---|---|
| `test_entry_price_valuation_is_estimated_not_realized` | Giriş fiyatıyla değerleme tahmin olarak yazılıyor |
| `test_payment_with_its_settlement_price_is_realized` | Uzlaşma fiyatlı ödeme gerçekleşmiş sayılıyor |
| `test_distinct_payments_at_the_same_second_are_not_merged` | Aynı saniyede 3 satır, 2 farklı ödeme olarak kalıyor |
| `test_payments_after_close_use_the_size_held_at_payment_time` | Kapanış sonrası ödeme, ödeme anındaki miktarla değerleniyor |
| `test_risex_rows_carry_their_settlement_index_price` | RISEx satırları uzlaşma fiyatını taşıyor |

`test_v2_phase5`'teki bir beklenti eski (hatalı) davranışı kodluyordu; `funding_estimated` olarak düzeltildi.

**Kalan risk:** Lighter RH, Arcus ve tradeXYZ ödemeleri, ödeme anı fiyatı kaydedilmediği için tahmin. Kaydedicinin mark / index / oracle zaman serisiyle değerlenebilirler; bu henüz yazılmadı.

## Görev 4 — Maliyet doğrulaması

**Kök neden:** çıkış maliyeti bugünkü defterden hesaplanıp kesinmiş gibi kullanılıyordu. Stres senaryosu yoktu. RFQ kotası net hesap motoruna doğrudan verilirse dolum gibi fiyatlanırdı; yalnız kapı engelliyordu.

**Değişiklik:**
- `v2/economics.py`:
  - giriş ve çıkış maliyeti ayrı (`entry_cost`, `exit_cost`);
  - `exit_basis = "current book (not guaranteed at exit)"`;
  - `rfq_indicative` modelli defter reddediliyor (`EXECUTION_MODEL_UNVERIFIED`; ikinci savunma hattı);
  - `stress()` senaryoları: base, exit_spread_x3, slippage_+5bp, funding_-50%, second_leg_half_filled, combined_moderate.
- `v2/strategies/common.add_stress`: üç strateji senaryoları raporluyor. `combined_moderate` (çıkış spread'i ×2, dolum başına +2 bp, funding −%25) negatifse aday `STRESS_NEGATIVE` (maliyet) ile reddediliyor. Bu yalnız sıkılaştıran yeni bir kontrol; korunan iki risk ayarı değişmedi.

**Testler:** `test_entry_exit_apart_and_stress_scenarios`, `test_rfq_quote_is_not_a_fill`.

**Kalan risk:** stres katsayıları muhakemeye dayanıyor, ölçüme değil. Gerçek çıkış dağılımı, kaydedicinin uzun süreli verisiyle kalibre edilmeli.

## Görev 5 — Üç stratejinin uçtan uca testleri

`test_v2_phase7.StrategyEndToEnd` (`WidePaper` + V2 kapısı):

| Test | Senaryo |
|---|---|
| Kapı engeli | Kapı bir neden döndürünce giriş yok |
| Funding | Açılış (iki bacak eşit) → yeniden başlatma → funding'in tersine dönmesiyle kapanış (STRATEGY_EXIT) → net = fiyat + funding − ücret → defter uzlaşması |
| Funding risk durdurması | Kayıp limiti → RISK_EXIT, uzlaşma |
| Yakınsama | Açılış → yeniden başlatma → kesişmede kârlı kapanış → uzlaşma |

Hibrit için `HybridRecoveryDefects` ve `test_v2_phase5.HybridDeskTests`:
- açılış, takip ve funding;
- yakınsama + funding'in tersine dönmesi aynı turda (tek kapanış);
- STOP önceliği;
- kısmi dolum;
- LEG_RECOVERY;
- EXIT_PENDING sırasında yeniden başlatma.

## Görev 6 — Süreklilik ve performans: OFFLINE HIZLANDIRILMIŞ STRES

> **Bu gerçek zamanlı 72 saatlik bir çalışma değildir.** Sentetik (MOCK) bir piyasada, simüle edilmiş saatle, gerçek V2 runtime, hibrit masa, kaydedici, tekrar oynatma ve güvenlik kontrolleri çalıştırıldı. `tools/v2_stress.py`, çıktı `C:\Users\W10\nb-benchmark\v2-stress-2026-11-12\stress_report.json`.

72 simüle saat, 1 dakika adım, 4.320 tur; gerçek süre yaklaşık 1 dakika:

| Ölçüm | Sonuç |
|---|---|
| Hata dönen tur (yakalandı) | 90 (kesinti 60, 429 30) |
| Runtime'dan kaçan istisna | **0** |
| Ani çökme ve yeniden başlatma | 1 (yarım segment "recovered" mühürlendi) |
| WAL sınırı nedeniyle durdurulan giriş turu | 60 |
| Disk dolu penceresinde reddedilen kayıt | 240; boşluk kaydı 92 |
| Hibrit pozisyonlar | 53 CLOSED, 2 HEDGED_OPEN |
| Defter uzlaşması | Hepsi tutarlı |
| Açık dengesiz risk | Yok |
| Tekrar oynatma bütünlüğü | Tamam (15.960 kayıt) |

Daha kısa bir senaryo (24 saat, 5 dakika adım, aynı arızalar) `AcceleratedStress` testinde.

**Kalan risk:** sentetik piyasa gerçek değil. Stres kapısı (`StressGate`) MOCK bir değerlendiricidir; gerçek V2 kapısı canlı veride ayrıca doğrulanmalı. Bellek ve CPU profili çıkarılmadı.

## Görev 7 — Başlatma ön kontrolü

**Kök neden:** V2 pilotunun başlamasını sağlıklı disk, yeterli alan ve yedek kanıtına bağlayan bir kod kontrolü yoktu. Önceki `preflight` 5 GB'ı yeterli sayıyordu.

**Değişiklik:**
- `v2/ops.launch_check()`:
  - D: ve E: (bozuk TOSHIBA diski) reddedilir;
  - en az 50 GB boş alan;
  - en çok 7 günlük `BACKUP_EVIDENCE.json` (farklı ve sağlıklı hedef sürücü, sha256 manifesti).
- `pilot/run_pilot.py`: `NB_V2=1` iken her şeyden önce bu kontrol çalışır, başarısızsa başlatma reddedilir.
- Runbook §7 güncellendi.

**Testler:**
- `test_launch_check`: 12 GB → ret, yedek yok → ret, E: → ret, D: → ret.
- `test_run_pilot_refuses_without_launch_evidence_under_v2`: gerçek alt süreç, ağ korumasından önce ret.

**Durum:** bugün C:'de yaklaşık 12 GB boş alan ve yedek kanıtı yok. **Canlı V2 PAPER pilotu bu koşullarda kod tarafından başlatılamaz.** E:'deki orijinal veritabanına dokunulmadı.

## Testler

Önce ve sonra:
- düzeltme öncesi: karakterizasyon testlerinin 13/13'ü başarısız;
- düzeltme sonrası: `test_v2_phase7.py` 22/22 başarılı.

Tam paket (`python -m unittest discover -p "test_*.py"`): **908 OK, 2 atlanan, 0 başarısız** (Phase 6 sonunda 886).

## Gerçek pilot öncesi kalan riskler (özet)

1. Sağlıklı disk ve yedek kanıtı yok (görev 7 başlatmayı engelliyor).
2. Public trades kaydedilmiyor; L2 delta yalnız TxFlow'da (kapalı).
3. Lighter RH, Arcus ve tradeXYZ funding ödemeleri tahmin (ödeme anı fiyatı yok).
4. Stres katsayıları ölçüme dayanmıyor; stres testi MOCK piyasada.
5. Canlıda hiç çalışmamış yollar: V2 döngüsü, TxFlow WS, kaydedicinin uzun süreli yazımı, hibrit masa.
6. Bugünkü kanıtla paper'a uygun fırsat 0 (Phase 4–6).
