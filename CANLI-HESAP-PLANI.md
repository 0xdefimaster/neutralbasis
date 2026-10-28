# Gerçek hesaba geçiş planı

Hedef: paper sonuçları, gerçek hesapta aynı kararlarla alınacak sonuca olabildiğince yakın olsun. Gerçek paraya ne zaman geçileceği bu sonuçlara bakılarak seçilsin.

Hesap açılacak borsalar: **Lighter, Extended, Hyperliquid, TxFlow, RISEx, Arcus**. Bot kullanıcının kendi Windows bilgisayarında çalışır. Anahtarlar yalnızca orada, Windows kullanıcı şifrelemesiyle (DPAPI, `connections.py`) saklanır. Sunucuya (Railway) anahtar konmaz.

## Aşama 1 — Borsa başı teminat (bu dalda yapıldı)

Önceki paper tek bir ortak cüzdan kullanıyordu. Gerçekte ise her borsada ayrı teminat tutulur, para borsalar arasında anında taşınamaz.

- Ayarlar → "Arşivle ve sıfırla" → Teminat: **Borsa başı**, tutar **1000**. Altı borsanın her biri $1000 ayrı teminatla başlar (toplam $6000). Önceki dönem arşivlenir.
- **Sadece hesap borsaları:** Paper yalnızca bu altı borsa arasında işlem açar. Diğer borsalar taramada görünmeye devam eder ama işlem havuzuna girmez.
- **Teminat bacak başına:** Her bacağın marjini ve ücreti yalnızca kendi borsasının boş teminatından karşılanır; rezerv de borsa başına uygulanır. Büyüklük, iki borsadan teminatı az olana göre küçülür.
- **Kâr öbür bacağı korumaz:** Bir borsanın özsermayesi kullandığı marjinin yarısına inerse pozisyon "Borsa teminatı riski" ile kapanır. Öbür borsadaki kâr bunu engellemez.
- **Funding:** Funding ve fiyat PnL'si ödendiği ya da gerçekleştiği borsanın teminatına yazılır.
- **Transfer yok:** Borsalar arasında transfer yapılmaz. Bir borsada biriken kâr, öbüründeki eksiği kapatmaz.
- **Teminat tablosu:** Çoklu paper ekranında borsa başı nakit, özsermaye, marjin, boş teminat ve veri durumu görünür.

## Aşama 1b — Gerçeğe yakınlık (bu dalda yapıldı)

- **Üç borsa masaya bağlandı:** TxFlow, RISEx ve Arcus artık masanın kendi beslemesinden (defter, 1h mumlar, funding geçmişi) çoklu paper masasına bağlı. Her borsa ancak gerçekten işlem açılabilecek veri tamsa işleme girer. Şu anki durum:
  - **Arcus:** Ücret tarifesi public API'den okunuyor. Ancak fiyat geçmişi oracle fiyatı; işlem fiyatı korelasyon kontrolünü geçmediği için çiftler genelde elenir.
  - **RISEx:** Taker ücreti public olarak doğrulanmadı, işlem açılmaz. Gerekçe panelde yazar.
  - **TxFlow:** Taker ücreti ve gerçekleşmiş funding geçmişi doğrulanmadı, işlem açılmaz.

  Bu iki boşluk Aşama 2'de hesap anahtarıyla (gerçek ücret kademesi, gerçek funding ödemeleri) kapanacak.
- **Lot kuralları:** Emir miktarı iki borsanın adım büyüklüğüne aşağı yuvarlanıyor. Borsanın asgari miktarı ya da asgari tutarı altında kalan işlem açılmıyor. Kurallar Lighter, Extended, TxFlow, RISEx ve Arcus'ta kendi API'lerinden, CCXT borsalarında (Hyperliquid) piyasa tanımından geliyor. Yayımlanmayan kural uydurulmuyor.
- **Ölçülen gecikme:** Her bacak, ayarlanan tabandan (750 ms) ya da o borsanın son 50 defter isteğinde ölçülen p90 gidiş-dönüş süresinden hangisi uzunsa o kadar bekleyip, sonra taze defterden doluyor. Bu süre borsa tablosunda görünüyor.

## Hâlâ modellenmeyenler

- **İki kontrol arasındaki fiyat sıçramaları:** Pozisyonlar her turda defterden işaretleniyor. Turlar arasındaki kısa ani hareket (fitil) görülmüyor. Gerçekte bu hareket likidasyona yol açabilir.
- **Gerçek ücret kademesi:** Public tarife kullanılıyor; Aşama 2'de hesaptan okunacak.
- **Dolum doğruluğu:** Paper'ın dolum fiyatının gerçekle ne kadar örtüştüğü ancak gerçek emirle ölçülebilir (Aşama 3, isteğe bağlı mikro emir).

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
