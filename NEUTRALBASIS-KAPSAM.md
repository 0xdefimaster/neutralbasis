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

En iyi adaylar derin kontrolden geçer (her turda her strateji için varsayılan 5, turda varlık başına bir çift; geçmişi eksik çıkanlar bu 5'e sayılmaz, turda en fazla 20; elenen çift kenara alınır ve sonraki turlar listede aşağı iner, bkz. "Aday hunisi"): aynı anda alınmış iki güncel defterde VWAP giriş/çıkış ve kayma, 7 günlük eşlenmiş 1h işlem mumlarıyla korelasyon/beta/z, 24 saatlik gerçekleşmiş funding. Long bacak güncel ve gerçekleşmiş orandan kötü olanı öder, short bacak düşük olanı alır. Spread 7g ortalamasının altındaysa yakınsama maliyet sayılır, üstündeyse kazanç sayılmaz. Net eşiği, kayıp bütçesi ve marjin rezervini geçen en iyi aday paper olarak açılır.

Funding her bacakta borsanın kendi ödeme zamanında gerçekleşmiş oranla uzlaştırılır; değerleme ödeme anındaki gözlenen mark, o yoksa 1 saat sonra giriş fiyatı (tahmini olarak işaretlenir). Yayımlanmayan ödeme 1 saat sonra doğrulanmamış 0 olarak kaydedilir. Çıkış: kullanıcı, pozisyon/günlük kayıp limiti, spread stres bütçesi, azami tutma süresi veya gelecek funding farkının tersine dönmesi.

Sermaye ayrı ve tek ortak paper cüzdandır (varsayılan $1000); mevcut Lighter RH–Extended masasının bakiyelerine dokunmaz. Gerçekte her borsada ayrı teminat ve transfer gerekir; bu modellenmez. Gerçek emir ve API anahtarı yoktur. Spot–perp çiftleri ve pozisyon rotasyonu henüz bu masada yok. Testler: 88 (1 atlanan).


## Çoklu borsa: iki strateji
**Funding (korele çiftler).** Önceki sıralama korunur; derin kontrolde 7 günlük 1h getiri korelasyonu eşik üstünde olmalıdır. 30 günlük korelasyon artık giriş şartı değil, bağlamdır (bkz. "Funding kararı: 1, 3 ve 7 gün; 30 gün bağlam"). Çıkış: kayıp/stres limitleri, funding farkının tersine dönmesi. (Kâr kilidi ve kâr hedefi kaldırıldı; bkz. "Çıkışlar stratejinin tezine bağlandı".) Gelecek funding asgari netin altına inerse ve pozisyon kârdaysa kâr alınır. Giriş ücretleri peşin ödendiği için yeni pozisyon negatiften başlar; kayıp limitleri, stres ve funding dönüşü zararla kapatabilir. "Hiç zarar etmeden" garanti edilemez.

**Fiyat yakınsaması (her borsa çifti).** Ucuz borsada long, pahalıda short; eşit miktar, 5x (borsa sınırı daha düşükse o). 30 günlük eşlenmiş 1h işlem mumlarında fiyat farkı: 7 günde en az 6 kesişim ve 4 çift yönlü gün, 30 günde en az 20 kesişim ve 15 çift yönlü gün; 30 günlük ortalama fark sıfıra yakın olmalı (kalıcı bazis yok). Bugünkü genişliğin yarısına ulaşan geçmiş açılmaların kaç saatte kapandığı ölçülür (en az 3 örnek); ortalama süre beklenen tutma, iki katı (en çok 72 saat) azami tutmadır. Net = giriş VWAP boşluğu − yarım defter kayması − 4 taker ücreti + beklenen sürede ödenecek/alınacak funding (gerçekleşmiş oran varsa kötü olanı). Fiyat boşluğu 7g ortalama + 1 standart sapmanın ve 5 bp'nin üstünde olmalı. Çıkış: fiyatlar kesişince, makas 2 bp altına inip net hedef sağlanınca veya azami süre dolunca ("beklenen kazanç erken gelince" çıkışı kaldırıldı); kayıp/stres limitleri her zaman önceliklidir.

Her iki strateji aynı ortak paper cüzdanı kullanır, varlık başına bir pozisyon açılır ve her turda iki stratejinin en iyi adaylarından neti en yüksek olan seçilir. Testler: 95 (1 atlanan).


## Son şartname uyumu
- Ortak hesap: net = iki bacağın fiyat PnL'si + net funding − komisyon; kayma dolum fiyatlarında. Strateji bazlı ayrı rapor (açık, işlem, kârlı, fiyat PnL, funding, komisyon, net, doğrulanmamış/bekleyen ödeme) panelde.
- Yakınsama: beklenen kazanç, farkın 7g ortalamasına (veya sıfıra) dönmesiyle hesaplanır; dört ücret, kayma, yalnızca funding gideri (gelir sayılmaz) ve 7g fark standart sapması kadar risk payı düşülür. Fark ortalama/kesişim bölgesine gelince kapanış sonrası net asgari netin üstündeyse kapanır; tam kesişim beklenmez.
- Funding: ayrıca 7g fark standart sapması `wide_max_spread_sd_bps` (30 bp) altında olmalı; aynı risk payı nette düşülür. Funding dönüşü/zayıflığı `wide_weak_minutes` (30 dk) boyunca sürmezse çıkılmaz. Açık pozisyonda 7g korelasyon düşerse veya z-skoru eşik+1'i aşarsa "Fiyat uyumu bozuldu" (30 günlük korelasyon pozisyon kapatmaz). Kâr hedefi ve kâr kilidi sonradan kaldırıldı.
- Kaldıraç tavandır: bir bacağın tutma süresindeki 4 sigma hareketi tasfiye mesafesinin yarısını geçmeyecek şekilde düşürülür. Bir bacağın zararı kendi marjininin yarısına ulaşırsa "Bacak marjin riski" ile kapanır.
- Her işlemde strateji, giriş gerekçesi ve kapanış nedeni kaydedilir. Bir bacak dolmazsa dolan bacak kapatılır. Funding API'si veya piyasa listesi bozuk olsa da defterden izleme ve kapatma sürer; bekleyen ödemeler sonradan uzlaştırılır. Testler: 102 (1 atlanan).


## Arka planda geçmiş verisi
Listelenen her piyasa (native DEX'ler ve CCXT perp/spot) için 1h kapanış mumları ve gerçekleşmiş funding arka planda, piyasalar arasında 1 saniye bekleyerek tek tek çekilir ve SQLite'a (`hist_candles`, `hist_funding`) yazılır. İlk geçişte 30 gün alınır; sonra her piyasa 30 dakikada bir yalnızca son kayıttan sonraki yeni satırlarla güncellenir. Hata veren piyasa 30 dakika sonra tekrar denenir. Yeniden başlatmada kayıtlı geçmiş istek atmadan hemen panele yüklenir. 35 günden eski satırlar silinir. Funding arb / Fiyat arb satırlarındaki 7g ortalama, kesişim ve ödenen funding sütunları "Geçmişi getir" beklemeden dolar; çoklu borsa masası da 30 günlük geçmiş tamamsa kayıtlı veriyi kullanır. İlk dolum piyasa sayısına göre yaklaşık 20–40 dakika sürer; durum üst şeritte "GEÇMİŞ x/y PİYASA" olarak görünür.

## Funding matrisi (arb.sh/funding benzeri)

- Funding arb sekmesi varsayılan olarak **Borsa matrisi** açılır: satırlar varlık, sütunlar borsa; oranlar seçili gösterim periyodunda (1H/8H/24H/APR).
- Kapsam: toplu funding uç noktası (`fetchFundingRates`) olan her borsada listelenen **tüm** lineer USDT/USDC/USD perp'ler tek çağrıyla taranır. Toplu uç noktası olmayan borsalar ve yerel beslemeler (Lighter RH, Extended, TxFlow, RISEx, Arcus) seçili varlıklarla sınırlı kalır.
- Anlık funding farkı: çift filtresine (DEX içeren / Yalnız DEX / Yalnız CEX / Hepsi) uyan en yüksek oranlı borsada short, en düşükte long; şu anki oranlardır, fiyat farkı değildir. Hücreler LONG/SHORT olarak işaretlenir. Kazanç iki oranın farkıdır: negatif oranda long funding alır, short öder; bu yüzden iki negatif oran da seçilebilir. Filtre daha geniş bir çifti dışarıda bıraktıysa satırda "DEX şartı · filtresiz S …" yazar.
- ↺ notu: gösterilen yön son 7 günde kaybederken ters yön kazanırdıysa satırda ters yönün 7 günlük sonucu ve bugün açılırsa günde ödeyeceği funding yazar. Bu not oranların yakın zamanda döndüğünü gösterir; ters yönün geçmiş kazancı ileriye taşınmaz. Aynı not İki borsa görünümünde de var.
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

## Borsa başı teminat
Çoklu paper masası, sıfırlamada "Borsa başı" seçilirse işaretlenen hesap borsalarının her birine ayrı teminat verir (varsayılan 15 perp borsası: Lighter, Extended, Hyperliquid, TxFlow, RISEx, Arcus, Binance, Bybit, OKX, Bitget, Gate, Aster, Paradex, dYdX, GRVT; borsa başı $1000). Yalnızca bu borsalar arasında işlem açılır. Her bacağın marjini, ücreti, funding'i ve fiyat PnL'si kendi borsasının teminatına yazılır. Büyüklük teminatı az olan borsaya göre küçülür. Bir borsanın özsermayesi marjininin yarısına inerse pozisyon "Borsa teminatı riski" ile kapanır. Borsalar arası transfer modellenmez ve yapılmaz. TxFlow, RISEx ve Arcus'un çoklu paper veri adaptörü henüz yok; tabloda "veri yok" görünürler ve işlem açmazlar. Gerçek hesap aşamaları: CANLI-HESAP-PLANI.md.

## Gerçeğe yakınlık: lot, gecikme, üç ek borsa
TxFlow, RISEx ve Arcus çoklu paper masasına masanın kendi beslemesiyle bağlandı. Taker ücreti ya da funding geçmişi doğrulanmayan borsa işlem havuzuna girmez; gerekçe borsa tablosunda görünür. Emir miktarı iki bacağın adım büyüklüğüne aşağı yuvarlanır, asgari miktar ve tutar altındaki girişler reddedilir. Her bacağın dolum gecikmesi, ayarlanan taban ile o borsada ölçülen p90 defter isteği süresinden büyüğüdür.

## Borsa başı ücret ve dönem raporu
Ayarlar'da her borsa için kullanıcının kendi hesabındaki taker ücreti girilebilir; girilmeyen borsada public tarife kullanılır, ücreti bilinmeyen borsa işlem açmaz. Çoklu paper ekranındaki dönem raporu süreyi, kesintiyi, işlem sayısını, ücret sonrası neti, kazanç/kayıp oranını, en iyi 3 işlemin nete payını, girişlerin kapalı kaldığı süreyi, dönem içi ayar değişikliklerini ve borsa teminatı riski kapanışlarını eşikleriyle gösterir. Motor Windows'ta çalışırken uyku modunu engeller; OtomatikBaslat.cmd motoru oturum açılınca ve her 5 dakikada bir kontrol edip gerekirse yeniden başlatır. Adımlar: CANLI-HESAP-PLANI.md.

## Geçmiş verisi: borsa başına paralel, tüm likit perp'ler
Arka plan geçmiş kaydedicisi artık her borsa için ayrı bir işçiyle çalışır (Lighter, Extended, TxFlow, RISEx ve Arcus kendi beslemeleriyle tek işçide). Yavaş ya da istek sınırına takılan bir borsa diğerlerini bekletmez. Kaydedilen piyasalar: seçili varlıklar, en geniş funding farkları, paper adaylarının bacakları ve paper masasının işlem açabileceği bütün likit perp'ler (her bacakta OI ya da 24 saatlik hacim en az $1M). Paper adaylarının bacakları her işçide önce doldurulur. Beklenmeyen bir hata işçiyi durdurmaz; bir dakika sonra devam eder.

## Funding kararı: 1, 3 ve 7 gün; 30 gün bağlam
Funding girişi artık uzun dönem korelasyonuna takılmıyor. Karar kısa dönem sinyaline ve borsaların gerçekten ödediği funding'e dayanıyor:
- **1 gün:** Her bacak, güncel oranı ile son 24 saatte ödenen oranın kendi aleyhine olanıyla hesaplanır (long yüksek olanı öder, short düşük olanı alır). Bu tahmin pozitif değilse giriş yok.
- **3 ve 7 gün:** Borsaların yayımladığı ödemelerden bu yönün gün gün aldığı funding hesaplanır (short'un aldığı − long'un ödediği). Son 3 günün ve son 7 günün toplamı pozitif olmalı; 7 günlük toplam en iyi gün çıkarılınca da pozitif kalmalı, yani avantaj tek bir sıra dışı güne dayanmamalı. Her bacağın penceresi, borsanın güncel ödeme aralığının gerektirdiği ödemelerin en az %95'ini içermeli ve ödemeler arasında 8 saatten uzun boşluk olmamalı; yoksa geçmiş eksik sayılır (ret nedeninde "x/y ödeme" yazar) ve çift 15 dakika sonra yeniden denenir. Eksik ödeme sıfır ya da kâr varsayılmaz: saatlik ödeyen bir borsada yalnızca 8 saatte bir kayıt gelirse, eksik ödemeler daha az maliyet gibi okunmaz. Ödeme aralığını kısaltan bir borsa (ör. 8 saatten 1 saate), pencere yeni aralıkla dolana kadar bekler. Borsanın API'si haftanın tamamını vermiyorsa arka plan kaydedicisinin sakladığı ödemeler tamamlar; aynı ödeme bir kez sayılır.
- **Beklenen funding:** 1 günlük tahmin ile son 3 günün ortalamasından düşük olanı. Yalnızca bugün ortaya çıkmış bir avantaj bugünkü seviyesiyle varsayılmaz. Ücretler, kayma ve fiyat farkı risk payı düşüldükten sonra asgari neti geçmelidir.
- **Fiyat riski:** Funding stratejisi fiyatların kesişmesini beklemez. Son 7 günde fiyatların birlikte hareket edip etmediği (getiri korelasyonu, beta, z-skoru, fiyat farkı standart sapması) kontrol edilir. Ayrışmanın olası zararı stres payı olarak büyüklüğü sınırlar; 7g fark standart sapması kadar risk payı nette düşülür. Düzenli kesişim yalnızca yakınsama stratejisinin şartıdır.
- **30 gün (bağlam):** Uzun dönem (30 güne kadar mevcut pencere) giriş şartı değildir. Getiri korelasyonu eşiğin altındaysa veya o pencerede bu yönde gerçekleşen funding negatifse not düşülür ve her çelişki sanal büyüklüğü yarıya indirir. Uzun dönem verisi yoksa yalnızca not düşülür. Açık pozisyon da 30 günlük korelasyon yüzünden kapanmaz; "Fiyat uyumu bozuldu" çıkışı 7 günlük korelasyon ve z-skoruna bakar.
- **Eleme nedenleri** ölçülen değer ve eşikle yazılır (ör. "Avantaj tek bir güne dayanıyor · 7g +$0.12, en iyi gün hariç -$4.68"). Aday inceleme kartı nedenleri değerler olmadan gruplar. Çift sayfası uzun dönem notlarını ve büyüklük indirimini gösterir.

Yakınsama stratejisi değişmedi: 7 ve 30 günlük düzenli kesişim ve korelasyon şartları onun tezidir. Testler: 247 (1 atlanan).

## Çift sayfası: gerçekleşen, tahmin ve defter ayrı
Çift sayfasındaki "Şimdi açsaydın ve tutsaydın" kartı gerçekleşen funding ile ileriye dönük bir fiyat varsayımını tek "Sonuç" rakamında topluyordu; "Kâr hesaplayıcı" da tahmini funding ile fiyatın ortalamaya dönüşünü aynı nete ekliyordu. Bunlar farklı varsayımlar olduğundan ayrıldı:
- **Gerçekleşen · son N gün** (24S/3G/7G/14G/30G): çift N gün önce açılıp şimdi kapansaydı. Kalemler ayrı: funding (borsaların yayımladığı ödemeler, bacak başına tutar ve ödeme sayısı), fiyat (iki bacağın 1s kapanışlarıyla long getirisi − short getirisi; başlangıç ve bitiş saati yazılır, pencere başındaki kapanış en çok 1 saat geç olabilir), ücret (4 taker emri; kullanıcının girdiği hesap ücreti varsa o), kayma (bugünkü defterde bu büyüklükte giriş + çıkış). Bir kalem eksikse net boş kalır ve eksik kalem yazılır; kısmi sonuç tam sonuç gibi gösterilmez.
- **Tahmin · sonraki 1/7/30 gün**: iki sütun, güncel oranla ve son 7 günde ödenen ortalamayla. Güncel oranlı funding her bacağın sonraki ödeme saatinden ve kendi aralığından sayılan ödeme adediyle hesaplanır. Ücret + kayma bir kez düşülür. Fiyat farkının 7g ortalamasına dönmesi ve en geniş seviyesine gitmesi ayrı senaryo satırlarıdır, nete eklenmez. Uç oran uyarısı ve başa baş süresi burada.
- **Defter · maliyet ve kapasite**: sayfa açılınca iki bacağın defteri bacak büyüklüğünde bir kez okunur (yenile düğmesi var). Bacak başına yarım spread, bu büyüklükte giriş ve çıkış kayması ile ücreti ayrı (bp), en kötü dolumu ortadan 10/25/50 bp içinde tutan gidiş-dönüş kapasitesi (giriş ve çıkış tarafından küçüğü; bacak büyüklüğünün altındaysa kırmızı), görünen derinlik, defter yaşı ve $10K/$100K/$1M referans maliyetleri.
- **Veri yaşı**: her bacağın oranı ve fiyatı ne kadar önce okundu, kayıtlı son ödeme ne kadar eski. Son ödeme kaydı ödeme aralığından 30 dakika fazla gerideyse "kayıt geride" uyarısı ve kontrol listesinde "Veri güncelliği" dikkat olarak görünür. Taker sütunu kullanıcının hesap ücretini "hesabım" diye işaretler.

## Aday hunisi: taramadan açılan işleme
Çoklu paper ekranındaki "Aday inceleme" kartının başında artık her aşamanın sayısı var:
- Taranan perp piyasası; işlem görebilir piyasa ve dışarıda kalanların nedeni (hesap borsası değil, taker ücreti bilinmiyor, funding oranı/aralığı yok, fiyat yok, yalnızca son işlem fiyatı, veri eski); iki borsada işlem görebilen varlık ve borsa çifti sayısı.
- Her strateji için: aday, ücret sonrası pozitif skorlu aday, son 1 saatte derin kontrol edilen benzersiz çift, kontrol derinliği, kenarda bekleyen çift ve son turun sonucu (incelendi / uygun / açıldı / uygun ama açılamadı).
- **Kontrol derinliği**, listenin başından kaçıncı adaya kadar her çiftin ya son 30 dakikada incelendiğini, ya kenara alındığını ya da engelli olduğunu (açık pozisyon, kapanış sonrası bekleme) gösterir. Ondan sonraki adayların sırası henüz gelmemiştir. Böylece ekranda görünen bir fırsatın motora hiç ulaşıp ulaşmadığı görülebilir.

Derin kontrol yalnızca ilk birkaç adaya bakmıyor: her turda her strateji için en iyi 5 aday incelenir, elenen çift kenara alınır ve sonraki turlar listede aşağı iner. Ancak elenen çiftler 30 dakika sonra listenin başına geri dönüyordu. Yavaş değişen bir nedenle elenen en üst sıradaki çiftler (ör. 7 günde funding avantajı yok) her yarım saatte bir kontrol hakkını yeniden kullanıyor, aşağıdaki çiftlere sıra gelmeyebiliyordu. Artık günlerle ölçülen nedenler daha uzun bekler: 3 günlük funding ve normal fiyat farkının kayması 3 saat; 7 günlük funding, avantajın tek güne dayanması, 7g/30g korelasyon ve hedge betası 6 saat. Bir çift en yavaş nedeni kadar bekler, çünkü bütün nedenleri kalkmadan uygun olamaz. Geçmiş eksikse 15 dakika, diğer nedenlerde 30 dakika aynen kalır.

## Gölge paper: kısmi dolum, stres ve karne
Paper zaten emir göndermeyen bir gölge paper: canlı public veride çalışır, dolumları o anki defterden hesaplar, funding'i ödeme anında borsanın yayımladığı oranla işler. Bu bölümle eksik kalan gerçekçilik ve ölçüm parçaları eklendi:
- **Kısmi dolum:** Giriş emri, planlanan fiyatın %0,1 ötesine kadar "hemen ya da iptal" (IOC) emridir. Önceden bütün miktar defterde bu sınır içinde dolmazsa giriş tamamen reddediliyordu; daha iyi bir fiyat da reddediliyordu. Artık defter yetmezse emir kısmi dolar, kalanı iptal olur, daha iyi fiyat kabul edilir. İlk bacak kısmi dolarsa ikinci bacak o miktarda gönderilir. İkinci bacak kısmi dolarsa ilk bacağın eşleşmeyen kısmı hemen kapatılır; bu kapatmanın fiyat sonucu ve ücreti işleme ve borsa teminatına yazılır (`UNWIND` olayı). Kısmi miktar iki bacağın emir adımına aşağı yuvarlanır; bir borsanın asgari emir miktarının altında kalırsa dolum yok sayılır. Hiçbir bacak dolmazsa işlem açılmaz. Pozisyonun karar kaydındaki büyüklüğe bağlı tutarlar (ücret, stres payı, beklenen net) dolan orana göre küçültülür ve `fill_ratio` olarak işlem kaydına ve CSV'ye yazılır.
- **Kayma kaydı:** Her dolumun (giriş, eşleşmeyen kısmın kapatılması, çıkış) orta fiyattan farkı işleme `slippage` olarak yazılır.
- **Kapanış ve kurtarma:** Kapanışta her açık bacağın defteri okunur ve defterin o an alabildiği kadar kapatılır, hedge korunarak: önce bir bacağın diğerinden fazla olan kısmı, sonra eşleşen kısım iki bacakta birlikte. Bir bacak, diğeri kapatılamadığı için tek başına kapatılmaz (ör. bir borsa yanıt vermezse iki bacak da açık kalır); böylece kapanış hatası yeni bir hedgesiz pozisyon bırakmaz. Kalan kısım "Kurtarma" durumunda her turda yeniden denenir ve bu sırada yeni girişler durur. Bacaklar eşit değilse açık pozisyonlar tablosunda "Açık yönlü risk" (borsa, yön, miktar, $ değeri) gösterilir. Kısmi kapanışlar bacağa `partial_exits` olarak yazılır; fiyat sonucu, ücret ve kayma her parçada bir kez sayılır.
- **Stres testi:** Dönem raporu kapanan işlemleri olumsuz varsayımlarla yeniden hesaplar: kayma 2 katı, ücret +%50, funding geliri −%25, tahmini değerlenen funding sayılmaz, doğrulanmamış ödemeler aleyhte ve hepsi birlikte. Borsanın yayımlamadığı bir ödeme paper'da 0 yazılır; o bacak ödeyen taraftaysa bu, maliyetin kaybolması demektir. Her doğrulanmamış ödeme için bacağın bilinen son oranıyla (yoksa girişte kullanılan oranla) bu maliyet kaydedilir (`unverified_risk`, CSV'de `unverified_risk_usd`); alacaklı bacakta sıfırdır. Oran sonradan yayımlanınca kayıt silinir. "Doğrulanmamış funding aleyhte sayılınca net" yeni bir dönem ölçütüdür. "Stres altında net (hepsi birlikte)" yeni bir dönem ölçütüdür. Sonuç yalnızca paper'ın kendi dolumlarında pozitifse dönem hazır sayılmaz. Kayma kaydı olmayan eski işlemlere ek kayma eklenmez ve sayıları yazılır.
- **Karne:** Her strateji ve borsa çifti için işlem sayısı, net, işlem başı, kazanan sayısı, en kötü işlem, en kötü fiyat sonucu, ücretin brüt gelire (ücret öncesi funding + fiyat) oranı, ödenen kayma, ortalama süre, biriken netin zirveden en derin düşüşü ve veri güvenilirliği (doğrulanmamış ödeme, funding gelirinin tahmini değerlenen payı) gösterilir.

Testler: 262 (1 atlanan).


## Veritabanı kilidi: tur süresi
Motorun bütün iş parçacıkları (borsa tarayıcıları, geçmiş kaydedici, ~170 Extended RFQ akışı, karar döngüsü) tek SQLite bağlantısına tek kilitle yazar. Canlı motordan alınan yığın dökümünde 200'den fazla iş parçacığı bu kilidin önünde bekliyordu ve bir tur bir saati aştı. Düzeltme:
- WAL modunda `synchronous=NORMAL`: her yazımda diske senkron yapılmaz, yalnızca checkpoint'te yapılır. Elektrik kesintisinde son birkaç kayıt kaybolabilir; dosya bozulmaz.
- RFQ mesajları bellekte toplanır ve yaklaşık saniyede bir tek işlemde yazılır. Bir akış başka akışın yazmasını beklemez. Süresi dolan mesajlar saatte bir, ayrı bir iş parçacığında kimlik aralıklarıyla silinir. Eskiden bu silme indekssiz olduğu için tablonun tamamını kilit altında tarıyordu.
- Retention (eski HTTP cevaplarının gövdesini silme) kesim noktasının kimliğini ikiye bölerek bulur ve yalnızca o noktaya kadar 500'lük kimlik aralıklarında ilerler. Kaldığı yeri veritabanına kaydeder. Eskiden her çalışmada tablonun sonuna kadar yürüyordu.
- Aday hunisinin başlığında son iki tur arasındaki süre görünür. 10 dakikayı aşarsa kırmızıdır, çünkü dönem raporu bunu kesinti sayar. Üst şerit tarama sürerken de son verinin saatini gösterir.

## Çıkışlar stratejinin tezine bağlandı
Önceki dönemin işlemlerinde kazançlar küçük, kayıplar büyük kaldı: yakınsamada 4 işlemde 1 kazanç, en kötü işlem −$11,97; funding işlemlerinin ikisi ~2 saatte kapandı ve ücret brüt sonucun %149'u oldu. Sebep, iki stratejide de fiyat hareketine bakan erken kâr çıkışlarıydı.
- **Yakınsama:** "Beklenen yakınsama kazancı alındı" çıkışı kaldırıldı. Giriş tahmini, farkın normale tam dönüşünden 1 standart sapmalık risk payı ve maliyetler düşülerek hesaplanır; bu tahmini hedef yapmak pozisyonu normalden yaklaşık 1 sd önce kapatıyordu (BULLA: normal 40,5 bp iken ~100 bp yukarıda kapandı). Artık pozisyon fark normal bölgeye dönünce, azami süre dolunca ya da kayıp/stres/marjin limitlerinde kapanır. Risk payı yalnızca giriş kararında kullanılır.
- **Funding:** "Kâr hedefi alındı" ve "Kâr kilitlendi" çıkışları ve `wide_lock_trail` ayarı kaldırıldı. Bu çıkışlar hedge'in fiyat gürültüsüyle tetikleniyor, funding toplanmadan tam ücret ödetiyordu. Pozisyon funding avantajı sürdükçe tutulur. Kapanış sebepleri: funding'in tersine dönmesi, kârdayken kalan funding'in asgari netin altına inmesi, 7g fiyat uyumunun bozulması, kayıp/stres/marjin limitleri ve azami tutma süresi.
- Giriş kuralları değişmedi. Kazançlar artık erken kilitlenmediği için bazı işlemler kâra geçtikten sonra geri verebilir; bu, yeni dönemin stres ve karne sonuçlarında görünür.

## Yakınsama: kademeli giriş, geçmiş en büyük açılmada stop
Yakınsama artık "fark geçmişte en fazla ne kadar açıldıysa o çizgi" kuralıyla çalışır. Ölçüler eşlenmiş 30 günlük 1h kapanışlardan gelir: normal fark (medyan) ve normalden bu yöndeki en büyük sapma.
- **Kademeler:** sapma en büyüğün %50 / %70 / %85'ine ulaşınca girilir. Her kademe işlem başı kayıp bütçesinin %20 / %30 / %50'sini riske eder. Pozisyon açıkken fark açılıp sıradaki kademeye ulaşırsa (en büyük sapmayı geçmeden) aynı pozisyona eklenir. Ekleme iki bacağa ortalama fiyatla girer; ödemeler her anda gerçekten tutulan miktarla uzlaştırılır. Kademe eklenemezse 30 dakika beklenir.
- **Stop:** en büyük sapmanın %10 ötesinde 5 dakika kalırsa, ya da %25 ötesine geçerse hemen kapanır. Ayarlardan değiştirilebilir. Büyüklük %25'lik sert stopa göre hesaplanır: oradaki fiyat zararı, ücretler ve iki defterin makası, girilen kademelerin bütçe payına sığar. En kötü durumda kayıp bütçe kadardır. Eski "1 sd risk payı" düşülmez; risk artık stop ve büyüklükle sınırlanır.
- **Geçmiş şartı:** bir kademe, o genişliğe kadar açılıp azami bekleme süresi içinde normale dönen en az `wide_conv_min_episodes` (3) örnek ister; dönmeyen örnek en fazla 1 olabilir. Kademe 1 bu şartı sağlamazsa girilmez. Daha geniş bir kademe sağlamazsa o kademenin riski alınmaz. Bugünkü sapma en büyük sapmanın üstündeyse girilmez.
- **İleri dönem kontrolü:** stop geçmişten alındığı için, aynı geçmişte hiç aşılmamış görünür. Bu yüzden ilk yarının en büyük sapması ikinci yarıda stop payı kadar kaç ayrı kez aşıldı diye bakılır. 1'den fazlaysa çift reddedilir ("Geçmiş sınır ileri dönemde aşıldı", 6 saat beklemeye alınır).
- Funding yine en yavaş dönüş süresince yalnızca maliyet olarak düşülür. Kapanış: fark normal bölgeye dönünce, azami süre dolunca, stopta ya da kayıp limitlerinde.

## Yetenek kaydı (Faz 0.3)
`capabilities.py`, ilk perp DEX evreni için her borsa ve her yetenekte dört durumdan birini tutar: tam, kısmi, yok, doğrulanmadı. Evren: Hyperliquid, Paradex, Lighter Core, Lighter RH, Extended, RISEx, TxFlow, Arcus, Variational Omni. Lighter RH ve Lighter Core ayrı borsadır: ayrı API sunucusu, ayrı piyasalar, RH'de USDG teminat.
- **Beyan:** her borsa için bağlayıcının neyi uyguladığı yazılıdır (kaynak ve bilinen sınır). Birkaç örnek:
  - Arcus işlem fiyatı geçmişi: yok (oracle).
  - TxFlow gerçekleşmiş funding: yok.
  - Variational: bağlayıcı yok.
  - Hesap, pozisyon ve emir yetenekleri hiçbir borsada yazılmadı.
- **Doğrulama canlı veriden yapılır, bağlantı testinden değil:**
  - taze piyasa satırları: keşif, kontrat bilgisi, mark/endeks, funding oranı, OI, hacim, ücret kaynağı;
  - her defter okuması: geçerli ve taze mi, son bir saat;
  - geçmiş kaydedicinin 30 günlük kapsamı;
  - funding aralığı: kayıtlı gerçekleşmiş ödemelerin ortanca aralığı, yayımlanan aralıkla %5 içinde mi;
  - funding işareti: mark endeksten en az 5 bp uzakken funding'in yönü primle aynı mı (3 günde en az 10 gözlem, %80 uyum). Uyum %20'nin altındaysa işaret ters sayılır ve borsa funding'de kullanılmaz.
- **Kapı:** funding ve yakınsama için şu yeteneklerin tam ya da kısmi olması gerekir: keşif, kontrat bilgisi, işlem fiyatı geçmişi, funding oranı, gerçekleşmiş funding, aralık, işaret ve ücret. Eksik bacaklı çift "yalnız araştırma" olarak listede görünür; derin kontrole girmez, işlem açılmaz. Huni bunu "Yürütülemez (araştırma)" satırında sayar.
  - Emir defteri kapıda değil, çünkü her kullanımda zaten taze ve çapraz değil mi diye kontrol ediliyor. Kayıt yine de son okumayı gösterir.
  - Ödemeleri yayımlanan aralıkla uyuşmayan piyasa da tek başına araştırmada kalır.
- **Panel:** Borsalar sayfasında 9 DEX × 22 yetenek tablosu ve borsa başına paper uygunluğu. Hücre ipucu yöntemi, kaynağı, sınırı ve son doğrulama zamanını gösterir.
- **Ne zaman uygun olur:** motor çalıştıkça kanıt birikir. İşaret ve aralık doğrulanana kadar o borsa bacaklı funding ve yakınsama işlemi açılmaz. Doğrulama birkaç tur ile birkaç saat arası sürer.

## Paper çalışma alanı (Faz 0.2)
Çoklu paper sayfasının başında "Çalışma alanı" kartı var.
- **Yeni dönem:** yalnızca ilk perp DEX evreninden taranan borsalar seçilebilir; CEX'ler salt okunur referanstır.
  - Seçilebilenler: Hyperliquid, Paradex, Lighter Core, Lighter RH, Extended, RISEx, TxFlow, Arcus. Variational'ın bağlayıcısı olmadığı için seçilemez.
  - Her borsaya ayrı teminat girilir. Funding ve yakınsama seçilir; hibrit Faz 2'de gelecek.
  - Ayarlar başlangıçta birlikte uygulanır: büyüklük, kaldıraç, azami pozisyon, kayıp limitleri, asgari net, yakınsama kaldıracı ve stop ayarları. Bunlar dönemin yapılandırması sayılır, dönem içi değişiklik sayılmaz.
  - Her borsa satırında o borsanın funding ve yakınsama uygunluğu (yetenek kaydı) görünür.
- **Başlatma açık bir eylemdir;** kendiliğinden sıfırlama yoktur.
  - Önceki dönem kendi çalışma alanı ve ayarlarıyla arşivlenir.
  - Açık pozisyon varken başlatılamaz.
  - Çalışma alanı başlayınca eski Lighter RH–Extended masası yeni giriş açmaz; açık pozisyonlarını izlemeye devam eder. Böylece iki paper evreni yan yana çalışmaz.
- **Ortak cüzdanla yeni dönem başlatılamaz.** Çalışma alanı öncesi dönemler arşivde "çalışma alanı öncesi" etiketiyle okunur kalır.
- **Çalışan dönem:** borsalar, başlangıç teminatı, özsermaye, strateji ve ayarlar görünür.
  - Bir borsada yeni girişler kapatılıp açılabilir. Bu teminatı ve açık bacakları değiştirmez; dönem raporunda ayar değişikliği sayılır. Huni bu borsaları "Çalışma alanı dışında" sayar.
  - "Tüm paper pozisyonlarını kapat" bütün pozisyonları bir sonraki turda kapatır.
- **API:**
  - `POST /api/wide_workspace` — `{venues: {borsa: tutar}, strategies: {funding, convergence}, config: {...}}`
  - `POST /api/wide_venue` — `{venue, enabled}`
  - `POST /api/wide_close_all`
