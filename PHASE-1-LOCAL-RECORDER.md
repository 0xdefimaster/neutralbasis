# Phase 1 — Yerel Piyasa Kanıtı Kaydedici

Phase 0.7'deki piyasa düzeyinde kanıt kurallarını gerçek borsa verisiyle doğrulamak için bilgisayarında çalışan, bağımsız ve **salt okunur** bir kayıt aracı. Bu bir trading botu değildir:
- emir göndermez;
- hesap, bakiye veya özel endpoint okumaz;
- API anahtarı istemez;
- paper motorunu başlatmaz;
- motorun veritabanına (`data-five-rh/paper.sqlite3`) yazmaz.

Kendi klasörüne yazar: `phase1-evidence/`.

## Durum: kod hazır, gerçek veri henüz yok
- **Kod:** yazıldı ve test edildi. Testler sahte (mock) yanıtlarla çalışır; bu yanıtlar piyasa kanıtı sayılmaz.
- **Gerçek veri:** bu kod geliştirilirken bulut ortamından borsa API'lerine bağlanılamadı (proxy 403). Henüz **hiçbir gerçek veri toplanmadı.** İlk gerçek bağlantı senin bilgisayarında `Phase1-Kayit.cmd --once` ile denenecek.
- **Borsa belgeleri:** bu ortamdan okunamadı. Funding kuralları "bağlayıcı beyanı" (DECLARED) olarak işaretlidir; resmî belge doğrulaması ayrıca yapılmalıdır.

## Borsalar ve endpoint'ler
Endpoint'ler projede zaten kullanılan çağrılardan alındı; yeni endpoint uydurulmadı. Hepsi public ve kimlik doğrulamasız.

| Kod | Borsa | Host | Kullanılan çağrılar | Kaynak |
|---|---|---|---|---|
| `Hyperliquid` | Hyperliquid | `api.hyperliquid.xyz/info` (POST) | `metaAndAssetCtxs`, `fundingHistory`, `candleSnapshot`, `l2Book` | CCXT hyperliquid adaptörünün gönderdiği istekler |
| `LighterCore` | Lighter Core | `mainnet.zklighter.elliot.ai/api/v1/` | `orderBookDetails`, `funding-rates`, `fundings`, `candles`, `orderBookOrders` | `bot.Feed`, CCXT lighter (`funding-rates`) |
| `Lighter` | Lighter RH | `api.rh.lighter.xyz/api/v1/` | aynı Lighter API | `risk_scan.FiveFeed`, `bot.Feed` |
| `Extended` | Extended | `api.starknet.extended.exchange/api/v1/` | `info/markets`, `info/<piyasa>/funding`, `info/candles/<piyasa>/trades`, `info/markets/<piyasa>/orderbook` | `bot.Feed` |

Notlar:
- **Hyperliquid POST kullanır:** public verisini yalnızca `POST /info` ile verir. Kayıt aracı yalnızca yukarıdaki dört okuma türüne izin verir; başka bir tür (ör. hesap durumu) kodda reddedilir.
- **Lighter Core ve Lighter RH ayrı borsalardır:** ayrı host, ayrı piyasa kimliği, ayrı klasör (`raw/lighter_core`, `raw/lighter_rh`). Aynı Lighter API yazılımını kullandıkları için aynı istemci sınıfı iki ayrı örnekle çalışır.
- **Lighter `funding-rates`:** başka borsaların oranlarını da listeler; yalnızca `exchange == "lighter"` kayıtları alınır. Bu oranın birimi belgelenmemiştir: ham saklanır, birimi `UNKNOWN`.
- **Lighter gerçekleşmiş funding:** `direction` alanı ham olarak saklanır. Projenin okuması (yüzde değer, long → +, short → −) yanına DECLARED olarak yazılır.
- **Extended ve Hyperliquid:** gerçekleşmiş ödemede yön alanı yok. Yön türetilmez, `UNKNOWN` kalır.

## Windows'ta kurulum ve çalıştırma
Mevcut `.venv` yeterli; yeni bağımlılık yok (yalnızca Python standart kütüphanesi, Python 3.11+).

1. Kodu güncelle:

   ```bash
   git pull
   ```

2. Önce tek bir bağlantı ve kayıt turu yap. Proje klasöründe `Phase1-Kayit.cmd --once` çalıştır ya da:

   ```bash
   .venv\Scripts\python.exe -m tools.phase1_recorder --once
   ```

   Rapor her borsa için "BAĞLANDI" ya da "BAĞLANTI KURULAMADI" yazar.
3. Uzun kayıt için `Phase1-Kayit.cmd`'yi argümansız çalıştır; 24 saat kaydeder. 72 saat için:

   ```bash
   Phase1-Kayit.cmd --duration-hours 72
   ```

4. Durdurmak için:
   - pencerede Ctrl+C,
   - ya da başka bir pencereden `Phase1-Durdur.cmd`.

   Oturum o anki isteği bitirir, kayıtlar zaten her görevden sonra yazılmış olur.
5. Durum raporu (API'ye bağlanmaz): `Phase1-Durum.cmd`.
6. Doğrulama ve paket: `Phase1-Paket.cmd`. Önce `--validate-only` çalışır (hash, şema, ham yanıtları yeniden ayrıştırma; ağ yok), sonra ZIP üretir.

Paper motoruyla aynı anda çalışabilir: port açmaz ve motorun dosyalarına dokunmaz. Aynı klasörde ikinci bir kayıt oturumu başlatılamaz (kilit dosyası).

### Komutlar

```text
python -m tools.phase1_recorder --once
python -m tools.phase1_recorder --duration-hours 24
python -m tools.phase1_recorder --status
python -m tools.phase1_recorder --validate-only
python -m tools.phase1_recorder --export
python -m tools.phase1_recorder --stop
```

Ek seçenekler:
- `--venues Hyperliquid,Extended`
- `--assets BTC,ETH`
- `--all-markets`: tüm aktif piyasalar.
- `--root <klasör>`
- `--config ayar.json`

Varsayılan varlıklar projenin listesidir: BTC, ETH, SOL, HYPE, SUI, XRP, LINK, DOGE, AVAX, BNB, LTC, AAVE, UNI, NEAR, ADA.

## Ne zaman ne kaydedilir

| Veri | Varsayılan | Ayar |
|---|---|---|
| Piyasa bilgisi (tüm piyasalar) | açılışta, sonra 6 saatte bir | `metadata_seconds` |
| Güncel funding (seçili varlıklar) | 60 sn | `funding_seconds` |
| Gerçekleşmiş funding | 15 dk; son 2 saat her seferinde yeniden okunur | `settlements_seconds` |
| 1h mumlar | ilk açılışta 30 gün; sonra 30 dakikada bir eksik saatler | `candles_seconds`, `backfill_days` |
| Emir defteri (ilk 20 seviye) | 60 sn | `orderbook_seconds`, `book_levels` |
| İstek denetimi | her istekte | — |

- **Borsalar birbirinden bağımsızdır:** her borsa ayrı iş parçacığında çalışır. Bir borsanın API'si çökse de diğerleri sürer; bir piyasa hata verse de diğer piyasalar sürer.
- **Hata ve yeniden deneme:**
  - 429, 5xx, zaman aşımı ve bağlantı hatası üstel bekleme ile yeniden denenir (2, 4, 8, 16 sn).
  - 429'da `Retry-After` uyulur.
  - Her borsa için istekler arasında asgari aralık vardır (`min_spacing`).
- **Yeniden başlatma:** kaldığı yerden devam eder; ödeme geçmişi ve mumlar kontrol noktasından okunur.
- **Eksik mumlar:** en çok 3 kez yeniden istenir. Doldurulmaz, interpolasyon yapılmaz.

## Klasör yapısı

```text
phase1-evidence/
├── recorder.sqlite3    istek denetimi, ham dosya dizini, kayıt anahtarları, kontrol noktaları, karşılaştırmalar
├── raw/<borsa>/<gün>/<sha256>.json.gz        her ham yanıt
├── normalized/<tür>/<borsa>/<gün>.jsonl      market, funding_snapshot, settlement, candle, orderbook, comparison, error
├── logs/  reports/  manifests/  exports/
```

- Bütün zamanlar UTC'dir.
- **Tekrar yazım olmaz:** ham yanıt borsa ve hash başına bir kez saklanır, her kayıt kendi anahtarıyla bir kez yazılır.
- Klasör `.gitignore`'da; gerçek veri GitHub'a gitmez.
- Hiçbir istekte kimlik bilgisi gönderilmez. Adı anahtar, token, imza, auth vb. olan parametre ve başlıklar kayda `<redacted>` olarak geçer.

## Yayımlanan oran ↔ gerçekleşen ödeme eşleştirmesi
- **Eşleştirme kuralı:** her gerçekleşmiş ödeme için o ödemenin kendi dönemindeki son anlık oran kullanılır: önceki ödemeden sonra, bu ödemeden önce.
  - Borsa bir sonraki ödeme zamanını yayımlıyorsa (Extended), anlık kaydın bu ödemeyi göstermesi gerekir.
  - Böylece önceki ya da sonraki döneme bağlama olmaz, bir ödeme iki kez sayılmaz ve izlenmeyen dönem için karşılaştırma üretilmez.
- **Durumlar:**
  - `MATCHED`
  - `SIGN_CONFLICT`
  - `TOO_SMALL`
  - `NO_SIGNED_RATE`: yön bilinmiyor.
  - `PENDING`: ödeme henüz kayıtlı değil.
  - `NOT_RECORDED`: ödeme zamanı ve bekleme süresi geçti, kayıt yok.
- Her karşılaştırma iki ham yanıtın hash'ini taşır.
- 3 tutarlı karşılaştırma raporda belirtilir; bu tek başına üretim güvenilirliği değildir.

## Rapordaki durumlar
- `VERIFIED`: kayıtlı veriyle ölçüldü ve beyanla uyuşuyor. Örnek: gözlenen ödeme aralığı beyan edilen aralıkla %5 içinde ve düzenli.
- `OBSERVED`: borsanın yanıtında var ya da veriden ölçüldü, ama belgeyle doğrulanmadı. Örnek: yayımlanan oran ödemeyle aynı büyüklükte (medyan oran 0,8–1,25). Belge okunmadığı için oran birimi hiçbir zaman VERIFIED yazılmaz.
- `DECLARED`: bağlayıcı beyanı, veriyle kontrol edilmedi. Örnekler: kontrat büyüklüğü, işaret kuralı.
- `UNKNOWN`: veri yetersiz ya da borsa yayımlamıyor (`null` saklanır).
- `CONFLICT`: veri beyanla veya kendisiyle çelişiyor. Örnekler:
  - 8 saatte ödenen piyasa için saatlik beyan;
  - ters işaretler;
  - 8 kat oran farkı.

Rapor her borsa ve piyasa için şunları gösterir:
- bağlantı durumu, istek ve hata sayıları, 429 olayları;
- piyasa bilgisi;
- anlık oran, ödeme ve karşılaştırma sayıları;
- mum kapsamı ve eksik saatler;
- defter sayısı;
- yinelenen ödeme, sırasız kayıt ve hatalı zaman damgası sayıları;
- aralık, birim ve yön durumları, işaret tutarlılığı;
- doğrulanmamış alanlar.

## Paket (ZIP)
Paket `phase1-evidence\exports\phase1-evidence-YYYY-MM-DD-HHMMSS.zip` olarak oluşturulur. İçeriği:
- `manifest.json`: git commit, sürüm, kayıt başlangıç ve bitişi, borsalar, piyasalar, endpoint'ler, sayılar, dosya hash'leri, uyarılar;
- `source_versions.json`, `coverage_report.json`, `consistency_report.json`;
- `audit.jsonl`, `raw/`, `normalized/`, `errors/`;
- `checksums.sha256`.

Yalnızca göreli yollar yazılır. Pakette API anahtarı, hesap bilgisi ya da yerel kullanıcı klasörü yoktur.

## Testler
- **Çalıştırma:** recorder testleri `python -m unittest tools.phase1_recorder.tests.test_recorder` ile ayrı çalışır; projenin tam test paketine de dahildir (`python -m unittest discover -p "test_*.py"`).
- **Kapsam:** spesifikasyondaki 20 senaryonun hepsi.
- **Gerçek API testi:** gerçek public API isteyen test ayrı işaretlidir ve yalnızca `NB_LIVE=1` ile çalışır:

  ```bash
  set NB_LIVE=1
  ```

  ```bash
  .venv\Scripts\python.exe -m unittest tools.phase1_recorder.tests.test_recorder.LivePublicApiTests
  ```

## Disk kullanımı
Asıl yer kaplayan, dakikada bir saklanan toplu yanıtlardır: Hyperliquid `metaAndAssetCtxs`, Lighter `orderBookDetails` ve Extended `info/markets`. Bunlar sıkıştırılmış olarak borsa başına günde onlarca MB tutabilir. Gerekirse `funding_seconds` ve `orderbook_seconds` büyütülebilir.
