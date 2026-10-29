# Gerçek hesaba geçiş planı

Hedef: paper sonuçları, gerçek hesapta aynı kararlarla alınacak sonuca olabildiğince yakın olsun. Gerçek paraya ne zaman geçileceği bu sonuçlara bakılarak seçilsin.

Varsayılan hesap borsaları (15 perp): **Lighter, Extended, Hyperliquid, TxFlow, RISEx, Arcus, Binance, Bybit, OKX, Bitget, Gate, Aster, Paradex, dYdX, GRVT**. Liste sıfırlama ekranında işaretlenerek değiştirilebilir; taranan her perp borsası seçilebilir. Bot kullanıcının kendi Windows bilgisayarında çalışır. Anahtarlar yalnızca orada, Windows kullanıcı şifrelemesiyle (DPAPI, `connections.py`) saklanır. Sunucuya (Railway) anahtar konmaz.

## Aşama 1 — Borsa başı teminat (yapıldı)

Önceki paper tek bir ortak cüzdan kullanıyordu. Gerçekte ise her borsada ayrı teminat tutulur, para borsalar arasında anında taşınamaz.

- Ayarlar → "Arşivle ve sıfırla" → Teminat: **Borsa başı**, tutar **1000**. İşaretli borsaların her biri $1000 ayrı teminatla başlar (varsayılan 15 borsa, toplam $15.000). Önceki dönem arşivlenir.
- **Sadece hesap borsaları:** Paper yalnızca işaretli borsalar arasında işlem açar. Diğer borsalar taramada görünmeye devam eder ama işlem havuzuna girmez.
- **Teminat bacak başına:** Her bacağın marjini ve ücreti yalnızca kendi borsasının boş teminatından karşılanır; rezerv de borsa başına uygulanır. Büyüklük, iki borsadan teminatı az olana göre küçülür.
- **Kâr öbür bacağı korumaz:** Bir borsanın özsermayesi kullandığı marjinin yarısına inerse pozisyon "Borsa teminatı riski" ile kapanır. Öbür borsadaki kâr bunu engellemez.
- **Funding:** Funding ve fiyat PnL'si ödendiği ya da gerçekleştiği borsanın teminatına yazılır.
- **Transfer yok:** Borsalar arasında transfer yapılmaz. Bir borsada biriken kâr, öbüründeki eksiği kapatmaz.
- **Teminat tablosu:** Çoklu paper ekranında borsa başı nakit, özsermaye, marjin, boş teminat ve veri durumu görünür.

## Aşama 1b — Gerçeğe yakınlık (yapıldı)

- **Üç borsa masaya bağlandı:** TxFlow, RISEx ve Arcus artık masanın kendi beslemesinden (defter, 1h mumlar, funding geçmişi) çoklu paper masasına bağlı. Her borsa ancak gerçekten işlem açılabilecek veri tamsa işleme girer. Şu anki durum:
  - **Arcus:** Ücret tarifesi public API'den okunuyor. Ancak fiyat geçmişi oracle fiyatı; işlem fiyatı korelasyon kontrolünü geçmediği için çiftler genelde elenir.
  - **RISEx:** Taker ücreti public olarak doğrulanmadı. Hesap sayfanızdaki ücreti Ayarlar → "Borsa taker ücretleri" alanına yazınca işleme girer.
  - **TxFlow:** Ücret girilse bile gerçekleşmiş funding geçmişi public olarak doğrulanmadığı için işlem açmaz. Bu boşluk Aşama 2'de hesap anahtarıyla (gerçek funding ödemeleri) kapanacak.
- **Lot kuralları:** Emir miktarı iki borsanın adım büyüklüğüne aşağı yuvarlanıyor. Borsanın asgari miktarı ya da asgari tutarı altında kalan işlem açılmıyor. Kurallar Lighter, Extended, TxFlow, RISEx ve Arcus'ta kendi API'lerinden, CCXT borsalarında (Hyperliquid) piyasa tanımından geliyor. Yayımlanmayan kural uydurulmuyor.
- **Ölçülen gecikme:** Her bacak, ayarlanan tabandan (750 ms) ya da o borsanın son 50 defter isteğinde ölçülen p90 gidiş-dönüş süresinden hangisi uzunsa o kadar bekleyip, sonra taze defterden doluyor. Bu süre borsa tablosunda görünüyor.

## Aşama 1c — 30 günlük teste hazırlık (yapıldı)

- **Borsa başı ücret:** Ayarlar → "Borsa taker ücretleri (%)". Her borsanın kendi hesap sayfanızdaki taker ücreti yazılır; boş kalan borsada public tarife kullanılır. Borsa tablosunda ücretin kaynağı yazar ("senin girdiğin" / "public tarife" / "bilinmiyor").
- **Dönem raporu:** Çoklu paper ekranındaki kart dönemin güvenilir olup olmadığını ölçüt ölçüt gösterir:
  - süre en az 30 gün;
  - kesinti en çok %5;
  - en az 40 kapanmış işlem;
  - ücretler sonrası net pozitif;
  - stres altında net pozitif: kapanan işlemler kayma 2 katı, ücret +%50, funding geliri −%25, tahmini değerlenen funding sayılmadan ve doğrulanmamış ödemeler aleyhte sayılarak yeniden hesaplanır; sonuç yalnızca paper'ın kendi dolumlarında pozitifse ölçüt sağlanmaz;
  - doğrulanmamış funding aleyhte sayılınca net pozitif: borsanın yayımlamadığı ödemeler ödeyen bacakta maliyet sayıldığında da sonuç pozitif kalmalı;
  - kazanç/kayıp oranı en az 1.3;
  - en iyi 3 işlemin nete payı %50'nin altında;
  - yeni girişler kapalı kalan süre en çok %5;
  - dönem içinde ayar değişikliği yok;
  - "Borsa teminatı riski" kapanışı yok.

  Ayrıca açık pozisyonların kapatılırsa netini, en büyük düşüşü, kesinti sayısını ve borsa başı işlem sayısını gösterir. Kartın altındaki karne her strateji ve borsa çifti için işlem sayısını, neti, işlem başı ortalamayı, en kötü işlemi ve en kötü fiyat sonucunu, ücretin brüt gelire oranını, ödenen kaymayı, ortalama süreyi, en derin düşüşü ve veri güvenilirliğini (doğrulanmamış ödeme, tahmini değerlenen funding payı) gösterir.
- **Kesinti kaydı:** İki tur arasında 10 dakikadan uzun boşluk kesinti sayılır. Yeni girişlerin kapalı olduğu süre ve dönem içindeki ayar/ücret değişiklikleri de kaydedilir. Sıfırlama bu kayıtları yeni dönem için temizler.
- **Uyku engelleme:** Motor Windows'ta çalışırken bilgisayarın uykuya geçmesini engeller. Ekran yine kapanabilir.
- **Otomatik başlatma:** `OtomatikBaslat.cmd` bir kez çalıştırılır. Windows Görev Zamanlayıcı oturum açılınca ve her 5 dakikada bir motoru kontrol eder; motor kapanmış ya da çökmüşse yeniden başlatır. `OtomatikKapat.cmd` görevi kaldırır.

## 30 günlük paper testi: adım adım

1. Güncel kodu alın (`git pull`). Motor çalışıyorsa `.\Durdur.cmd`, sonra `.\Baslat.cmd` çalıştırın. PowerShell'de dosya adının başına `.\` gerekir; Gezgin'de çift tıklamak da olur. Motoru `python risk_main.py` ile ön planda değil, `Baslat.cmd` ile arka planda çalıştırın; PowerShell penceresi kapanınca motor durmaz.
   - İlk açılışta veritabanı bir kerelik sıkıştırılır. Bu birkaç dakika sürebilir ve o sırada panel açılmaz. Bittiğinde `data-five-rh\motor.log` dosyasında "Bakım bitti" yazar.
2. Panel → Çoklu paper. Açık pozisyon varsa "Kapat" ile kapatın.
3. Ayarlar → "Borsa taker ücretleri": her borsanın hesap sayfanızdaki taker ücretini yazıp kaydedin. Bunu sıfırlamadan **önce** yapın; sonra yapılan değişiklik dönem raporunda ayar değişikliği sayılır.
4. Ayarlar → "Arşivle ve sıfırla": "Borsa başı (gerçekçi)", tutar 1000. Listede yalnızca gerçekten hesap açıp perp işlem yapabileceğiniz borsaları işaretli bırakın.
5. `.\OtomatikBaslat.cmd` çalıştırın. "Erişim engellendi" hatası verirse sağ tık → Yönetici olarak çalıştırın.
6. Windows ayarları:
   - **Dizüstü:** kapak kapanınca "Hiçbir şey yapma" seçilmeli.
   - **Windows Update:** güncellemeleri 4–5 hafta duraklatın ya da etkin saatleri ayarlayın.
   - **Yeniden başlatma sonrası:** oturum açılana kadar motor çalışmaz. Bu süre kesinti olarak kayda geçer.
7. 30 gün boyunca ayarları değiştirmeyin. Haftada bir dönem raporuna ve borsa tablosuna bakın.
8. Gün 30'da dönem raporundaki ölçütlere bakın. Tümü sağlanmadıysa sonuç karar vermek için yeterli değildir.

## Hâlâ modellenmeyenler

- **İki kontrol arasındaki fiyat sıçramaları:** Pozisyonlar her turda defterden işaretleniyor; aradaki kısa ani hareketler görülmüyor. Borsa başı $1000 teminatla etkin kaldıraç düşük olduğundan likidasyon için çok büyük bir hareket gerekir. Ama botun kendi zarar-kes kararları bir tur gecikebilir; gerçek bir bot da aynı gecikmeyi yaşar.
- **Gerçek ücret kademesi:** Elle girilmezse public tarife kullanılır. Aşama 2'de hesaptan okunacak.
- **Dolum doğruluğu:** Paper'ın dolum fiyatının gerçekle ne kadar örtüştüğü ancak gerçek emirle ölçülebilir (Aşama 3, isteğe bağlı mikro emir). Paper bu konuda temkinli tarafta: yalnızca taker emir, bacaklar sırayla, en az 750 ms gecikme ve +1 bp ek kayma. Giriş emirleri planlanan fiyatın %0,1 ötesine kadar "hemen ya da iptal" (IOC) emridir: defter yetmezse kısmi dolar; ilk bacak kısmi dolarsa ikincisi o miktarda gönderilir, ikinci bacak kısmi dolarsa ilk bacağın eşleşmeyen kısmı hemen kapatılır ve maliyeti işleme yazılır. Gerçek bir borsada kısmi dolumun boyutu ve sıra önceliği farklı olabilir.
- **Borsa olayları:** Bakım, listeden çıkma, ADL ve para çekme durdurmaları modellenmez.

## Aşama 2 — Bağlantı ve salt okunur hesap

Amaç: paper'ın kullandığı varsayımları kullanıcının gerçek hesabıyla değiştirmek. Bu aşamada hiçbir emir gönderilmez.

| Borsa | Anahtar türü | Okunacaklar |
|---|---|---|
| Hyperliquid | Cüzdan adresi yeter; işlem için ayrıca **API (agent) cüzdanı**, para çekemez | Ücret kademesi, bakiye, açık pozisyon, funding geçmişi |
| Lighter | Hesap indeksi + API anahtar indeksi + API özel anahtarı (resmî SDK imzalar) | Ücret, bakiye, pozisyon |
| Extended | API anahtarı + Stark anahtarı (resmî x10 SDK) | Ücret kademesi, bakiye, pozisyon, funding ödemeleri |
| TxFlow / RISEx / Arcus | Belgeleri kontrol edilecek | Önce public veri adaptörü (funding geçmişi, ücret, defter) tamamlanacak |

Yapılacaklar:
- **Hesaplar ekranı:** Borsa başına anahtar girişi ve bağlantı testi. Ekran sadece "bağlı / bağlı değil" ve anahtarın son 4 karakterini gösterir; anahtar ekrana ya da loglara yazılmaz.
- **Gerçek ücret kademesi:** Paper, public tarife yerine hesabın kendi maker/taker ücretini kullanır.
- **Gerçek bakiyeyle başlatma (isteğe bağlı):** "Paper teminatını gerçek bakiyeyle başlat" seçeneği.
- **Anahtar izinleri:** Para çekme izni olan anahtar kabul edilmez. Tespit edilebilen borsalarda kontrol edilir, edilemeyenlerde kullanıcıya açıkça uyarı gösterilir.

## Aşama 3 — Gerçek emir adaptörleri ve gölge ölçüm

Her borsa için aynı arayüz kullanılır: emir gönder (IOC, fiyat sınırlı), emri iptal et, dolumları, pozisyonu ve funding ödemelerini oku. Paper motoru bu arayüzü kullanır ve paper ile gerçek arasındaki tek fark, emrin gerçekten gönderilip gönderilmediğidir.

- **Mikro canlı (isteğe bağlı):** Paper bir pozisyon açtığında aynı anda gerçek hesapta küçük bir emir açılır (ör. $20). Gerçek dolum fiyatı, gecikme ve ücret, paper'ın tahminiyle yan yana kaydedilir. Ölçülen ortalama fark paper'ın kayma ve gecikme ayarlarına geri yazılır.
- **Testler:** Her adaptör önce sahte sunucuyla test edilir. Ardından kullanıcının bilgisayarında, en küçük emir büyüklüğüyle bir aç-kapat denemesi yapılır.

## Aşama 4 — Gerçek işlem

Gerçek işleme ancak paper ve mikro canlı sonuçları belirlenen eşikleri geçtiğinde, kullanıcı açıkça açarsa geçilir.

- **Mod:** Borsa başı "paper / gerçek" anahtarı. Varsayılan paper.
- **Sınırlar:** Pozisyon başına, borsa başına ve günlük kayıp için sert sınırlar. "Tümünü kapat" (acil durdurma) düğmesi.
- **Tek bacak riski:** Bir bacak dolup öbürü dolmazsa dolan bacak hemen kapatılır. Bu mantık paper'da zaten var; gerçekte de aynısı uygulanır.
- **Mutabakat:** Her turda borsadaki gerçek pozisyon okunur. Motorun kaydıyla uyuşmazsa yeni giriş durur ve kullanıcıya uyarı çıkar.

## Gerçek paraya geçiş için önerilen eşikler

- Borsa başı teminatlı paper'da **en az 30–50 kapanmış işlem** ve en az 2–4 hafta.
- Ücretler sonrası net pozitif; kâr birkaç işleme değil, geneline yayılmış olmalı.
- Mikro canlı ölçümde gerçek dolum ile paper tahmini arasındaki fark, işlem başı beklenen kârdan belirgin biçimde küçük olmalı.
- Hiç "Borsa teminatı riski" kapanışı olmamalı ya da sebebi anlaşılmış olmalı.
