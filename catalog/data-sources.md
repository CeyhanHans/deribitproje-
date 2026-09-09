# Veri Kaynakları

## Başlangıç: ücretsiz Deribit API

| Veri | Endpoint | Kullanım |
|---|---|---|
| Sözleşme tanımı | `public/get_instruments` | Vade, strike, C/P, kontrat büyüklüğü |
| Canlı quote ve Greeks | `public/get_order_book` | Bid/ask, mark, IV, delta, gamma, vega, theta, open interest |
| Underlying mumları | `public/get_tradingview_chart_data` | Sinyal ve hedge fiyat serisi |
| Gerçekleşmiş volatilite | `public/get_historical_volatility` | Volatilite filtresi |
| DVOL | `public/get_volatility_index_data` | Volatilite rejimi filtresi |
| Uzlaşma | `public/get_delivery_prices` | Vade sonu PnL |

## Geçmiş işlem verisi: ücretsiz başlangıç

| Kaynak | Veri | Karar |
|---|---|---|
| Deribit History API | Tekil opsiyonların geçmiş trade kayıtları; işlem anındaki fiyat, miktar, IV, mark ve index alanları | İlk tarihsel trade tabanlı backtest için kullanılacak |
| RiveChen downloader | History API'den JSONL/Parquet trade indirme, checkpoint, dedup ve doğrulama | Kopyalamadan önce küçük veri örneğiyle doğrulanacak referans uygulama |

`get_instruments?expired=true` yalnızca **yakın zamanda vadesi biten** sözleşme tanımı döndürür; tam tarihsel opsiyon zinciri arşivi değildir. Uzun geçmiş için trade endpointi ve enstrüman yaşam döngüsü kaydı gerekir.

## Veri kalitesi kuralı

- Geçmiş **mark** verisi veya işlem fiyatı, gerçekleşebilir alış/satış fiyatı değildir.
- Alışta ask, satışta bid kullanılır.
- Eski L2 quote verisi yoksa slippage ve dolum sonuçları "tahmini" yazılır.

## Sonraki aşama: yalnız gerekirse ücretli arşiv

Tardis veya Amberdata; geçmiş trade/quote/L2 snapshot sağlayabilir. Satın alma ancak ilk ücretsiz testte strateji umut verirse ve maliyet $3–5 sınırındaysa değerlendirilir.

Mevcut araştırmada Tardis, Amberdata, CoinGlass API ve CryptoDataDownload'ın tam opsiyon arşivleri $0–5 bütçe sınırına uygun görünmüyor. Tardis yalnız örnek/tek gün erişimi varsa değerlendirilecek; CoinGlass ise yalnız bağlam kaynağı kalacak.

## Yardımcı analiz kaynağı: CoinGlass

CoinGlass'ın Deribit sayfası; vade/strike bazlı open interest, hacim, put-call oranı ve max pain gibi **piyasa bağlamı** metrikleri sunar. Bu metrikler sinyal filtresi için incelenebilir; ancak kaydedilmiş sayfa tek başına güvenilir, zaman serili veya emir-dolum verisi değildir. Backtest giriş/çıkış fiyatı yerine kullanılmaz.
