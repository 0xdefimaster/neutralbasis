# 07 — İki aşamalı keşif ve 2+ borsa filtresi

Kod karşılığı: `v2/discovery.py`.

## A — LightweightDiscovery

- Yalnız metadata okunur: id, aktiflik, tick/lot, çarpan, seans.
- Borsa başına TTL + jitter uygulanır; süresi dolmayan borsa yeniden çekilmez.
- Hata veren borsa son kataloğunu korur ve hata kaydıyla işaretlenir.
- Kapsam dışı bir `exchange_id` istenirse `ValueError` fırlatılır.
- Tek borsalı piyasa için defter, funding geçmişi ya da trade çekilmez.

## B — Kesişim ve SelectiveSubscription

`intersect()` her kanonik varlığa bir durum verir:

| Durum | Anlamı |
|---|---|
| `MULTI_VENUE` | 2+ farklı `exchange_id`, kimlik doğrulanmış |
| `SINGLE_VENUE_ONLY` | Tek borsa |
| `IDENTITY_UNVERIFIED` | Kimlik kanıtı yok |

Yalnız `MULTI_VENUE` üyeleri için ağır kanallar açılır: book, mark, trades, funding, history.

`SubscriptionManager.apply()`:
- `(to_open, to_close)` döndürür; aynı girdiyle tekrar çağrılınca boş döner (idempotent).
- `hold(xid, market, reason)` ile açık PAPER pozisyon, bekleyen dolum, kurtarma ya da replay için gereken akış, tutma bırakılana kadar kapanmaz.
- Varlık ikinci bir borsada listelenince akış açılır; listeden çıkınca kapanır (tutma yoksa).

Çift sayımı: n borsa n(n−1)/2 sırasız çift ve iki katı yön verir; 8 borsa için 28 çift, 56 yön. tradeXYZ ile Hyperliquid aynı host'u kullanır, ama sayım `exchange_id`'ye göre yapıldığı için bozulmaz.

## Ölçümler (canlı smoke, 2026-11-12)

`06_MARKET_IDENTITY.md` tablosu geçerli.
- Katı kesişim: 15 varlık, 1 borsa çifti. Bu varlıklar dışında hiçbir ağır akış açılmaz.
- Sembol eşleşmesi 242 varlık gösterirdi.

"Bütün taramayı kapatıp hiçbir gerçek çift bulamamak başarı değildir." Bu yüzden kaçırılan piyasa ölçüsü olarak `ticker_only` raporlanıyor. Phase 3'ün hedefi bu farkı kanıtla daraltmak; kapıyı gevşetmek değil.

## Bugün yapılmayanlar (Phase 3)

- Gerçek borsa adaptörleri: `fetch(exchange_id)`, şu an fixture ayrıştırıcıları.
- Sürekli çalışan bir döngü.
- WebSocket abonelik işçileri.
- Delist ve yeniden listeleme olaylarının atomik geçişi. Şu an kesişim her yenilemede yeniden hesaplanıyor.
