# Araştırmayla Doğrulanan Kaynaklar

| Kaynak | Ücretsiz veri | Backtestte rol | Karar |
|---|---|---|---|
| [Deribit Public API](https://docs.deribit.com/) | Enstrüman, trade, anlık bid/ask/mark/IV/Greeks/OI ve delivery | Ana kaynak | Kullan |
| [Deribit Options Data Collection](https://docs.deribit.com/articles/options-data-collection-best-practices) | Resmi collector mimarisi | Canlı veri biriktirme tasarımı | Kullan |
| [RiveChen downloader](https://github.com/RiveChen/deribit-historical-data) | History API trade indirme yardımcı kodu | Trade backfill referansı | Küçük örnekle doğrula |
| [Tardis](https://docs.tardis.dev/historical-data-details/deribit) | Bazı sample/deneme erişimleri olabilir | Geçmiş L2/quote gerekiyorsa | Bütçe dışı, şimdilik alma |
| [CoinGlass](https://www.coinglass.com/options/Deribit) | Görsel OI/hacim/put-call/max pain bağlamı | Sinyal filtresi | Kullan, fiyat verisi sayma |

## Kanıtlı sınırlamalar

- `public/get_instruments` aktif veya **yakın zamanda** vadesi biten enstrümanları döndürür; tam geçmiş enstrüman arşivi değildir.
- Geçmiş trade, gerçekleşen işlemi gösterir; tek başına geçmiş bid/ask veya L2 dolum doğrulaması değildir.
- Tam tarihsel quote/IV/OI/L2 verisi $0–5 bütçede hazır bir veri seti olarak doğrulanmadı.
- RiveChen deposu, `trade_seq` ile sayfalama/checkpoint/dedup yaklaşımı için yararlıdır; tüm tarihsel veri setinin eksiksiz olduğunu garanti etmez.
