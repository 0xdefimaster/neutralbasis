# herdcall v2 — Windows paketi

Funding kesintisi sonrası tahmini değerleme kurtarması, yayımlanmayan funding saatinin girişleri kalıcı kilitlememesi ve 67 test; Python kaynakları Black ile biçimlendirildi. Ayrıntılar HERDCALL-KAPSAM.md son bölümündedir.

1. ZIP dosyasını bir klasöre çıkarın.
2. Python 3.11 veya üzeri kurulu olmalı. İlk kullanımda Kurulum.cmd dosyasına çift tıklayın. İnternet üzerinden requirements.txt bağımlılıkları yüklenir.
3. Baslat.cmd dosyasına çift tıklayın. Tarayıcıda http://127.0.0.1:8765/#price adresini açın.
4. Durdur.cmd motoru kapatır. Paneldeki durdur düğmesi yalnızca yeni paper girişlerini durdurur; açık pozisyonlar izlenir.

İlk geçmiş taraması birkaç dakika sürebilir. Klasörü motor çalışırken taşımayın. Aynı portta ikinci kopya başlatmayın.

Grafikler tüm çiftlerin detayında bulunur. İki borsada kesintisiz eşlenmiş 7 günlük gerçek işlem mumu varsa iki fiyat ve spread grafiği çizilir. Eksik/oracle veri tamamlanmış gibi gösterilmez. Bazı platformların geçmiş bağlantıları desteklenmez veya API sınırına takılabilir. Paper işlemi yoksa açılış/kapanış işareti gösterilmez.

Kapsam: 63 platform kataloğu / 30 public adaptör; her platform bağlı değildir. Otomatik yakınsama paper yürütmesi yalnızca Lighter RH–Extended üzerinde, yapılandırılmış 15 varlığın ortak piyasalarında çalışır. Diğer platformlar veri/analiz kapsamındadır. Gerçek emir gönderilmez.

Bu paket temiz başlangıç içindir. API anahtarları, bağlantı kayıtları, yerel veritabanı, paper bakiyeler ve işlem geçmişi içermez. Çalıştırıldığında yeni data-five-rh klasörü oluşur. Public tarama için anahtar gerekmez.

Net paper sonuç = fiyat PnL + funding − ücretler. Slippage dolum fiyatına dahildir. Gelecek çıkış likiditesi tahminidir. Ayrıntılar HERDCALL-KAPSAM.md dosyasında.
