# 11 — Paper yürütme ve risk

## Bugün (V2-0, `wide_paper.py`)

| Konu | Uygulama |
|---|---|
| Dolum | Defterde IOC; %0,1 fiyat sınırı; borsanın ölçülen p90 gecikmesi; +1 bp ek kayma. Bacaklar sırayla dolar |
| Kısmi dolum | Lot tabanı; eşleşmeyen kısım hemen kapatılır (`unwind`) |
| Bacak dolmazsa | `RECOVERY` → kapanış, sınıf `LEG_RECOVERY`. Kazanç sayılmaz (V2-0) |
| Muhasebe | `ledger.py` defteri: SETTLED / SETTLED_ESTIMATED_PRICE / NOT_RECORDED / UNKNOWN; `reconcile` (V2-0 pilotunda 15/15) |
| Risk kapıları | Pozisyon ve günlük kayıp, spread stresi, bacak marjini, borsa teminatı, korelasyon / z / sd, başa baş (24 saat) |

## V2 hedefi (Phase 4)

**Yürütme modelleri** (özellik kapılı):

| Model | Kullanıldığı borsalar |
|---|---|
| `orderbook_vwap` | lighter_rh, risex, arcus, txflow, tradexyz |
| `rfq_time_limited_quote` | variational (kota en fazla 600 sn cache; firm quote farklı olabilir; last look) |
| `vault_dynamic_quote` | veranta |
| `limit_queue_estimate` | Yalnız araştırma; maker dolumu varsayılmaz |

**Durum makinesi:**

```
PROPOSED → RISK_APPROVED → ENTRY_LEG_1 / ENTRY_LEG_2_PENDING → HEDGED_OPEN | PARTIAL_EXPOSURE
→ EXIT_PENDING → CLOSED | LEG_RECOVERY | FAILED
```

`LEG_RECOVERY` için kurtarma maliyeti ve delta açıklığı kaydedilir.

**Defter kalemleri** (net = kalemlerin toplamı):
`price_pnl`, `realized_funding`, `estimated_funding`, `execution_fees`, `spread`, `slippage`, `borrow`, `liquidation`, `gas`, `recovery_cost`.

**Bağımsız risk kapısı ek olarak şunlara bakacak:**
- kota yaşı;
- RWA seansı;
- tasfiye mesafesi;
- borsa kesintisi;
- çıkış likiditesi;
- eşzamanlılık.

Risk motoru devre dışıysa yeni giriş açılmaz.

**Ayar değişikliği politikası:**
- V2-0 pilotunda funding pozisyonlarının 10'u toplu kapatıldı ve ölçüm bozuldu.
- V2'de ayar değişikliği açık pozisyonları sessizce kapatmayacak.
- Sahibin onaylayacağı bir politika tasarlanacak: mevcut pozisyon eski ayarlarla tamamlanır, yeni ayarlar yalnız yeni girişlere uygulanır.
- Bu fazda koda dokunulmadı.

Paper PnL hiçbir zaman gerçek kazanç olarak raporlanmaz. Paper kurtarmanın gerçekte güvenli olduğu iddia edilmez.
