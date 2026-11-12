# V2 pilot baseline — 2026-11-10 02:40 → 2026-11-12 00:35 (Türkiye saati)

**PAPER.** Bütün işlemler, dolumlar, ücretler ve ödemeler SIMULATED. Fiyatlar ve funding oranları OBSERVED public market data.

**Kaynaklar.** Salt okunur:
- `C:\Users\W10\nb-pilot\data\paper.sqlite3` (STRICT)
- `C:\Users\W10\nb-pilot-loose\data\paper.sqlite3` (LOOSE)
- `C:\Users\W10\nb-observer\observed_funding.sqlite3`

**Dondurulmuş veri seti.** `C:\Users\W10\nb-benchmark\pilot-2026-11-10_12\dataset.sqlite3`, sha256 `5fb3f25a…510d`.

**Yeniden üretim.** `python -m tools.v2_evidence baseline --dataset <dir>` çıktısı `results/baseline.json` dosyasına yazılır.

**Kod.** `local/audit-fixes-2026-11-09` @ e8a0e7b (pilotlar bu dalın sürümleriyle çalıştı).

Doğrulama durumları:
- **V** = VERIFIED_FROM_DB
- **D** = DERIVED
- **T** = PARSED_FROM_TEXT
- **U** = UNVERIFIED / UNKNOWN

## 1. Sonuçlar

| Ölçüm | Değer | Durum |
|---|---|---|
| Kapanmış işlem | 15 | V |
| Kapanmış işlemlerin toplam neti | −$19,13 | V |
| Funding işlemleri | 12 işlem, 0 kazanan, −$21,13 | V |
| Yakınsama (fiyat farkı) işlemleri | 3 işlem, +$1,99 | V |
| İki bacaklı temiz yakınsama | 2 işlem (WLD, SUI), +$1,10 | V + D |
| `LEG_RECOVERY` (ikinci bacak dolmadı) | 2: ONDO funding −$0,01, UNI yakınsama +$0,90 | D (yeni sınıf, eski kayıtlara uygulandı) |
| Durdururken açık kalan pozisyon | 3 (BTC, AVAX, NVDA funding), son kayıtlı "kapatılsaydı" −$4,47 | V (kv). 00:34 API anlık görüntüsünde −$4,87; zaman farkı var |
| Yeniden başlatma (BOOT olayı) | STRICT 9, LOOSE 16 | V |

Not: −$19,13, kuruş yuvarlamasıyla daha önce bildirilen −$19,14 ile aynıdır. Kesin değer −$19,131.

### PnL bileşenleri (kapanmış 15 işlem)

| Bileşen | Funding (12) | Yakınsama (3) | Toplam | Durum |
|---|---|---|---|---|
| Fiyat PnL (kayma dahil) | −$10,22 | +$4,62 | −$5,60 | V |
| — bunun içindeki kayma | $8,98 | $2,65 | $11,63 | V (`slippage`) |
| Funding, SETTLED (simüle uzlaşma, yayımlanan oran ve gözlenen fiyat) | +$1,53 | $0 | +$1,53 | V (ledger `funding`) |
| Funding, tahmini fiyatla değerlenen | +$0,05 | $0 | +$0,05 | V (ledger `funding_estimated`) |
| Ücretler | $12,49 | $2,62 | $15,11 | V |
| **Net** | **−$21,13** | **+$1,99** | **−$19,13** | V |

### Sonuç sınıfları (yeni, `wide_paper.trade_outcome`)

| Sınıf | Funding | Yakınsama |
|---|---|---|
| STRATEGY_EXIT (iki bacak, stratejinin kendi çıkışı) | 1 (SOL −$1,45) | 2 (WLD +$0,34, SUI +$0,75) |
| MANUAL_CLOSE (toplu veya elle kapatma) | 10 (−$19,67) | 0 |
| LEG_RECOVERY | 1 (ONDO −$0,01) | 1 (UNI +$0,90) |
| RISK_EXIT | 0 | 0 |

Funding işlemlerinin 10'u, borsa ayarlarını değiştirirken yapılan toplu kapatmayla kapandı. Strateji kendi kuralıyla ölçülemedi.

### İşlemler

| Pilot | Varlık | Strateji | Long / Short | Süre | Tahmini net (açılış) | Net | Sınıf |
|---|---|---|---|---|---|---|---|
| LOOSE | SOL | funding | RISEx / Lighter | 21,0 sa | +$1,40 | −$1,84 | MANUAL |
| LOOSE | DOGE | funding | Hyperliquid / Extended | 20,6 sa | +$0,04 | −$2,27 | MANUAL |
| LOOSE | ONDO | funding | RISEx / — | 12 sn | — | −$0,01 | LEG_RECOVERY |
| LOOSE | ONDO | funding | RISEx / Hyperliquid | 15,3 sa | +$0,00 | −$0,00 | MANUAL |
| STRICT | WLD | yakınsama | LighterCore / Extended | 54 sn | +$0,51 | +$0,34 | STRATEGY |
| LOOSE | XRP | funding | RISEx / Lighter | 6,1 sa | +$0,74 | −$2,27 | MANUAL |
| LOOSE | PUMP | funding | Hyperliquid / RISEx | 6,1 sa | +$0,08 | −$3,17 | MANUAL |
| LOOSE | ONDO | funding | RISEx / Extended | 5,8 sa | +$0,09 | −$3,75 | MANUAL |
| LOOSE | BTC | funding | RISEx / Extended | 4,7 sa | +$0,14 | −$1,72 | MANUAL |
| LOOSE | AVAX | funding | LighterCore / Extended | 4,4 sa | +$0,30 | −$0,30 | MANUAL |
| LOOSE | SUI | yakınsama | Extended / LighterCore | 2 dk 35 sn | +$1,27 | +$0,75 | STRATEGY |
| LOOSE | SOL | funding | RISEx / Extended | 2,4 sa | +$0,60 | −$1,45 | STRATEGY |
| LOOSE | UNI | yakınsama | Extended / — (Hyperliquid dolmadı) | 16 sn | +$0,27 (T) | +$0,90 | LEG_RECOVERY |
| LOOSE | DOGE | funding | Hyperliquid / Extended | 11 dk | +$0,06 | −$2,38 | MANUAL |
| LOOSE | BTC | funding | RISEx / Lighter | 25 dk | +$0,08 | −$1,97 | MANUAL |

Tahmini net, LOOSE funding işlemlerinde **168 saatlik** ufuk içindir (T: giriş metninden). İşlemler bu sürenin yalnız %0–13'ü kadar tutuldu.

### Kayıp veri (U)

Kapanmış LOOSE işlemlerinin yapısal giriş kararı (ücret, kayma, risk payı ayrıntısı) dönem sıfırlamalarında silindi; `kv wide_closed` dönem başına tutuluyor. Elde kalanlar:
- `WIDE_OPEN` olayındaki tahmini net;
- giriş metni.

V2-0 ile her işlem kaydı `entry_economics` taşıyor; bu kayıp bundan sonra tekrarlanmaz.

## 2. STRICT ve LOOSE doğrudan karşılaştırılamaz

İki pilotun sermayesi, borsa kapsamı, risk parametreleri ve süreleri farklıydı. Sonuçları toplanabilir ama birbirleriyle karşılaştırılamaz. "LOOSE daha çok işlem açtı, öyleyse gevşek ayar daha iyi" gibi bir çıkarım bu veriyle yapılamaz.

| | STRICT | LOOSE |
|---|---|---|
| Sermaye | $8.000 (8 borsa × $1.000) | $8.000 → $80.000 → $130.000 → $140.000 (4 dönem) |
| İşlem sepeti borsaları | 8 | 8 → 13 → 14 |
| Taranan borsa | 9 | 15 |
| Kapsam anahtarları | yok | `NB_EXTRA_PERP_DEXES` (6 borsa), `NB_UNIVERSE_MIN_USD=200000`, `NB_LIGHTER_RH_ALL`, `NB_RFQ_STREAMS=40`, `NB_TXFLOW_ALL`, `NB_RISEX_RWA` |
| `wide_horizon` | 24 sa | 168 sa |
| `wide_min_net` | $0,50 | $0 |
| `wide_min_corr` | 0,90 | 0,80 |
| `wide_max_spread_sd_bps` | 30 | 60 |
| Yakınsama kesişim 7g / 30g, epizot | 6 / 20, 3 | 3 / 10, 2 |
| `wide_deep_top` | 5 | 10 |
| `wide_rwa_verified`, `wide_txflow_paper` | kapalı | açık |
| Ücret ayarları | RISEx | RISEx, TxFlow, Aster, Pacifica, Hibachi, Nado |
| Ayar değişikliği ve dönem | 1 dönem | 4 dönem, dönem içi değişiklikler; açık pozisyonlar dönem değişiminde toplu kapatıldı |
| Pazar dönemi | Aynı ~46 saat, ama ayarlar farklı zamanlarda değişti | |

Ayrıca her iki pilotun da örneği çok küçük:
- STRICT: 1 işlem;
- LOOSE: 14 işlem.

Bu işlemler masanın kendi güven eşiğine (30 gün, 40 işlem) uzak. Bu baseline bir başlangıç ölçümüdür, strateji kanıtı değildir.

## 3. Veri toplama (V, kaynak DB'lerden salt okunur sayım)

| | STRICT | LOOSE |
|---|---|---|
| Fiyat kaydı (marks) | 1.529.228 | 1.421.820 |
| Funding geçmişi (30 gün geriye) | 355.917 | 544.387 (12 borsa) |
| Mum geçmişi | 333.685 | 716.940 (14 borsa) |
| Extended RFQ mesajı | 170.907 (66 piyasa) | 205.270 (77 piyasa) |
| Ham API yanıtı (audit) | 248.387 | 327.678 |
| Simüle funding ödemesi | 0 | 173 |
| Son durumda piyasa / varlık / çift | 662 / 131 / 503 | 1.347 / 253 / 2.352 |

### Gözlemci

Dönem: 2026-11-11 23:28 → 2026-11-12 00:31. Tüm satırlar `OBSERVED_PUBLISHED`; ödeme kaydı değil.

- **TxFlow:** 3.368 satır, 242 piyasa. 14 tur; tamamen başarısız tur yok, 20 piyasa hatası var.
- **Variational:** 4.576 satır, 572 piyasa. 14 turun 6'sı tamamen başarısız; hepsi `HTTP 429 Too Many Requests`.

Variational'ın 429 hatasının, pilotun aynı IP'den yaptığı Variational okumalarıyla çakışmadan kaynaklandığını düşünüyorum. Bu bir hipotez, doğrulanmadı (U).
