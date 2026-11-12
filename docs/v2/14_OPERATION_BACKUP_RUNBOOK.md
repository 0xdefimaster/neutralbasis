# 14 — İşletim, yedek ve kurtarma runbook'u

**Bu belge bir plandır; hiçbir adımı otomatik çalıştırılmadı.** E: diskine dokunan her adım sahibin açık onayını ister.

## Durum (2026-11-12)

- **E: diski** (TOSHIBA MQ02ABD100H HDD; D: ile aynı fiziksel disk):
  - System log disk event 7 (bozuk blok) sayıları: 11-08 1.553, 11-09 6.734, 11-10 13.842, 11-11 2.134, 11-12 (02:32'ye kadar) 180. Düşüş, diskin boşta kalmasından.
  - Windows diski "Healthy" gösteriyor; bu göstergeye güvenilmez.
  - 2026-11-10'da temiz olmayan bir kapanış yaşandı.
- **`E:\NeutralBasis\data-five-rh\paper.sqlite3`:** 26,47 GB, WAL 3,08 GB, shm 32 KB. Son yazma 2026-11-10 07:40. **Yedeği yok. Bütünlüğü UNKNOWN.**
- **C:'deki pilot DB'leri** (11,2 GB): tek kopya; `quick_check` ok.

## Kurtarma sırası (onay sonrası)

1. **Sağlıklı hedef:** en az 60 GB boş, E:/D: dışında bir disk. Harici SSD ya da Contabo; C:'de 12 GB var, yetmez.
2. **Fiziksel durum:**
   - `smartctl -a` (smartmontools) ya da üretici aracıyla SMART okunur: reallocated, pending ve uncorrectable sektör sayıları.
   - Pending sayısı yüksekse dosya kopyası yerine **imaj** alınır.
3. **İmaj ya da kontrollü kopya** (motor kapalı; hiçbir SQLite bağlantısı açılmaz):
   - Tercih: ddrescue benzeri, okuma hatalarını atlayıp sonra yeniden deneyen bir imaj aracı (Windows'ta HDD Raw Copy veya bir WSL/Linux live ortamında `ddrescue`). Disk ya da yalnız bölüm imajı alınır.
   - Alternatif: `robocopy /J /R:1 /W:1` ile `paper.sqlite3`, `-wal` ve `-shm` **birlikte** kopyalanır. Kopyalama sırasında dosyalara hiçbir süreç dokunmaz.
   - Kaynak dosyalar asla açılmaz, taşınmaz ve checkpoint yapılmaz.
4. **Hash:** kaynak ve kopya için sha256. Kaynaktan okuma hatası alınırsa hash eşleşmez; bu kayıt altına alınır.
5. **Kopyada bütünlük** (orijinalde değil):
   - `pragma quick_check`, ardından `integrity_check`.
   - WAL kopyada açılınca checkpoint yapılabilir. Bu yüzden önce kopyanın bir **ikinci kopyası** alınır ve kontrol ona yapılır.
6. **Hasar varsa:** `sqlite3 .recover` ikinci kopyada çalıştırılır. Kurtarılan tablolar raporlanır, eksikler UNKNOWN olarak işaretlenir.
7. **Ancak bundan sonra:** E: emekliye ayrılır ya da yalnız çöp veriye kullanılır. Motor E:'de başlatılmaz.

## C:'deki pilot DB'leri

- Motor kapalı olduğu sürece `robocopy` ile harici diske kopyalanabilir.
- Sonra sha256 ve kopyada `quick_check`.
- Canlı bir SQLite DB için dosya kopyası yerine `sqlite3 src ".backup dst"` ya da Python `Connection.backup` kullanılır.

## 7/24 çalıştırma (deploy yok; yalnız runbook)

- Sağlıklı disk ya da VPS kullanılır; E: kullanılmaz.
- Kullanıcı servisi (systemd ya da NSSM) çalıştırma ve durdurma betikleriyle yönetilir. Otomatik başlatma sahibin onayıyla açılır.
- Dashboard 127.0.0.1'e bağlanır; uzaktan erişim SSH tüneliyle olur.
- Günlük yedek: küçük DB'ler ve manifestler `.backup` ile; büyük ham veri saklama süresine göre.
- Geri dönüş: dal ya da `git revert`. Veri tarafında: dondurulmuş veri seti + manifest + yedek.
