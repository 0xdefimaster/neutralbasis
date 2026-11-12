# 01 — Baseline ve güvenlik ön kontrolü (Phase 0)

Tarih: 2026-11-12. Yöntem: salt okunur komutlar.

## Repo

| Kontrol | Sonuç |
|---|---|
| Checkout | `C:\Users\W10\nb-verify` |
| Başlangıç dalı ve commit | `v2/baseline-benchmark` @ 4576d0e (V2-0); çalışma ağacı temiz |
| Worktree | Tek (`C:/Users/W10/nb-verify`) |
| GitHub | `main` = `local/audit-fixes-2026-11-09` = e8a0e7b. V2-0 ve V2 dalları push edilmedi |
| Yedek dal | `backup/pre-email-rewrite-2026-11-12` @ 3bc826f |
| Bu çalışmanın dalı | `v2/venue-policy`; 4576d0e'nin üstünde |

## V2-0 kanıtı

4576d0e yerelde mevcut. İçinde:
- `wide_paper.py`: `LEG_RECOVERY`, `entry_economics`, ayrık PnL, `funding_economics`, `breakeven_refusal`;
- `tools/v2_dataset.py`, `tools/v2_evidence.py`, `tools/v2_preflight.py`;
- `docs/v2/{MASTER_PLAN,PILOT_BASELINE,BENCHMARK,V2_0_REPORT}.md`;
- `test_v2_accounting.py`, `test_v2_evidence.py`.

Bu belgeler silinmedi ve yeniden yazılmadı. Rakamlar, kaynakları ve doğrulama durumlarıyla birlikte `PILOT_BASELINE.md` içinde duruyor.

Test sayısı: V2-0'da 801 (2 atlanan). Bu fazın sonundaki gerçek sayı `13_TEST_MATRIX.md` ve `17_ACCEPTANCE_REPORT.md` içinde.

## Süreçler ve disk

| Kontrol | Sonuç |
|---|---|
| Python süreci | Yok |
| Dinlenen port 8765 / 18765 / 18766 | Yok |
| Zamanlanmış görev (neutral / nb / pilot) | Yok |
| C: boş alan | 12,2 GB |
| E: | Arızalı. Dokunulmadı (bkz. `14_OPERATION_BACKUP_RUNBOOK.md`) |

## Korunan veriler (değiştirilmedi)

| Veri | Konum | Durum |
|---|---|---|
| STRICT pilot DB | `C:\Users\W10\nb-pilot\data\paper.sqlite3` | 5,51 GB; `quick_check` ok (2026-11-12, salt okunur) |
| LOOSE pilot DB | `C:\Users\W10\nb-pilot-loose\data\paper.sqlite3` | 5,67 GB; `quick_check` ok |
| Gözlemci DB | `C:\Users\W10\nb-observer\observed_funding.sqlite3` | 1,3 MB; ok |
| Dondurulmuş V2-0 veri seti | `C:\Users\W10\nb-benchmark\pilot-2026-11-10_12` | sha256 5fb3f25a… |
| E: ana motor DB | `E:\NeutralBasis\data-five-rh\paper.sqlite3` | 26,47 GB + 3,08 GB WAL; **yedeği yok, bütünlüğü UNKNOWN** |
| Kod yedeği | `C:\Users\W10\nb-backup\2026-11-10\*.bundle` | `bundle verify` ok. Bu bir **kod yedeği, DB yedeği değil** |
