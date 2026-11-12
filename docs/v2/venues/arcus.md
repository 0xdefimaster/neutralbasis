# arcus — Arcus Perps

| Alan | Değer | Durum | Kaynak |
|---|---|---|---|
| REST / WS | `https://api.arcus.xyz`; `wss://api.arcus.xyz/v1/ws` | VD | docs.arcus.xyz/api-reference/introduction.md |
| Keşif | `GET /v1/markets`: 79 ONLINE; `category` (CRYPTO / EQUITY / …), `type` PERPETUAL, `fullAssetName`, `pythId`, RTH alanları | VL (21087a67…) | — |
| Kimlik | 20 kripto doğrulandı; 59 doğrulanamadı (RWA registry'si yok) | Türetilmiş | — |
| Ücret | **ppm**: Base 150/450 (%0,015 / %0,045); RWA özel 0/225; negatif maker = rebate | VD + VL | concepts/perpetuals/fees.md, `/v1/feetiers` |
| Funding | Saatlik; pozitifse long öder; oracle fiyatı; kripto tabanı %0,01 / 8 saat; RWA SOFR + %0,5; ±%4 kelepçe; `/v1/fundingRates` geçmişi | VD | concepts/perpetuals/funding.md |
| Mark / oracle | Mark tek değerleme referansı; oracle sağlayıcısı NOT FOUND (`pythId` alanı var) | VD / U | prices.md |
| RWA seansı | RTH 04:00–20:00 ET; off-hours'ta bant, durdurma yok | VD | real-world-assets.md |
| Rate limit | 1.500 ağırlık/dk; markets / trades / fundingRates 20 ağırlık | VD | rate-limits.md |
| Güvenlik | Olay, istismar ya da duraklatma kaydı bulunamadı (changelog 2026-11-11'e kadar) | VD (yokluk) | changelog.md |
| Açık | BBO ve L2 REST yolları; oracle sağlayıcısı | U | — |
| Paper | U (yürütme modeli doğrulanmadı) | — | — |
