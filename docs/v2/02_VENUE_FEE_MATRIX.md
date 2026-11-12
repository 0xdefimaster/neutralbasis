# 02 — Ücret matrisi (8 borsa)

Kod karşılığı `v2/fees.py` (`FEES`).

- **UNKNOWN hiçbir zaman 0 sayılmaz.** `fees.taker()` bu durumda `FeeUnknown` fırlatır.
- **Maker dolumu varsayılmaz.** Risk ve ekonomi hesabı doğrulanmış taker (ya da kota) maliyetiyle yapılır.
- **Dört taraf ayrı hesaplanır:** iki bacağın açılışı ve kapanışı (`fees.four_leg_fees`). Spread, kayma ve likidite ayrıca düşülür.

Tüm kayıtlar `retrieved_at = 2026-11-12`.

| exchange_id | Kademe / kapsam | Maker | Taker | Ham birim | Durum | Kaynak | Not |
|---|---|---|---|---|---|---|---|
| lighter_rh | Standard | 0 | 0 | yüzde | VERIFIED_DOC | docs.lighter.xyz/developers/robinhood-chain/overview.md | RH Standard okuma limiti **60 istek/dk** |
| lighter_rh | Plus | %0,015 | %0,015 | yüzde | VERIFIED_DOC | aynı | Core Plus'tan farklı (0,5 bp) |
| lighter_rh | Premium < $1M | %0,0120 | %0,0350 | yüzde | VERIFIED_DOC | aynı | > $500M'de %0,0084 / %0,0245 |
| lighter_rh | market bazında | `maker_fee` / `taker_fee` | | yüzde? | SUPPORTED_UNVERIFIED | canlı `orderBookDetails` ("0.0000") | Alanın birimi belgede NOT FOUND |
| risex | — | UNKNOWN | UNKNOWN | — | UNKNOWN | developer.rise.trade (`GET /v1/user/fees`, JWT) | Herkese açık tablo yok. Eski pilotun 0,0003 değeri doğrulanmamış bir sahip ayarı; kullanılmaz |
| arcus | Base (kripto) | 150 ppm = %0,015 | 450 ppm = %0,045 | ppm | VERIFIED_DOC + LIVE | docs.arcus.xyz/concepts/perpetuals/fees.md; `/v1/feetiers` | Negatif maker = rebate; 30 günlük hacim kademeleri |
| arcus | RWA özel takvim | 0 | 225 ppm = %0,0225 | ppm | VERIFIED_LIVE | `api.arcus.xyz/v1/markets` (`hasCustomFeeSchedule`) | AMD, SPY, GLD, … |
| txflow | VIP 0 | %0,0150 | %0,0450 | yüzde | VERIFIED_DOC | docs.txflow.com/trade/fees | Canlı: `feeTierInfo` / `feeTierRates`; referral %95 |
| variational | tümü | 0 | 0 | yüzde | VERIFIED_DOC | docs.variational.io/omni/trading/fees | **Maliyet OLP kota spread'inde** (`quotes.size_1k` vb.). Tasfiye kota ±%0,5; yatırma/çekme $0,1 |
| tradexyz | tier 0, growth açık, scale 1,0 | %0,003 | %0,009 | kesir | VERIFIED_LIVE (türetilmiş) | Hyperliquid fees belgesi + canlı `metaAndAssetCtxs dex=xyz` | Taker = 0,045% × `hip3_scale(scale, growth)` |
| tradexyz | tier 0, growth kapalı, scale 1,0 | %0,03 | %0,09 | kesir | VERIFIED_LIVE (türetilmiş) | aynı | 144 xyz varlığında taker aralığı %0,009–0,09 |
| popdex_perps | — | UNKNOWN | UNKNOWN | — | UNKNOWN | popdex.xyz/docs (boş) | arb.sh %0,010 / %0,032 üçüncü taraf: **kullanılmaz** |
| veranta | — | UNKNOWN | UNKNOWN | yüzde | UNKNOWN | sdk.veranta.xyz/data/markets.md | `open_fee_p`, `close_fee_p`, `spread_p`, skew-adjusted fee, rollover, profit share alanları var; değerler okunmadı |

## Ücretten ayrı maliyetler (modele ayrı kalem olarak girer)

| Kalem | Durum |
|---|---|
| Spread ve kota markup'ı (Variational OLP, Veranta) | Kota alanlarından okunur; okunamazsa UNKNOWN |
| Kayma | Defter VWAP'ı ya da kota boyut kovası |
| Funding | `09_FUNDING_MODEL.md` |
| Borç / rollover (Veranta) | UNKNOWN |
| Tasfiye ücreti | Variational ±%0,5; Arcus `liquidationFeePpm`; diğerleri UNKNOWN |
| Gas, yatırma/çekme | Yalnız sermaye analizinde; işlem başına maliyete girmez |
