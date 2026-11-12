# 08 — Ağ izin listesi ve CEX/legacy kapatma

Kod karşılığı: `v2/network_policy.py` (saf karar fonksiyonları) ve `pilot/network_guard.py` (V2 kipi, süreç düzeyinde audit hook).

- **Fail-closed:** listede olmayan host DNS aşamasında, kapsam dışı istek gönderimden önce reddedilir.
- **POST gövdesi denetlenir:**
  - Gövde, başlıkla aynı parçada ya da sonraki parçada (urllib3) gelebilir; ikisinde de denetlenir.
  - Aynı bağlantıda, gövdesi denetlenmemiş bir istekten sonra gelen yeni istek reddedilir.
  - JSON olmayan gövde (ör. chunked) reddedilir.
- **Hesap alanı içeren gövde reddedilir:** `user`, `signature`, `action`, `nonce`, `vaultAddress`, `agent`, `wallet`, `address`, `privateKey`, `apiKey`.

| exchange_id | Host | Metot | İzinli |
|---|---|---|---|
| lighter_rh | api.rh.lighter.xyz | GET | `/api/v1/{orderBooks, orderBookDetails, orderBookOrders, recentTrades, fundings, funding-rates, candles, candlesticks, exchangeStats}` |
| risex | api.rise.trade | GET | `/v1/markets`, `/v1/orderbook`, `/v1/markets/id/{n}/{trade-history, funding-rate-history}` |
| arcus | api.arcus.xyz | GET | `/v1/{markets, feetiers, fundingRates, prices, mids, candles, trades}`, `/v1/markets/feeSchedules`, `/health`. Hesap uç noktası `/v1/funding` **reddedilir** |
| txflow | rpc.txflow.com | POST `/info` | type ∈ {perpMeta, perpMetaDetail, activeAssetCtx, marketTicker, l2Book, trades, candleSnapshot, feeTierInfo, feeTierRates (`user` olmadan)} |
| variational | omni-client-api.prod.ap-northeast-1.variational.io | GET | `/metadata/v2/stats`, `/metadata/stats` |
| tradexyz | api.hyperliquid.xyz | POST `/info` | `meta` / `metaAndAssetCtxs` yalnız `dex:"xyz"`; `l2Book` / `fundingHistory` yalnız `coin:"xyz:…"`; `candleSnapshot` yalnız `req.coin:"xyz:…"`; `perpDexs` → BROAD_METADATA (denetim kaydına yazılır, yalnız xyz girdisi tüketilir) |
| popdex_perps | — | — | Hiçbiri (resmî API bulunamadı) |
| veranta | — | — | Hiçbiri (SDK / API sözleşmesi doğrulanmadı) |

**Hyperliquid'de reddedilenler:**
- `dex` eksik ya da `""` (native);
- başka bir dex;
- önekli olmayan coin;
- `predictedFundings` (belgeye göre yalnız birinci dex);
- `allMids`, `spotMeta`;
- tüm kullanıcı ve hesap sorguları;
- `/exchange`.

## CEX ve legacy kod yolu haritası

| Yol | Ne çağırıyor | V2'de |
|---|---|---|
| `wide_markets.CATALOG` → `getattr(ccxt, adapter)` | 14 CEX + CCXT DEX'leri (Hyperliquid native, LighterCore, Aster, Nado, Hibachi, ApeX, dYdX, GRVT, Derive, Backpack) | `v2.policy.restrict_catalog` hepsini kapatır (26 borsa). Taramada kalan: Arcus, Lighter, RISEx, TxFlow, tradeXYZ |
| `risk_scan.FiveFeed` | Extended, LighterCore (Core), Lighter RH, RISEx, Arcus, TxFlow | Extended ve Core istekleri koruma tarafından reddedilir. Kod yolunu kaldırmak Phase 3 işi (B6) |
| `extended_rfq.py` | Extended RFQ WS | Host listede değil, reddedilir. `NB_RFQ_STREAMS` V2'de başlangıçta reddedilir |
| `connectors.py` / `variational_public.py` | Variational | İzinli |
| `history_store.py` | Borsa funding/mum geçmişi (adaptörler üzerinden) | Aynı korumanın altında |
| `tools/funding_observer.py` | TxFlow + Variational | İzinli. 429 riski için `16_BLOCKERS.md` B4 |
| `pilot/opportunity_audit.py` | `ccxt.hyperliquid` | V2'de çalıştırılmamalı; çalıştırılırsa koruma native sorguları reddeder |
| Zamanlayıcı / Docker / systemd / autostart | Yok (2026-11-12 kontrolü: görev, Run anahtarı, servis yok) | — |

**Kanıt:**
- `test_v2_policy.EgressTests`, kurulu CCXT 4.5.90'da 15 CEX'in bütün API host'larının listede olmadığını doğruluyor.
- Canlı smoke'ta Binance, Extended ve Hyperliquid native gerçekten reddedildi (`04_API_SOURCE_REGISTER.md`).

## Hız sınırları (belgeden; dinamik kaynak config'i Phase 3'te)

| Borsa | Limit |
|---|---|
| lighter_rh | Standard 60 istek/dk (recentTrades ağırlığı 600). **Ağır akış için Premium ya da WS gerekir** |
| risex | REST 500 / 10 sn, WS 10 / sn |
| arcus | 1.500 ağırlık/dk; markets / trades / fundingRates 20 ağırlık |
| txflow | `/info` 50 / sn; WS 10 bağlantı |
| variational | 10 istek / 10 sn / IP; global 1.000 / dk; 429 + `retry_after_seconds` |
| tradexyz | Hyperliquid ağırlık tablosu okunmadı (U) |
