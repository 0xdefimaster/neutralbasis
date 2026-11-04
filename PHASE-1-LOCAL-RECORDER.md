# Phase 1 — Yerel Piyasa Kanıtı Kaydedici

Phase 0.7'deki piyasa düzeyinde kanıt kurallarını gerçek borsa verisiyle doğrulamak için bilgisayarında çalışan, bağımsız ve **salt okunur** bir kayıt aracı. Bu bir trading botu değildir:
- emir göndermez;
- hesap, bakiye veya özel endpoint okumaz;
- API anahtarı istemez;
- paper motorunu başlatmaz;
- motorun veritabanına (`data-five-rh/paper.sqlite3`) yazmaz.

Kendi klasörüne yazar: `phase1-evidence/`.

## Durum
- **Kod:** yazıldı ve test edildi. Testler sahte (mock) yanıtlarla çalışır; bu yanıtlar piyasa kanıtı sayılmaz.
- **Gerçek veri:** bulut ortamı borsa API'lerine bağlanamaz (proxy 403). İlk gerçek tur kullanıcının bilgisayarında `--once` ile yapıldı (2026-10-30, `LIVE_PUBLIC_API`): dört borsa da bağlandı. Bu tek turda karşılaştırma yoktur; oran birimi ve yön için uzun kayıt gerekir.
- **Variational:** yalnızca keşif modunda eklendi; henüz gerçek yanıtı görülmedi.
- **Borsa belgeleri:** bu ortamdan okunamadı. Funding kuralları "bağlayıcı beyanı" (DECLARED) olarak işaretlidir; resmî belge doğrulaması ayrıca yapılmalıdır.

## Borsalar ve endpoint'ler
Endpoint'ler projede zaten kullanılan çağrılardan alındı; yeni endpoint uydurulmadı. Hepsi public ve kimlik doğrulamasız.

| Kod | Borsa | Host | Kullanılan çağrılar | Kaynak |
|---|---|---|---|---|
| `Hyperliquid` | Hyperliquid | `api.hyperliquid.xyz/info` (POST) | `metaAndAssetCtxs`, `fundingHistory`, `candleSnapshot`, `l2Book` | CCXT hyperliquid adaptörünün gönderdiği istekler |
| `LighterCore` | Lighter Core | `mainnet.zklighter.elliot.ai/api/v1/` | `orderBookDetails`, `funding-rates`, `fundings`, `candles`, `orderBookOrders` | `bot.Feed`, CCXT lighter (`funding-rates`) |
| `Lighter` | Lighter RH | `api.rh.lighter.xyz/api/v1/` | aynı Lighter API | `risk_scan.FiveFeed`, `bot.Feed` |
| `Extended` | Extended | `api.starknet.extended.exchange/api/v1/` | `info/markets`, `info/<piyasa>/funding`, `info/candles/<piyasa>/trades`, `info/markets/<piyasa>/orderbook` | `bot.Feed` |
| `Variational` | Variational Omni (keşif) | `omni-client-api.prod.ap-northeast-1.variational.io` | `GET metadata/stats` | `connections.py` bağlantı testi |

Notlar:
- **Hyperliquid POST kullanır:** public verisini yalnızca `POST /info` ile verir. Kayıt aracı yalnızca yukarıdaki dört okuma türüne izin verir; başka bir tür (ör. hesap durumu) kodda reddedilir.
- **Lighter Core ve Lighter RH ayrı borsalardır:** ayrı host, ayrı piyasa kimliği, ayrı klasör (`raw/lighter_core`, `raw/lighter_rh`). Aynı Lighter API yazılımını kullandıkları için aynı istemci sınıfı iki ayrı örnekle çalışır.
- **Lighter `funding-rates`:** başka borsaların oranlarını da listeler; yalnızca `exchange == "lighter"` kayıtları alınır. Bu oranın birimi belgelenmemiştir: ham saklanır, birimi `UNKNOWN`.
- **Lighter gerçekleşmiş funding:** `direction` alanı ham olarak saklanır. Projenin okuması (yüzde değer, long → +, short → −) yanına DECLARED olarak yazılır.
- **Extended ve Hyperliquid:** gerçekleşmiş ödemede yön alanı yok. Yön türetilmez, `UNKNOWN` kalır.
- **Extended sıralaması:** funding ve mum yanıtları yeniden eskiye sıralı gelir. Bu tutarlı bir sıradır; "sırasız kayıt" yalnızca yanıtın kendi yönüne aykırı adımları sayar.
- **Variational yalnız keşiftir:** projede Variational bağlayıcısı yok, alan anlamları belgelenmedi. Dakikada bir `metadata/stats` ham yanıtı saklanır ve yanıttaki her JSON yolunun tipi bir şema kaydına yazılır (şema değişince yeni kayıt). Hiçbir alan funding, fiyat veya ödeme olarak yorumlanmaz; piyasa, funding, ödeme veya mum kaydı üretilmez. Raporda listing alanları, ilk listing'in yayımlandığı haliyle değerleri, yanıtın ne sıklıkla değiştiği (RFQ önbelleği için) ve önbellek başlıkları görünür. Alanların anlamı sonraki adımda bu kanıta ve belgeye bakılarak ayrıca onaylanacak.

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

   Kayıt sürerken pencerede her görev için bir satır görünür (ör. `[12:30:05] Hyperliquid funding: ...`). İlk geçmiş indirmesi borsa başına birkaç dakika sürebilir. Bitince rapor her borsa için "BAĞLANDI" ya da "BAĞLANTI KURULAMADI" yazar.
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
| Variational ham yanıtı ve şeması | 60 sn | `discovery_seconds` |
| İstek denetimi | her istekte | — |

- **Borsalar birbirinden bağımsızdır:** her borsa ayrı iş parçacığında çalışır. Bir borsanın API'si çökse de diğerleri sürer; bir piyasa hata verse de diğer piyasalar sürer.
- **Hata ve yeniden deneme:**
  - 429, 5xx, zaman aşımı ve bağlantı hatası üstel bekleme ile yeniden denenir (2, 4, 8, 16 sn).
  - 429'da `Retry-After` uyulur.
  - Her borsa için istekler arasında asgari aralık vardır (`min_spacing`).
  - Hiç yanıt alınamayan (zaman aşımı, DNS, bağlantı reddi) istekler art arda 3 kez tükenirse o borsa 10 dakika istek yapılmadan atlanır (`unreachable_after`, `unreachable_cooldown_seconds`). HTTP hata kodu dönen bir borsa atlanmaz. Atlanan görev raporda ve pencerede "atlandı" diye görünür.
  - `--once` hızlı bir kontroldür: 1 yeniden deneme, 10 sn zaman aşımı, 2 başarısızlıkta atlama (ayar dosyasında verilen değer önceliklidir).
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
- **Karşılaştırma kimliği:** tamamlanmış bir karşılaştırma borsa, piyasa ve ödemenin gerçek zamanıyla (`settled_at`) bir kez saklanır. Eşleştirme kaç kez yeniden çalışırsa çalışsın sayı artmaz. Borsanın duyurduğu ödeme zamanı (`expected_settlement_at`) yalnızca bir alandır; Hyperliquid ve Lighter'da boştur ve kimliğe girmez. `PENDING` ve `NOT_RECORDED` kayıtları tamamlanmış sayılmaz.
- **Eski kayıtların onarımı:** bu düzeltmeden önce çalışmış bir kayıt klasörü açıldığında karşılaştırma dizini otomatik onarılır:
  - eski tablo silinmez, `comparisons_legacy_<zaman>` adıyla saklanır;
  - dizin, zaten gerçek ödeme zamanıyla anahtarlanmış JSONL karşılaştırma kayıtlarından yeniden kurulur;
  - ham yanıtlara, piyasa, funding ve mum kayıtlarına dokunulmaz.

  Yapılan onarım `--validate-only` raporunda görünür.
- 3 tutarlı karşılaştırma raporda belirtilir; bu tek başına üretim güvenilirliği değildir.

## Ödeme kimliği ve veri güvenilirliği
- **Ödeme kimliği:** her ödemenin kimliği borsa + piyasa + `settled_at` üçlüsüdür. `settled_at` her borsada tam saniye UTC epoch değeridir; API'nin verdiği tam zaman `settled_at_api` alanında ayrıca saklanır.
- **Aynı ödeme toleransı:** aynı piyasada birbirine çok yakın iki ödeme zamanı aynı ödeme sayılabilir.
  - Ayar `same_event_seconds`: borsa bazında, ya da `"Borsa|VARLIK"` ile piyasa bazında.
  - Etkin tolerans şu üçünün en küçüğüdür: ayar, piyasanın gözlenen (yoksa beyan edilen) ödeme aralığının %1'i ve 60 sn. Saatlik piyasada en fazla 36 sn olur; aralık bilinmiyorsa 1 sn.
  - Farklı borsa ya da farklı piyasanın ödemeleri hiçbir koşulda birleşmez.
- **Kayma ve düzeltme:** tolerans içinde aynı değerler aynı ödemedir; saniye altı farklar iki kayıt oluşturmaz. Aynı anda (1 sn içinde) farklı değer borsa düzeltmesidir.
- **Borsa düzeltmesi:** borsa kayıtlı bir ödemenin değerini sonradan değiştirirse yeni bir revizyon olarak saklanır.
  - İlk sürüm silinmez.
  - Önceki ve yeni değer bir `settlement_correction` kaydına yazılır.
  - O ödemenin karşılaştırması yeni değerle yeniden hesaplanır ve eskisinin yerine geçer; sayı artmaz.
- **Belirsiz kimlik (AMBIGUOUS):** tolerans içinde ama 1 sn'den uzak ve farklı değerli iki kayıt hangisinin gerçek ödeme olduğu ayırt edilemeyen iki kayıttır.
  - İkisi de saklanır ve `identity_status=AMBIGUOUS` olur. Kayıtlı olan yeni bir revizyonla işaretlenir; hiçbir şey silinmez.
  - İkisi de ödeme sayımına ve karşılaştırmalara girmez.
  - Varsa eski karşılaştırması `EXCLUDED_AMBIGUOUS` olur ve sayılmaz.
- **Geçmiş mutabakatı:** ayrı bir görev varsayılan olarak 6 saatte bir (`reconcile_seconds`) her piyasanın son 24 saatini (`reconcile_hours`, üst sınır `backfill_days`) yeniden okur. Kayıtlar yeni ödemelerle aynı yoldan geçer:
  - değişmeyen ödeme yeni kayıt oluşturmaz;
  - değişen ödeme yeni revizyon olur;
  - kayıtlı olup artık listelenmeyen ödeme silinmez, `settlement_withdrawn` olarak raporlanır;
  - pencerenin karşılaştırmaları yeniden hesaplanır.

  Görev tekrar çalıştırılırsa hiçbir şey değişmez. Kontrol noktası ve istatistikleri (`reconcile:<borsa>:<piyasa>`) yeniden başlatmada korunur.
- **Çelişkili kayıt:** aynı yanıtta aynı ödeme için farklı değerler gelirse ödeme `CONFLICT` olarak işaretlenir ve oranı bilinmiyor kabul edilir; değerlerden biri seçilmez. Eşleştirmede `NO_SIGNED_RATE` olur ve tutarlılık sayısına girmez.
- **Eksik veri:** eksik veri hiçbir yerde sıfır sayılmaz: eksik alan `null` kalır, zorunlu alan eksikse yanıt reddedilir.
- **Çökme güvenliği:**
  - Çökme sırasında yarım kalan bir JSONL satırı okunurken atlanır ve raporlanır. Sonraki kayıt yeni satırdan başlar.
  - Aynı satır iki kez yazılmışsa bir kez okunur.
  - Ödeme ve karşılaştırma dizinleri her açılışta JSONL kayıtlarıyla karşılaştırılır; farklıysa tek bir işlem içinde yeniden kurulur. Yarıda kesilen bir onarım eski durumu bırakır ve bir sonraki açılışta tamamlanır.
- **Veri kaynağı:** her klasör veri kaynağını kaydeder: `LIVE_PUBLIC_API` ya da `MOCK:<test taşıyıcısı>`. Bir klasöre iki tür veri birlikte yazılamaz.
  - Gerçek olmayan klasörün raporunda, paketinde ve manifestinde (`live_data: false`) bu açıkça yazar.
  - Böyle bir klasörde VERIFIED, OBSERVED ve CONFLICT durumları `MOCK:` önekiyle gösterilir.

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
