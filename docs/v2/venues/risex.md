# risex — RISEx

| Alan | Değer | Durum | Kaynak |
|---|---|---|---|
| REST host | `api.rise.trade`: canlı 200. **Belgedeki OpenAPI sunucusu testnet; mainnet host'u belgede NOT FOUND** | VL / belge U | developer.rise.trade (marketservice_getmarkets.md) |
| Keşif | `GET /v1/markets`: 53 aktif; `category`, `underlying`, `funding_interval` (ns), `current_funding_rate` (18 ondalık), `mark_price`, `index_price` | VL (946f559c…) | — |
| Kimlik | 16 kripto doğrulandı (küratörlü liste + `category`); 37 doğrulanamadı | Türetilmiş | `06_MARKET_IDENTITY.md` |
| Defter | REST `/v1/orderbook` (limit ≤ 250); WS snapshot + delta, CRC32 checksum, **seq yok** (block_number / log_index); uyumsuzlukta yeniden abone ol | VD | orderbook-channel.md |
| Funding | Oran kesir; settlement'ta `index_price`; geçmiş = SettleFundingRate olayları. Belge hem sabit aralık hem "işlem dokununca settle" diyor (çelişki) | VD (kısmi) | getfundingratehistory.md, funding-channel.md |
| Zaman | Varsayılan **ns**; oracle WS `timestamp` s | VD | general-information |
| Ücret | **UNKNOWN** (`GET /v1/user/fees` JWT ister) | U | feetierservice_getuserfees.md |
| Rate limit | REST 500 / 10 sn; WS 10 / sn | VD | general-information |
| Uyarı | "WORK IN PROGRESS … big changes are expected" | VD | aynı |
| Paper | **BLOCKED (FEE_UNKNOWN)** | — | B3 |
