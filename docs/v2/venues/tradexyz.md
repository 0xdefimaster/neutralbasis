# tradexyz — trade.xyz (Hyperliquid HIP-3 dex "xyz")

| Alan | Değer | Durum | Kaynak |
|---|---|---|---|
| Kimlik | Ayrı `exchange_id`. Veri Hyperliquid info API'sinden, **yalnız xyz kapsamında**. Hyperliquid native V2 dışı | Karar D3 | — |
| Kapsam kuralı | `meta` / `metaAndAssetCtxs` → `dex:"xyz"`; `l2Book` / `fundingHistory` / `candleSnapshot` → `xyz:` önekli coin; `perpDexs` BROAD_METADATA; `predictedFundings` yalnız birinci dex olduğu için yasak | VD + test | hyperliquid.gitbook.io info-endpoint/perpetuals |
| Keşif | `metaAndAssetCtxs dex=xyz`: 144 evren, 122 aktif; `growthMode`, `deployerFeeScale`; ctx: `funding`, `markPx`, `oraclePx`, `impactPxs` | VL (0906e835…) | — |
| Ücret | Taker = %0,045 × `hip3_scale(scale, growth)`. XYZ100: scale 1,0 ve growth → **%0,009**; growth kapalı → %0,09. 144 varlıkta %0,009–0,09 | VL (türetilmiş) | trading/fees ("Fee formula for developers") |
| Funding | Canlı `funding` kesir; deployer çarpanı (hisseler 0,5; ANT 0,005); "8 saatlik" faiz/kelepçe parametreleri; HIP-3 işaret ve formülü bu sayfalarda NOT FOUND | VD (kısmi) / VL | hip-3-deployer-actions |
| OI cap | Notional (dex ve varlık başına) + boyut cap'i 1B; `assetToStreamingOiCap` canlı | VD + VL | aynı |
| CCXT | V2'de kullanılamaz (`spotMeta` isteği reddedildi). Doğrudan adaptör: `v2/venues/tradexyz.py` | Test + B7 | — |
| Eski veri | LOOSE pilot `tradeXYZ` geçmişi (79.218 funding satırı) aynı adla korunuyor; yeniden adlandırma yok | — | PILOT_BASELINE |
| Paper | U (kimlik: sınıf alanı yok → `perpCategories`; funding işareti belirsiz) | — | B5, B11 |
