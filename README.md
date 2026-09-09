# Deribit Options Backtest Catalog

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
