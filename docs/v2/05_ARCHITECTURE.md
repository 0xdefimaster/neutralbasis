# 05 — Mimari: hedef ağaç ve gerçek modüller

Mimari bir modüler monolittir: borsa başına bağımsız veri işçisi ve stratejiden bağımsız bir kaydedici. Mikroservis kurulmadı. Çalışan kod silinmedi; hedef ağaç, mevcut modüllere aşağıdaki gibi eşleniyor.

| Hedef | Bugün (gerçek dosya) | V2 durumu |
|---|---|---|
| `core/policy`, config tek kaynak | `v2/policy.py` | **Yeni**: 8 `exchange_id`, legacy/CEX listeleri, `NB_V2` fail-closed, `restrict_catalog` |
| `core/ids` | `v2/policy.LEGACY_NAME` / `FROM_LEGACY` | Yeni: veritabanında yeniden adlandırma yok; eski adlar okunur |
| ağ izni | `pilot/network_guard.py` + `v2/network_policy.py` | **Yeni V2 kipi**: host + metot + yol + gövde kapsamı + exchange_id; xyz istisnası; gövde ayrı parçada gelse de denetleniyor |
| `discovery/canonical_identity` | `v2/identity.py` | **Yeni**: pozitif kanıt kapısı, çarpan ayrıştırma, çakışma listesi |
| `discovery/metadata`, `intersection`, `subscriptions` | `v2/discovery.py` | **Yeni**: TTL+jitter katalog, 2+ borsa kesişimi, referans sayımlı abonelik ve tutmalar |
| `economics/fees` | `v2/fees.py` | **Yeni**: kaynaklı ücret kaydı, UNKNOWN ≠ 0, ppm/yüzde/bp, ns/ms/s, HIP-3 ölçeği, dört taraflı ücret |
| `economics/breakeven`, funding | `wide_paper.funding_economics`, `breakeven_refusal` (V2-0) | Korundu (24 saat / 1,0) |
| `venues/*` adaptörler | `risk_scan.py` (Lighter RH, RISEx, Arcus, TxFlow), `connectors.py`, `variational_public.py`, `wide_markets.py` (tradeXYZ CCXT) | Mevcut; V2 adaptör katmanına taşıma Phase 3 |
| `strategies`, `opportunities` | `wide_paper.py` (rank, assess_funding, assess_convergence, verdict) | Mevcut; hibrit ve funnel kodları Phase 4 |
| `paper/ledger` | `ledger.py` (ödeme durumları, reconcile) + V2-0 outcome sınıfları | Mevcut |
| `recorder` | `tools/phase1_recorder/`, `history_store.py`, `tools/funding_observer.py` | Mevcut; seçici V2 kaydedici Phase 5 |
| replay ve kanıt | `tools/v2_dataset.py`, `tools/v2_evidence.py`, `tools/v2_preflight.py` (V2-0); `tools/v2_smoke.py`, `tools/v2_discovery_snapshot.py` (yeni) | Mevcut |
| api, dashboard | `bot.py`, `desk_analytics.py`, `static/` | V2 sayaçları Phase 5 |

Kalabalık dosyalar (`bot.py`, `capabilities.py`, `risk_scan.py`, `wide_paper.py`) bu fazda bölünmedi. Bölmeden önce dış arayüzleri karakterizasyon testleriyle kilitlenecek (`13_TEST_MATRIX.md`).

## V2 kipi akışı (bugün çalışan kısım)

```
NB_V2=1
  -> v2.policy.check_environment()           legacy genişletme anahtarları reddedilir
  -> network_guard.install(v2=True)          süreç düzeyinde 8 borsa kapsamı
  -> v2.policy.restrict_catalog(CATALOG)     CCXT/CEX/legacy taramaları kapanır
  -> (Phase 3) v2 adaptörleri -> LightweightDiscovery -> intersect -> SubscriptionManager
```

Not: Eski `FiveFeed` (`risk_scan.py`) içindeki Extended ve LighterCore okumaları V2 kipinde koruma tarafından reddedilir; yani fail-closed çalışır. Ancak bu okumalar hâlâ deneniyor ve hata kaydı üretiyor. Bu yolların kaldırılması Phase 3'ün işi (`16_BLOCKERS.md` B6).
