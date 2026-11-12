# 17 — Kabul raporu (Phase 0–2 teslimi, 2026-11-12)

**Sonuç: V2 tamamlanmadı.** Phase 0 (baseline), Phase 1 (resmî kaynak araştırması) ve Phase 2'nin politika, ağ, kimlik, keşif ve ücret çekirdeği yapıldı. Adaptörlerin motora bağlanması (Phase 3), ekonomi, strateji ve paper durum makinesi (Phase 4), kaydedici ve dashboard (Phase 5) ile doğrulama (Phase 6) açık.

**Dal:** `v2/venue-policy` (4576d0e üstünde, yerel; push yok).

**Testler** (`python -m unittest discover -p "test_*.py"`): **828 OK, 2 atlanan.** V2-0'da 801'di; bu fazda 27 test eklendi (`test_v2_policy.py`).

## Kabul kriterleri

| Kriter | Durum | Kanıt |
|---|---|---|
| Hedef borsa sayısı tam 8 | ✅ | `v2/policy.EXCHANGE_IDS`, `UniverseTests` |
| Aktif CEX ve legacy veri işçisi sıfır | ⚠️ Kısmi | V2 kipinde CCXT kataloğu kapanıyor (26 borsa), ağ koruması CEX ve legacy host'ları DNS'te reddediyor (test + canlı). Eski `FiveFeed` Extended / Core yolları hâlâ deneniyor ve reddediliyor (B6) |
| tradeXYZ HIP-3 istisnası doğru kapsamda | ✅ | `network_policy`, `GuardV2Tests`; canlı: `dex=xyz` 200, native reddedildi; CCXT `spotMeta` reddedildi (B7) |
| 8 borsa için yetenek, ücret ve engel dosyaları | ✅ | `02`, `03`, `16`, `venues/*.md` |
| Metadata keşfi, tek borsa filtresi, 2+ kapısı | ⚠️ Çekirdek hazır | `v2/discovery.py` + testler; canlı anlık görüntü (15 doğrulanmış varlık / 1 çift; sembol eşleşmesiyle 242). Sürekli çalışan adaptör döngüsü yok |
| Yalnız iki bacağı kanıtlı piyasalar paper'a uygun | ⏳ | Mevcut `wide_paper` kapıları + V2 kimlik ve ücret kapıları henüz motora bağlı değil |
| Tüm adaylar açıklanabilir (0 işlem dahil) | ⏳ | Mevcut why-no-trade ve aday sayfası korunuyor; V2 huni kodları Phase 4 |
| Paper durum makinesi ve defter testleri (2 bacak, kurtarma, PnL) | ✅ (V2-0) / ⏳ (yeni durumlar) | `test_v2_accounting`, `test_paper_accounting` |
| CEX çıkışı sıfır, Hyperliquid native çağrısı sıfır, xyz okumaları kanıtlı | ✅ | Testler + canlı smoke manifest'i |
| V2-0 raporları ve dondurulmuş benchmark bozulmadı | ✅ | Dosyalar yerinde; veri seti sha256 5fb3f25a…; `test_v2_evidence` geçiyor |
| E: DB, WAL, paper pozisyonları ve pilotlar değişmedi; deploy yok | ✅ | E:'ye erişim yok; süreç yok; push yok |
| VERIFIED / UNKNOWN / BLOCKED dürüstçe gösteriliyor; kârlılık iddiası yok | ✅ | `03`, `16` |

## Yalnız belgede doğrulananlar ve canlı smoke görenler

| Tür | Kapsam |
|---|---|
| **Canlı public smoke** (2026-11-12 00:54 UTC; 7 okuma) | tradexyz `metaAndAssetCtxs dex=xyz` ve `perpDexs`; txflow `perpMeta`; risex `/v1/markets`; arcus `/v1/markets`; lighter_rh `orderBookDetails`; variational `/metadata/v2/stats`. Fixture'lar `C:\Users\W10\nb-benchmark\v2-smoke-2026-11-12` (E:'ye yazma reddediliyor) |
| **Yalnız belgede** | Lighter RH `fundings`, `orderBookOrders`; RISEx defter, trades, funding geçmişi; Arcus `fundingRates`, `trades`, `candles`; TxFlow `activeAssetCtx`, `l2Book`, `marketTicker`, `feeTierInfo`; tradexyz `l2Book`, `fundingHistory`, `candleSnapshot` (`xyz:`). Bu fazda canlı çağrılmadı |
| **Hiç doğrulanmayan** | popdex_perps (resmî API yok); veranta (ağ izni yok) |

## Bir sonraki güvenli adım (Phase 3, onay gerektirmez; yerel ve testli)

1. Borsa adaptörleri: `v2/venues/{lighter_rh, risex, arcus, txflow, variational}.py`. Yalnız istek kurucuları ve ayrıştırıcılar; fixture testleri bugünkü smoke dosyalarından.
2. Kimlik kaynakları: TxFlow `marketTags`, tradeXYZ `perpCategories`, Lighter RH kategori alanı. İzin listesine ancak belgeden doğrulandıktan sonra eklenir.
3. Variational için borsa genelinde token bucket ve `retry_after_seconds` (B4).
4. Eski `FiveFeed`'deki Extended ve Core yollarını V2 kipinde devre dışı bırakmak (B6), karakterizasyon testleriyle.

## Onay gerektirenler

- Risk ayarları (24 saat / 1,0 alternatifleri).
- Ayar değişikliğinde açık pozisyon politikası.
- popdex_perps için resmî kaynak.
- E: veri kurtarma (sağlıklı disk).
- Push / merge.
- Herhangi bir motor ya da pilot başlatma.
