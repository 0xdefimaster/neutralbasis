# 04 — API ve kaynak kaydı

Erişim tarihi 2026-11-12. Belgeler web-fetch ile okundu.

Canlı yanıtlar `C:\Users\W10\nb-benchmark\v2-smoke-2026-11-12\` klasöründe, gzip ve sha256 ile `manifest.json` altında. Smoke 2026-11-12 00:54 UTC'de V2 koruması altında çalıştı.

## Canlı yanıtlar (VERIFIED_LIVE kapsamı: yanıt şekli ve alanlar)

| exchange_id | İstek | HTTP | Bayt | sha256 (ilk 16) |
|---|---|---|---|---|
| tradexyz | `POST api.hyperliquid.xyz/info {"type":"metaAndAssetCtxs","dex":"xyz"}` | 200 | 64.440 | 0906e8354eb85aba |
| tradexyz | `POST /info {"type":"perpDexs"}` (BROAD_METADATA, denetim kaydında) | 200 | 22.324 | 56d27cb087a991f9 |
| txflow | `POST rpc.txflow.com/info {"type":"perpMeta","exchangeId":"all"}` + `x-api-version: 2026-08-16` | 200 | 103.009 | b9a6f78509d551c7 |
| risex | `GET api.rise.trade/v1/markets` | 200 | 62.634 | 946f559c53f773c3 |
| arcus | `GET api.arcus.xyz/v1/markets` | 200 | 172.601 | 21087a676d909dc9 |
| lighter_rh | `GET api.rh.lighter.xyz/api/v1/orderBookDetails` | 200 | 135.457 | d6b2063d849dfee2 |
| variational | `GET omni-client-api…/metadata/v2/stats` | 200 | 403.627 | 4fadac55c49d7ca8 |

Aynı süreçte **reddedildiği doğrulanan** istekler:

| İstek | Nerede reddedildi |
|---|---|
| `fapi.binance.com` | DNS |
| Hyperliquid `metaAndAssetCtxs` (dex yok) | Gönderimden önce |
| `api.starknet.extended.exchange` | DNS |

## Belge kaynakları

| Borsa | Sayfalar | Önemli bulgular | Okunamayanlar |
|---|---|---|---|
| lighter_rh | docs.robinhood.com/chain/lighter-domains/; docs.lighter.xyz/developers/robinhood-chain/overview.md; …/reference/rate-limits.md; …/trading/funding.md; …/api/funding/fundings.md; …/trading/trading-fees.md | Ayrı borsa; REST `https://api.rh.lighter.xyz/api/v1/`, WS `wss://api.rh.lighter.xyz/stream`; zincir id 466324; ücret kademeleri; Standard 60 istek/dk; `/fundings` GET, yanıtta zaman damgası **saniye**, istekte ms ya da s | apidocs.rh.lighter.xyz → docs.lighter.xyz/api (301). orderbook, recentTrades, candles ve funding-rates sayfaları okunmadı |
| risex | developer.rise.trade/llms.txt; …/general-information; …/marketservice_getmarkets.md; …/getorderbooklevels.md; …/gettradehistoryv2.md; …/getfundingratehistory.md; …/orderbook-channel.md; …/funding-channel.md; …/ws-connection.md | 500 istek/10 sn, WS 10/sn; zaman damgaları **ns**; oran 18 ondalık; WS defteri checksum'lı, seq yok; "WORK IN PROGRESS" | **Mainnet REST host'u** (belgede testnet); ücret tablosu (JWT) |
| arcus | docs.arcus.xyz/llms.txt; …/concepts/perpetuals/{fees,funding,prices,real-world-assets}.md; …/api-reference/{introduction,rate-limits}.md; changelog.md; canlı `/v1/feetiers`, `/v1/markets` | ppm ücret; saatlik funding; RWA SOFR+%0,5; mark formülü; RTH 04:00–20:00 ET; 1.500 ağırlık/dk | BBO ve L2 REST yolları; oracle sağlayıcısı; güvenlik olayı (bulunamadı) |
| txflow | docs.txflow.com/api/{,info,websocket,rate-limits}; …/api/info/{perpMeta,activeAssetCtx,l2Book,trades,marketTicker,feeTierInfo,feeTierRates}; …/api/websocket/{l2Book,l2BookDepth,activeAssetCtx}; …/trade/{fees,funding,contract-specifications} | `funding` yüzde; saatlik; tavan/taban ±%0,2; `/info` 50 istek/sn; zaman damgaları ms; herkese açık funding geçmişi yok | perpMetaDetail, mark-and-index-price, position-and-order-limits |
| variational | docs.variational.io (API, fees, funding-rates, trading-via-rfq, quoted-index-and-mark-prices, tradfi-perpetuals, pre-ipo-perpetuals, risk-limits); help makaleleri 14766804, 14675843, 14766737 | Read-only API canlı, trading API yok; 10 istek / 10 sn / IP, global 1.000/dk; 429 + `retry_after_seconds`; oran kesir; aralık 1–8 saat (Bybit > Binance > 1 saat kuralı); kota en fazla 600 sn cache'li | swaps, liquidation, OLP, trade-history, slippage sayfaları |
| popdex_perps | popdex.org; app.popdex.xyz; popdex.xyz/docs (boş); arb.sh/exchanges/popdex (üçüncü taraf) | Resmî API, zincir ve ücret **NOT FOUND**; arb.sh "PopDEX L1, 66 piyasa, 1 saatlik funding" diyor (doğrulanmadı) | defillama (403); popdex.io/docs başka bir proje |
| veranta | sdk.veranta.xyz (llms.txt, concepts, configuration, data/markets, data/prices-and-streams, data/compute, advanced/errors); pypi veranta-sdk; GitHub README + CHANGELOG | Base 8453; `veranta-sdk` 2.2.1 (2026-11-05); v2 kırıcı; gateway `prod-api.veranta.xyz`; `GET /v2/trading`; spread `POST {risk-v2}/spread`; `markets` anahtarsız | Ücret değerleri; rate-limit sayıları; seans takvimi; Socket.IO sayfası |
| tradexyz | hyperliquid.gitbook.io: info-endpoint, info-endpoint/perpetuals, HIP-3, fees, hip-3-deployer-actions | `dex` alanı yalnız meta / metaAndAssetCtxs / clearinghouseState / openOrders / allMids…; `predictedFundings` yalnız birinci dex; coin "xyz:SYM"; ücret ölçeği formülü; growth ≥%90 indirim; OI cap'leri | Rate-limit ağırlıkları (ayrı sayfa); HIP-3 funding işareti ve formülü; `recentTrades` |

## Çelişkiler

- **Hyperliquid deployer ücret payı:** HIP-3 sayfası "%0–300" diyor; fees sayfası "en fazla %50". İkisi de alıntılandı. Etkin ücret canlı `deployerFeeScale`'den hesaplanıyor.
- **RISEx funding:** belgede hem sabit aralık (`funding_interval`) hem "her işlemde settle" anlatımı var. Belge bunları uzlaştırmıyor.
- **PopDEX zinciri:** yalnız arb.sh "PopDEX L1" diyor; Morph iddiası hiçbir okunan sayfada geçmiyor.
