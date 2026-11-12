# V2 ekonomik değerlendirme benchmark'ı

PAPER / araştırma. Ağ yok, motor yok. Yalnız dondurulmuş veri seti okunur.

## Tekrar oynatma

```
python -m tools.v2_dataset --out C:\Users\W10\nb-benchmark\pilot-2026-11-10_12
python -m tools.v2_evidence all --dataset C:\Users\W10\nb-benchmark\pilot-2026-11-10_12
```

`v2_dataset`:
- kaynak DB'leri salt okunur açar (`mode=ro`);
- tek bir SQLite dosyası ve `manifest.json` yazar;
- manifest'te veri setinin sha256'sı ile her kaynağın boyutu ve mtime'ı bulunur;
- var olan veri setinin üzerine yazmaz;
- E:'ye yazmayı reddeder.

`v2_evidence` yalnız bu dosyayı okur. Aynı veri seti her zaman aynı sonucu verir: sıralı yineleme kullanır, rastgelelik yok. Her sonuç dosyası `dataset_sha256` taşır.

Bu teslimin veri seti şunlardan oluşur:
- sha256 `5fb3f25aa01ad2a3fdf79251be16ebebdcb6f9c76872e6cfb10925b27711510d`, 81,6 MB;
- kaynaklar: iki pilot DB ve gözlemci DB;
- funding geçmişi 2026-10-11 → 2026-11-12.

Etiketler:

| Veri | Etiket |
|---|---|
| İşlemler, olaylar, defter | SIMULATED |
| Funding ödemeleri ve fiyatlar | OBSERVED public market data |
| Gözlemci satırları | OBSERVED_PUBLISHED |

## (a) Funding tahmini replay'i

**Ne ölçülür.** Masanın girişteki funding tahmini (`assess_funding`) her 6 saatte bir, her varlıkta, iki borsalı her yön için yeniden kurulur:
- bacak başına muhafazakâr oran: long, son ödeme ile 24 saatlik ortalamanın kötüsünü öder; short, iyisini değil kötüsünü alır;
- `day1` ve `day3`, tahmin = `min(day1, day3)`;
- kalıcılık kapıları: 1g > 0, 3g > 0, 7g > 0, avantaj tek bir güne dayanmıyor;
- ödeme kapsamı ≥ %95 (motordaki sabitlerle aynı).

Bu tahmin, sonraki 24, 72 ve 168 saatte gerçekten yayımlanan ödemelerle karşılaştırılır.

**Yaklaşım.**
- Canlı kotasyon kayıtta yok; yerine son yayımlanan ödeme kullanılır (APPROXIMATION).
- Yalnız fiyat dışı (funding) tahmini ölçülür. Fiyat istatistiği kapıları uygulanmaz.

**Dışarıda kalanlar.** `capabilities.FUNDING_MODELS` içinde PERIODIC olmayan borsalar dışarıda kalır:
- Paradex: CONTINUOUS; kayıtları 5 saniyelik oran örneği, ödeme değil.
- Arcus.

**Kullanılan borsalar:** ApeX, Aster, Extended, Hibachi, Hyperliquid, Lighter, LighterCore, Pacifica, RISEx, tradeXYZ.

**Ölçülen gidiş-dönüş maliyeti.** Pilotun 11 iki bacaklı funding işleminde ücret + kayma:
- medyan **21,5 bp**;
- p10 18,8 bp, p90 25,7 bp.

**Sonuçlar** (DERIVED; örnekler zamanda örtüşür, bağımsız değildir, ~32 günlük veri):

| Ufuk | Küme | n | Gerçekleşen / tahmin | Gerçekleşen > 0 | Gerçekleşen ≥ tahmin | Maliyeti karşılayan |
|---|---|---|---|---|---|---|
| 24 sa | tüm uygun | 23.697 | 1,41 | %87 | %64 | %0,6 |
| 24 sa | her anın en iyisi | 96 | 0,91 | %90 | %45 | %24 |
| 72 sa | tüm uygun | 21.438 | 1,18 | %84 | %58 | %4,5 |
| 72 sa | her anın en iyisi | 88 | **0,62** | %93 | %21 | **%61** (tahmin %97 diyordu) |
| 168 sa | tüm uygun | 17.790 | 0,95 | %82 | %52 | %13 |
| 168 sa | her anın en iyisi | 72 | **0,57** | %88 | %18 | **%78** (tahmin %100 diyordu) |

Her anın en iyi fırsatının, gerçekleşen saatlik funding'iyle 21,5 bp'yi geri ödeme süresi:

| Ufuk | Medyan | p10 | p90 |
|---|---|---|---|
| 24 sa | 33 saat | 12 saat | 155 saat |
| 72 sa | 58 saat | 16 saat | 194 saat |
| 168 sa | 57 saat | 14 saat | 190 saat |

**Okuma.**
1. Ortalama bir uygun çift tahminini tutturuyor, hatta aşıyor (oran 0,95–1,41). Ama taşıma çok küçük: medyan 1–7 bp. Maliyeti karşılayan pay %1–13.
2. Masanın seçeceği en yüksek tahminler abartılı (seçim yanlılığı, ortalamaya dönüş). 72–168 saatte tahminin yalnız %57–62'si gerçekleşiyor.
3. Maliyeti karşılayabilen tek grup en iyi fırsatlar. Onlar da ancak medyanda 2–2,5 gün tutulursa karşılıyor.

## (b) Pilot işlemleri: tahmin ve sonuç

| | n | Tahmin (açılış) | Gerçekleşen net | Not |
|---|---|---|---|---|
| Funding, iki bacaklı | 11 | +$3,54 (168 saatlik ufukta) | −$21,12 | Funding tahmini tuttu, maliyet tutmadı |
| Yakınsama, iki bacaklı | 2 | +$1,77 | +$1,10 | Gerçekleşen, beklenenin %62'si; kayma $2,11, ücret $2,12 |

Funding işlemlerinin gerçek tutma sürelerinde:
- beklenen funding $1,35, gerçekleşen $1,58 (oran 1,18);
- zarar funding'den değil, gidiş-dönüş maliyetinden geldi: 11 işlemde ücret $12,48 + kayma ~$9.

İşlemler ortalama ~8 saatte kapatıldı; 10'u toplu kapatmayla. Oysa başa baş süreleri, açılıştaki beklenen 168 saatlik funding'e göre ~90–230 saatti.

`wide_max_breakeven_hours=24` (yeni varsayılan) ile bu 12 işlemin hiçbiri açılmazdı. Örnek, açık kalan BTC kararı:
- maliyet $1,63 + risk payı $0,27;
- saatlik funding $0,0136;
- başa baş süresi 140 saat.

Model maliyeti hafif düşük tahmin ediyor. Açık kalan 3 kararda gidiş-dönüş tahmini 16–20 bp; gerçekleşen medyan 21,5 bp.

## Sahibin kararına bırakılanlar (V2-1)

- `wide_funding_realization`: bu veriye göre üst sıradaki fırsatlar için ~0,6. Şu an 1,0, yani değişiklik yok.
- `wide_max_breakeven_hours`:
  - 24 saat, en iyi fırsatların çoğunu da dışarıda bırakır (medyan başa baş 33–58 saat);
  - 72 saat, ancak pozisyonlar başa baş süresine kadar tutulursa anlamlıdır.
- Toplu ve elle kapatma: bir dönem ya da ayar değişikliği açık funding pozisyonlarını kapatıyor. Bu, stratejinin ölçülmesini engelledi.
