# 09 — Funding modeli

## Oran kaydı alanları

Her funding oranı şu alanlarla kaydedilir:

`raw_rate`, `unit` (fraction / percent / bp / ppm / decimal18), `interval_seconds`, `applies_at`, `published_at`, `observed_at`, `kind` (projected / observed_published / settled_confirmed), `payer_side`, `mark_basis`, `settlement_currency`, `source_url`.

Birim dönüşümleri açık birimle yapılır (`v2/fees.to_fraction`, `to_seconds`); tahmin yapılmaz. Saatlik, 8 saatlik ve sürekli tahakkuk birbirine körlemesine çevrilmez.

## Borsa bazında (belgeden, 2026-11-12)

| exchange_id | Birim | Aralık | Pozitif oranda | Fiyat tabanı | Settled geçmiş | Durum |
|---|---|---|---|---|---|---|
| lighter_rh | Belgede yüzde gösterim; API alanının birimi NOT FOUND | Her saat başı (Core belgesi; RH "yalnız farkları listeler") | Long öder | Index | `/api/v1/fundings` (rate, value, direction); ödeme mi tahmin mi NOT FOUND | Kısmi |
| risex | 18 ondalıklı kesir (`current_funding_rate`); `funding_rate_8h` = ×8 | `funding_interval` ns (canlı 3600 sn) **ve** "işlem dokununca settle" | Long öder (WS örneği) | Settlement'ta `index_price` | `funding-rate-history` = SettleFundingRate olayları | Kısmi; belge kendi içinde çelişkili |
| arcus | Saatlik kesir (belge formülü + canlı 0,0000125; birim etiketi çıkarım) | 1 saat | Long öder | Oracle fiyatı | `/v1/fundingRates` | İyi |
| txflow | **Yüzde** (0,01 = %0,01) | 1 saat (`settlementIntervalMs`) | Long öder | Mark | **Yok** (yalnız kullanıcıya özel `userFundings`) | Yayımlanan oran var; settled kanıtı yok |
| variational | Kesir (`interval_rate`) | 1–8 saat, market başına (Bybit > Binance > 1 saat kuralı); pre-IPO sabit %0,005/8 saat | Long öder | Index (mark değil) | **API'de yok** | Yayımlanan oran var; settled kanıtı yok |
| popdex_perps | U | U (arb.sh: 1 saat) | U | U | U | BLOCKED |
| veranta | `funding_fee_per_hour_p` (saatlik %) | Saatlik | NOT FOUND | U | U | RESEARCH_ONLY |
| tradexyz | Kesir (canlı `funding` 0,00000625) | Belge parametreleri "8 saatlik"; ödeme saatlik (eski pilot geçmişi saatlik) | Bu sayfalarda NOT FOUND | Deployer oracle'ı | `fundingHistory` `xyz:` ile | Kısmi; deployer çarpanı (hisseler 0,5, ANT 0,005) canlıdan okunur |

## Ekonomi (V2-0'dan korunan)

```
expected_net = conservative_funding_cashflow + conservative_basis_pnl
             - two_leg_entry_cost - two_leg_exit_cost - borrowing_and_carry
             - liquidity_impact - risk_reserve
```

- `wide_paper.funding_economics` başa baş saatini hesaplıyor.
- `breakeven_refusal` 24 saatlik kapıyı uyguluyor. **Değiştirilmedi.**
- `wide_funding_realization` 1,0 kaldı (indirim yok). Benchmark'a göre en iyi fırsatlarda gerçekleşme ~0,57–0,62 olduğundan bu değer iyimser bir varsayım.
- Önerilen alternatifler (yalnız sahibin onayıyla):
  1. 24 saat + 0,6: en sıkı seçenek, işlem çok az.
  2. 72 saat + 0,6 + başa başa kadar tutma politikası.

V2-0 benchmark'ı: medyan gidiş-dönüş maliyeti 21,5 bp; en iyi fırsatların medyan başa baş süresi 33–58 saat (`BENCHMARK.md`).

## Kalibrasyon planı (Phase 4)

- Out-of-sample: yürüyen pencere.
- Gerçekleşen ile tahminin oranı, seçim yanlılığı (her anın en iyisi ile tümü), korelasyon, kapsam, en kötü durum ve belirsizlik bantları.
- Kaynak: `v2_evidence.replay`, kanıtlı periyodik borsalarla sınırlı.
- TxFlow ve Variational, settled kanıtı olmadan funding uygunluğuna giremez. TxFlow istisnası yalnız `wide_txflow_paper` sahip kararıyla.
