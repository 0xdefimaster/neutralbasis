# 20 — V2 PAPER pilot operasyon runbook'u (onaya sunulur)

**Bu belge bir talimat listesidir; hiçbir adım çalıştırılmadı.** Başlatma, sahibin yazılı onayıyla yapılır. **PAPER ONLY**:
- gerçek emir, imza, cüzdan ya da trading anahtarı yok;
- E: diskine hiçbir şey yazılmaz, E:'deki hiçbir şey okunmaz.

## 0. Ön koşullar (hepsi sağlanmadan başlatılmaz)

| # | Koşul | Nasıl kontrol edilir |
|---|---|---|
| P1 | Kod `v2/venue-policy` dalında, testler temiz | `git -C C:\Users\W10\nb-verify log -1`; `python -m unittest discover -p "test_*.py"` → 0 başarısız |
| P2 | Veri klasörü E: dışında, en az 20 GB boş | `python -c "from v2 import ops; print(ops.preflight(r'<VERİ>', r'<VERİ>\paper.sqlite3'))"` → `(True, [])` |
| P3 | Eski pilot veritabanları korunuyor; yenisi **ayrı** bir klasörde | `<VERİ>` = ör. `C:\Users\W10\nb-v2-pilot\data` (yeni ve boş). `nb-pilot` ve `nb-pilot-loose` kullanılmaz |
| P4 | Risk ayarları sahibin değerleri | `wide_max_breakeven_hours=24`, `wide_funding_realization=1.0`. Değiştirilmez |
| P5 | Hibrit açılış kapalı | `wide_hybrid_enabled` yok ya da False. Açmak ayrı onay ister |
| P6 | Disk kapasitesi | C:'de ~12 GB boş, sınırda. Önerilen: Contabo ya da yeni disk (`14_OPERATION_BACKUP_RUNBOOK.md`) |
| P7 | Çalışan başka pilot ya da motor yok | 8765 / 18765 / 18766 portları boş; Python süreci yok |

## 1. Başlatma (onaydan sonra)

```powershell
$env:NB_V2 = "1"                 # 8 borsa kapsamı, ağ koruması V2 kipinde
$env:NB_V2_TXFLOW_WS = "0"       # TxFlow WS canlıda denenmedi; ilk koşuda kapalı
Remove-Item Env:NB_EXTRA_PERP_DEXES, Env:NB_RFQ_STREAMS, Env:NB_LIGHTER_RH_ALL -ErrorAction SilentlyContinue
$env:NB_PILOT_DATA = "C:\Users\W10\nb-v2-pilot\data"
$env:NB_PILOT_PORT = "18767"
& C:\Users\W10\nb-verify-venv\Scripts\python.exe C:\Users\W10\nb-verify\pilot\run_pilot.py
```

`pilot/run_pilot.py` başlangıçta sırayla şunları yapar:
1. E: yolunu ve canlı motor portunu reddeder;
2. `v2.policy.check_environment()` çalıştırır (eski genişletme anahtarları varsa başlamaz);
3. ağ korumasını V2 kipinde kurar;
4. CCXT kataloğunu 8 borsaya daraltır;
5. `pilot.json` dosyasına kapsamı yazar.

Doğrulama (kodda):
- `run_pilot.py` `NB_PILOT_DATA`, `NB_PILOT_PORT`, `NB_PILOT_MAX_GB` (disk bekçisi, varsayılan 5 GB) ve `NB_PILOT_AUDIT_RAW_DAYS` değişkenlerini okur.
- Motor `risk_main.RiskEngine`, yani `desk_analytics.DeskEngine`; `NB_V2=1` iken V2 kaynağını, kapıyı ve runtime'ı bağlar.
- V2 dosyaları `<VERİ>/v2/` altında: `hybrid_positions.json` ve `recorder/`.

## 2. İlk 15 dakikada kontroller

| Kontrol | Beklenen | Nerede |
|---|---|---|
| CEX / legacy çıkışı | 0 | `<VERİ>\logs\network-guard.jsonl`: yalnız `audit` (perpDexs / perpCategories) satırları; `refused` satırı varsa nedeni incelenir |
| V2 satırları | Yaklaşık 400 (anlık görüntüye göre değişir) | `GET http://127.0.0.1:18767/api/v2/state` → `rows` |
| Strateji özetleri | Her stratejide aday, kategori ve neden sayıları | `/v2` sayfası |
| Kaydedici | `writing: true`, `refused_unwanted` artıyor olabilir, `refused_disk = 0` | `/api/v2/state` → `recorder` |
| WAL | OK (< 1 GB) | `ops.wal_status` |
| Variational 429 | Ara sıra olabilir; yeniden denemeyle geçmeli | Runtime `errors` |

## 3. Sürekli izleme

- **Dashboard:** `http://127.0.0.1:18767/v2`. Yalnız 127.0.0.1'e bağlıdır; uzaktan erişim SSH tüneliyle.
- **Durdurma kriterleri** (biri olursa durdur, raporla):
  - `refused` ağ satırı;
  - `safe=false` (disk ya da WAL);
  - recorder `writing=false`;
  - `LEG_RECOVERY` sayısında artış;
  - tekrarlayan `EVAL_ERROR`.
- Runtime ve kaydedici yeni girişleri kendiliğinden durdurur (fail-closed). Açık pozisyonlar izlenmeye devam eder.

## 4. Durdurma

`Ctrl+C` ya da süreci sonlandır (`Stop-Process -Id (Get-Content <VERİ>\pilot.pid)`).

- Açık WidePaper pozisyonları veritabanında kalır.
- Hibrit pozisyonlar `<VERİ>\v2\hybrid_positions.json` dosyasında kalır.
- Yeniden başlatmada OPENING durumundaki pozisyon `LEG_RECOVERY` olarak, EXIT_PENDING durumundaki pozisyon çıkışı tamamlanarak kurtarılır (test edildi).
- Kaydedicinin yarım segmenti bir sonraki başlatmada "recovered" olarak mühürlenir.

## 5. Geri dönüş

- **Kod:** `git checkout local/audit-fixes-2026-11-09` (V2 öncesi). V2 kapalıyken (`NB_V2` yok) davranış eskisiyle aynı.
- **Veri:** V2 pilotu ayrı bir klasörde çalışır; eski pilot ve E: verisine dokunmaz. Klasörü silmeden önce gerekirse arşivlenir.

## 6. Onay gerektiren ayrı kararlar

1. Bu runbook'la V2 PAPER pilotunun başlatılması (veri klasörü ve disk).
2. `NB_V2_TXFLOW_WS=1` (TxFlow WebSocket'in canlı denenmesi).
3. `wide_hybrid_enabled=True` (hibrit paper pozisyonları açılması).
4. Risk ayarlarında değişiklik (24 saat / 1,0).

## 7. Phase 7 eki: başlatma ön kontrolü (kodda zorunlu)

`NB_V2=1` iken `pilot/run_pilot.py`, başka hiçbir şey yapmadan önce `v2.ops.launch_check(<VERİ>)` çalıştırır. Bulgulardan biri bile varsa başlatmayı reddeder.

1. **Disk:** veri klasörü D: veya E: üzerinde olamaz. İkisi de bozuk bloklu aynı TOSHIBA disk.
2. **Boş alan:** en az **50 GB**. C:'deki ~12 GB yetmez.
3. **Yedek kanıtı:** `<VERİ>\BACKUP_EVIDENCE.json` dosyası olmalı ve şu alanları içermeli:
   - `created`: epoch saniye, en çok 7 gün önce;
   - `destination`: veri diskinden farklı, sağlıklı bir sürücüdeki yol;
   - `sha256_manifest`: boş olmayan bir yedek manifesti.

   Bu dosya, `14_OPERATION_BACKUP_RUNBOOK.md`'deki yedek alınıp doğrulandıktan sonra elle yazılır.

## 8. Gözlem modu (`observation_v3`) ve adli inceleme aracı

Gözlem modu **salt okunur bir araştırma aracıdır; motor değildir**. PAPER pozisyon açmaz, motoru ve kaydediciyi başlatmaz. `run_pilot.py`, `NB_OBSERVATION_V3=1` görürse çalışmayı reddeder. NB_V2 ağ politikası bu moddan etkilenmez.

1. **Çevrimdışı çalıştırma** (ağ yok): 05:12 kaydı yeniden oynatılır; huni, NATGAS/INIT izleri, radar ve panel üretilir.

   ```
   python -m tools.v2_forensics --phase4 C:\Users\W10\nb-benchmark\v2-phase4-2026-11-12 --out <C:\...\klasör>
   ```

2. **Gözlem okuması** (açık onay, yaklaşık 20 kamuya açık istek): Hyperliquid native ve EntropyIO metadata, INIT ve xyz:NATGAS 90 günlük funding geçmişi ile defteri, TxFlow, Variational ve Phoenix snapshot okunur.

   ```
   python -m tools.v2_forensics --out <klasör> --observe
   ```

   Ardından 1. adımı aynı klasörle yeniden çalıştırın; `forensics_observed.json` üretilir.

3. **Kontroller:**
   - `observation_manifest.json` içinde `guard.refused` 0 olmalı;
   - `by_venue` alanında CEX olmamalı;
   - `guard-observation.jsonl` boş ya da yalnız denetim kayıtları içermeli.

4. **Çıktılar:** `funnel.json` (tüm izler), `forensics.json`, `forensics_observed.json`, `dashboard_research.html`.

5. **Yasaklar:**
   - D:/E: yolu: araç reddeder;
   - `qfex login` ve `qfex agents init`;
   - TxFlow `userFundings` için yetkisiz adres;
   - Phoenix'te belgelenmemiş yollar (ağ politikası reddeder).
