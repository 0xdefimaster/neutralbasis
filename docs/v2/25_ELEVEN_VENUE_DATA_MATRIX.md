# 25 — 11 mekân veri matrisi ve `observation_v3`

**PAPER ONLY.** Kodda tek kaynak `v2/observation.py` (`VENUE_MATRIX`); panel de bunu okuyor.

## 1. İki evren (ayrı tutuluyor)

| Evren | Mekânlar | Kullanım |
|---|---|---|
| `PAPER_EXECUTION_UNIVERSE_V2` | lighter_rh, risex, arcus, txflow, variational, popdex_perps, veranta, tradexyz (**8, değişmedi**) | Yalnız bunlar PAPER bacağı olabilir |
| `OBSERVATION_UNIVERSE_V3` | lighter_rh, risex, arcus, txflow, variational, tradexyz, popdex_perps, **entropy_io, hyperliquid_native, phoenix_perps, qfex** (11) | Yalnız kamuya açık okuma: huni ve radar |

Gözlem evreninde olup PAPER evreninde olmayanlar (`OBSERVATION_ONLY`): entropy_io, hyperliquid_native, phoenix_perps, qfex.
- `assert_paper_venue()` bunlar için hata verir.
- `Gate.static`, PAPER satırlarına karışmış bir gözlem satırını `CONFIG_EXCLUDED` ile reddeder.

Veranta yalnız PAPER evreninde; sahibin gözlem listesinde yok.

## 2. Ağ modu: `observation_v3` (NB_V2 değişmedi)

- `pilot/network_guard.install(..., mode="observation_v3")` politikayı `v2.observation.check` yapar.
- `NB_V2=1` iken `v2.network_policy` aynen kullanılır; native, io ve Phoenix istekleri orada hâlâ reddedilir (test: `test_nb_v2_policy_unchanged_refuses_observation_reads`).
- NB_V2 ile `observation_v3` birlikte kurulamaz.
- `pilot/run_pilot.py`, `NB_OBSERVATION_V3=1` görürse başlamayı reddeder. Gözlem modu motor içinde hiç çalışmaz.

Gözlem moduna eklenen okumalar:
- **hyperliquid_native:** `POST api.hyperliquid.xyz/info`, `dex` alanı yok ya da `""`. Tipler: meta, metaAndAssetCtxs, l2Book, fundingHistory, candleSnapshot. Coin önekli olmamalı.
- **entropy_io:** aynı tipler, `dex:"io"` ya da `io:` coin'leri ile.
- **phoenix_perps:** yalnız `GET perp-api.phoenix.trade/v1/exchange/snapshot`.

Her modda reddedilenler:
- hesap/kullanıcı/imza alanları;
- spot (`@N`, `A/B`);
- diğer HIP-3 dex'leri (flx, km, …);
- `predictedFundings` (CEX tahmin oranlarını taşır; CEX verisi sıfır kalır);
- allMids;
- QFEX hostları;
- tüm CEX hostları;
- yazma yolları.

Bütçe: hyperliquid_native ve entropy_io, tradeXYZ ile aynı bütçeyi kullanır (aynı host, tek istemci). Phoenix'in limiti UNKNOWN; küçük bir bütçe verildi (10 istek / 60 s).

## 3. Matris

| Mekân | Rol | Metadata | Defter | Anlık funding | Funding geçmişi (kanıt) | Trades | Ücret | Durum |
|---|---|---|---|---|---|---|---|---|
| lighter_rh | PAPER+gözlem | OK | OK | OK | MARKET_SETTLEMENT_CONFIRMED | rota izinli | bilinen | ACTIVE |
| risex | PAPER+gözlem | OK | OK | OK | MARKET_SETTLEMENT_CONFIRMED | rota izinli | **UNKNOWN** (hesap bazlı) | BLOCKED_FEE_UNKNOWN |
| arcus | PAPER+gözlem | OK | OK | OK | MARKET_SETTLEMENT_CONFIRMED | rota izinli | bilinen | ACTIVE (hisseler: ABD seansı) |
| txflow | PAPER+gözlem | OK | OK (+WS, kapalı) | OK | RATE_PUBLISHED (userFundings: ENGELLİ) | rota izinli | bilinen | ACTIVE (funding kanıtsız) |
| variational | PAPER+gözlem | OK | RFQ kotasyonu | OK | RATE_PUBLISHED (gözlemci: RATE_SAMPLED_AT_CUTOFF) | API yok | 0 (OLP spread) | BLOCKED_EXECUTION_MODEL |
| tradexyz | PAPER+gözlem | OK | OK | OK | MARKET_SETTLEMENT_CONFIRMED | izinli değil | türetilmiş (HIP-3) | ACTIVE |
| popdex_perps | PAPER+gözlem | doğrulanmış API yok | — | — | MISSING | — | UNKNOWN | BLOCKED_NO_API |
| veranta | yalnız PAPER | doğrulanmış API yok | — | — | MISSING | — | UNKNOWN | BLOCKED_NO_API |
| **entropy_io** | yalnız gözlem | OK (22 piyasa) | rota izinli | OK | MARKET_SETTLEMENT_CONFIRMED (rota izinli) | izinli değil | türetilmiş (HIP-3) | OBSERVATION_ACTIVE |
| **hyperliquid_native** | yalnız gözlem | OK (235 piyasa) | OK | OK | MARKET_SETTLEMENT_CONFIRMED (INIT 90 gün okundu) | izinli değil | bilinen (tier 0, %0.045) | OBSERVATION_ACTIVE |
| **phoenix_perps** | yalnız gözlem | OK (exchange snapshot) | rota UNKNOWN | rota UNKNOWN | MISSING (SDK'da var, yol UNKNOWN) | rota UNKNOWN | yayımlı (takerFee 0.00035) | OBSERVATION_METADATA_ONLY |
| **qfex** | yalnız gözlem | rota UNKNOWN | WS (doğrulanmadı) | WS (doğrulanmadı) | MISSING | WS | UNKNOWN | BLOCKED_NO_VERIFIED_ROUTE |

## 4. Kaynaklar (2026-11-12'de okundu)

**entropy_io**
- `perpDexs` cevabı (V2 smoke, 00:54 UTC): `{"name":"io","fullName":"EntropyIO","deployer":"0x320c…cac6"}`.
- 07:11 TR'de `metaAndAssetCtxs dex:"io"`: 22 piyasa (OAI, ANTH, AAPL, NVDA, …). NATGAS ve INIT yok.

**hyperliquid_native**
- Info endpoint belgesi: `dex` isteğe bağlı; `""` ilk perp dex demek; zaman aralıklı cevaplar en fazla 500 satır; HIP-3 coin'leri `dex:COIN`.
- 07:11 TR'de 235 piyasa okundu. INIT funding geçmişi 2160 satır, 5 sayfa.

**phoenix_perps**
- Ellipsis-Labs/rise-public `ts/README.md`: varsayılan `apiUrl https://perp-api.phoenix.trade`, WS `wss://perp-api.phoenix.trade/v1/ws`. Belgede ham yolu verilen tek uç `/v1/exchange/snapshot`.
- Defter, işlemler ve funding geçmişi SDK metodu olarak var (`getOrderbook`, `getMarketFills`, `getFundingRateHistory`) ama ham yolları okunmadı: **UNKNOWN**, reddediliyor. Yöntemin GET olduğu belgede yazmıyor (okuma için varsayıldı) ve 200 döndü.
- Snapshot alanları:
  - 107 sembol (WTIOIL, GOLD, NVDA, …; NATGAS ve INIT yok);
  - `takerFee 0.00035`, `makerFee 0.00005` (fees.md ile aynı);
  - `fundingConfig {fundingIntervalSeconds 3600, fundingPeriodSeconds 28800, maxFundingRatePerInterval 1931}`; oran birimi UNKNOWN.

**qfex**
- QFEX-org/cli `config.go`: `api.qfex.com`, `wss://mds.qfex.com/`.
- `internal/daemon/mds.go`: kimlik doğrulamasız abonelik `{"type":"subscribe","channels":[…],"symbols":[…]}`, kanallar `level2`, `trade`, `bbo`, `mark_price`, `funding`, ….
- REST yolları okunmadı (UNKNOWN). Funding birimi ve işareti UNKNOWN.
- WS istemcisi yazılmadı. **`qfex login` ve `qfex agents init` asla çalıştırılmaz**; ikincisi `~/.claude` ayarlarını değiştiriyor.

**TxFlow `userFundings`**
- Ağ politikasında `user` hesap anahtarı olduğu için reddediliyor.
- Belgedeki dar istisna yetkili bir test adresi gerektiriyor. Adres yok ve cevap biçimi doğrulanmadı; parser yazılmadı.
- Durum: **ENGELLİ (adres yok)**.

## 5. CEX ve kapsam kanıtı

- 07:11 gözlem okuması: 19 izinli, 0 reddedilen istek. Mekân dağılımı: hyperliquid_native 7, tradexyz 7, entropy_io 1, variational 2, txflow 1, phoenix_perps 1. **CEX: 0.**
- Testler (`NetworkModeTests`): CEX hostları iki modda da reddediliyor ve izinli host listelerinde yok.
