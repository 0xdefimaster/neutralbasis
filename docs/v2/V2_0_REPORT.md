# V2-0 raporu — baseline, benchmark ve muhasebe ayrımı

Tarih: 2026-11-12. Dal: `v2/baseline-benchmark` (yerel, push edilmedi). **PAPER ONLY.**

Ayrıntılar:
- [PILOT_BASELINE.md](PILOT_BASELINE.md)
- [BENCHMARK.md](BENCHMARK.md)
- [MASTER_PLAN.md](MASTER_PLAN.md)

Sonuç dosyaları: `C:\Users\W10\nb-benchmark\pilot-2026-11-10_12\results\` altında `baseline.json`, `benchmark.json`, `observer.json` ve `preflight.json`. Veri seti sha256 `5fb3f25a…510d`.

## 1. Üç soru

### Sistem fırsatları eksiksiz görebiliyor mu? — **Hayır.**

Kapsam genişledi. Son LOOSE durumu: 15 borsa taranıyor; 1.347 piyasa, 253 varlık, 2.352 çift. STRICT'te bu 662 / 131 / 503 idi. Bilinen kör noktalar:

- **Dış referansa göre eksik.** Loris'in herkese açık tablosu 45 borsa ve 1.950 sembol gösteriyor; masa 15 borsa tarıyor. arb.sh ile birebir karşılaştırma yapılamadı: iç API'yi yoklama isteği güvenlik sınıflandırıcısı tarafından engellendi. "Eksiksiz" iddiasını ölçecek bir referans yok (UNKNOWN).
- **Görülen ama değerlendirilemeyen borsalar:**

| Borsa | Neden değerlendirilemiyor |
|---|---|
| Paradex | Sürekli funding; modeli yok, yalnız araştırma |
| Nado | CCXT'de funding geçmişi yok |
| ApeX | Güncel funding oranı yok |
| Hibachi | Mark fiyatı yok; aralık çelişkisi var (CCXT 8 saat, belge saatlik) |
| TxFlow, Variational | Herkese açık ödeme geçmişi yok |

- **Kimlik ve eşleme boşlukları:**
  - kPEPE / 1000PEPE / PEPE eşlenmiyor (`marketdata/canonical.py` motor tarafından kullanılmıyor);
  - Variational'da QNT ve ANTHROPIC fiyatları diğer borsalardan %24–342 farklı (QNT 178'e karşı 40,3; ANTHROPIC 1.380'e karşı 1.824). Aynı sembol farklı enstrümanı gösteriyor olabilir.
- **Kesintiler:** 46 saatte 25 yeniden başlatma (STRICT 9, LOOSE 16). Kesinti anlarında hiçbir şey görülmedi.

### Bulduğu fırsatlar gerçekçi maliyetlerle kârlı mı? — **Funding: dar bir kümede ve ancak uzun tutulursa. Yakınsama: kanıt yetersiz.**

- **Ölçülen gidiş-dönüş maliyeti:** 11 iki bacaklı funding işleminde ücret + kayma medyanı **21,5 bp** (p10 18,8, p90 25,7). Modelin girişteki maliyet tahmini 16–20 bp; maliyeti biraz düşük tahmin ediyor.
- **Funding replay** (32 gün, 10 periyodik borsa):
  - Uygun çiftlerin çoğunun taşıması küçük: medyan 1–7 bp. 24 saatte maliyeti karşılayan pay %0,6, 168 saatte %13.
  - Her anın en iyi fırsatı maliyeti %24 (24 saat), %61 (72 saat), %78 (168 saat) oranında karşıladı.
  - Ama tahmini abartılı: 72–168 saatte gerçekleşen, tahminin %57–62'si. Medyan başa baş süresi 33–58 saat.
- **Pilot:**
  - Funding tahmini tuttu: beklenen $1,35, gerçekleşen $1,58.
  - Pozisyonlar başa baş süresinin (~90–230 saat) çok altında, ortalama ~8 saatte kapatıldı (10'u toplu kapatma). Sonuç −$21,13.
- **Yakınsama:**
  - 2 temiz işlem, +$1,10 (ortalama ~5,5 bp).
  - Gerçekleşen, tahminin %62'si.
  - n=2 ile kârlılık hakkında hüküm verilemez.
  - Önceki offline taramalar (2026-11-10) taker ücreti, gerçekçi defter maliyeti ve 1 saatlik giriş gecikmesiyle sağlam bir kâr bulamamıştı.

### Paper execution ve risk motoru bu fırsatları güvenilir simüle edebiliyor mu? — **Kısmen; muhasebe güvenilir, ölçüm koşulları değildi.**

Güvenilir olan:
- **Defter tutarlı:** 15 işlemin 15'inde SETTLED olaylarının toplamı işlemin gerçekleşen sonucuna eşit.
- **Ödeme durumları ayrılıyor:** 173 ödemenin 156'sı SETTLED, 17'si SETTLED_ESTIMATED_PRICE; NOT_RECORDED ve UNKNOWN 0. Bileşenler ayrık: fiyat, funding (gerçek ve tahmini), ücret, kayma.
- **Dolum simülasyonu:** defter yürüyerek IOC, %0,1 fiyat sınırı, borsanın ölçülen p90 gecikmesi ve +1 bp ek kayma.
- **Bacak kurtarma çalıştı:** iki olayda tek kalan bacak 12–16 saniyede kapatıldı.

Güvenilir olmayan veya eksik olan (V2-0'da düzeltilenler işaretli):
- Tek bacaklı kârlı işlem "kazanan" sayılıyordu. **Düzeltildi:** artık `LEG_RECOVERY` sınıfında ve kazanç sayılmıyor.
- Giriş kararı dönem sıfırlamasında kayboluyordu. **Düzeltildi:** her işlem `entry_economics` taşıyor.
- Başa baş süresi kontrol edilmiyordu; funding girişi fiyat yakalamasına dayanarak maliyetini aşabiliyordu. **Düzeltildi:** `wide_max_breakeven_hours`, varsayılan 24 saat, yalnız sıkılaştırır.
- Toplu ve elle kapatma, stratejinin kendi çıkışını ölçmeyi engelledi. Politika kararı sahibin (V2-1).
- 46 saatte 25 yeniden başlatma oldu. Motor durunca açık pozisyonlar veritabanında kalıyor (şu an 3); gerçekte bu süre boyunca risk açıktır. Simülasyon kesinti riskini modellemiyor.
- Simülasyonda olmayanlar: emir kuyruğu sırası, borsa reddi ve teminat transferi.
- Örnek çok küçük (15 işlem). Masanın kendi güven eşiği 30 gün ve 40 işlem.

## 2. Yapılan kod değişiklikleri (yalnız PAPER masası)

| Dosya | Değişiklik |
|---|---|
| `wide_paper.py` | `trade_outcome` / `outcome_of` / `is_win`. `LEG_RECOVERY` kazanç sayılmaz; `report()`, `period_report()`, `scorecard()` ve CSV bunu kullanır |
| `wide_paper.py` | `report()` bileşenleri: `settled_funding`, `estimated_funding`, `slippage`, `outcomes`, `leg_recovery`, `two_leg_net` |
| `wide_paper.py` | `funding_economics`: saatlik nakit akışı, giriş/çıkış maliyeti, risk payı, beklenen net nakit akışı, başa baş saati. `breakeven_refusal` kapısı hem değerlendirmede hem girişteki funding yenilemesinde uygulanır |
| `wide_paper.py` | Yeni ayarlar: `wide_max_breakeven_hours=24` (1–168), `wide_funding_realization=1.0` (0,05–1; yalnız düşürür) |
| `wide_paper.py` | Pozisyon ve işlem kaydında `entry_economics`, kapanışta `outcome` |
| `tools/v2_dataset.py` | Salt okunur, dondurulmuş, sha256'lı veri seti; üzerine yazmaz, E:'ye yazmaz |
| `tools/v2_evidence.py` | baseline, benchmark (funding replay ve işlem tahmini / sonuç), gözlemci karşılaştırması |
| `tools/v2_preflight.py` | Şema hash'i, satır sayıları, `quick_check` (salt okunur), git bundle doğrulaması, arızalı disk için yalnız `stat` |
| `test_v2_accounting.py`, `test_v2_evidence.py` | 14 yeni test (MOCK veriler etiketli) |

Davranış değişikliği:
- Yeni başa baş kapısı, maliyetini 24 saatte kendi funding'iyle geri ödeyemeyen funding girişlerini reddeder.
- LOOSE pilotun 12 funding işleminin hiçbiri bu kapıdan geçemezdi.
- Kapı yalnız sıkılaştırır. Başka hiçbir kapı gevşetilmedi.

## 3. Sahibin kararları (V2-1 öncesi)

1. `wide_max_breakeven_hours`: 24 saat (çok az işlem) mi, yoksa 48–72 saat ve pozisyonları başa baş süresine kadar tutma politikası mı?
2. `wide_funding_realization`: benchmark'a göre üst sıradaki fırsatlar için ~0,6.
3. Ayar ya da dönem değişikliğinde açık funding pozisyonlarının toplu kapatılması devam etsin mi? Bu, ölçümü bozuyor.
4. "Yeni yedi borsa" hangileri? (UNKNOWN)
5. Çalıştırma yeri: E: (arızalı), Contabo ya da yeni bir disk (§5).

## 4. TxFlow ve Variational gözlemcisi ile adaptör karşılaştırması

**Kod düzeyinde bulgu (V).** Gözlemci ile motorun adaptörleri aynı uç noktaları ve aynı alanları okuyor:

- TxFlow:
  - `POST rpc.txflow.com/info`, `activeAssetCtx`;
  - oran `nodeCtx.funding`, yüzde cinsinden; adaptör 100'e bölüyor;
  - aralık `nextFundingTime − prevFundingTime`.
- Variational:
  - `/metadata/v2/stats`;
  - ikisi de `variational_public.parse` ile ayrıştırıyor.

Bu yüzden karşılaştırma bağımsız bir doğruluk kanıtı olamaz; yalnız tutarlılık ve akla yatkınlık gösterir. İki borsa da herkese açık ödeme (settlement) kaydı yayımlamıyor. Gerçekleşen ödeme bu verilerle **doğrulanamaz (UNVERIFIED)**.

| | TxFlow | Variational |
|---|---|---|
| Satır / piyasa | 3.368 / 242 | 4.576 / 572 |
| Birim | yüzde / ödeme, aralık hep 1 saat | kesir / aralık; aralık 1–8 saat (medyan 4) |
| Başarısız tur | 0/14 (20 piyasa hatası) | **6/14**, hepsi HTTP 429 |
| Mark ve referans farkı | Pilotun kendi TxFlow mark'ı ile, ±60 sn: medyan 2,2 bp, p90 12,3 bp, en çok 224 bp | Extended / Hyperliquid / Lighter ile: medyan 8,8 bp, p90 28,8 bp. **QNT 34.183 bp, ANTHROPIC 2.436 bp** (kimlik uyuşmazlığı) |
| Saatlik oran ve Hyperliquid | 1.313 eş: aynı işaret %74, korelasyon 0,34 | 1.104 eş: aynı işaret %83, korelasyon 0,87 |

**Karar.** Gözlemci verisi `OBSERVED_PUBLISHED` olarak kalır. İşlem uygunluğunda doğrudan kullanılmaz. Uygunluk için gerekenler:

1. Varlık kimliği fiyatla doğrulanmalı; QNT ve ANTHROPIC gibi uyuşmayanlar engellenmeli.
2. En az 7 gün kesintisiz gözlem olmalı.
3. Variational okumaları motorla aynı IP'den çakışmamalı (429).
4. Settlement kaynağı belgeden doğrulanmalı. Herkese açık bir kaynak yoksa uygunluk ancak sahibin açık PAPER kararıyla olabilir; `wide_txflow_paper` bunun örneği.

## 5. Veri koruma ve migrasyon ön kontrolü

`tools/v2_preflight.py` 2026-11-12'de salt okunur çalıştırıldı. Sonuç: `results/preflight.json`.

| Veri | Boyut | WAL | Bütünlük (`quick_check`) | Şema hash'i | Yedek |
|---|---|---|---|---|---|
| STRICT pilot DB (C:) | 5,51 GB | 42 MB | **ok** (249 sn) | 32f61967… | **Yok**, tek kopya |
| LOOSE pilot DB (C:) | 5,67 GB | 6 MB | **ok** (181 sn) | 32f61967… (aynı) | **Yok**, tek kopya |
| Gözlemci DB (C:) | 1,3 MB | — | **ok** | d43af7f8… | Veri setinin içinde |
| Dondurulmuş veri seti (C:) | 81,6 MB | — | **ok** | 0db4767f… | Tek kopya, sha256 manifest'te |
| **E: ana motor DB** | **26,47 GB** | **3,08 GB** | **Açılmadı (UNKNOWN)** | — | **Hiçbir yerde yok** |

E: ana motor DB'si yalnız `stat` ile ölçüldü: son yazma 2026-11-10 07:40. Bozuk bloklu bir diskte 26 GB'lık tam okuma başlı başına risk olduğu için dosya açılmadı.

**Git:**
- `neutralbasis-latest.bundle` ve `neutralbasis-all.bundle` için `git bundle verify` ok; ikisi de tam geçmiş içeriyor.
- GitHub `main` = `local/audit-fixes-2026-11-09` = e8a0e7b.
- E-posta yeniden yazımından önceki geçmiş yerelde `backup/pre-email-rewrite-2026-11-12` dalında.
- V2-0 işi `v2/baseline-benchmark` dalında, yalnız yerel commit.

**Migrasyon:** V2-0 hiçbir şemayı değiştirmedi. Yeni alanlar (`outcome`, `entry_economics`) işlem gövdesine (JSON) yazılıyor; `ALTER` yok.

**E: diski** (System log, disk event 7, Harddisk1 bozuk blok):

| Gün | Olay |
|---|---|
| 2026-11-08 | 1.553 |
| 2026-11-09 | 6.734 |
| 2026-11-10 | 13.842 |
| 2026-11-11 | 2.134 |
| 2026-11-12 (02:32'ye kadar) | 180 |

Düşüş, disk boşta kaldığı için; disk iyileşmedi. 2026-11-10'daki temiz olmayan kapanış (Kernel-Power 41) bu diskle başladı. Windows diski hâlâ "Healthy" gösteriyor; bu göstergeye güvenilmez.

**Önerilen sıra.** Sahip E:'de çalıştırmaya karar verirse:

1. E: ana motor DB'si (26,5 GB + 3,1 GB WAL + shm) önce **E: dışına** kopyalanmalı: harici disk ya da Contabo. Kopyada `quick_check` çalıştırılmalı. Orijinal yazma kipinde açılmamalı: açılışta WAL checkpoint'i bozuk diske yazar.
2. C:'deki iki pilot DB (11,2 GB) de E: dışına ikinci bir kopya olarak alınmalı. C:'de yer yok.
3. Motor E:'de yeni ve boş bir veri klasörüyle başlamalı; eski DB'ler yeniden kullanılmamalı.
4. Küçük veriler (veri seti, ayarlar, bundle) günlük olarak C:'ye yedeklenmeli.
5. Event 7 sayısı izlenmeli; artış görülürse durdurulmalı.

Yine de **önerilen yer E: değil**: Contabo ya da yeni bir disk.
