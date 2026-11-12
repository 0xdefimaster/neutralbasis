# 23 — Phase A: Salt okunur depo denetimi (NATGAS/INIT master prompt)

Tarih: 2026-11-12 (TR). **PAPER ONLY.** Bu fazda kod değişmedi. Motor, pilot ve kaydedici başlatılmadı. E: diskine dokunulmadı. Push, merge ve deploy yapılmadı.

## 1. Git durumu (doğrulandı)

| Alan | Belgedeki | Gerçek |
|---|---|---|
| Çalışma deposu | — | `C:\Users\W10\nb-verify` (tek worktree) |
| Dal | `v2/venue-policy` | `v2/venue-policy` |
| HEAD | `2cafe02` | **`b3e3bc4`** — "V2 phase 7: paper reliability fixes found by characterization tests" |
| Çalışma ağacı | — | temiz |
| Uzak (GitHub main) | — | `e8a0e7b`; V2 commit'leri yalnız yerel |

Commit zinciri: `e8a0e7b` → `4576d0e` (V2-0) → `ca071f2` (politika) → `fd84ae9` (Phase 3) → `36cf213` (Phase 4) → `2cafe02` (Phase 5–6) → `b3e3bc4` (Phase 7).

Belgedeki `2cafe02` bir faz eski. Bu çalışmanın tabanı `b3e3bc4`.

## 2. Testler

- Belgedeki sayı yaklaşık 886; bu `2cafe02` dönemine ait.
- `b3e3bc4` üzerinde tam takım: **908 test, OK (2 atlandı)**, 278 s.
- Komut: `python -m unittest discover -p "test_*.py"` (venv `C:\Users\W10\nb-verify-venv`, Python 3.13).

## 3. Ortam

- Çalışan Python süreci yok; motor ya da pilot çalışmıyor.
- C: 223 GB, boş alan **13 GB**. `ops.launch_check` en az 50 GB istiyor.
- E:/D: arızalı TOSHIBA HDD. Bu fazda hiç okunmadı.

## 4. Sayılar fikstürden yeniden üretildi

Kaynak: 05:12 TR kaydı `C:\Users\W10\nb-benchmark\v2-phase4-2026-11-12\answers.json.gz`. 746 cevap; sha256 `f1ff9f2aa228…07da69` manifest ile aynı. Kayıt `tools/v2_forensics.replay` ile yeniden oynatıldı.

| Ölçü | Değer |
|---|---|
| Kimlik grupları | 194 MULTI_VENUE (**119 Tier A**, 75 Tier B), 472 SINGLE_VENUE_ONLY, 119 IDENTITY_UNVERIFIED |
| V2 strateji satırı | 401 (119 varlık) |
| Aday | 481 funding + 1,058 yakınsama + 1,058 hibrit = **2,597** |
| PAPER'a uygun | **0** |

Not: Phase 3 raporunda Tier B 80 idi. 05:12 kaydında 75; fark, kayıt anındaki listeden geliyor.

## 5. Bu denetimde bulunan iki yapısal sorun

1. **Ekonomisi hesaplanmayan adaylar görünmüyordu.** Kapı (gate) statik bir retle durunca strateji hiç çalışmıyor ve `net` boş kalıyor. Panel yalnız "ilk ret" sayısını gösteriyordu. Böylece NATGAS gibi sıralamada 1. olan bir aday hiçbir listede yer almıyordu. 2,597 adayın **2,399'u** bu durumda: NOT_EVALUATED.
2. **Test fikstüründe yanlış `exchange_id`.** `test_v2_phase4.row()`, `exchange_id` alanını mekân adının küçük harfinden üretiyordu (`"lighter"`). Doğrusu `"lighter_rh"`. Yeni PAPER evreni kontrolü bunu yakaladı; fikstür `policy.FROM_LEGACY` ile düzeltildi (Phase C).
