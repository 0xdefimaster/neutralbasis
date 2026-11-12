# 13 — Test matrisi

Komut: `python -m unittest discover -p "test_*.py"` (venv `C:\Users\W10\nb-verify-venv`, Python 3.13).

Testler MOCK fixture'larla deterministik çalışır. Canlı public smoke ayrı ve açık opt-in bir işlemdir (`tools/v2_smoke.py`). **Birim testin geçmesi canlı API kanıtı değildir; kârlılık kanıtı hiç değildir.**

| # | Gereksinim | Test | Durum |
|---|---|---|---|
| 1 | Tam 8 `exchange_id`; legacy otomatik açılamaz | `test_v2_policy.UniverseTests` | ✅ |
| 2 | CEX ağ isteği sıfır (REST, CCXT host'ları, DNS) | `EgressTests.test_legacy_and_cex_hosts_are_refused`, `test_every_ccxt_cex_host_is_outside_the_allowlist`, `GuardV2Tests`, `UniverseTests.test_catalog_keeps_only_the_eight`; canlı smoke (3 red) | ✅ REST, DNS, CCXT kataloğu. ⏳ WS ve zamanlayıcı fixture'ları (Phase 5) |
| 3 | `api.hyperliquid.xyz/info` yalnız xyz kapsamında | `EgressTests.test_hyperliquid_only_inside_the_xyz_dex`, `TradexyzAdapterTests`, `GuardV2Tests.test_body_sent_with_or_after_the_headers_is_checked` | ✅ |
| 4 | Tek borsalı piyasa: abonelik, aday ve backfill yok | `DiscoveryTests.test_single_venue_markets_get_no_streams_or_pairs` | ✅ abonelik. ⏳ aday üretimi ve backfill (Phase 3–4) |
| 5 | İkinci listeleme açar, delist kapatır, tutma korur | `DiscoveryTests.test_second_listing_opens_and_delisting_closes_except_holds` | ✅ |
| 6 | RWA / CFD / endeks, QNT / ANTHROPIC, 1000PEPE, aynı sembol farklı varlık | `IdentityTests` | ✅ |
| 7 | Funding birimi (yüzde / kesir / bp / ppm), zaman damgası (ns / µs / ms / s) | `FeeTests.test_units`; V2-0 `test_v2_evidence` | ✅ birim. ⏳ 1 saat / 8 saat / sürekli tahakkuk modeli (Phase 4) |
| 8 | Maker / taker ayrı, dört taraflı ücret, HIP-3 ölçeği, 0 ücret + spread, UNKNOWN | `FeeTests`, `TradexyzAdapterTests.test_parse_fees_and_missing_values` | ✅. ⏳ hesap kademesi seçimi |
| 9 | Snapshot + delta sequence gap, WS yeniden bağlanma, 429, şema kayması | — | ⏳ Phase 3 (WS işçileri yok) |
| 10 | L2 VWAP / vault / RFQ; kısmi dolum; diğer bacak reddi; `LEG_RECOVERY` kazanç sayılmaz | V2-0 `test_v2_accounting`, `test_wide_paper` | ✅ defter / LEG_RECOVERY. ⏳ RFQ ve vault modelleri |
| 11 | Defter uzlaştırma; tahmini funding doğrulanmış sayılmaz | `test_paper_accounting`, `test_migrations`, V2-0 `test_v2_accounting`; pilotta 15/15 | ✅ |
| 12 | 24 saatlik başa baş değişmedi; sessiz gevşetme yok; ayar değişikliği pozisyonu sessizce kapatmıyor | `test_v2_accounting.EconomicsTests`, `DeskTests` | ✅ 24 saat / 1,0. ⏳ ayar değişikliği politikası (sahip kararı) |
| 13 | STRICT / LOOSE korundu; dondurulmuş veri seti deterministik, sha256 | `test_v2_evidence.DatasetTests` | ✅ |
| 14 | Çökme, disk dolu, WAL riski, salt okunur DB koruması | Mevcut `test_retention`, `test_runtime_reliability`, `test_risk_main` (kısmi); `v2_preflight` `mode=ro` | ⏳ kaos testleri |
| 15 | Özel anahtar, imza ya da emir gönderimi yok | `ACCOUNT_KEYS` testleri (TxFlow `user`, HL `clearinghouseState`), `/exchange` reddi; mevcut paper-only testleri | ✅ |
| 16 | UI sayaçları, tüm adayların sayfalanması, why-no-trade | Mevcut `test_candidate_page`, `test_zero_trade` | ⏳ V2 sayaçları (Phase 5) |

## Bu fazın gerçek sonuçları

| Çalıştırma | Sonuç |
|---|---|
| V2-0 (4576d0e) | 801 OK, 2 atlanan |
| Bu dal, tradexyz testlerinden önce | 825 OK, 2 atlanan |
| Son çalıştırma | `17_ACCEPTANCE_REPORT.md` |
