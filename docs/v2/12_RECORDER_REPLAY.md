# 12 — Kaydedici ve replay

## Mevcut olanlar

| Bileşen | Kapsam |
|---|---|
| `history_store.py` (`hist_funding`, `hist_candles`) | Pilot DB'ye yazar |
| `tools/phase1_recorder/` | Ham kayıt; manifest |
| `tools/funding_observer.py` | TxFlow + Variational; `OBSERVED_PUBLISHED` |
| V2-0 dondurulmuş veri seti | `C:\Users\W10\nb-benchmark\pilot-2026-11-10_12`, sha256 5fb3f25a…. **Hiçbir zaman silinmez** |
| V2 smoke fixture'ları | `C:\Users\W10\nb-benchmark\v2-smoke-2026-11-12`, sha256'lı manifest |

## Hedef (Phase 5)

**Seçici kaydedici:**
- Yalnız 8 borsa.
- Yoğun ham kayıt (REST / WS / L2 / trades / funding) yalnız `MULTI_VENUE` ve doğrulanmış enstrümanlarda; pozisyon ve kurtarma tutmaları da kaydedilir.
- Tek borsalı metadata düşük frekansta tutulur.

**Depolama:**
- Değişmez ve sıkıştırılmış ham olaylar.
- Normalize kolonlu veri (Parquet).
- SQLite checkpoint ve manifestler.
- Her kayıtta: sha256, şema sürümü, UTC `source_ts` / `received_ts`, gap indeksi, replay imleci.
- Atomik yazma, disk bütçesi, saklama süresi ve yedek/geri yükleme.

**Kurallar:**
- Motor kapalıyken oluşan veri boşluğu açıkça işaretlenir; otomatik backfill varsayılmaz.
- Backfill rate-limit bütçesine bağlıdır.
- SQLite WAL büyümesi izlenir. Eski 3 GB WAL olayı nedeniyle checkpoint yönetimi ayrı bir bileşen olacak.

## Kapasite

CPU, RAM, ağ ve günlük disk baytı profili gerçek ölçümle çıkarılacak.

Bilinen tek gerçek veri V2-0 pilotu: ~46 saatte DB başına 5,5 GB. Eski evrenle, audit ham verisiyle ve yaklaşık 1 günlük saklamayla ölçüldü.

V2 evreni (bugün katı kesişimde 15 varlık) çok daha küçük olacak. VPS boyutu (ör. 4 vCPU / 8 GB) ölçümden sonra önerilecek.
