# 15 — Migrasyon planı

Bu fazda **şema değişikliği yok**. V2-0 alanları (`outcome`, `entry_economics`) işlem gövdesine JSON olarak yazılıyor; V2 politika modülleri DB'ye hiç dokunmuyor.

## Gelecekteki migrasyonlar için kapı

Her biri ayrı onay ister.

1. **Ön kontrol:** `tools/v2_preflight.py` ile şema hash'i, satır sayıları, `quick_check` ve WAL boyutu (salt okunur).
2. **Yedek:** sağlıklı ikinci bir diske `.backup` alınır; yedekte sha256 ve `quick_check` yapılır.
3. **Migrasyon önce yedeğin kopyasında prova edilir:** süre, boyut ve sonraki `quick_check`.
4. **Geri dönüş:** yedek dosyası ve şema sürümü (`schema_migrations`).
5. **Yasak:** E:'deki DB üzerinde migrasyon; WAL silme veya küçültme; VACUUM (onaysız); veritabanında borsa adını yeniden adlandırma.

## Kimlik uyumluluğu

Veritabanlarında eski borsa adları (`Lighter`, `RISEx`, `tradeXYZ`, …) kalır. V2 kodu bunları `v2.policy.FROM_LEGACY` ile `exchange_id`'ye çevirerek okur; tarihsel kayıtlar yeniden yazılmaz.
