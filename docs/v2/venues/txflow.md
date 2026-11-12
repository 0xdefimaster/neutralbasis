# txflow — TxFlow Perps

| Alan | Değer | Durum | Kaynak |
|---|---|---|---|
| API | `POST https://rpc.txflow.com/info` (tüm tipleri okuma); başlık `x-api-version: 2026-08-16`; `/exchange` **yasak** | VD | docs.txflow.com/api/ |
| Keşif | `perpMeta`: 241 aktif; `baseCurrency`, `index`, `priceTick`, `basePrecision`, `maxLeverage` | VL (b9a6f785…) | — |
| Kimlik | Sınıf alanı yok, bu yüzden 0 / 241 doğrulandı (`marketTags` okunacak) | U | B5 |
| Funding | `funding` **yüzde** (0,01 = %0,01); adaptördeki `/100` doğru. Saatlik; pozitifse long öder; mark üzerinden; tavan/taban ±%0,2 | VD | trade/funding, api/info/marketTicker |
| Settled geçmiş | **Herkese açık yok** (yalnız kullanıcıya özel `userFundings`) | NS | api/info |
| Defter | `l2Book` (seq yok); WS `l2BookDepth`: snapshot + delta, `seq` / `prevSeq` | VD | api/websocket/l2BookDepth |
| Ücret | VIP0 %0,0150 / %0,0450 … VIP6 0 / %0,024; referral %95 | VD | trade/fees |
| Zaman | ms | VD | — |
| Rate limit | `/info` 50 / sn; WS 10 bağlantı; 429 + Retry-After | VD | api/rate-limits |
| Gözlemci karşılaştırması | Gözlemci ve adaptör aynı alanı okuyor (bağımsız kanıt değil); mark farkı medyan 2,2 bp | V2-0 | V2_0_REPORT §4 |
| Paper | **BLOCKED** (SETTLEMENT_EVIDENCE_MISSING); `wide_txflow_paper` yalnız sahip kararıyla | — | B8 |
