# 06 — Piyasa kimliği ve ekonomik eşdeğerlik

Kod karşılığı: `v2/identity.py`.

## Kurallar

1. **Sembol eşleşmesi kimlik değildir.** İki piyasa ancak şu koşulların hepsi sağlanırsa aynı enstrümandır:
   - doğrulanmış aynı `underlying_id`;
   - aynı `contract_type` (perp ≠ swap ≠ vadeli);
   - iki tarafta da bilinen kontrat çarpanı;
   - çakışma listesinde olmamak.
2. **`underlying_id` yalnız iki yoldan gelir:**
   - **REGISTRY:** `(exchange_id, market_id) → underlying_id`, iki borsanın kendi tanımına atıfla yazılmış küratörlü kayıt. RWA, hisse, endeks, emtia, FX ve pre-IPO için tek yol budur.
   - **CURATED_CRYPTO:** borsanın kendi metadata'sı piyasayı kripto diye sınıflıyorsa (`asset_class == crypto`) ve taban sembol küratörlü kripto listesindeyse.
3. **Çarpan:** `1000PEPE` ve `kPEPE` = PEPE × 1000; `1MBABYDOGE` = × 10⁶; `xyz:NVDA` → NVDA; `-PERP` / `-USD` / `USDT` ekleri atılır. Çarpanı bilinmeyen sayısal önek UNVERIFIED olur. `1INCH` ve `2Z` istisnadır.
4. **Fiyat yakınlığı yalnız tutarlılık kontrolüdür, kanıt değildir** (`price_consistent`).
5. **Çakışma listesi:**

| Sembol | Neden |
|---|---|
| QNT | Variational 178,2, Extended 40,33 |
| ANTHROPIC | Pre-IPO; Variational 1B hisse tahmini; 1.380'e karşı 1.824 |
| OPENAI | Pre-IPO |

## Canlı anlık görüntü (2026-11-12 00:54 UTC smoke)

Araç: `tools/v2_discovery_snapshot.py`.

| Borsa | Aktif perp | Sınıf kaynağı | Doğrulanan | Doğrulanamayan |
|---|---|---|---|---|
| tradexyz | 122 | yok | 0 | 122 |
| txflow | 241 | yok (`marketTags` okunmadı) | 0 | 241 |
| risex | 53 | `category` | 16 | 37 |
| arcus | 79 | `category` / `type` | 20 | 59 |
| lighter_rh | 71 | yok | 0 | 71 |
| variational | 590 | yok (`name`, `-PERP`/`-SWAP`) | 0 | 590 |

| Ölçüm | Değer |
|---|---|
| Katı kesişim: 2+ borsada doğrulanan varlık | **15** (AAVE, ARB, BNB, BTC, DOGE, ENA, ETH, HYPE, NEAR, ONDO, PUMP, SOL, TAO, WLD, XRP) |
| Bunlar kaç borsa çiftinde | **1** (arcus ↔ risex); teorik 28 |
| Yalnız sembol eşleşmesiyle (kararlarda **kullanılmaz**) | 242 varlık, 15 borsa çifti |

## Yorum

Kimlik kapısı bugün çok dar ve bu bilinçli bir tercih. Aradaki fark (15'e karşı 242), borsaların metadata'da varlık sınıfı yayımlamamasından geliyor. Phase 3'te her borsa için resmî bir sınıf kaynağı okunacak:

| Borsa | Okunacak kaynak |
|---|---|
| TxFlow | `marketTags` / `tags` |
| Lighter RH | `orderBookDetails` ayrıntısı / kategori |
| tradeXYZ | `perpCategories` (`dex:` önekli) |
| Variational | `name` ve TradFi sembol kuralı (belge: "symbol matches the TradFi symbol") |

RWA eşleşmeleri ayrıca REGISTRY'ye kanıtla yazılacak.

## Phase 3 güncellemesi (2026-11-12): grup kimliği

Yukarıdaki tek pazarlık kural (`resolve`) geriye dönük uyumluluk için duruyor. Karar artık grup düzeyinde, `identity.verify` ile veriliyor:

1. **Çapa:** sınıf, borsanın kendi belgeli alanından gelir.

   | Borsa | Alan | Değerler |
   |---|---|---|
   | RISEx | `category` | — |
   | Arcus | `category` | — |
   | TxFlow | `marketTags` | TradFi / Crypto sekmesi |
   | tradeXYZ | `perpCategories` | — |
   | Lighter RH | `funding_premium_multiplier` | 100 kripto, 50 RWA, 1 pre-IPO |
   | Variational | Belgedeki TradFi tablosu | 11 sembol |

2. **Fiyat:** birim fiyat, çapaların medyanından kriptoda en fazla %1, RWA'da en fazla %2 sapabilir.
3. **İsim:** isimler çelişirse isimli üyelerin hepsi gruptan çıkar.
4. **Sınıfsız üye:** ancak iki bağımsız çapa ya da pozitif isim eşleşmesiyle katılabilir.
   - Tek kripto çapası + kripto adı + fiyat farkı ≤ %0,5 → **Tier B**. Raporlanır, strateji satırı üretmez.
   - Diğer durumlar → `SINGLE_ANCHOR_UNCONFIRMED`.
5. **RWA alt başlıkları eşlenmez.** Arcus GLD'yi COMMODITIES, RISEx index_etf olarak etiketleyebilir; ayrımı fiyat ve isim yapar.

Sonuç: Tier A 119 varlık (40 kripto, 79 RWA); Tier B 80. Ayrıntı ve eleme nedenleri `18_PHASE3_REPORT.md` içinde.
