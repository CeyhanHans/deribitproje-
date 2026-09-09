# Deribit Options Backtest Catalog

## Yeni bilgisayar veya ajan için başlangıç

Bu depo kaynak kodu, karar gerekçelerini ve 24 görevlik uygulama planını içerir.
**Durum: çekirdek prototip; tam backtest ürünü henüz tamamlanmadı.** Yayın hazırlığında 64/64 unit test geçti; gerçek tarih aralığında uçtan uca strateji raporu henüz yok.

- [Güncel durum ve bilinen hatalar](docs/STATUS.md)
- [Kararlar ve gerekçeleri](docs/DECISIONS.md)
- [Kurulum ve kaldığımız yerden devam](docs/HANDOFF.md)
- [Ajan kuralları](AGENTS.md)
- [Görev sırası](orchestration/tasks/0_OKU_ONCE.txt) · [İlk görev](orchestration/tasks/1.txt) · [Ortak mimari](orchestration/tasks/ORTAK_MIMARI.txt)

Hedef: seçilen tarih aralığında, yapılandırılabilir opsiyon stratejisiyle gerçek piyasa verisinden işlem sayısı, kazanma/kaybetme oranı, net PnL ve risk raporu üretmek. Long straddle ve 2024 Mart–2025 Mayıs yalnız kabul örnekleridir.

Python 3.11+ ile: `python -m unittest discover -s tests -v`.
Mevcut yayın hazırlığı Python 3.14.7 üzerinde doğrulandı; 3.11 ayrı ortamda henüz denenmedi.
Sonraki adım Görev1 sözleşme/baseline kabulüdür. Aşağıdaki ilk taslak ve eski katalog/raporlardaki plan ifadeleri mevcut implementasyon kanıtı değildir; güncel durumda docs/STATUS.md esas alınır.

Kişisel Deribit opsiyon stratejilerini **gerçek para riske atmadan** önce test etmek için düşük maliyetli araştırma kataloğu.

## Hedef

- İlk sürümde yalnızca BTC inverse opsiyonları incelemek.
- Başlangıç bütçesi: **$0**; doğrulanmış ihtiyaç oluşursa veri için en fazla **$3–5**.
- Amaç kârlılık vaadi değil; bir fikrin spread, komisyon, vade ve volatilite altında mantıklı olup olmadığını ölçmek.

## İlk sürüm sınırları

- Yalnızca günlük/1 saatlik sinyal testleri.
- Giriş fiyatı `ask`, çıkış fiyatı `bid`; mark fiyatı yalnızca değerleme için.
- Deribit ücretleri, kontrat büyüklüğü ve vade uzlaşması modele dahil edilecek.
- Testnet verisi strateji performans kanıtı sayılmayacak.
- Geçmiş L2 emir defteri olmadan dolum/slippage sonucu "tahmini" olarak etiketlenecek.

## Katalog

- [Veri kaynakları](catalog/data-sources.md)
- [Veri sözlüğü](catalog/data-dictionary.md)
- [Strateji kaydı şablonu](strategies/strategy-template.md)
- [Backlog](backlog.md)
- [Gerekenler durumu](REQUIRED.md)

## Başlangıç akışı

1. Deribit kamu API'sinden instrument, index, mark, bid/ask, IV, Greeks ve uzlaşma verisini topla.
2. Her stratejiyi `strategies/` altında varsayım ve maliyetleriyle kaydet.
3. Önce basit bid/ask tabanlı backtest çalıştır.
4. Sonuç umut verirse, yalnız o strateji için düşük maliyetli geçmiş quote/L2 verisi araştır.

Bu depo yatırım tavsiyesi vermez ve canlı işlem emri göndermez.
