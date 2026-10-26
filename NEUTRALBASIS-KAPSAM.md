# Neutralbasis kapsam denetimi

Önceki sürüm arb.sh’nin tam karşılığı değildi: yalnızca beş native DEX, seçili 15 varlık ve iki platformda funding paper modeli vardı.

## Bu sürümde eklenenler

- Referanstaki 63 platformun tamamı için durum kataloğu. 30 platformda kodlanmış public veri adaptörü; çalışan, erişim hatası veren ve adaptörü eksik kaynaklar ayrı gösterilir.
- DEX/CEX/tümü seçimi; tüm erişilebilir kaynaklar arasında seçili 15 varlığın karşılaştırılması. Lighter Core ve Robinhood iki ayrı kaynaktır.
- Spot long + perpetual short taraması. Spot için son işlem fiyatı yerine alış kotasyonu kullanılır; yalnızca son işlem fiyatı bulunan kaynaklar çift sıralamasına alınmaz. Spot short, borç veya gizli kaldıraç varsayılmaz.
- Yüzde/bp, saatlik/8 saatlik/günlük/bileşiksiz APR gösterimi; farklı funding aralıkları normalize edilir. Aralığı bilinmeyen oran sıfır sayılmaz.
- Funding, spread, 7 günlük ortalamaya uzaklık, hacim, OI, kaldıraç ve net gösterge filtreleri. Bilinmeyen alanlar seçilen eşiği geçmez.
- Kayıtlı filtreler, filtre bağlantısı, favoriler, sütun seçimi, açık/koyu tema ve sayfalama.
- Panel açıkken yeni filtre eşleşmelerini gösteren yerel bildirim.
- Seçilen çiftte gerçek geçmişi indirme; gerçekleşmiş funding toplamları ve spread grafiği. Tam geçmiş varsa kesişim sayısı.
- Seçilen büyüklük için güncel dört yönün defter VWAP hesabı. Gelecekteki çıkış maliyeti güncel defterden bir vekildir; fiyat garantisi veya paper giriş onayı değildir.
- CEX ve desteklenen DEX’lerde 1/7/30/90 gün ve özel saat aralığıyla sabit tutuş backtesti. Her kaynakta bulunabilen geçmiş uzunluğuyla sınırlıdır.
- Fiyat snapshot’ları en fazla 30 saniye farklıysa ve en eski fiyat en fazla 90 saniyelikse çift sıralamaya alınır. Farklı zamanlarda gözlenen fiyatlar fırsat sayılmaz.

## Hâlâ eksik olanlar

- Katalogdaki 33 platformun public adaptörü henüz yazılmadı. Katalog kaydı bu platformun bağlandığı anlamına gelmez.
- Kodlanmış adaptörlerin tümü her ortamda erişilebilir olmayabilir; ağ/bölge engeli, eski yanıt ve eksik API alanları bağlantıyı engeller. Gerçek durum Borsalar ekranındadır.
- Otomatik paper yürütme hâlâ Lighter RH–Extended funding modeliyle sınırlıdır. Yeni CEX/DEX kaynakları otomatik emir havuzuna alınmadı.
- Otomatik fiyat yakınsaması stratejisinin giriş/çıkış yürütmesi, tüm borsalarda funding uzlaştırması ve hesap bağlantıları tamamlanmadı.
- Tüm çiftlerin geçmişi önceden hazır değildir; geniş evrende fiyat/funding geçmişi isteğe bağlı indirilir. Sürdürülebilirlik sıralaması, tüm çiftlerin günlük pozitif funding oranları ve otomatik tarihsel risk sıralaması eksiktir.
- Aynı borsada spot–perp backtesti henüz yok. Native geçmiş adaptörleri en fazla 30 günlük mevcut veri penceresini doğrular. CEX’lerde 90 gün yalnızca API geçmişi uygunsa hesaplanır.
- Tarihsel emir defteri replay, geçmiş likidasyon/marjin simülasyonu ve hesap bazlı tüm ücretler yoktur. Taker tarifeleri public API/CCXT varsayımlarıdır; kullanıcının hesabıyla doğrulanmış tarife olarak sunulmaz.
- Telegram/e-posta ve panel kapalıyken bildirim yoktur. Gerçek para işlemleri ve transfer bulunmaz.
- arb.sh’nin haber, farming, airdrop ve diğer yan ürün sayfaları bu sürüme dahil değildir.

## Kaynaklar ve doğrulama

Public veri için [CCXT](https://github.com/ccxt/ccxt) 4.5.90 ve mevcut native API adaptörleri kullanılır. Referans ekranları: [fiyat arbitrajı](https://arb.sh/price-arbitrage), [funding](https://arb.sh/funding?view=list), [backtest](https://arb.sh/backtest).

53 bağımsız motor ve hesap testi: ödeme aralığı normalizasyonu, 8 saatlik funding, spot funding’inin sıfır olması, eksik ödeme reddi, aynı ödemenin iki kez sayılmaması, çelişen kayıt reddi ve asenkron fiyat snapshot’larının çift sıralamasından çıkarılması dahil.

Yerel uygulama: http://127.0.0.1:8765/#exchanges

ZIP kurulumunda Kurulum.cmd bağımlılıkları yerel .venv ortamına yükler; Baslat.cmd motoru başlatır. Paket içindeki build-neutralbasis.cjs, JavaScript/CSS kaynakları değiştirildiğinde Node ile index.html dosyasını yeniden oluşturur. Çalıştırmak için Node gerekli değildir.


## Yakınsama paper stratejisi — 25 Ekim 2026
Lighter RH–Extended ortak 15 varlık için otomatik tarama eklendi. 30 gün kesintisiz işlem mumu; son 7 günde en az 6 sıfır kesişimi ve 4 çift yönlü gün; ortalama basis ve korelasyon/beta kapıları. Giriş boşluğu en az 5 bp ve geçmiş ortalama + 1 standart sapma üzerinde olmalı. Dört emir ücreti, güncel çıkış defteri vekili, olası funding gideri ve stres sonrası minimum net aranır. Gelecek çıkış defteri bilinmez. Çıkış ≤2 bp ve güncel net hedef; azami süre, kayıp ve marjin kontrolleri önceliklidir. Dolumlar sıralı VWAP, gecikme ve ek kayma modelidir; bir bacak başarısızsa kurtarma uygulanır. Funding geçmişi eksikse giriş yok.

Fonlama, fiyat PnL, ücret ve net Performans ekranında ve CSV içinde ayrıdır. Slippage dolum fiyatlarına dahildir; ayrıca düşülmez. Mevcut eski funding pozisyonları eski çıkış kurallarıyla izlenir. Yeni native paper girişler yakınsama kurallarını kullanır. Diğer borsa çiftlerinde otomatik paper yürütme henüz yok. Testler: 60 başarılı.


## İki fiyat grafiği
Fiyat arbitrajındaki çift detayı iki borsanın gerçek eşlenmiş 1h kapanışlarını iki ayrı renkte aynı fiyat ölçeğinde gösterir. Alt grafikte log(short/long) farkı, 7 günlük ortalama, sıfır, tarihsel giriş sinyali ve 2 bp çıkış sinyali bulunur. Sinyal eşiği masraf/risk onayı değildir. Geçmiş çift açıldığında public kaynaklardan istenir; eksik/oracle seriler doldurulmaz. Üzerine gelinen noktada zaman/fiyat gösterilir. Paper işaretleri eşleşen bacakların gerçek dolum zaman/fiyatlarından üretilir; aynı çiftin spot piyasasına perp işareti taşınmaz. Kayıt yoksa yapay işaret çizilmez.


## Funding uzlaştırması düzeltmesi — v2
Eksik Extended ödeme markı artık kalıcı bekleme nedeni değildir: önce ödeme öncesi 90 saniyede gözlenen mark, sonra ödeme öncesindeki son kapanmış 1h işlem mumunun kapanışı kullanılır. İkincisi settlement markı değil, denetim kaydıyla saklanan açık bir fiyat vekilidir. Tarihsel uç nokta da erişilemezse ödeme saatinden bir saat sonra kaydedilmiş gerçek paper giriş fiyatıyla tahmini değerleme yapılır. Yöntem, referans zamanı/fiyatı ve varsa kaynak denetim kimliği ödeme kaydında, pozisyon kaydında ve olay günlüğünde tutulur. CSV funding_valuation_methods alanını içerir. Tahmini tutar estimated_funding içinde ayrıca sayılır. Fiyat tahmini nakdi değiştirebilir; doğrulanmış gerçek hesap tahakkuku değildir.

Gerçekleşmiş funding oranı yoksa oran uydurulmaz ve ödeme bekler. Bu düzeltme eksik fiyatın yarattığı kilidi çözer; borsanın yayımlamadığı oranı çözmüş saymaz. Yeniden başlatmada daha önce yazılmış ödeme kayıtları doğrudan tanınır; geçici olarak boş funding yanıtı bunları tekrar bekleyen yapmaz. Aynı ödeme cüzdana ikinci kez eklenmez. Yeni testler: kesinti sonrası tarihsel mum, kapanmış pozisyonda giriş fiyatı vekili, yeniden başlatma, ileri zamanlı fiyat reddi, eksik oran ve denetim izi. Windows ortamında 65 test başarılı. Python kaynakları Black 25.1.0 ile biçimlendirildi. Miras zinciri ve eski temel motor döngüsü henüz birleştirilmedi.


## Yayımlanmayan funding saati — kilit düzeltmesi
Borsa bir ödeme saatinin gerçekleşmiş oranını hiç yayımlamazsa (bakım, geçmiş uç noktası kesintisi) o saat önceden sonsuza kadar bekleyen kalıyor ve tüm yeni girişleri durduruyordu. Artık ödeme saatinden 1 saat sonra hâlâ oran yoksa saat doğrulanmamış olarak kaydedilir: oran uydurulmaz, nakit 0 yazılır, ödeme kaydında unverified işareti, pozisyonda funding_unverified listesi ve FUNDING_UNVERIFIED olayı bulunur. Bekleme kalkar, yeni girişler açılır. Panel bu kaydı doğrulanmamış olarak gösterir; CSV funding_unverified_hours alanını içerir.

Oran sonradan yayımlanır ve pozisyon hâlâ izleniyorsa (açık pozisyon veya bekleyen ödemesi olan kapanmış pozisyon) doğrulanmamış kayıt gerçek ödemeyle bir kez değiştirilir ve FUNDING_LATE olayı yazılır; tutar cüzdana ikinci kez eklenmez. Bekleyen ödemesi kalmamış kapanmış pozisyonlarda geç gelen oran artık sorgulanmaz. Bu durumda doğrulanmamış saat 0 olarak kalır ve gerçek funding sonucundan farklı olabilir. Süre bot.py içindeki FUNDING_UNVERIFIED_AFTER sabitidir. Testler: 67 (1 atlanan).


## Çoklu borsa paper masası
Otomatik paper yürütme artık Lighter RH–Extended ile sınırlı değil. Yeni "Çoklu paper" sekmesi, taranan tüm borsaların (CCXT public adaptörleri + taker ücreti tanımlı Lighter RH ve Extended) perp–perp çiftlerini tek listede sıralar. Skor: horizon içinde her borsanın kendi funding aralığı ve sonraki ödeme saatine göre gerçekten ödeyeceği funding farkı, eksi dört taker ücreti. Taker ücreti veya funding aralığı bilinmeyen, yalnızca son işlem fiyatı olan ya da 3 dakikadan eski kaynaklar sıralamaya girmez.

En iyi adaylar (varsayılan 3, varlık başına bir tane) derin kontrolden geçer: aynı anda alınmış iki güncel defterde VWAP giriş/çıkış ve kayma, 7 günlük eşlenmiş 1h işlem mumlarıyla korelasyon/beta/z, 24 saatlik gerçekleşmiş funding. Long bacak güncel ve gerçekleşmiş orandan kötü olanı öder, short bacak düşük olanı alır. Spread 7g ortalamasının altındaysa yakınsama maliyet sayılır, üstündeyse kazanç sayılmaz. Net eşiği, kayıp bütçesi ve marjin rezervini geçen en iyi aday paper olarak açılır.

Funding her bacakta borsanın kendi ödeme zamanında gerçekleşmiş oranla uzlaştırılır; değerleme ödeme anındaki gözlenen mark, o yoksa 1 saat sonra giriş fiyatı (tahmini olarak işaretlenir). Yayımlanmayan ödeme 1 saat sonra doğrulanmamış 0 olarak kaydedilir. Çıkış: kullanıcı, pozisyon/günlük kayıp limiti, spread stres bütçesi, azami tutma süresi veya gelecek funding farkının tersine dönmesi.

Sermaye ayrı ve tek ortak paper cüzdandır (varsayılan $1000); mevcut Lighter RH–Extended masasının bakiyelerine dokunmaz. Gerçekte her borsada ayrı teminat ve transfer gerekir; bu modellenmez. Gerçek emir ve API anahtarı yoktur. Spot–perp çiftleri ve pozisyon rotasyonu henüz bu masada yok. Testler: 88 (1 atlanan).


## Çoklu borsa: iki strateji
**Funding (korele çiftler).** Önceki sıralama korunur; derin kontrolde artık 7 gün ve 30 günlük 1h getiri korelasyonunun ikisi de eşik üstünde olmalıdır. Çıkış: kayıp/stres limitleri, funding farkının tersine dönmesi, ayrıca kâr kilidi. Pozisyonun kapatılırsa net değeri en az asgari net kadar zirve yaptıktan sonra zirvenin `wide_lock_trail` (varsayılan %50) kadarını geri verirse ve hâlâ pozitifse kapanır. Gelecek funding asgari netin altına inerse ve pozisyon kârdaysa kâr alınır. Giriş ücretleri peşin ödendiği için yeni pozisyon negatiften başlar; kayıp limitleri, stres ve funding dönüşü zararla kapatabilir. "Hiç zarar etmeden" garanti edilemez.

**Fiyat yakınsaması (her borsa çifti).** Ucuz borsada long, pahalıda short; eşit miktar, 5x (borsa sınırı daha düşükse o). 30 günlük eşlenmiş 1h işlem mumlarında fiyat farkı: 7 günde en az 6 kesişim ve 4 çift yönlü gün, 30 günde en az 20 kesişim ve 15 çift yönlü gün; 30 günlük ortalama fark sıfıra yakın olmalı (kalıcı bazis yok). Bugünkü genişliğin yarısına ulaşan geçmiş açılmaların kaç saatte kapandığı ölçülür (en az 3 örnek); ortalama süre beklenen tutma, iki katı (en çok 72 saat) azami tutmadır. Net = giriş VWAP boşluğu − yarım defter kayması − 4 taker ücreti + beklenen sürede ödenecek/alınacak funding (gerçekleşmiş oran varsa kötü olanı). Fiyat boşluğu 7g ortalama + 1 standart sapmanın ve 5 bp'nin üstünde olmalı. Çıkış: fiyatlar kesişince, makas 2 bp altına inip net hedef sağlanınca, beklenen kazanç erken gelince veya azami süre dolunca; kayıp/stres limitleri her zaman önceliklidir.

Her iki strateji aynı ortak paper cüzdanı kullanır, varlık başına bir pozisyon açılır ve her turda iki stratejinin en iyi adaylarından neti en yüksek olan seçilir. Testler: 95 (1 atlanan).


## Son şartname uyumu
- Ortak hesap: net = iki bacağın fiyat PnL'si + net funding − komisyon; kayma dolum fiyatlarında. Strateji bazlı ayrı rapor (açık, işlem, kârlı, fiyat PnL, funding, komisyon, net, doğrulanmamış/bekleyen ödeme) panelde.
- Yakınsama: beklenen kazanç, farkın 7g ortalamasına (veya sıfıra) dönmesiyle hesaplanır; dört ücret, kayma, yalnızca funding gideri (gelir sayılmaz) ve 7g fark standart sapması kadar risk payı düşülür. Fark ortalama/kesişim bölgesine gelince kapanış sonrası net asgari netin üstündeyse kapanır; tam kesişim beklenmez.
- Funding: ayrıca 7g fark standart sapması `wide_max_spread_sd_bps` (30 bp) altında olmalı; aynı risk payı nette düşülür. Funding dönüşü/zayıflığı `wide_weak_minutes` (30 dk) boyunca sürmezse çıkılmaz. Açık pozisyonda 7g/30g korelasyon düşerse veya z-skoru eşik+1'i aşarsa "Fiyat uyumu bozuldu". Kâr hedefi (beklenen net) ve kâr kilidi korunur.
- Kaldıraç tavandır: bir bacağın tutma süresindeki 4 sigma hareketi tasfiye mesafesinin yarısını geçmeyecek şekilde düşürülür. Bir bacağın zararı kendi marjininin yarısına ulaşırsa "Bacak marjin riski" ile kapanır.
- Her işlemde strateji, giriş gerekçesi ve kapanış nedeni kaydedilir. Bir bacak dolmazsa dolan bacak kapatılır. Funding API'si veya piyasa listesi bozuk olsa da defterden izleme ve kapatma sürer; bekleyen ödemeler sonradan uzlaştırılır. Testler: 102 (1 atlanan).


## Arka planda geçmiş verisi
Listelenen her piyasa (native DEX'ler ve CCXT perp/spot) için 1h kapanış mumları ve gerçekleşmiş funding arka planda, piyasalar arasında 1 saniye bekleyerek tek tek çekilir ve SQLite'a (`hist_candles`, `hist_funding`) yazılır. İlk geçişte 30 gün alınır; sonra her piyasa 30 dakikada bir yalnızca son kayıttan sonraki yeni satırlarla güncellenir. Hata veren piyasa 30 dakika sonra tekrar denenir. Yeniden başlatmada kayıtlı geçmiş istek atmadan hemen panele yüklenir. 35 günden eski satırlar silinir. Funding arb / Fiyat arb satırlarındaki 7g ortalama, kesişim ve ödenen funding sütunları "Geçmişi getir" beklemeden dolar; çoklu borsa masası da 30 günlük geçmiş tamamsa kayıtlı veriyi kullanır. İlk dolum piyasa sayısına göre yaklaşık 20–40 dakika sürer; durum üst şeritte "GEÇMİŞ x/y PİYASA" olarak görünür.

## Funding matrisi (arb.sh/funding benzeri)

- Funding arb sekmesi varsayılan olarak **Borsa matrisi** açılır: satırlar varlık, sütunlar borsa; oranlar seçili gösterim periyodunda (1H/8H/24H/APR).
- Kapsam: toplu funding uç noktası (`fetchFundingRates`) olan her borsada listelenen **tüm** lineer USDT/USDC/USD perp'ler tek çağrıyla taranır. Toplu uç noktası olmayan borsalar ve yerel beslemeler (Lighter RH, Extended, TxFlow, RISEx, Arcus) seçili varlıklarla sınırlı kalır.
- En geniş fark: çift filtresine (DEX içeren / Yalnız DEX / Yalnız CEX / Hepsi) uyan en yüksek oranlı borsada short, en düşükte long. Hücreler LONG/SHORT olarak işaretlenir.
- Net son 7g: o çiftin kayıtlı gerçekleşen funding ödemeleri farkı × büyüklük, maliyetler açıksa 4 emir komisyonu düşülür. 7 günlük ödemeler tam kayıtlı değilse "geçmiş eksik" yazar; tahmin yapılmaz.
- Seçili olmayan varlıklarda funding geçmişi, en geniş farklı ilk 40 varlığın iki ucu için arka plan geçmiş kaydedicisi tarafından doldurulur (yalnız funding, mum çekilmez).
- İstikrarlı: net 7g > 0. Yüksek getiri: fark ≥ %0,1 / 8s. 50 satır, "100 daha göster".
- Veri `/api/funding-matrix` üzerinden, 60 sn önbellekli.

## Özet ana sayfa (arb.sh ana sayfası benzeri)

Adres çubuğunda sekme yoksa panel **Özet** ile açılır. Yalnızca masanın gerçekten sahip olduğu verilerden hesaplanır:
- Kutucuklar: DEX'lerde BTC funding ortalaması (CEX ortalaması ve en yüksek DEX ile), son 7 gün gerçekleşen funding'e göre ücret sonrası kârda olan çift sayısı, 7g ortalamaya dönüşte kârda olan fiyat farkı sayısı, kapsam (varlık/borsa), çoklu paper özsermaye.
- Tablolar: en iyi funding çiftleri (fark ve APR), en geniş fiyat farkları (seçili varlıklar), uç funding (tek piyasa), yeni listelemeler.
- Yeni listelemeler: bir borsanın ilk taraması mevcut listeyi yalnızca kaydeder; sonrasında hiçbir borsada görülmemiş bir perp çıkarsa listelenir, 7 gün boyunca ekleyen borsalar eklenir (`kv: matrix_listings`).
- DEX hacmi/OI, haberler ve soru kutusu için veri kaynağımız olmadığından eklenmedi.

## Aday döndürme, öncelikli geçmiş ve inceleme paneli

- Derin kontrolde elenen çift 30 dakika kenara alınır (eksik geçmiş nedeniyle elendiyse 15 dakika). Sonraki turlar aynı ilk adayları tekrar kontrol etmek yerine listede sıradakilere geçer; süre dolunca çift yeniden sıraya girer.
- Geçmiş kaydedici her 10 piyasada bir listeyi yeniden okur ve önce işlem yapılabilir adayların bacaklarını doldurur: eksik geçmiş yüzünden elenen çiftler en önde, ardından her stratejinin pozitif skorlu ilk 40 adayı.
- Çoklu paper ekranındaki "Aday inceleme" kartı tur sayısını, derin kontrol sayısını, uygun ve atlanan adayları, elenme nedenlerinin dağılımını (toplam ve şu an bekleyen) ve son elenen çiftleri tekrar deneme saatiyle gösterir. Sayaçlar bellek içindedir; yeniden başlatınca sıfırlanır.

## Ana sayfa fiyat farkları: tüm varlıklar

"En geniş fiyat farkları" artık seçili 15 varlıkla sınırlı değil. Funding matrisinin toplu taramasından gelen mark fiyatlarını kullanır: her varlıkta en ucuz borsada long, en pahalıda short. Aynı ticker'ı taşıyan farklı tokenler sahte fark göstermesin diye, borsalar arası fark %15'i aşarsa medyandan en uzak fiyat atılır ve kalanlar uyuşana kadar devam edilir. Mark fiyatı anlık göstergedir; işlem kararı Fiyat arb ve çoklu paper masasındaki defter/geçmiş kontrolleriyle verilir.


## Varlık ve çift sayfaları
Funding matrisindeki her varlık için `#asset?asset=BTC` sayfası eklendi. Varlığın tüm borsalardaki güncel oranı, ödeme aralığı, sonraki ödemeye kalan süre, mark fiyatı ve taker ücreti burada görünür. Sayfada ayrıca kayıtlı gerçekleşmiş funding'in 24s/7g/30g toplamları, en iyi funding çifti ve en geniş makul fiyat farkı (%15'ten geniş fark farklı token sayılır) bulunur. Seçili olmayan varlıklarda OI, hacim ve kaldıraç okunmaz.

Her perp–perp çifti için `#pair?asset=…&long=…&short=…` sayfası eklendi. Sayfada long/short seçicileri, ⇄ ve favori düğmeleri ile bacak tablosu (ödeme başına $, aralık, geri sayım, ücret) var. "Şimdi açsaydın" bölümü 24S/3G/7G/14G/30G pencereleri için gerçekleşen funding'den dört taker ücretini düşer. Pencere yalnızca iki bacağın tüm ödemeleri kayıtlıysa hesaplanır, eksik pencere ölçeklenmez. Kâr hesaplayıcı anlık oranla ya da tam 7 günlük ortalamayla çalışır ve başa baş süresini verir. "Açmadan önce" listesi ücretleri, aralıkları ve 24 saatlik ödeme projeksiyonunu gösterir. Aynı listede funding yönü, son 7 gün, fiyat farkı, likidite ve marjin kontrolleri de var. Kümülatif funding grafiği yalnızca iki bacağın da kayıtlı olduğu aralığı çizer. Varlığın tüm borsalardaki oranları da sayfada listelenir.

"Geçmişi doldur" düğmesi seçilen bacakların 30 günlük gerçekleşmiş funding'ini arka planda kaydeder. Tek istekte en çok 4 bacak doldurulur ve aynı bacak 10 dakikada bir kez istenir. Fiyat farkı değişimi ve kayma bu sayfalardaki sonuçlara dahil değildir. Matristeki ve ana sayfadaki satırlar bu sayfalara bağlanır. Üst aramada varlık adı yazıp Enter'a basınca varlık sayfası açılır, `/` tuşu aramaya odaklar.

Desteklenmeyenler: DEX hacim/OI/pazar payı geçmişi (Stats/Charts), OI hareketleri, haber, airdrop, farm, vault ve soru paneli. Tüm borsalarda kaldıraç, KYC ve trade bağlantıları da desteklenmiyor. Bunlar için toplu kaynak bağlı değildir.


## En iyi funding çiftleri ve çift sayfası — arb.sh hizası
Toplu tarama artık 24 saatlik hacmi okuyor; borsanın toplu çağrısı varsa OI ve kaldıraç sınırı da okunuyor. Ana sayfadaki en iyi funding çiftleri için en az bir DEX bacağı gerekiyor. İki bacakta da OI ≥ $1M olmalı; OI yayımlanmıyorsa 24 saatlik hacme bakılıyor. Fiyatları %15'ten fazla ayrışan bacaklar (aynı ticker'ı taşıyan farklı tokenler) eleniyor. Uç funding kartı OI ≥ $2M istiyor. Matrise likidite filtresi eklendi: hepsi, ≥ $1M ve ≥ $10M; varsayılan ≥ $1M. Yerel DEX beslemeleri (Lighter RH, Extended, TxFlow, RISEx, Arcus) hacim ya da OI yayımlamaz. Bu beslemeler yalnızca seçili varlıkları kapsadığı için filtreyi geçer.

Çift sayfasına şunlar eklendi:
- Ödemeler bacak başına "al/öde %/aralık" olarak gösteriliyor.
- "Şimdi açsaydın" bölümü artık funding + fiyat − maliyet = sonuç şeklinde. Fiyat bileşeni, şu anki farkın pencere ortalamasına dönmesi varsayımıyla hesaplanıyor ve iki bacağın kayıtlı 1h kapanışlarıyla bulunuyor. Pencere saatlerinin en az %90'ı gerekiyor.
- Anlık funding farkı son 7 günün ortalamasının iki katından fazlaysa uyarı çıkıyor.
- Grafikte Fiyat farkı, Fiyatlar ve Funding sekmeleri var. Her biri 24S/3G/7G/14G/30G aralıklarıyla açılabiliyor.
- "Defterleri oku" düğmesi iki bacağın güncel defterinden $10k, $100k ve $1M için tek yön giriş maliyetini hesaplıyor (taker ücreti + kayma, bp). Defter okunduysa sonuçtaki maliyet bu değerle (giriş + aynı tutarda çıkış) hesaplanıyor.
- Kontrol listesine şunlar eklendi: defterlere sığma, azami kaldıraçta marjin, farklı ödeme aralıklarında 24 saatlik projeksiyon, ortalamadan dar fark ve ince bacak.

"Geçmişi doldur" düğmesi seçili olmayan varlıklarda da iki bacağın 30 günlük 1h mumlarını kaydediyor; yerel DEX'ler hariç.

Desteklenmeyenler:
- Giriş/çıkış geçmişi grafiği. Bunun için geçmiş defter örnekleri gerekiyor.
- Borsa trade bağlantıları.
- Puan/airdrop rozeti.
- KYC kontrolü.
- MEXC. Toplu funding yok, bu yüzden seçili varlıklar dışındaki MEXC çiftleri görünmez.
