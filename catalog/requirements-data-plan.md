# Requirements + Data Plan

Bu belge, Deribit Options Backtest Catalog icin 1-17 kapsam maddesini tek, sirali ve kanit standardi olan urun/veri planina cevirir.

Kapsam bu dosyada yalniz `catalog/` seviyesindedir. Kod, collector, backtest motoru, strateji uygulamasi, GitHub islemi ve canli emir gonderimi bu planin disindadir.

## Sabit kararlar

1. Ilk urun BTC inverse option olacaktir.
2. Baslangic veri butcesi $0 olacaktir.
3. Ucretli veri ancak dogrulanmis ihtiyac olursa ve toplam maliyet $3-5 sinirinda kalirsa degerlendirilecektir.
4. Birincil kaynak Deribit public API olacaktir.
5. CoinGlass yalniz piyasa baglami kaynagidir; backtest fiyat, dolum veya performans kaniti degildir.
6. Testnet verisi strateji performans kaniti sayilmayacaktir.
7. Sistem yatirim tavsiyesi veya canli trade araci degildir.
8. Ilk backtest pencereleri ayri raporlanacaktir: son 30, 60, 90, 120 ve 360 gun.
9. Ilk zaman cozunurlugu 1 saattir; sistem her saniyeyi degil, her saatlik gozlem setini degerlendirir.
10. Ilk strateji tek bir kurala kilitlenmeyecektir; call/put, long/short, cok-bacak, entry filtresi, stop-loss, take-profit, sure cikisi ve expiry cikisi parametrik kalir.

## 1-17 Gereksinim listesi

| No | Gereksinim | Kabul kriteri | Kanit standardi |
|---:|---|---|---|
| 1 | Projenin amaci opsiyon stratejilerini gercek para kullanmadan once mantik, maliyet ve veri kalitesi acisindan test etmektir. | README, REQUIRED ve bu belge ayni amaci soyler. | Depo ici belge eslesmesi. |
| 2 | Ilk kapsam sadece BTC inverse option olacaktir. | Collector ve backtest varsayilanlari `currency=BTC`, `kind=option` ile baslar. | Deribit instrument yaniti ve cikti dosyasinda BTC option enstrumanlari. |
| 3 | Baslangic butcesi $0; ucretli veri ust siniri $3-5 olacaktir. | Ucretli kaynak satin alma plan disinda kalir; ancak gerekce raporu olursa degerlendirilir. | Kaynak tablosunda ucretsiz/ucretli ayrimi ve maliyet notu. |
| 4 | Birincil veri kaynagi Deribit public API olacaktir. | Her fiyat/veri alani icin endpoint eslemesi vardir. | Endpoint URL, parametre ve ham ornek yanit saklanir. |
| 5 | Enstruman tanimi `public/get_instruments` ile alinacaktir. | BTC option aktif liste alinabilir; expired modu ayrica denenir. | Ham JSON ornegi, sorgu parametreleri ve zaman damgasi. |
| 6 | `public/get_instruments(expired=true)` tam tarihsel opsiyon zinciri arsivi sayilmayacaktir. | Belge ve veri planinda "recently expired only" siniri yazilir. | Resmi dokuman satiri veya ekran kaydi; eksik eski vadeler icin `UNVERIFIED` etiketi. |
| 7 | Canli bid/ask, order book, mark, IV, Greeks ve open interest icin `public/get_order_book` veya WebSocket ticker/book kanallari kullanilacaktir. | Tekil enstruman icin `best_bid_price`, `best_ask_price`, `mark_price`, Greeks ve OI alanlari dogrulanir. | Ham yanit + alan kontrolu. |
| 8 | `public/get_order_book` tarihsel defter arsivi degil, sorgu anindaki current snapshot olarak kullanilacaktir. | Gecmis dolum iddiasi icin tek basina kullanilmaz. | Snapshot dosya adinda `captured_at_utc` bulunur; backtestte `live-collected` veya `estimated` etiketi vardir. |
| 9 | Zaman hassas canli veri icin surekli REST polling yerine WebSocket subscription tercih edilecektir. | Collector tasarimi REST'i state sync/snapshot, WebSocket'i akis verisi icin ayirir. | Collector tasarim notu; subscription kanal listesi. |
| 10 | Gecmis trade backfill icin Deribit public trade endpointleri ve RiveChen referans yaklasimi arastirilacaktir. | Kucuk BTC option trade ornegi indirilebilir ve semasi dogrulanir. | Ham trade dosyasi, `trade_seq`/timestamp siralama ve dedup kontrolu. |
| 11 | Gecmis trade fiyati gerceklesen islem bilgisidir; gecmis bid/ask veya L2 dolum kaniti degildir. | Trade tabanli testler "official-historical" ama dolum/slippage icin "estimated" etiketi alir. | Sonuc raporunda veri kalite etiketi. |
| 12 | Gecmis mark verisi yalniz degerleme ve karsilastirma icin kullanilacaktir. | Entry/exit fill fiyati olarak mark kullanilmaz. | Backtest cikti alanlarinda `mark_price` ve `entry_price/exit_price` ayridir. |
| 13 | `public/get_mark_price_history` 5 dakikalik mark verisidir ve yalniz volatilite endeksi hesaplamasina katilan option subset'i icin mevcuttur. | Bos liste donen enstruman veri hatasi sayilmaz; kapsam siniri olarak kaydedilir. | Endpoint sonucu, bos/var ayrimi ve `data_quality_label`. |
| 14 | Underlying/index ve rejim verileri Deribit mum, historical volatility, DVOL ve delivery endpointleriyle desteklenecektir. | Sinyal ve settlement hesaplari icin ayni UTC zaman standardi kullanilir. | Endpoint yanitlari ve UTC normalize edilmis alanlar. |
| 15 | Alista ask, satista bid kullanilacaktir; mark fiyat yalniz valuation icindir. | Backtest motoru fiyat secimini pozisyon yonune gore yapar. | Test vektorleri: long entry ask, long exit bid; short entry bid, short exit ask. |
| 16 | Komisyon, kontrat birimi, settlement currency, expiry ve delivery PnL modele dahil edilecektir. | Ilk strateji raporu fee, spread cost, delivery price ve PnL ayrimini gosterir. | Resmi fee tablosu surumu + hesaplama testi. |
| 17 | Her sonuc veri kalitesiyle birlikte raporlanacaktir; browser/canli kabul testi yapilmadan PASS verilmeyecektir. | `live-collected`, `official-historical`, `third-party-L2`, `estimated`, `UNVERIFIED` etiketlerinden biri bulunur. | Sonuc raporu ve eksik kabul kontrolleri listesi. |
| 18 | Baseline rapor pencereleri 30/60/90/120/360 gun olacaktir. | Her pencere veri kapsami, trade sayisi ve eksik saatleri ayri gosterir. | Rapor metadata'si. |
| 19 | Baseline cozunurluk 1 saat olacaktir. | Normalized input timestamp'leri saatlik kovalar halinde tutulur. | Normalization testi ve ciktida `resolution=1h`. |
| 20 | Gecmis bid/ask yoksa fill tahmini ayrica etiketlenecektir. | Entry/exit trade fiyatindan hesaplanan muhafazakar spread/slippage + fee modeli `estimated` olarak raporlanir. | Backtest sonuc metadata'si ve execution model parametreleri. |

## Data plan

### Faz 0: katalog ve kaynak kilidi

- Mevcut `catalog/data-sources.md`, `catalog/data-dictionary.md` ve `catalog/research-sources.md` karar kaydi olarak korunur.
- Resmi Deribit dokuman linkleri kaynak listesine baglanir.
- Her dis kaynak icin rol ayrimi yapilir: fiyat/dolum, valuation, piyasa baglami, veya ucretli arsiv adayi.

Basari olcutu: kaynak tablosu ile bu plan arasinda celiski olmamasi.

### Faz 1: ucretsiz Deribit enstruman evreni

- `public/get_instruments` ile `currency=BTC`, `kind=option`, `expired=false` aktif BTC option evreni alinir.
- `expired=true` yalniz yakin vadesi bitmis enstruman kesiti olarak saklanir.
- Enstruman yasam dongusu icin `instrument.state.option.BTC` WebSocket kanali collector tasarimina eklenir.

Saklanacak minimum alanlar: `instrument_name`, `expiry_utc`, `strike`, `option_type`, `base_currency`, `quote_currency`, `settlement_currency`, `contract_size`, `min_trade_amount`, `tick_size`, `is_active`, `instrument_state`, `captured_at_utc`.

Kanit: ham JSON + normalize edilmis tablo + sorgu parametreleri.

### Faz 2: current quote ve live snapshot biriktirme

- Tekil enstruman icin `public/get_order_book` anlik resync/snapshot kaynagi olarak kullanilir.
- Surekli canli veri icin REST loop yerine WebSocket `ticker`/`book` abonelikleri kullanilir.
- Snapshotlar asla gecmise donuk L2 kaniti gibi etiketlenmez.

Saklanacak minimum alanlar: `timestamp_utc`, `instrument_name`, `best_bid`, `best_ask`, `best_bid_amount`, `best_ask_amount`, `mark_price`, `index_price`, `underlying_price`, `bid_iv`, `ask_iv`, `mark_iv`, `delta`, `gamma`, `vega`, `theta`, `open_interest`, `book_state`, `change_id`, `captured_at_utc`.

Kanit: ham snapshot + alan doluluk raporu + veri kalite etiketi `live-collected`.

### Faz 3: historical backfill

- `public/get_last_trades_by_instrument_and_time` veya ilgili currency/time trade endpointleriyle ucretsiz trade gecmisi denenir.
- RiveChen downloader sadece referans uygulama olarak incelenir; kopyalamadan once kucuk ornekle dogrulanir.
- Trade verisi bid/ask/L2 yerine gecmez.

Saklanacak minimum alanlar: `timestamp_utc`, `instrument_name`, `trade_id`, `trade_seq`, `direction`, `price`, `amount`, `contracts`, `iv`, `mark_price`, `index_price`.

Kanit: kucuk ornek dosya + sayfalama/dedup notu + kapsanan tarih araligi.

### Faz 4: valuation ve settlement

- `public/get_mark_price_history` yalniz desteklenen option subset'i icin 5 dakikalik mark serisi olarak kullanilir.
- Bos mark history sonucu desteklenmeyen enstruman icin beklenen durumdur; backtest hatasi sayilmaz.
- Vade sonu kapanis icin `public/get_delivery_prices` ve gerekiyorsa settlement/delivery endpointleri kullanilir.

Saklanacak minimum alanlar: `timestamp_utc`, `instrument_name`, `mark_price`, `delivery_price`, `expiry_utc`, `settlement_currency`, `data_quality_label`.

Kanit: mark subset sonucu, bos liste ornegi, delivery yaniti.

### Faz 5: backtest fiyatlama kurali

- Long option entry: ask.
- Long option exit: bid.
- Short option entry: bid.
- Short option exit: ask.
- Mark: unrealized valuation ve sanity check.
- Gecmis L2 yoksa slippage/dolum: `estimated`.

Kanit: backtest test vektoru + raporda fee, spread ve slippage ayrimi.

### Faz 6: ucretli veri karari

Ucretli L2/quote arsivi ancak su kosullarin tamami saglanirsa yeniden degerlendirilir:

- Ucretsiz trade/live snapshot testi stratejinin incelenmeye deger oldugunu gosterir.
- Eksik bilgi gercekten L2 veya tarihsel quote olmadan cozulemez.
- Kaynak toplam $3-5 sinirinda kalir.
- Satin alma oncesi kullanicidan acik onay alinir.

Kanit: karar notu, maliyet, veri kapsami, geri donus yolu.

## Resmi Deribit endpoint sinirlari

| Endpoint | Kullanilacak rol | Sinir |
|---|---|---|
| `public/get_instruments` | Aktif veya yakin expired instrument tanimi | `expired=true` tam tarihsel arsiv degil; resmi dokumanda recently expired instruments olarak tarif edilir. |
| `public/get_order_book` | Current order book snapshot ve resync | Gecmis order book veya L2 arsivi degil; response current best bid/ask, book levels ve timestamp tasir. |
| `public/get_mark_price_history` | 5 dakikalik historical mark valuation | Sadece volatility index hesaplamalarina katilan option subset'i icin vardir; digerlerinde bos liste donebilir. |
| `public/get_last_trades_by_instrument_and_time` | Gecmis trade backfill | Trade gerceklesmesini verir; gecmis quote, spread veya tum dolum kosullarini kanitlamaz. |
| `public/get_tradingview_chart_data` | Underlying/OHLC sinyal serisi | OHLC mumdur; opsiyon quote/dolum verisi yerine gecmez. |
| `public/get_delivery_prices` | Expiry/delivery PnL girdisi | Vade sonu delivery baglami verir; intraday entry/exit fiyat kaniti degildir. |

## Kanit ve etiket standardi

- `PASS`: yalniz ham veri, normalize cikti ve beklenen alan kontrolleri gorulduyse.
- `PARTIAL`: veri geldi ama alan, tarih araligi veya kapsam eksigi varsa.
- `UNVERIFIED`: statik varsayim, dokuman okuma veya uygulanmamis plan.
- `estimated`: gecmis quote/L2/dolum verisi olmadan modellenen slippage veya fill.
- `official-historical`: Deribit public historical trade/mark/delivery endpointinden gelen tarihsel veri.
- `live-collected`: collector calisirken kaydedilen live WebSocket veya snapshot verisi.
- `third-party-L2`: ucretli/harici L2 veya quote arsivi; kaynak, maliyet ve lisans notu zorunludur.

## Acik kararlar

- Ilk gercek stratejinin entry filtresi, strike secimi, vade secimi ve pozisyon boyutu henuz yazilmadi.
- Gecmis opsiyon evrenini 360 gun icin eksiksiz kurma seviyesi henuz dogrulanmadi; ucretsiz Deribit trade history bilinen enstrumanlar icin calisir, tam tarihsel quote/L2 zinciri saglamaz.
- Historical trade verisini saatlik normalize backtest girdisine ceviren pipeline henuz tamamlanmadi.

## Kaynak baglari

- Deribit API Documentation: https://docs.deribit.com/
- `public/get_instruments`: https://docs.deribit.com/api-reference/market-data/public-get_instruments
- `public/get_order_book`: https://docs.deribit.com/api-reference/market-data/public-get_order_book
- `public/get_mark_price_history`: https://docs.deribit.com/api-reference/market-data/public-get_mark_price_history
- Market Data Collection Best Practices: https://docs.deribit.com/articles/market-data-collection-best-practices
- CoinGlass Deribit Options: https://www.coinglass.com/options/Deribit
