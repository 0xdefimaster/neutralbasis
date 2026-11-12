# 00 — Karar kaydı (V2)

| # | Tarih | Karar | Kaynak | Durum |
|---|---|---|---|---|
| D1 | 2026-11-12 | V2 evreni kesin 8 borsa: `lighter_rh`, `risex`, `arcus`, `txflow`, `variational`, `popdex_perps`, `veranta`, `tradexyz` | Sahibin V2 master prompt'u | Uygulandı (`v2/policy.py`) |
| D2 | 2026-11-12 | Hyperliquid native, Extended, LighterCore, Paradex, Aster, Pacifica, Nado, Hibachi, ApeX, dYdX, GRVT, Derive ve tüm CEX'ler V2'de veri toplamaz. Kodları ve geçmişleri yalnız dondurulmuş replay için kalır | Aynı | Uygulandı: ağ politikası ve `restrict_catalog` |
| D3 | 2026-11-12 | tradeXYZ ayrı bir `exchange_id`; Hyperliquid info API'sini yalnız `dex="xyz"` / `coin="xyz:…"` kapsamında kullanır | Aynı + Hyperliquid belgeleri | Uygulandı (`v2/network_policy.py`) |
| D4 | 2026-11-12 | V2 isteğe bağlı: `NB_V2=1`. Varsayılan davranış (eski pilot) değişmez. `NB_V2` 0/1 dışında bir değer alırsa süreç başlamaz | Mühendislik kararı; risk az ve geri alınabilir | Uygulandı |
| D5 | 2026-11-12 | Kimlik fail-closed: sembol eşleşmesi kimlik değildir. Pozitif kanıt olmadan `IDENTITY_UNVERIFIED` | Master prompt §3 | Uygulandı (`v2/identity.py`) |
| D6 | 2026-11-12 | Bilinmeyen ücret `None` / `UNKNOWN` olarak tutulur, asla 0 sayılmaz | Master prompt §4 | Uygulandı (`v2/fees.py`) |
| D7 | 2026-11-12 | Risk ayarları değişmedi: `wide_max_breakeven_hours=24`, `wide_funding_realization=1.0`. 24 saatlik kapı işlem sayısını ciddi azaltır; 1,0 hiçbir tahmin indirimi yapmaz. Alternatifler (48–72 saat, 0,6) yalnız sahibin onayıyla | V2-0 benchmark | Değişmedi |
| D8 | 2026-11-12 | E: diskine (DB, WAL, repo) dokunulmaz; bağlanma, `quick_check` ve kopyalama yok. Yalnız daha önce alınan `stat` bilgisi kullanılır | Master prompt §5 | Uyuldu |
| D9 | 2026-11-12 | İş `v2/venue-policy` yerel dalında (V2-0 4576d0e'nin üstünde). Push, merge ve deploy yok | CLAUDE.md | Uyuldu |
| D10 | 2026-11-12 | Canlı public smoke yalnız metadata okur (7 istek), V2 koruması altında çalışır ve sonuçları C:'ye yazar | Master prompt §11 ("açık opt-in") | Yapıldı; `C:\Users\W10\nb-benchmark\v2-smoke-2026-11-12` |

## Çürütülen veya düzeltilen eski varsayımlar

- **Arcus ücretinin bp olduğu:** yanlış. Alanlar ppm cinsinden; 150/450 = %0,015 / %0,045 (Arcus fees belgesi ve canlı `/v1/feetiers`). Eski kodda `taker_fee_ppm / 1e6` zaten doğruydu.
- **Variational'ın 429 hatalarının "pilotla çakışma" olduğu (V2-0 hipotezi):** belgeye göre limit IP başına 10 istek / 10 sn. Gözlemci ve motor aynı IP'den bu sınırı aşıyordu. Hipotez artık belgeye dayanıyor.
- **"Lighter RH = LighterCore":** yanlış. RH ayrı bir borsa: hesap, API anahtarı ve market id'leri ayrı (Lighter RH genel bakış belgesi). Eski kodda RH = "Lighter", Core = "LighterCore".
- **RISEx REST host'u:** belgelerdeki OpenAPI sunucusu testnet. Mainnet REST host'u belgede NOT FOUND. `api.rise.trade` eski koddan geliyor ve canlı smoke'ta 200 döndü (VERIFIED_LIVE, belgede yok).
- **tradeXYZ ücreti:** canlı `deployerFeeScale=1.0` ve growth mode ile tier 0 taker ücreti %0,009; growth mode kapalıyken %0,09. Belgeli formülle uyumlu.
