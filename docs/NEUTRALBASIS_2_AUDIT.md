# Neutralbasis 2 — Phase 0 Depo Denetimi

- **Tarih:** 2026-11-02
- **Dal:** `claude/friendly-hamilton-v2g1fw`, HEAD `7e0747e` (PR #54 açık, birleştirilmedi)
- **Kapsam:** salt okuma. Bu belge dışında hiçbir dosya değiştirilmedi.
- **Revizyon 2026-11-02 (yalnız dokümantasyon):** paper-only politikası eklendi (bölüm T, `CLAUDE.md`). Yol haritası düzeltildi: Faz 7 = paper yürütme motoru, Faz 9 = ayrıca yetkilendirilecek gelecek kilometre taşı. Commit edilmemiş çalışma notu güncellendi. Kaynak kod, test ve ayar değişmedi.

Durum terimleri: **IMPLEMENTED**, **PARTIALLY IMPLEMENTED**, **MISSING**, **VERIFIED**, **REQUIRES VERIFICATION**, **BLOCKED**.

Kanıt kuralı: **VERIFIED** yalnız gerçek borsa verisiyle gözlenen davranış için kullanılır. Bu bulut ortamı borsa API'lerine ve belgelerine erişemiyor (proxy 403 / DNS). Gerçek veri yalnız kullanıcının bilgisayarında üretiliyor. Bu belgede "kodda var" ifadesi "çalıştığı kanıtlandı" anlamına gelmez.

> **Not — gözden geçirilmemiş çalışma:** `marketdata/`, `test_marketdata.py` ve `Veri-Yetenekleri.cmd` önceki "Faz 1: yetenek manifesti + kanonik şema" görevinden kaldı (17 test geçiyor). Denetim sırasında değiştirilmediler. Oturum kapanınca kaybolmasınlar diye denetimden sonra `b328f3e` olarak commit edildiler; commit mesajında "unreviewed" olarak işaretliler ve hiçbir modül onları kullanmıyor. Kabul ya da geri alma kararı kullanıcıda (bkz. R).

---

## A. Yönetici özeti

Neutralbasis sıfırdan bir proje değil. Çalışan bir **paper trading masası** var:

- tek süreçli bir Python uygulaması (`risk_main.py`), yerel bir HTTP paneli ve tek bir SQLite deposu;
- 63 platformu tarayan public veri katmanı;
- funding ve fiyat yakınsama stratejileri;
- emir defterinden VWAP dolum, kısmi dolum ve bacak kurtarma simülasyonu;
- funding ödemelerinin tek tek hesaplanması, append-only defter ve paper dönem raporu;
- ayrı ve bağımsız bir kanıt kaydedicisi (`tools/phase1_recorder`).

**Gerçek emir gönderen kod yok.** Hesap, bakiye ve pozisyon okuyan kod da yok. Tek istisna: isteğe bağlı salt okunur Extended ücret anahtarı (`/user/fees`).

Güçlü yanlar:
- **Sıkı veri disiplini.** Bilinmeyen değer uydurulmuyor, interpolasyon yapılmıyor, yayımlanmayan funding "0" sayılmıyor.
- **Ölçülü paper gerçekçiliği:** taze ve senkron defter şartı, iki bacakta sıralı IOC dolum, gecikme, kısmi dolumda eşleşmeyen kısmın hemen kapatılması, venue başına teminat ve tasfiye riski.
- **Geniş test paketi:** 482 test.

Ana zayıflıklar:
1. **Veri doğrulanmış değil.** Funding işaret, birim ve aralık kuralları DECLARED; borsa belgeleri okunmadı. Canlı kanıt yalnız 4 borsa için ve henüz çok kısa (kaydedici 2026-10-30'da başladı).
2. **Mimari bir dosyada yığılmış.** Strateji, fırsat, simülasyon, risk, pozisyon yönetimi ve muhasebe `wide_paper.py` içinde iç içe (3.860 satır, tek sınıf). Fırsat yaşam döngüsü, emir durum makinesi ve ayrı bir risk servisi yok.
3. **Aynı veri birden fazla kaynaktan okunuyor.** Dört HTTP katmanı, iki funding geçmişi deposu, üç ayrı ödeme aralığı tespiti var.
4. **İstatistik basit seviyede.** Medyan baz, kesişim sayımı, dönüş oranı ve tarihsel en büyük açılma var. Kointegrasyon, yarı-ömür, yapısal kırılma testi ve hedge oranı yok.
5. **Para hesabı `float` ile.** Yalnız miktar yuvarlamada `Decimal` kullanılıyor. Muhasebe `float`.
6. **CI yok.** Testler yalnız elle çalıştırılıyor.
7. **Canlı işleme hazırlık: sıfır.** Bu bilinçli bir tercih; paper dışı bir yol yok.

---

## B. Depo mimarisi

### Giriş noktaları ve çalıştırma
- **`risk_main.py`**
  - `DeskEngine`'i (`desk_analytics.py`) kurar.
  - `retention.maintain()` çalıştırır.
  - `bot.serve()` ile HTTP panelini açar: yerelde `127.0.0.1:8765`; barındırmada `0.0.0.0` + `NB_DASHBOARD_PASSWORD` (HTTP Basic).
  - Bayraklar: `--once`, `--maintain`, `--autostart`.
- **Windows betikleri:** `Baslat.cmd`/`.ps1`, `Durdur`, `OtomatikBaslat`/`Kapat` (zamanlanmış görev), `Kurulum.cmd` (venv + `requirements.txt` = yalnız `ccxt==4.5.90`).
- **Railway:** `railway.json` (`python -u risk_main.py --autostart --host 0.0.0.0`, `/healthz`). Şu an çalışıp çalışmadığı **REQUIRES VERIFICATION**.
- **Kaydedici:** `python -m tools.phase1_recorder` (`Phase1-*.cmd`). Bağımsız süreç; kendi klasörü `phase1-evidence/`.

### Sınıf zinciri (çalışan uygulama)
```
DeskEngine (desk_analytics.py)
 └ RiskEngine (risk_scan.py)          eski 5 borsalı masa + FiveFeed
    └ Engine (bot.py)                 Store, HTTP sunucusu, eski çift masası, funding değerleme
 bileşenler:
  FiveFeed / bot.Feed                 yerel REST: Lighter RH, Extended, TxFlow, RISEx, Arcus
  WideMarkets (wide_markets.py)       CCXT tarayıcı: 63 kayıt, 25'i CCXT bağdaştırıcılı; borsa başına thread, 60 sn
  Connectors (connectors.py)          tipli cephe: Instrument / MarketSnapshot / Book / FundingPayment
  capabilities.Registry               22 yetenek × 4 durum, canlı kanıtla güncellenir; FUNDING_MODELS; piyasa kanıtı
  HistoryKeeper (history_store.py)    1s mum + gerçekleşen funding, 30 gün doldurma, 35 gün saklama
  MatrixCache / PairViews             funding matrisi, varlık ve çift sayfaları
  ExtendedRfq (extended_rfq.py)       sistemdeki TEK WebSocket: Extended RFQ gerçek defteri
  WidePaper (wide_paper.py)           çoklu borsa paper masası: stratejiler, dolum, pozisyon, funding, dönem raporu
  Ledger (ledger.py) + migrations.py  normalize paper kayıtları, append-only nakit olayları
  Retention (retention.py)            disk temizliği
  connections.py                      bağlantı testi + DPAPI ile şifreli isteğe bağlı Extended anahtarı
Panel: index.html (build-neutralbasis.cjs ile panel.js, wide_panel.js, arb_panel.js, risk_panel.js birleştirilir)
```

### Depolama: `data-five-rh/paper.sqlite3` (tek dosya)
| Tablo | İçerik |
|---|---|
| `kv` | JSON durum: cüzdan, pozisyonlar, ayarlar, registry |
| `audit` | Her public HTTP yanıtı, sıkıştırılmış |
| `events` | Olay günlüğü |
| `marks` | Mark fiyatları |
| `payments` | Funding ödemeleri |
| `hist_candles`, `hist_funding` | Geçmiş veri |
| `rfq_messages` | Extended RFQ akışı |
| `schema_migrations`, `paper_periods`, `paper_trades`, `ledger_events` | Migration ve muhasebe |

Kaydedicinin ayrı deposu var: `recorder.sqlite3` + gzip ham dosyalar + JSONL. Paper deposuna yazmayı reddeder.

### Saklama süreleri
| Veri | Süre |
|---|---|
| Ham audit gövdesi | 3 gün (pozisyon, dolum veya ödemenin referans verdiği gövdeler hariç) |
| Audit parmak izi | 30 gün |
| marks | 14 gün |
| events | 30 gün |
| hist_* | 35 gün |
| Kaydedici | **saklama politikası yok** (sınırsız büyür) |

### Bağımlılık ve kapsam
- Tek dış paket: `ccxt`. Kaydedici ve `marketdata/` yalnız standart kütüphane.
- Takip edilen dosya: 95. Python kaynağı + testler yaklaşık 21.600 satır (`wide_paper.py` 3.860, `test_wide_paper.py` 2.759).

---

## C. DEX bağdaştırıcı matrisi

Açıklama:
- **N** = yerel REST (FiveFeed/bot.Feed)
- **C** = CCXT (WideMarkets)
- **R** = kaydedici (`tools/phase1_recorder`)
- **WS** = WebSocket

Durum sütunları kodu anlatır; "Canlı kanıt" sütunu gerçek veriyi.

| Borsa (kod adı) | Yol | Canlı kanıt | Piyasa verisi | Funding | Trading | Enstrüman eşleme | Testler |
|---|---|---|---|---|---|---|---|
| Hyperliquid (`Hyperliquid`) | C + R | R: 2026-10-30 tek tur, bağlandı | keşif, mark, L2, 1s mum, OI/hacim (C). Endeks yok; R `oraclePx`'i ayrı tutar | anlık + saatlik ödeme geçmişi; aralık 3600 VERIFIED (tek tur); birim/işaret DECLARED | MISSING | ticker = `name`; fiyat adımı yanıtta yok | test_recorder, test_wide_markets (MOCK) |
| Paradex (`Paradex`) | yalnız C | yok | C'nin sunduğu (`has` bayrağına bağlı) | CONTINUOUS (DECLARED): ayrık ödeme yok → yalnız araştırma | MISSING | CCXT sembolü | test_capabilities (MOCK) |
| TxFlow (`TxFlow`) | N | yok (paper motoru okuyor, kanıt kaydı yok) | perpMeta, activeAssetCtx (mark, oracle, funding, aralık), candleSnapshot, l2Book | anlık funding var; **gerçekleşmiş geçmiş yok**, kod bilerek hata verir | MISSING | `index` sayısı | test_risk_scan (MOCK) |
| Arcus (`Arcus`) | N | yok | markets (mark, oracle, funding, sonraki ödeme), **oracle mum** (işlem fiyatı değil), l2OrderBook, feetiers | fundingRates geçmişi (ödeme mi örnek mi, doğrulanmadı) | MISSING | `baseAsset` | test_risk_scan (MOCK) |
| Extended (`Extended`) | N + R + WS | R: tek tur, bağlandı | info/markets, mum, defter; RFQ gerçek defteri WS | anlık + gerçekleşmiş (yön alanı yok); aralık VERIFIED (tek tur) | MISSING (yalnız salt okunur ücret anahtarı) | `name` (BTC-USD) | test_extended_rfq, test_recorder (MOCK) |
| Variational (`Variational`) | yalnız R keşif | ham `metadata/stats`; gerçek yanıt henüz görülmedi | hiçbiri yorumlanmıyor | MISSING | MISSING | MISSING | test_variational (MOCK) |
| RISEx (`RISEx`) | N | yok | markets (mark, endeks, funding, aralık), mum, defter | geçmiş var (ödeme mi örnek mi, doğrulanmadı) | MISSING | `underlying` | test_risk_scan (MOCK) |
| Lighter Core (`LighterCore`) | C + R | R: tek tur, bağlandı | keşif, mark, endeks, defter, mum | funding-rates (birim UNKNOWN) + fundings (yön alanlı); aralık VERIFIED (tek tur) | MISSING | `symbol` + `market_id` | test_recorder (MOCK) |
| Robinhood Network = Lighter RH (`Lighter`, `api.rh.lighter.xyz`) | N + R | R: tek tur, bağlandı; LINK/AAVE/UNI yeni listelenmiş (~166 ödeme) | Core ile aynı API, ayrı host ve ayrı market_id | paper canlı oranı son gerçekleşen ödemeden alıyor; aralık VERIFIED (tek tur) | MISSING | `symbol` + `market_id` | test_recorder, test_wide_paper (MOCK) |

**Ortak notlar**
- **Hata yönetimi:**
  - Kaydedici: üstel geri çekilme, 429'da Retry-After, erişilemeyen borsayı atlama.
  - FiveFeed: önbellek (`max_age`) ve hata fırlatma.
  - CCXT: `enableRateLimit` ve 6 sn timeout.
  - Bir borsanın hatası diğerlerini durdurmuyor (thread ayrımı). Hata sessizce yutulmuyor; panelde ve events tablosunda görünüyor.
- **Hesap ve trading yetenekleri:** dokuz borsanın hiçbirinde yok. Bakiye, teminat, marjin, açık emir, kaldıraç ayarı ve tasfiye bilgisi okunmuyor; emir verme, iptal, durum sorgulama ve dolum okuma da yok. Paper-only politikası (bölüm T) gereği bunlar **kapsam dışı**: eksik değiller ve eklenmeyecekler. Hesap ve emir olayları paper motorunda simüle edilir.
- **Doğrulama kaydı:** registry'nin (`capabilities.py`) beyanlarının çoğu hâlâ `NOT_VERIFIED`. Canlı kanıt geldikçe paper motoru içinde yükseliyor, ama motorun gerçek PC kaydı bu ortamdan görülemiyor (**REQUIRES VERIFICATION**).
- **Lighter adlandırması:** kodda `Lighter` = RH, `LighterCore` = Core. İki entegrasyon zaten ayrı (ayrı host, ayrı market_id, ayrı klasör). Commit edilmemiş `marketdata/venues.py` açık kimlikler öneriyor: `lighter_rh`, `lighter_core`.

---

## D. Piyasa verisi değerlendirmesi

| Konu | Durum | Kanıt / not |
|---|---|---|
| Veri kaynakları | IMPLEMENTED | Dört HTTP katmanı: `bot.Feed.get`/`FiveFeed.req`, denetimli CCXT `fetch` (`wide_markets.py:302`), kaydedici `Http`, `connections.request`. Hepsi audit yazıyor ama kuralları farklı. |
| WebSocket | PARTIALLY IMPLEMENTED | Yalnız `extended_rfq.py`: kendi RFC 6455 istemcisi; SNAPSHOT/DELTA, sıra boşluğunda defteri düşürme, sessiz soket tespiti. Başka WS yok; yeniden bağlanma yalnız burada. |
| REST yedeği | IMPLEMENTED | Her şey REST yoklaması. CCXT turu 60 sn, motor döngüsü `poll_seconds=30`. |
| Fiyat normalizasyonu | PARTIALLY IMPLEMENTED | `Connectors` cephesi tipli görünüm veriyor. Aynı ticker'ın farklı token olması %15 fiyat sapmasıyla ayrılıyor (`MAX_PRICE_GAP`). Kontrat çarpanı ve lineer/ters ayrımı yok. Last, bid, ask, mid, mark, index ayrı alanlar, ama her borsada hepsi yok. |
| Zaman damgası senkronu | PARTIALLY IMPLEMENTED | Girişte iki defter ≤5 sn arayla ve ≤30 sn yaşta olmalı (`wide_paper.priced`). Funding ≤120 sn (`ENTRY_FUNDING_MAX_AGE`), satırlar ≤300 sn (`ROW_MAX_AGE`). Borsa saat farkı ölçülmüyor. İşlem zamanı ayrı tutulmuyor. |
| Veri kalitesi durumu | PARTIALLY IMPLEMENTED | Registry 4 durumu (FULL/PARTIAL/NONE/NOT_VERIFIED) ve piyasa kanıtı girişleri engelliyor. Kaydedici VERIFIED/OBSERVED/DECLARED/UNKNOWN/CONFLICT kullanıyor. İstenen HEALTHY/DEGRADED/STALE/DISCONNECTED/INVALID/UNKNOWN modeli tek bir yerde yok. |
| Defter toplama | PARTIALLY IMPLEMENTED | Girişte taze okunuyor. Kaydedici 60 sn'de bir ilk 20 seviyeyi saklıyor. Paper deposunda defter geçmişi yalnız 3 gün ham audit olarak var. |
| Trades | MISSING | Hiçbir borsadan okunmuyor. |
| Tahmini funding | MISSING | — |

---

## E. Funding motoru değerlendirmesi

**Var olanlar:**
- **Ödeme bazlı nakit hesabı** (`wide_paper.settle`, `bot.Engine.funding_valuation`):
  - Her bacağın kendi takvimi (`next_payment`, `payments_within`).
  - Yayımlanan oran × miktar × ödeme anındaki mark (`held_at` ile pozisyon büyüklüğü zamanında).
  - Mark yoksa giriş fiyatı ile tahmini değerleme (`SETTLED_ESTIMATED_PRICE`).
  - Yayımlanmayan ödeme 0 sayılmaz (`NOT_RECORDED`, tutarsız). Geçmiş okunamıyorsa `UNKNOWN`. Geç yayımlanan oran 7 gün içinde işlenir.
- **Giriş kararı (`assess_funding`):**
  - Muhafazakâr oran: long bacak anlık ve 24s gerçekleşenin büyüğünü öder, short küçüğünü alır (`conservative_raw`).
  - 1/3/7 günlük gerçekleşen avantaj.
  - Ödeme kapsaması ≥%95 (`PAYMENT_COVERAGE`), 8s+15dk boşluk sınırı (`FUNDING_GAP`).
  - 30 günlük bağlam; her çelişki boyutu yarıya indirir.
- **Funding modeli kaydı** (`capabilities.FUNDING_MODELS`):
  - PERIODIC: Hyperliquid, LighterCore, Lighter, Extended.
  - CONTINUOUS: Paradex.
  - UNKNOWN: diğerleri.
  - Yalnız PERIODIC borsalarda paper işlem açılıyor (`EXECUTABLE_MODELS`).
- **İşaret sezgisi:** mark/endeks primi ile oran işaretinin uyumu gözleniyor (`sign_heuristic`). Kaydedici yayımlanan ve gerçekleşen oranı karşılaştırıyor.

**Eksikler ve riskler:**
- **Konvansiyonlar DECLARED.** "Pozitif oran: long öder" kuralı ve oran birimleri belgeyle doğrulanmadı. Lighter `funding-rates` birimi UNKNOWN. Paper motoru Lighter RH canlı oranını son gerçekleşen ödemeden alıyor; bu bir tahmin değil, gecikmeli bir gözlem.
- **Yön alanı yok:** Extended ve Hyperliquid geçmişinde ödeyen taraf bilgisi yok; yön türetilmiyor.
- **Eksik özellikler:** oran tavanı/tabanı, dalgalanma, dönüş olasılığı ve tutma süresi boyunca beklenen nakit dağılımı yok. Sınıflandırma (FAVORABLE … INSUFFICIENT_DATA) yok; bunun yerine metin gerekçeli ret var.
- **TxFlow, Arcus, RISEx** gerçekleşmiş geçmişi ya yok ya da "ödeme mi örnek mi" belirsiz. Paper girişlerinde zaten kullanılmıyorlar.

---

## F. Fiyat farkı değerlendirmesi

**Var olanlar** (`wide_paper.convergence_stats`, `pair_stats`, `desk_analytics.history_summary`, `convergence.qualify`):
- Eşlenmiş 1s işlem kapanışlarından log fark serisi; oracle seriler reddediliyor.
- Baz: 7 ve 30 günlük medyan.
- Ortalama/SD ve z-skoru; korelasyon (`wide_min_corr=0.90`); kesişim sayısı (7g ≥6, 30g ≥20).
- Kademeli dönüş oranları (`CONV_TIERS`) ve dönüş süreleri.
- Tarihsel en büyük açılma ve bunu aşınca stop (`wide_conv_stop_pct`, `hard_stop`).
- Asgari epizot sayısı ve azami tutma süresi (72s).

**Bu, temel bir fiyat karşılaştırmasından fazlası ama istatistiksel olarak doğrulanmış bir yakınsama stratejisi değil:**
- Kointegrasyon testi, yarı-ömür / OU tahmini, regresyon hedge oranı (hep 1:1 miktar), yapısal kırılma testi ve rejim tespiti **MISSING**.
- Örtüşen gözlemler bağımsız sayılıyor. Eşik seçimi için örneklem dışı ya da ileri yürüyen doğrulama yok.
- Kullanılan tek çözünürlük 1 saat. Kısa vadeli (dakika) ilişki için veri yok.
- Yalnız fiyatlı yakınsama araştırma olarak gösteriliyor; taşıma maliyeti sınırlanamazsa işlem açılmıyor (`PRICE_ONLY_NOTE`). Bu iyi bir koruma.

---

## G. Fırsat motoru değerlendirmesi

**PARTIALLY IMPLEMENTED.** Açık bir durum makinesi yok; dolaylı bir akış var:
- `rank` → `usable_rows` → `assess_funding` / `assess_convergence` → `entry_evidence` → `priced` → `open`.
- Huni sayaçları (`build_funnel`, `round_counts`): taranan → değerlendirilen → açılan.
- Ret gerekçeleri, bekleme süreleri (`REJECT_COOLDOWN`) ve çift başına son karar (`decisions`, `verdict`).
- Funding ve yakınsama ayrı değerlendiriliyor. Birleşik tip (Tip C) ayrı bir sınıf değil: yakınsama değerlendirmesi funding taşımasını maliyet ve risk olarak içeriyor.

**Eksikler:**
- Fırsat kimliği ve kalıcı fırsat kaydı (yalnız açılan pozisyonun `decision` kopyası tutuluyor).
- DISCOVERED → … → CLOSED durumları ve geçiş kuralları.
- Faktör katkılarıyla açıklanabilir sıralama (şu an net ve çeşitli kapılar var).
- Son kullanma zamanı.

---

## H. Kâr motoru değerlendirmesi

**PARTIALLY IMPLEMENTED.**
- **Girişte** (`priced`) dört emrin defter VWAP'ı, taker ücretleri, defter makası ve stres kaybı hesaplanıyor. Risk bütçesine sığdırılıyor.
- **Pozisyonda** net = fiyat PnL + funding − ücret. Kayma dolum fiyatına dahil, ikinci kez düşülmüyor.
- **Stage 3 sonrası** üç sonuç ayrı: gerçekleşen, tahmini, belirsizlik riski (`WidePaper.accounting`, `period_accounting`).
- **Dönem raporu:** profit factor, ilk 3 işlemin payı, kesinti payı ve stres senaryoları (`STRESS`, `stressed`).

**Eksikler ve riskler:**
- Tek merkezi "beklenen net PnL" fonksiyonu yok. Funding ve yakınsama kararları kendi hesaplarını yapıyor. Çifte sayım bilinçli olarak önlenmiş görünüyor ama bunu kanıtlayan ortak bir tanım ya da test yok (**REQUIRES VERIFICATION**).
- Kontrat çarpanı ve ters kontrat matematiği yok; her şey lineer ve taban birimde varsayılıyor.
- Para `float`. `Decimal` yalnız miktar yuvarlamasında (`bot.py:741`).
- Ücret kademesi: public tarife veya kullanıcının girdiği oran (`wide_fees`). Maker varsayılmıyor (iyi).
- Sermaye getirisi metriği yok (sermaye kullanımı ve beklenen kuyruk kaybı / ES dahil).

---

## I. Simülasyon değerlendirmesi

**PARTIALLY IMPLEMENTED (paper için güçlü):**
- **Dolum:** taze defter, sıralı iki bacak, venue gecikmesi (yapılandırılmış 750 ms veya ölçülen p90 RTT, hangisi büyükse), sınır fiyatlı IOC.
- **Kısmi dolum:** ikinci bacak birinciye göre boyutlanıyor. Eşleşmeyen kısım hemen kapatılıyor (`unwind`). Hiç dolmazsa işlem sayılmıyor. Bacak kurtarma durumu var (`RECOVERY`).
- **Kayma ölçümü:** `extra_slippage` = 1 bp ek kayma; dolum mid'e göre ölçülüyor.
- **Stres:** girişte boyutlandırma stresi; dönem raporunda kayma 2×, ücret +%50, funding −%25 gibi senaryolar.

**Eksikler:**
- İyimser / temel / muhafazakâr olarak ayrılmış ön-işlem simülatörü yok. Simülasyon doğrudan dolum yolunda.
- Borsa reddi, API zaman aşımında emir durumu belirsizliği ve venue kesintisi senaryoları yok.
- Çıkış likiditesi giriş anının defterinden tahmin ediliyor.
- Kuyruk sırası modeli yok (taker varsayımı bunu gereksiz kılıyor).

---

## J. Risk değerlendirmesi

**Mevcut sert kontroller** (`wide_paper`):

| Kontrol | Uygulama |
|---|---|
| Pozisyon sayısı | `wide_max_positions` ≤10 |
| İşlem başı kayıp bütçesi | `wide_risk` |
| Günlük kayıp | `wide_daily_loss`, `daily_pnl` cüzdan özsermayesinden |
| Kaldıraç tavanı | ≤5 |
| Bacak tasfiye mesafesi | Tek bacağın 4σ hareketi tasfiye mesafesinin yarısını geçemez (`LEG_SAFETY`) |
| Teminat rezervi | `wide_reserve` |
| Venue başına teminat | `per_venue`, `headroom` |
| Aynı varlık | Aynı varlıkta ikinci pozisyon yok; kapanış sonrası bekleme süresi |
| Kanıt kapıları | Registry ve piyasa kanıtı (`entry_evidence`, `pair_blockers`) |
| Çıkış kuralları (`exit_reason`) | Pozisyon kayıp limiti, günlük limit, spread stres bütçesi, bacak marjin riski, borsa teminatı riski, yakınsama stopları, azami tutma süresi |

Ayar sınırları `LIMITS` sözlüğünde doğrulanıyor.

**Eksikler:**
- **Kill switch tam değil.** "Yeni girişleri durdur" (`/api/pause`) ve "tümünü kapat" (`/api/wide_close_all`) var. Ama emir iptali, mutabakat ve "doğrulama sonrası devam" adımları yok; emir olmadığı için bu şimdilik geçerli bir eksiklik değil.
- **Portföy düzeyi kontroller yok:** brüt, net ve korelasyonlu maruziyet ile varlık ve venue limitleri sayı olarak tanımlı değil (yalnız varlık başına tek pozisyon kuralı var). Toplam kayıp ve drawdown limiti yok.
- **Belirsizlik riski günlük limite dahil değil.** Ayrı tasarım ve onay bekliyor; kullanıcı kararı.

---

## K. Yürütme değerlendirmesi

- **Gerçek yürütme: MISSING (bilinçli).** Emir gönderme, imzalama, nonce, istemci emir kimliği ve borsa mutabakatı yok.
- **Paper yürütmede:**
  - Pozisyon kimliği `uuid4`.
  - Ödemeler, cüzdan ve pozisyon tek işlemde kalıcı yazılıyor (`bot.py:1046`), böylece yeniden başlatma çift kredi yazmıyor.
  - Defter olayları benzersiz anahtarla yazılıyor (idempotent).
- **Emir durum makinesi yok.** Yalnız pozisyon durumları var: OPENING, OPEN, DATA_WAIT, RECOVERY, CLOSING, CLOSED.
- **Zaman aşımı sonrası durum mutabakatı** bugünkü paper dolumunda yok. Faz 7 (paper yürütme motoru) belirsiz emir durumu, ret ve zaman aşımı senaryolarını **simüle ederek** modellemeli. Gerçek borsa yürütmesi kapsam dışı (bölüm T).

---

## L. Pozisyon yönetimi değerlendirmesi

**IMPLEMENTED (paper):**
- Her döngüde mark güncelleme (`mark_position`), funding ödeme takibi, çıkış kuralları, kademeli ekleme (`add_tiers`, `add_fill`), kullanıcı kapatması.
- `kv` içinde kalıcı. Yeniden başlatmada pozisyonlar okunuyor.
- Kesinti süresi dönem sonucundan ayrılıyor (`DOWNTIME_GAP`).

**Eksikler:**
- Yeniden dengeleme ve hedge oranı ayarı yok.
- "Mutabakat gerekli" durumu yok (borsa durumu olmadığı için).
- Çıkış kararı tek pozisyon bazlı; portföy PnL'i hesaba katılmıyor.

---

## M. Muhasebe ve defter değerlendirmesi

**IMPLEMENTED:**
- **`ledger.py` + migration 1:** `paper_periods`, `paper_trades`, append-only `ledger_events` (benzersiz anahtar).
- **Ödeme durumları:** SETTLED, SETTLED_ESTIMATED_PRICE, NOT_RECORDED, UNKNOWN.
- **Mutabakat:** `ledger.reconcile` yalnız SETTLED ödemelerle yapılıyor.
- **Migration sistemi:** sürümlü; çalışmadan önce yedek alıyor ve yalnız ekleme yapıyor.
- **Kaydedicide ayrıca:** ödeme kimliği toleransı, revizyonlar, AMBIGUOUS, listeden kaybolan ödeme kaydı.

**Riskler:**
- **Doğruluk kaynağı ikili.** Cüzdan ve pozisyonlar `kv` JSON'da; defter tabloları onun aynası. Asıl kaynak `kv`. Defter tutarlılığı dönem raporunda kontrol ediliyor ama iki yerde tutulan bir bakiye var.
- **Para tutarları `float`.**
- **Borsa hesabıyla mutabakat yok** (hesap okunmuyor).

---

## N. Test değerlendirmesi

**Komut:** `python3 -m unittest discover -p "test_*.py"`
- **Sonuç:** 482 test çalıştı, 480 geçti, 2 atlandı (`test_bot.py:347` yalnız Windows DPAPI; `tools/phase1_recorder/tests/test_recorder.py:693` yalnız `NB_LIVE=1` canlı API).
- 17 test commit edilmemiş `test_marketdata.py`'den geliyor.

Dosya başına test sayısı:

| Dosya | Test | Dosya | Test |
|---|---|---|---|
| test_wide_paper | 132 | test_risk_scan | 13 |
| test_capabilities | 42 | test_extended_rfq | 13 |
| recorder/test_recorder | 37 | test_wide_markets | 12 |
| test_bot | 27 | recorder/test_comparisons | 10 |
| test_pair_view | 26 | test_candidate_rotation | 10 |
| test_funding_matrix | 20 | test_paper_accounting | 9 |
| recorder/test_audit | 18 | test_hosted_serve | 9 |
| recorder/test_identity_reconcile | 17 | test_retention | 8 |
| test_marketdata (commit edilmedi) | 17 | test_migrations | 7 |
| test_history_store | 17 | test_funding_recovery | 7 |
| | | test_desk_analytics, test_convergence, test_connectors | 7'şer |
| | | recorder/test_variational | 5 |
| | | test_long_history | 3 |
| | | test_risk_main | 2 |

**Önemli test edilmemiş alanlar:**
- **Gerçek borsa yanıtları.** Testlerin hepsi MOCK yanıtlarla çalışıyor; canlı test isteğe bağlı ve bu ortamda çalışamıyor.
- **CCXT bağdaştırıcılarının gerçek davranışı.**
- **Panel JS'i.** Birim testi yok; yalnız elle yapılan Playwright smoke denemeleri var, depoda değil.
- **Uzun süreli çalışma:** bellek ve disk büyümesi (kaydedicide saklama süresi de yok).
- **Eş zamanlılık:** thread yarışları sınırlı test ediliyor (store kilidi testi var).
- **CI yok:** `.github/` yok. Kapsam ölçümü yapılmıyor; kapsam yüzdesi bilinmiyor.

---

## O. Güvenlik değerlendirmesi

**İyi:**
- Özel anahtar, cüzdan ve emir yolu yok.
- Tek kimlik bilgisi: isteğe bağlı salt okunur Extended anahtarı, Windows DPAPI ile kullanıcıya bağlı şifreleme (`connections.py`).
- `.gitignore` veri klasörlerini, `.env` ve `*.sqlite3` dosyalarını dışarıda tutuyor.
- Barındırılan modda parola zorunlu (`bot.serve`). Yerel modda Host başlığı ile DNS-rebinding koruması var.
- Kaydedici anahtara benzeyen başlık ve parametreleri gizliyor.

**Riskler:**
- **Railway.** HTTP Basic yalnız TLS arkasında güvenli; Railway TLS sağlıyor ama doğrulanmadı. Panelin `/api/shutdown`, `/api/wide_reset`, `/api/wide_close_all` gibi yıkıcı uçları tek bir parolaya bağlı. CSRF koruması **REQUIRES VERIFICATION**.
- **Bağımlılık taraması yok.** `ccxt` sabitlenmiş ama denetlenmiyor.
- **`srv2.log` depoda izleniyor.** İçeriği zararsız (42 bayt), yine de günlük dosyaları depo dışında tutulmalı.

---

## P. Mimari boşluklar

1. **`wide_paper.py` tek sınıfta 3.860 satır.** Strateji, fiyatlama, dolum, pozisyon, funding, muhasebe ve rapor birbirine bağlı. Değişikliklerin yan etkisi yüksek.
2. **Tekrarlanan veri yolları:**
   - Extended dört yoldan okunuyor (FiveFeed, kaydedici, RFQ WS, bağlantı testi).
   - Lighter RH, Lighter Core ve Hyperliquid ikişer yoldan.
   - Dört HTTP katmanı, iki funding geçmişi deposu (`hist_funding` ve kaydedici), üç ödeme aralığı tespiti, iki durum sözlüğü.
3. **Global yan etki:** `FiveFeed.__init__` içinde `bot.L = RH` modül değişkenini değiştiriyor.
4. **Eski kod yaşıyor.** Eski 5 borsalı masa (`RiskEngine.cycle/assess`) ve eski çift masası (`bot.Engine`) hâlâ her döngüde çalışıyor; workspace açılınca yeni girişleri kapatılıyor (`legacy_entries=False`).
5. **Durum bağımlılığı:** `kv` JSON durumu ile normalize tablolar arasında çift yazım.
6. **Belgeler güncel değil.** `README.md` "yakınsama yalnız Lighter RH–Extended" ve "67 test" diyor; bunlar artık doğru değil.
7. **Kanonik enstrüman kimliği yok.** Kimlik `(borsa, ticker)` ikilisi; kontrat çarpanı modeli yok. (Commit edilmemiş `marketdata/canonical.py` buna bir başlangıç.)

---

## Q. Önerilen sıra (gerçek bulgulara göre)

Kural: çalışan yolları değiştirmeden önce yenisini gölgede paralel çalıştırıp aynı sonucu verdiğini kanıtlamak.

1. **Faz 1a — Kanıt toplama (kod değişikliği yok).** Kaydedici kullanıcı PC'sinde en az 72 saat çalışsın. Böylece oran birimi, işaret ve aralık 4 borsada ölçülür, Variational ham şeması görülür.
2. **Faz 1b — Veri bütünlüğü.**
   - Commit edilmemiş `marketdata/` (manifest + kanonik şema) gözden geçirilip kabul edilsin.
   - Tek veri kalitesi durum modeli (HEALTHY … UNKNOWN) tanımlanıp mevcut registry ile kaydedici durumları buna eşlensin.
   - Kaydediciye saklama politikası eklensin.
   - TxFlow, Arcus ve RISEx kaydediciye eklensin; Paradex ve Hyperliquid için mevcut uç noktalar.
3. **Faz 2/3 — İstatistik ve funding.** Mevcut `convergence_stats` ve `assess_funding` korunur, yanlarına doğrulama eklenir: yarı-ömür, kointegrasyon, örneklem dışı eşik testi, funding sınıflandırması, kanıta dayalı işaret. Önce rapor üretirler; kapı olarak kullanılmaları ayrı onaya bağlı.
4. **Faz 4 — Tek kâr fonksiyonu.** `priced` + `accounting` mantığı tek bir saf fonksiyona çıkarılır ve altın testlerle eski sonuçlarla birebir karşılaştırılır. Para tutarları `Decimal`'a geçirilir; önce yalnız muhasebe katmanında.
5. **Faz 5/6 — Simülasyon ve risk.** Mevcut dolum simülasyonu korunur; iyimser/temel/muhafazakâr çalıştırma eklenir. Portföy maruziyet limitleri ve drawdown eklenir. Belirsizlik riskinin günlük limite dahil edilmesi ayrı tasarım ve onay gerektirir.
6. **`wide_paper.py` bölme.** Davranışı değiştirmeden, test paketi korunarak modüllere ayrılır.
7. **Faz 7 — PAPER YÜRÜTME MOTORU.** Canlı yürütme entegrasyonu değil. Simüle emir durum makinesi (CREATED … RECOVERY_REQUIRED), idempotent simüle emir kimlikleri, kısmi dolum, hedge kurtarma, simüle ret ve zaman aşımı, yeniden başlatma kurtarması ve iç pozisyon mutabakatı. Hepsi mevcut paper dolum simülasyonunun üzerine kurulur. Borsa hesabı okunmaz, emir gönderilmez, kimlik bilgisi gerekmez.
8. **Faz 8 — Paper trading doğrulaması.** Gerçek public veriyle, yalnız simüle yürütme.
9. **Faz 9 — Canlı hazırlık incelemesi.** Gelecekteki, ayrıca yetkilendirilecek bir kilometre taşı. Otomatik başlamaz. Başarılı bir paper dönemi de onu başlatmaz. Ancak tam bir hazırlık incelemesi ve proje sahibinin açık yazılı onayıyla açılabilir.

Paralel ve ucuz iş: CI (GitHub Actions ücretsiz kotası) ile her push'ta test.

---

## R. Engeller ve gereken girdiler

| Gereken | Neden | Engellediği | Şimdi güvenle yapılabilecek |
|---|---|---|---|
| 9 borsanın resmî API belgeleri (funding işareti, birimi, aralığı, tavanı; zaman damgası anlamı; hız limitleri) | Bu ortam belgeleri okuyamıyor; kurallar DECLARED | Funding motoru doğrulaması, VERIFIED etiketleri | Kaydedicinin canlı kanıtı |
| Kaydedicinin kullanıcı PC'sindeki ≥72 saatlik çıktısı (`Phase1-Paket.cmd` ZIP'i) | Canlı kanıt yalnız orada | Birim ve işaret doğrulaması | Analiz kodu |
| Variational gerçek `metadata/stats` yanıtı | Alan anlamları bilinmiyor | Variational bağdaştırıcısı | Yok |
| ~~Hesap/trading API erişimi~~ **Gerekmiyor** | Paper-only politikası: hesap ve emir olayları simüle edilir; Extended anahtarı da istenmez (bölüm T) | — | Her şey |
| Gözden geçirilmemiş `marketdata/` (`b328f3e`) için karar: kabul / değiştir / geri al | Önceki görevden kaldı | Faz 1b | — |
| Railway dağıtımı hâlâ çalışıyor mu? | Güvenlik yüzeyi | O bölümü | — |
| PR #54 birleştirilecek mi? | Yeni iş bu dalın üstünde | Dal stratejisi | — |

---

## S. Hazırlık değerlendirmesi

| Alan | Durum |
|---|---|
| Veri altyapısı | PARTIALLY IMPLEMENTED; canlı kanıt 4 borsada, kısa süreli (REQUIRES VERIFICATION) |
| Funding stratejisi | PARTIALLY IMPLEMENTED; paper'da çalışıyor; konvansiyonlar DECLARED |
| Fiyat yakınsama stratejisi | PARTIALLY IMPLEMENTED; sezgisel istatistik, doğrulanmış model değil |
| Kâr hesabı | PARTIALLY IMPLEMENTED; tek fonksiyon yok, `float` |
| Simülasyon | PARTIALLY IMPLEMENTED; paper için iyi; senaryo katmanları yok |
| Risk | PARTIALLY IMPLEMENTED; pozisyon ve gün düzeyinde var, portföy düzeyinde yok |
| Paper yürütme | PARTIALLY IMPLEMENTED; dolum, kısmi dolum ve bacak kurtarma var; emir durum makinesi yok (Faz 7) |
| Yürütme (gerçek) | Kapsam dışı: politika gereği DISABLED (bölüm T) |
| Muhasebe | IMPLEMENTED (paper); borsa mutabakatı MISSING |
| Paper trading | IMPLEMENTED; sonuçların güvenilirliği veri doğrulamasına bağlı (REQUIRES VERIFICATION) |
| Canlı trading | DISABLED (politika); kod yok; Faz 9 ayrı yazılı onay gerektirir |

**Bütçe etkisi:** bu fazda $0. Önerilen sıranın ilk adımları da $0 (yerel çalışma, ücretsiz API'ler, GitHub Actions ücretsiz kotası). VPS ancak sürekli kayıt gerekirse önerilir; o zaman ayrıca fiyat ve onay istenecek.

---

## T. Paper-only işletim politikası ve güvenlik doğrulaması

### T.1 Politika
Kalıcı proje kuralı (`CLAUDE.md`): **varsayılan mod PAPER, canlı mod DISABLED.**
- Bütün geliştirme, deney, strateji değerlendirmesi ve test simüle yürütmeyle yapılır.
- Gerçek public piyasa verisi kullanılabilir.
- Emir, dolum, kısmi dolum, ücret, kayma, funding ödemesi, bakiye, pozisyon, marjin ve PnL simüle edilir.
- Gerçek emir gönderilmez. Özel anahtar, imza, trading nonce'u ya da trading kimlik bilgisi gerekmez ve istenmez.
- İsteğe bağlı salt okunur kimlik bilgileri, public bilgi yeterliyse asla zorunlu olmaz.
- Paper ve canlı yürütme yalıtılmış kalır. Hiçbir uygulama ya da ayar değişikliği canlı yürütmeyi açamaz.
- Canlı işleme geçiş ayrı bir hazırlık incelemesi ve proje sahibinin açık yazılı onayını gerektirir (Faz 9).

### T.2 Gerçek public veri ve simüle hesap olayları

| Gözlenen borsa verisi (gerçek, public) | Nerede |
|---|---|
| Ham HTTP yanıtları | `audit` (paper deposu); kaydedicide `raw/` + `audit` |
| Mark fiyatları | `marks` |
| Mum ve gerçekleşen funding **oranları** | `hist_candles`, `hist_funding`; kaydedici `normalized/` |
| Extended RFQ defter mesajları | `rfq_messages` |
| Anlık oranlar, defterler, OI/hacim | Bellek içi satırlar (`WideMarkets`, `FiveFeed`); her biri bir audit kaydına bağlı |

| Simüle hesap olayları (paper) | Nerede |
|---|---|
| Cüzdan / bakiye (başlangıç `wide_capital` ayarından) | `kv` → `wide_wallet` |
| Açık ve kapalı pozisyonlar, dolumlar, kısmi dolumlar, unwind | `kv` → `wide_positions`, `wide_closed`; `events` |
| Funding **nakdi** (gerçek yayımlanmış oran × simüle miktar × mark) | `payments`, `ledger_events` |
| Ücretler, kayma, gerçekleşen / tahmini PnL, belirsizlik riski | Pozisyon kaydı, `paper_trades`, `ledger_events` |
| Paper dönemleri | `paper_periods` |

Sınır: bir funding ödemesi iki parçalıdır. Oran gözlenen veridir; nakit simüle olaydır. Kayıtta ikisi ayrı tutulur: oran `audit_id` ve kaynak alanlarıyla, nakit ödeme durumuyla (SETTLED / SETTLED_ESTIMATED_PRICE / NOT_RECORDED / UNKNOWN).

### T.3 Güvenlik doğrulaması

**Yöntem:** HEAD `b328f3e` üzerinde statik kod incelemesi: bütün Python ve panel JS dosyalarında arama, sonra eşleşen satırların okunması. Kod çalıştırılarak ağ trafiği izlenmedi.

| İddia | Durum | Kanıt | Sınır / varsayım |
|---|---|---|---|
| Gerçek emir gönderilmiyor | **VERIFIED (kod incelemesi)** | Testler dışında `create_order`, `edit_order` ya da emir uç noktası yok. CCXT'de yalnız public çağrılar var: `load_markets`, `fetch_tickers`, `fetch_ticker`, `fetch_order_book`, `fetch_ohlcv`, `fetch_funding_rate(s)`, `fetch_funding_rate_history`, `fetch_funding_intervals`, `fetch_mark_prices`, `fetch_open_interest(s)`, HTX public `swap_batch_funding_rate` (`wide_markets.py:235`). POST yalnız Hyperliquid `/info` (kaydedicide 4 tür izinli listesi, `clients/hyperliquid.py`) ve TxFlow `/info` (`perpMeta`, `activeAssetCtx`, `candleSnapshot`, `l2Book`, `risk_scan.py`) uçlarına gidiyor. Panel JS yalnız yerel `/api/...` çağırıyor. | CCXT kütüphanesinin içi denetlenmedi. CCXT nesneleri anahtarsız kuruluyor (`wide_markets.py:293`), bu yüzden kimlik isteyen uç noktaların çalışmaması **beklenir**: bu CCXT tasarımına dayanan bir varsayım. Ağ düzeyinde kanıt için paper deposundaki `audit` URL listesi kullanıcı PC'sinde taranabilir (REQUIRES VERIFICATION). |
| Borsada emir iptali yok | **VERIFIED (kod incelemesi)** | `cancel_order`, `cancel_all` ya da iptal uç noktası yok. | Aynı sınır. |
| Özel anahtarla imza yok | **VERIFIED (kod incelemesi)** | İmza kütüphanesi (eth_account, ecdsa, starknet) ya da özel anahtar alanı yok. `hmac` yalnız panel parolasının sabit zamanlı karşılaştırması için (`bot.py:1575`). | — |
| Trading nonce'u gerekmiyor | **VERIFIED (kod incelemesi)** | `nonce` hiçbir kaynakta geçmiyor. | — |
| Trading izni gerekmiyor | **VERIFIED (kod incelemesi)** | Tek kimlik bilgisi isteğe bağlı salt okunur Extended anahtarı (T.4). | — |
| Paper işletimi için gerçek bakiye gerekmiyor | **VERIFIED (kod incelemesi + testler)** | Bakiye ya da pozisyon okuyan çağrı yok (`fetch_balance`, `fetch_positions` geçmiyor). Paper cüzdanı ayardan başlıyor (`wide_paper.py:993`, `wide_capital`). 482 testlik paket kimlik bilgisi olmadan çalışıp geçiyor. | — |
| Paper ve canlı yalıtımı | **VERIFIED (dolaylı)** | Canlı yürütme yolu yok; yalıtılacak ikinci bir yol bulunmuyor. | Gelecekte bir yol eklenirse yalıtım ayrıca tasarlanmalı ve test edilmeli (Faz 9). |
| Kod değişince garanti sürüyor | **Garanti edilmez** | Bu doğrulama yalnız incelenen commit için geçerli. | Öneri (onaylanırsa, ayrı iş): yasaklı çağrı adlarını arayan bir test ya da CI kontrolü. Bu turda kod değişikliği yapılmadı. |

### T.4 İsteğe bağlı Extended ücret kimlik bilgisi
- **Ne yapıyor:** panelde "İsteğe bağlı salt okunur API anahtarı" alanı var (`panel.js`). Girilirse yalnız `GET https://api.starknet.extended.exchange/api/v1/user/fees?market=BTC-USD` isteğine `X-Api-Key` başlığıyla gidiyor (`connections.py:116-127, 187-230`). Windows'ta DPAPI ile şifreli, başka yerde yalnız bellekte saklanıyor.
- **Kullanımı (VERIFIED, kod incelemesi):** okunan ücret yalnız `verified_extended_fees` anahtarına yazılıyor ve sadece bağlantılar sayfasında gösteriliyor. Bu anahtarı okuyan tek modül `connections.py`. Paper motoru bu değeri kullanmıyor; panel de bunu söylüyor: "Paper ücret varsayımı otomatik düşürülmez".
- **Paper ücretleri anahtarsız olarak şuradan geliyor:**
  - Lighter/Extended için ayar varsayılanları (`bot.DEFAULTS`: `extended_fee=0.00025`, `lighter_fee=0.00028`; ayar alt sınırları bu değerlere eşit, daha düşük girilemez);
  - CCXT piyasa bilgisindeki public `taker` alanı;
  - Arcus public `feetiers`;
  - kullanıcının panelde elle girdiği ücret (`wide_fees`).
- **Neden gereksiz:** public tarife yeterliyse anahtar hiçbir paper hesabını değiştirmez. Uygulama anahtarsız tam çalışıyor (bütün testler anahtarsız).
- **Politika:** Extended API anahtarı **istenmez**. Alan isteğe bağlı olarak kalır. Bu turda kod değişmediği için alan panelde hâlâ görünüyor.
- **Açık nokta:** `extended_fee=0.00025` ve `lighter_fee=0.00028` varsayılanlarının borsaların güncel public tarifesiyle uyuşup uyuşmadığı **REQUIRES VERIFICATION**. Belgeler bu ortamdan okunamıyor.

### T.5 Paper yürütme mimarisi ve sınırları
- **Mimari:** `WidePaper.open` her bacağı sırayla taze bir defter okuyarak doldurur:
  - venue gecikmesi: yapılandırılmış 750 ms ya da ölçülen p90 RTT, hangisi büyükse;
  - limit fiyatla IOC, 1 bp ek kayma;
  - kısmi dolumda ikinci bacak birinciye göre boyutlanır, eşleşmeyen kısım hemen kapatılır (`unwind`);
  - hiç dolmazsa işlem sayılmaz; bacak kurtarılamazsa `RECOVERY`.
- **Muhasebe:** ödemeler, cüzdan ve pozisyon tek işlemde yazılır. Defter olayları benzersiz anahtarlı ve append-only.
- **Sınırlar:**
  - Kuyruk sırası, maker dolum ve piyasa etkisinin defter sonrası toparlanması modellenmiyor. Yalnız taker varsayılıyor; bu bilinçli olarak muhafazakâr.
  - Borsa reddi, API zaman aşımında belirsiz emir durumu ve venue kesintisi simüle edilmiyor (Faz 7).
  - Çıkış likiditesi giriş anının defterinden tahmin ediliyor.
  - Tasfiye, borsa motoru değil yaklaşık mesafe kuralıyla izleniyor (`LEG_SAFETY`).
  - Para tutarları `float`.
  - Funding nakdi DECLARED işaret ve birim kurallarına dayanıyor (bölüm E).

### T.6 Gelecek geliştirme kısıtları
- Her yeni modül paper ile uyumlu olmalı. Yürütme yalnız simüle yürütme arayüzü üzerinden yapılır.
- Hata senaryoları (ret, zaman aşımı, kısmi dolum, başarısız hedge, yeniden başlatma) simüle yürütmeyle test edilir.
- Borsa bağdaştırıcılarına hesap, emir, iptal ya da imza çağrısı eklenmez. Bir kütüphane bunları sunsa bile çağrılmaz.
- Gözlenen veri ile simüle olay kayıtta, raporda ve panelde ayırt edilebilir kalır.
- Canlı moda geçişi açan bir ayar, bayrak ya da ortam değişkeni eklenmez.
- Faz 9 otomatik başlamaz; ayrı bir hazırlık incelemesi ve yazılı onay gerektirir.
