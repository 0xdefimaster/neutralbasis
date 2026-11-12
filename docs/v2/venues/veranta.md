# veranta — Veranta (eski Avantis), Base

| Alan | Değer | Durum | Kaynak |
|---|---|---|---|
| SDK | `veranta-sdk` 2.2.1 (2026-11-05); import `veranta_sdk`; `AsyncVeranta` / `Veranta`; v2 kırıcı; v1 `TraderClient` kaldırıldı | VD | pypi.org/project/veranta-sdk; GitHub CHANGELOG |
| Zincir | Base 8453 | VD | sdk.veranta.xyz/configuration.md |
| Host'lar | Gateway `https://prod-api.veranta.xyz`; tx-builder; `data.veranta.xyz`; fiyat: feed-v3 `/v2/pairs/{index}/price-update-data` (Lazer / Hermes) | VD | configuration.md, CHANGELOG |
| Read-only | `client.markets` anahtarsız; pair kataloğu `GET /v2/trading`; `GET /v2/open-interests`; spread `POST {risk-v2}/spread` (kota, işlem değil) | VD | data/markets.md, CHANGELOG |
| Maliyet | `open_fee_p`, `close_fee_p`, `spread_p` (%); skew-adjusted open fee; `rollover_fee`; `funding_fee_per_hour_p`; profit share (Upside); loss protection. **Değerler okunmadı** | VD (alanlar) / U (değerler) | data/markets.md, data/compute.md |
| Seans | `is_market_open` (forex / emtia); takvim NOT FOUND | U | — |
| Rate limit | Sayı NOT FOUND; 429 `RateLimitedError` | U | advanced/errors.md |
| Durum | **RESEARCH_ONLY**: ağ izni yok. SDK kurulmadı; ticaret örnekleri çalıştırılmadı | — | B2 |
| Sonraki adım | `/v2/trading` yanıt sözleşmesini belgeden doğrula; izole ve anahtarsız bir smoke; vault / dinamik kota maliyet modeli | — | — |
