# 28 — NATGAS/INIT master prompt: kabul raporu

Tarih: 2026-11-12. Dal `v2/venue-policy`; taban `b3e3bc4`, bu çalışma onun üzerine yerel bir commit.

**PAPER ONLY.**
- Gerçek emir, imza, anahtar ya da giriş yok.
- E:'ye dokunulmadı.
- Motor, pilot ve kaydedici başlatılmadı.
- Push, merge ve deploy yapılmadı.

## 1. Kabul kontrol listesi

| # | Kriter | Durum | Kanıt |
|---|---|---|---|
| 1 | Gerçek HEAD doğrulandı | ✔ `b3e3bc4` (belgedeki 2cafe02 eski) | 23 |
| 2 | Test sayısı yeniden doğrulandı | ✔ 908 → 945 | §4 |
| 3 | 119 / 2,597 / 0 fikstürden yeniden üretildi | ✔ | 23 §4, 26 §4 |
| 4 | NATGAS 05:12 kaydında var mı | ✔ Evet, 3 mekân, Tier A | 24 A |
| 5 | NATGAS ilk ve tüm retleri | ✔ RWA_REFERENCE_UNVERIFIED; + SETTLEMENT_EVIDENCE_MISSING (TxFlow bacağı) | 24 A, test |
| 6 | INIT ilk ve tüm retleri | ✔ CONFIG_EXCLUDED / SINGLE_VENUE_ONLY; + IDENTITY_UNVERIFIED, EXECUTION_MODEL_UNVERIFIED, SETTLEMENT_EVIDENCE_MISSING, INSUFFICIENT_LIQUIDITY ($5k, $10k), REGIME_SHIFT | 24 B, test |
| 7 | Ekran değerleri SCREENSHOT_UNVERIFIED etiketli | ✔ | `docs/v2/fixtures/` |
| 8 | Ayrı evrenler; PAPER evreni tam 8 ve değişmedi | ✔ `policy.py` diff boş | 25 §1, test |
| 9 | `observation_v3` ayrı ağ modu; NB_V2 genişlemedi | ✔ `network_policy.py` diff boş | 25 §2, test |
| 10 | entropy_io (dex "io") doğrulandı | ✔ perpDexs fullName "EntropyIO"; 22 piyasa okundu | 25 §4 |
| 11 | hyperliquid_native kapsamlı okuma | ✔ 235 piyasa, INIT 90 gün geçmiş | 25, 24 B |
| 12 | phoenix_perps | Kısmi: yalnız metadata (snapshot); defter, funding ve geçmiş rotası UNKNOWN | 25 §4 |
| 13 | qfex | ENGELLİ: REST yolu UNKNOWN; login veya agents init yok | 25 §4 |
| 14 | Gözlem mekânı PAPER bacağı olamaz | ✔ `assert_paper_venue`, Gate CONFIG_EXCLUDED | test |
| 15 | Funding kanıt durumları | ✔ `v2/funding_evidence.py` | 26 §3 |
| 16 | Kapsamayı dikkate alan pencereler (55/90) | ✔ | test |
| 17 | Değişmez trace id, ilk ve tüm retler, eksik kanıt, hash, güven, sonraki adım | ✔ `v2/funnel.py` | 26 §1 |
| 18 | NOT_EVALUATED ≠ FAILED ≠ UNPROFITABLE | ✔ 2,399 / 70 / 128 | 26 §4 |
| 19 | Denklik sınıfları | ✔ | 26 §2 |
| 20 | Radar POTENTIAL / RESEARCH_ONLY, asla uygun değil | ✔ | 26 §5 |
| 21 | $1k / $5k / $10k, iki yön | ✔ | 27 §2–3 |
| 22 | INIT ve NATGAS işaret birim testleri | ✔ | 27 §4 |
| 23 | Aykırı değer ve anomali testleri | ✔ | 27 §5 |
| 24 | TxFlow userFundings dar istisnası | ENGELLİ: yetkili adres yok, ağ reddediyor | 25 §4 |
| 25 | Trades ve L2 kaydı | L2: mevcut kaydedici; trades: **NOT_SUPPORTED** (kaydedilmiyor) | §3 |
| 26 | Panel: 11 mekân, NATGAS/INIT kartları, gözlenen ↔ uygun | ✔ `research_sections` | 26 §6 |
| 27 | CEX isteği 0 | ✔ gözlem okumasında 19/19 izinli, 0 CEX | 25 §5 |
| 28 | Risk ayarları 24 / 1.0 değişmedi | ✔ `wide_paper.py` diff boş | §4 |

## 2. Kod değişiklikleri

**Yeni dosyalar**
- `v2/observation.py`: iki evren, `observation_v3` ağ politikası ve 11 mekân matrisi.
- `v2/funding_evidence.py`: kanıt durumları, pencereler, işaret kuralı ve anomaliler.
- `v2/funnel.py`: izler, kanonik kodlar, denklik ve radar.
- `v2/venues/hyperliquid_obs.py`: native ve io için salt okunur adaptör.
- `tools/v2_forensics.py`: adli inceleme aracı.
- `test_v2_forensics.py`: 37 test.
- `docs/v2/23`–`28` ve `docs/v2/fixtures/arbsh_screenshots_2026-11-12.json`.

**Değişen dosyalar**
- `pilot/network_guard.py`: `mode` parametresi. Politika modülü seçilebiliyor; NB_V2 aynı modülü kullanıyor.
- `pilot/run_pilot.py`: `NB_OBSERVATION_V3` ayarlıysa çalışmayı reddediyor.
- `v2/gates.py`: PAPER evreni dışındaki satıra `CONFIG_EXCLUDED`.
- `v2/ratelimit.py`: Hyperliquid tek bütçe; Phoenix bütçesi.
- `v2/venues/variational.py`: `quote_book_for` (boyuta göre kotasyon kovası).
- `v2/dashboard.py`: araştırma bölümleri.
- `test_v2_phase4.py`: fikstürdeki `exchange_id` hatası düzeltildi.
- `docs/v2/20_OPERATIONS_RUNBOOK.md`: §8.

## 3. Kalan engeller

| Engel | Tür | Ne gerekir |
|---|---|---|
| RWA referans kontratı (NATGAS ve 115 aday) | Belge / veri | Her mekânın kendi belgesinden kontrat ayı ve roll kuralı |
| TxFlow ödenmiş funding | Dış API / yetki | Yetkili salt okunur test adresi (yok) |
| Variational RFQ dolum modeli ve settlement | Dış API | Firm quote kaydı; settlement geçmişi uç noktası UNKNOWN |
| RISEx ücreti | Dış API | Hesap bazlı ücret yayımlanmıyor |
| PopDEX ve Veranta | Dış API | Doğrulanmış kamuya açık API yok |
| Phoenix defter, funding ve geçmişi | Doğrulama | SDK kaynağında ham yolların okunması |
| QFEX | Doğrulama | REST yolları ve WS funding birimi; login yok |
| Hyperliquid native PAPER'da | Sahip kararı | Ayrı bir inceleme; bu fazda yapılmadı |
| Public trades kaydı | Kod | Kaydedici yalnız defter yazıyor (NOT_SUPPORTED) |
| Disk ve yedek | Operasyon | C: 13 GB boş (en az 50 GB gerekli), BACKUP_EVIDENCE yok, E:/D: arızalı |

## 4. Testler ve değişmezlik kanıtı

- Tam takım: **945 test, OK (2 atlandı)**, 271 s. Önceki 908 testin hepsi geçiyor; 37 yeni test eklendi.
- `git diff b3e3bc4 -- v2/policy.py v2/network_policy.py wide_paper.py` boş. PAPER evreni, NB_V2 ağ politikası ve risk ayarları (24 / 1.0) değişmedi.

## 5. Pilot kapısı kararı

**BLOCKED** (kısa PAPER pilotu için).
- `ops.launch_check` geçmiyor: C: 13 GB < 50 GB, `BACKUP_EVIDENCE.json` yok, uygun sağlıklı disk yok.
- 05:12 kaydında PAPER'a uygun aday 0.

Salt okunur gözlem smoke'u (`READY_FOR_READONLY_SMOKE` aşaması) bu fazda bir kez çalıştırıldı ve geçti: 19 istek, 0 ret, 0 CEX. Sürekli bir gözlem servisi kurulmadı.
