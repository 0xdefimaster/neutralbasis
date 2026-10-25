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
