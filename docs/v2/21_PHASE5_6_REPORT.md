# 21 — Phase 5–6 raporu: gerçek zamanlı akış, hibrit masa, kaydedici, dayanıklılık

Tarih: 2026-11-12. Dal: `v2/venue-policy` (yerel; push, merge ve deploy yok). **PAPER ONLY.** Motor ve kaydedici **başlatılmadı**; E:'ye dokunulmadı.

Rakamların kaynağı: Phase 4'ün kayıtlı yanıtları (`C:\Users\W10\nb-benchmark\v2-phase4-2026-11-12\`, sha256 `f1ff9f2a…`, 2026-11-12 05:12 TR, ABD seansı kapalıyken). Bu yanıtlar yeni `Runtime` döngüsünden geçirildi (`tools/v2_dashboard.py`; ağ yok, motor yok). Kâr iddiası değil.

## 1. Üç stratejinin durumu

| | Funding arbitrajı | Fiyat yakınsaması | Hibrit |
|---|---|---|---|
| Değerlendirme modülü | `v2/strategies/funding.py`: tamam | `convergence.py`: tamam (iki yön) | `hybrid.py`: tamam |
| Gerçek zamanlı akış | `v2/runtime.Runtime`: her turda toplayıcı → aday → kapı | aynı | aynı |
| Paper açma ve kapama | `WidePaper` + V2 kapısı (`assess` kancası) | `WidePaper` + V2 kapısı | **`v2/hybrid_desk.HybridDesk`**: kendi pozisyon tipi ve durum makinesi; açılış `wide_hybrid_enabled` ile (varsayılan **kapalı**, onayına bağlı) |
| Bugün üretilen aday | 481 | 1.058 (529 çift × 2 yön) | 1.058 |
| Paper'a uygun | **0** | **0** | **0** |
| Eksik | Settlement kanıtı yalnız RH / Arcus / tradeXYZ'de | Variational'da fiyat geçmişi yok | Gerçek açılış onaya bağlı; canlıda hiç çalışmadı |

### Ret nedenleri (ilk neden; kayıtlı anlık görüntü)

| Neden | Kategori | Funding | Yakınsama | Hibrit |
|---|---|---|---|---|
| EXECUTION_MODEL_UNVERIFIED (Variational RFQ) | Yürütme | 234 | 526 | 526 |
| RWA_SESSION_CLOSED (Arcus `isOutsideRth`) | Yürütme | 168 | 336 | 336 |
| SETTLEMENT_EVIDENCE_MISSING (TxFlow / Variational yalnız oran) | Veri | 40 | — | 118 |
| RWA_REFERENCE_UNVERIFIED (emtia / FX / endeks) | Veri | 23 | 46 | 46 |
| PRICE_HISTORY_MISSING | Veri | 3 | 46 | 6 |
| GAP_NOT_EXTREME | Maliyet | — | 82 | — |
| BREAKEVEN (> 24 saat) | Maliyet | 11 | — | 10 |
| CARRY_NOT_PERSISTENT | Maliyet | 2 | — | 13 |
| INSUFFICIENT_LIQUIDITY | Yürütme | — | 12 | 2 |
| NET_BELOW_MIN (net > 0 ama $0,50 altında dahil) | Maliyet | — | 10 | 1 |

Karşı-olgusal senaryoda (seans açık kabul edildi, fiyatlar seans dışı) da uygun aday **0**. En iyi tahmini netler:

| Strateji | Aday | Tahmini net |
|---|---|---|
| Yakınsama | AMD (tradeXYZ → Lighter RH) | +$0,38 (alt sınır $0,50) |
| Yakınsama | AMD (tradeXYZ → Arcus) | +$0,18 |
| Funding | AAPL | −$0,40 (başa baş 55 saat) |

Tüm tablolar `dashboard_session_open.html` içinde.

## 2. Eski 46 saatlik pilot ile V2

`tools/v2_compare_pilot.py`, çıktı `pilot_vs_v2.json`.

| Pilot işlemi | V2'de ilk engel |
|---|---|
| 10 işlem | `OUT_OF_V2_SCOPE` (Extended, Hyperliquid native, LighterCore) |
| 3 işlem (SOL, XRP, BTC; RISEx ↔ Lighter) | `FEE_UNKNOWN` (RISEx) |
| 2 işlem (ONDO, UNI) | `LEG_RECOVERY` (tek bacak; kazanç sayılmaz) |
| **V2'ye geçebilecek** | **0 / 15** |

| Gidiş-dönüş maliyeti ($1.000 / bacak) | Medyan | p10 | p90 |
|---|---|---|---|
| Pilotta gerçekleşen (11 iki bacaklı funding işlemi) | 21,5 bp | 18,8 | 25,7 |
| V2'nin gerçek defterlerle fiyatladığı (391 aday) | **16,1 bp** | 7,1 | 28,0 |

V2'de çiftlere göre medyan maliyet:

| Çift | bp |
|---|---|
| Lighter RH ↔ tradeXYZ | 6,1 |
| Arcus ↔ tradeXYZ | 9,4 |
| Lighter RH ↔ Variational | 10,2 |
| Arcus ↔ Lighter RH | 11,1 |
| Variational ↔ tradeXYZ | 11,9 |
| Arcus ↔ Variational | 14,7 |
| Lighter RH ↔ TxFlow | 16,6 |
| TxFlow ↔ tradeXYZ | 20,1 |
| Arcus ↔ TxFlow | 22,2 |
| TxFlow ↔ Variational | 22,5 |

## 3. Tamamlanan modüller (Phase 5–6)

| Görev | Modül | Durum |
|---|---|---|
| 1 Gerçek zamanlı değerlendirme | `v2/runtime.py` | Tamam. `desk_analytics.loop()` yalnız `NB_V2=1` ile ve yalnız motor döngüsü başlarken iş parçacığını açar |
| 2 Hibrit pozisyon ve durum makinesi | `v2/hybrid_desk.py` | Tamam. Durumlar OPENING → HEDGED_OPEN → EXIT_PENDING → CLOSED, artı LEG_RECOVERY ve FAILED. Çıkış öncelik sırası STOP > CONVERGED > FUNDING_REVERSED > MAX_HOLD; tek kapanış, ikincil nedenler ayrı kayıtlı. Defter idempotent anahtarlı. Kalıcılık atomik |
| 3 Seçici kaydedici | `v2/recorder.py` | Tamam. Yalnız V2 satırları; gzip segment, sha256 manifest, gap indeksi, disk koruması, çökme sonrası "recovered" mühürleme, saklama süresi. E: reddedilir |
| 4 CEX / legacy sıfır, xyz kapsamı | Önceki fazlar + kaydedici ve runtime yalnız V2 satırları | Korunuyor |
| 5 Borsa başına veri bütünlüğü | `v2/marketdata.py` | `SeqBook` (seq / prevSeq, boşluk ve çapraz defter → resync), `TxFlowDepthStream` (abone / ping / yeniden bağlanma / geri çekilme), `PollWorker`, borsa başına eskime sınırları. Kapı eski defteri reddeder (`BOOK_STALE`). TxFlow WS toplayıcıya bağlı, **varsayılan kapalı** (`NB_V2_TXFLOW_WS=1`) |
| 6 Dashboard | `v2/dashboard.py` + `GET /v2`, `GET /api/v2/state` (`bot.py`) | Strateji başına aday, kategori ve neden sayıları; tahmini net bileşenleri; açık hibrit pozisyonlar; borsa durumu |
| 7 Paper yürütme uçtan uca | `test_v2_phase5.HybridDeskTests`, `test_v2_phase4.LifecycleTests`, mevcut `test_wide_paper` | IOC, kısmi dolum ve unwind, ikinci bacak dolmazsa LEG_RECOVERY, funding ödemesi (çift görülse de tek kayıt), giriş ve çıkış ücreti, tek kapanış, EXIT_PENDING ve yeniden başlatma |
| 8 Variational RFQ | Kapı | Hâlâ `EXECUTION_MODEL_UNVERIFIED`. Belgeli bir dolum modeli kurulamadı (firm kota farklı olabilir, last look, 600 sn'ye varan cache) |
| 9 RISEx ücreti | Satır üretilmiyor + kapı | Değişmedi |
| 10 Pilot karşılaştırması | `tools/v2_compare_pilot.py` | Yukarıda |
| 11 Risk ayarları | `test_v2_phase5.OpsTests.test_risk_defaults_unchanged` | 24 saat / 1,0 değişmedi |
| 12 Uzun çalışma testleri | `test_v2_phase5` | Çökme ve yeniden başlatma (hibrit masa, kaydedici), bağlantı kaybı (WS yeniden bağlanma, sorgulama geri çekilmesi), eski veri, WAL eşikleri, disk dolu, açık pozisyon kurtarma |

## 4. Borsa başına veri yolu ve kanıt durumu

| Borsa | Taşıma | Bütünlük | Eksik API kanıtı |
|---|---|---|---|
| lighter_rh | REST sorgulama, 50 istek/dk bütçe | Eskime sınırı 30 sn | WS formatı okunmadı; Standard limit dar (60 / dk); funding `direction` anlamı belgede yok; hesap kademesi |
| risex | REST | 15 sn | WS checksum biçimi belgede yok; **ücret UNKNOWN**; mainnet host'u belgede yok |
| arcus | REST (`l2OrderBook`, lastSequenceId) | 15 sn | WS güncelleme mesaj formatı okunmadı |
| txflow | **WS `l2BookDepth`** (kapalı) + REST | seq / prevSeq, 10 sn | Herkese açık settled funding yok; WS canlıda denenmedi |
| variational | Paylaşılan `/stats` (bütçe, önbellek, 429 bekleme) | 60 sn | Settled funding yok; fiyat geçmişi yok; RFQ dolum modeli yok |
| tradexyz | REST (`l2Book`), XyzClient | 15 sn | Hyperliquid WS sayfası okunmadı; rate-limit ağırlıkları belgeden (1.200 / dk) |
| popdex_perps | — | — | Resmî API yok (BLOCKED) |
| veranta | — | — | Read-only sözleşme doğrulanmadı (RESEARCH_ONLY) |

## 5. Gerçek zamanlı PAPER pilotunu başlatmadan önce kalan engeller

1. **Onay:** `20_OPERATIONS_RUNBOOK.md`'deki başlatma; ayrıca TxFlow WS ve hibrit açılış için ayrı onaylar.
2. **Disk:** C:'de yaklaşık 12 GB boş. Önceki pilot 46 saatte DB başına 5,5 GB büyüdü (eski evren, ham audit dahil). V2 evreni daha küçük, ama ölçülmedi. Önerilen: sağlıklı yeni bir disk ya da Contabo.
3. **Uygun aday yok:** bugünkü kanıt ve maliyetle pilot büyük olasılıkla işlem açmayacak. Bu doğru bir sonuç; asıl kazanım fırsatların sürekli ölçülmesi.
4. **Canlıda hiç çalışmamış yollar:** V2 motor kaynağının uçtan uca döngüsü, TxFlow WS, kaydedicinin uzun süreli yazımı, hibrit masa. Testler offline ve MOCK verilerle.
5. **Kanıt boşlukları:** RISEx ücreti, TxFlow ve Variational settlement, Variational RFQ modeli, RWA referans kontratları, Lighter RH WS ve hesap kademesi.

## 6. Testler

- `test_v2_phase5.py` (yeni): 23 test.
- V2 test dosyalarının toplamı: 85.
- Tam paket (`python -m unittest discover -p "test_*.py"`): **886 OK, 2 atlanan, 0 başarısız.**
