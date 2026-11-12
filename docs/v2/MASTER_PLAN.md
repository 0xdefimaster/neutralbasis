# NeutralBasis V2 — Ana plan

Durum: taslak, 2026-11-12. Sahibin onayını bekliyor. **PAPER ONLY**: canlı emir, imza,
anahtar ve gerçek sermaye yok. Her faz bir raporla biter ve bir sonraki faz sahibin
onayıyla başlar.

## İlke

Daha çok işlem değil, daha doğru ve kanıtlanabilir işlem değerlendirmesi.

- Risk kapıları gevşetilmez. V2 değişiklikleri kapıları ya aynı bırakır ya da sıkılaştırır.
- Bir rakam kaynağı ve doğrulama durumuyla birlikte yazılır:
  - `VERIFIED_FROM_DB`: veritabanından okundu.
  - `DERIVED`: okunan veriden hesaplandı.
  - `PARSED_FROM_TEXT`: kayıtlı metinden ayrıştırıldı.
  - `UNVERIFIED`: doğrulanmadı.
  - `UNKNOWN`: bilinmiyor.

## Faz sırası

| Faz | İçerik | Durum |
|---|---|---|
| V2-0 | Pilot baseline, tekrar oynatılabilir benchmark, muhasebe ayrımı, `LEG_RECOVERY`, dinamik başa baş, gözlemci karşılaştırması, veri koruma ön kontrolü | **Bu teslim** |
| V2-1 | Benchmark sonuçlarına göre sahibin seçtiği ayarlar (`wide_funding_realization`, `wide_max_breakeven_hours`), işlem süresince kapatma politikası, yeni bir 7+ günlük PAPER pilot | Onay bekliyor |
| V2-2 | Yeni borsaların entegrasyonu ve genişletilmesi | V2-0 benchmark'ı ve her borsa için aşağıdaki kapıdan sonra |
| V2-3 | Sürekli funding modeli (Paradex), Nado ve Hibachi kanıt boşlukları | Belge ve veri kanıtıyla |

## V2-0 görevleri (sahibin 2026-11-12 listesi)

| # | Görev | Nerede |
|---|---|---|
| 1 | Pilot sonuçları, tarih, kaynak ve doğrulama durumuyla | `docs/v2/PILOT_BASELINE.md`, `tools/v2_evidence.py baseline` |
| 2 | STRICT ve LOOSE doğrudan karşılaştırılamaz | `PILOT_BASELINE.md` §2 |
| 3 | Dinamik başa baş, beklenen net nakit akışı, çıkış maliyeti, risk payı | `wide_paper.funding_economics`, `breakeven_refusal`, ayarlar `wide_max_breakeven_hours` ve `wide_funding_realization` |
| 4 | Tek bacaklı işlem başarılı arbitraj sayılmaz: `LEG_RECOVERY` | `wide_paper.trade_outcome`, `outcome_of`, `is_win` |
| 5 | Funding PnL, fiyat PnL, ücret, tahmini ödeme ve simüle settlement ayrımı | `WidePaper.report()`, işlem kaydında `entry_economics` |
| 6 | TxFlow ve Variational gözlemci verisi karşılaştırması; doğrulanmadan uygunluk kararlarında kullanılmaz | `tools/v2_evidence.py observer`, `V2_0_REPORT.md` §4 |
| 7 | Eski pilot DB'leri, E: motor verisi ve Git korunur; migrasyon öncesi ön kontrol | `tools/v2_preflight.py`, `V2_0_REPORT.md` §5 |
| 8 | Yeni borsalardan önce tekrar oynatılabilir benchmark | `tools/v2_dataset.py`, `tools/v2_evidence.py benchmark`, `docs/v2/BENCHMARK.md` |
| 9 | Risk kapıları gevşetilmez | Yeni kapı yalnız sıkılaştırır; `wide_funding_realization` ≤ 1 |
| 10 | Üç soru ayrı ayrı yanıtlanır | `docs/v2/V2_0_REPORT.md` |

## Yeni borsa kapısı (V2-2 öncesi, her borsa için)

"Yeni yedi borsa" listesi **UNKNOWN**; sahip teyit etmeli. Kodda 2026-11-11'de altı borsa
eklendi: Aster, Pacifica, Nado, Hibachi, ApeX ve tradeXYZ. Variational gözlemci
tarafında. Hangi yedi borsanın kastedildiğini sahip belirtmeli.

Bir borsa işlem kararlarına ancak şu koşullar sağlandığında girer:

1. Funding modeli belgeden okunmuş olmalı (`capabilities.FUNDING_MODELS`). Kaynağı ve tarihi yazılı olmalı.
2. Saklanan geçmiş ödeme olmalı, oran örneği değil (Paradex örneği: 5 saniyelik örnekler).
3. Benchmark replay'inde (`tools/v2_evidence.py benchmark`) o borsanın çiftleri ayrı ölçülmeli: gerçekleşme oranı ve maliyeti karşılama payı.
4. Varlık kimliği diğer borsalarla fiyatla doğrulanmalı. Variational'da QNT ve ANTHROPIC uyuşmuyor.
5. Ücret belgeden alınmalı. "UNKNOWN" ücretle işlem açılmaz.

## Değişmeyen kurallar

- PAPER ONLY. Canlıya geçiş ayrı bir hazırlık incelemesi ve sahibin yazılı onayı gerektirir.
- Push, merge ve production restart için sahibin onayı gerekir.
- E: diski arızalı (System log event 7, bozuk blok):
  - 2026-11-10'da 13.842 olay; 2026-11-11'de 2.134.
  - Üzerinde çalıştırma kararı sahibindir. Riskler `V2_0_REPORT.md` §5'te.
