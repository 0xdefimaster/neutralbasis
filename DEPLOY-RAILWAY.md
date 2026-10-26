# Railway test kurulumu

neutralbasis Railway üzerinde sürekli çalışan tek bir servis olarak koşar: paper motoru her 30 saniyede bir tarar, panel aynı servisten şifreyle açılır. Gerçek emir gönderilmez, borsa API anahtarı gerekmez.

## Adımlar

1. Railway'de **New Project → Deploy from GitHub repo** ile bu depoyu seçin. `railway.json` derleme ve başlatma ayarlarını kendisi verir (`python -u risk_main.py --autostart --host 0.0.0.0`, Python sürümü `.python-version` dosyasından).
2. **Variables** sekmesine `NB_DASHBOARD_PASSWORD` ekleyin (uzun, rastgele bir değer). Bu değişken yoksa motor dış adreste başlamayı reddeder.
3. Servise sağ tıklayıp **Attach Volume** ile bir volume ekleyin, bağlama yolu `/data` olsun. SQLite veritabanı (paper bakiye, pozisyonlar, geçmiş) buraya yazılır. Volume olmazsa her yeniden deploy'da paper geçmişi sıfırlanır.
4. **Settings → Networking → Generate Domain** ile genel adres alın. Panel: `https://<adres>/#price`. Tarayıcı kullanıcı adı ve şifre sorar: kullanıcı adı `paper`, şifre `NB_DASHBOARD_PASSWORD`.
5. **Settings → Region** için Avrupa bölgesi (ör. Amsterdam) önerilir. Bazı borsaların public API'leri ABD IP'lerini engelleyebilir; paneldeki Borsalar sayfasında bağlanamayan platformları kontrol edin.

## Ortam değişkenleri

| Değişken | Zorunlu | Açıklama |
|---|---|---|
| `NB_DASHBOARD_PASSWORD` | Evet | Panel şifresi (HTTP Basic, kullanıcı `paper`). |
| `PORT` | Railway verir | Dinlenen port. Elle ayarlamayın. |
| `RAILWAY_VOLUME_MOUNT_PATH` | Railway verir | Volume bağlıysa veri klasörü olarak kullanılır. |
| `NB_DATA_DIR` | Hayır | Veri klasörünü elle seçmek için; volume yolunun önüne geçer. |
| `NB_HOST` | Hayır | Dinlenen adres; `railway.json` zaten `0.0.0.0` verir. |

Örnek liste `.env.example` dosyasındadır. Gerçek şifreyi depoya koymayın.

## Davranış

- Tarama aralığı varsayılan 30 saniyedir (`poll_seconds`). Panelin Ayarlar sayfasından 15–300 saniye arasında değiştirilebilir; değer volume'daki veritabanında saklanır.
- `--autostart` ile motor açılışta otomatik paper girişlerini etkinleştirir. Paneldeki durdur düğmesi yalnızca yeni girişleri durdurur.
- `/healthz` şifresiz sağlık kontrolüdür; Railway deploy kontrolünde kullanılır. Panel ve diğer tüm API yolları şifre ister.
- Railway servisi durdururken SIGTERM gönderir; motor açık durumu veritabanına yazıp kapanır.
- Paneldeki API anahtarı saklama Windows DPAPI gerektirir. Linux'ta anahtar kalıcı kaydedilmez; paper tarama için gerekli değildir.
- Tek kopya (`numReplicas: 1`) çalıştırın. İki kopya aynı paper veritabanını paylaşamaz.

## Yerelde Railway gibi denemek

```
NB_DASHBOARD_PASSWORD=deneme PORT=8765 NB_DATA_DIR=./data-railway python -u risk_main.py --autostart --host 0.0.0.0
```

Windows kurulumu (Baslat.cmd) değişmedi: değişken verilmezse motor yine yalnızca 127.0.0.1:8765 üzerinde, şifresiz çalışır.
