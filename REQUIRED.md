# Gerekenler Durumu

Bu liste, her yeni bilgi sonrası güncellenir. Tik yalnızca kanıtı görülen maddeye atılır.

- [x] Proje amacı: opsiyon stratejilerini gerçek para kullanmadan mantık/maliyet açısından test etmek.
- [x] Veri bütçesi: başlangıçta $0; gerekirse en fazla $3–5.
- [x] İlk ürün kapsamı: BTC inverse opsiyon.
- [x] Birincil veri kaynağı: Deribit kamu API'si.
- [x] Yardımcı piyasa bağlamı kaynağı: CoinGlass Deribit Options sayfası.
- [x] Enstrüman listesini alma isteği belirlendi: `public/get_instruments`, `currency=BTC`, `kind=option`, `expired=true`.
- [x] Deribit API'den ilk doğrulanmış enstrüman yanıtı alındı: test ve canlıda 56 yakın-vadeli BTC opsiyonu.
- [x] Aktif sözleşme evreni için `expired=false` production JSON doğrulandı: 2026-09-08T22:14:44Z production response, 908 BTC inverse option, `state=open`, `is_active=true`, `settlement_currency=BTC`.
- [x] Ücretsiz geçmiş trade backfill adayı belirlendi: Deribit History API + RiveChen referans downloader.
- [x] Backtest tarih aralığı ve zaman çözünürlüğü seçildi: BTC inverse için ayrı ayrı 30/60/90/120/360 gün ve ilk sürümde 1 saatlik değerlendirme çözünürlüğü.
- [ ] İlk stratejinin giriş, çıkış, vade ve pozisyon büyüklüğü kuralları yazılacak.
- [x] BTC inverse standard trading fee formülü sabitlendi ve regression test edildi; hesap/VIP belirsizlikleri parametrik not edildi.
- [ ] Delivery fee ayrımı ve delivery basis kuralı ayrı doğrulanacak.
- [x] Bid/ask tabanlı veri toplama düzeni kuruldu: tek snapshot public collector eklendi; toplam 30 test PASS kanıtı görüldü.
- [x] Canlı production snapshot doğrulandı: 2026-09-08T22:29Z `public/get_order_book`, `BTC-10SEP26-78500-P`, `state=open`, `testnet=false`, bid 0.008 BTC, ask 0.009 BTC, mark 0.0086 BTC, underlying yaklaşık 78,500 USD, greeks/OI mevcut. 2026-09-08T22:34Z production `BTC-10SEP26-78500-C` Call snapshot da doğrulandı: bid 0.008 BTC, ask 0.009 BTC, mark 0.0085 BTC, `state=open`, `testnet=false`. Aynı strike/vade Call+Put eşleşmesi artık tamam; bu tarihsel seri değildir.
- [x] İlk küçük geçmiş trade örneği indirildi ve şeması doğrulandı: Deribit resmi public History API ile `BTC-27JUN25-100000-C` için 3 trade, sequence 1-3 ve 7 test PASS kanıtı görüldü. 2026-09-09'da production `BTC-10SEP26-78500-C` için 30 günlük pencerede sınırlı canlı örnek de doğrulandı: 9 trade, 4 adet 1 saatlik bar.
- [x] Historical collector varsayılan host'u `history.deribit.com` olarak düzeltildi ve regression test edildi.
- [x] RiveChen yaklaşımından bounded adapter entegre edildi: varsayılan `max_pages=1` ve `count<=1000` sınırı uygulanıyor.
- [x] Eski option trade retrieval bounded feasibility kanıtlandı: 2024-2025 döneminden 8 örnek C/P kontratta trade çekimi PROVEN.
- [ ] 360 günlük tam sözleşme evreni henüz PROVEN değil.
- [x] BTC-PERPETUAL hourly kline proxy collector eklendi ve bağımsız audit ile doğrulandı; bu direct `btc_usd` index OHLC değildir. 360 gün önceki 3 saatlik pencere 4 bar döndürdü. 30/60/90/120 gün planı tek bounded request, 360 gün planı bulk indirme yapmadan 2 contiguous chunk (`5000h+3640h`) olarak doğrulandı; toplam 46 test PASS.
- [x] Genel yapılandırılabilir çok-bacaklı strateji şeması eklendi.
- [x] İlk backtest motoru çekirdeği eklendi: call/put, long/short, çok bacak, net USD stop-loss, net USD take-profit, time exit, expiry exit, tahmini slippage ve komisyon desteği test edildi.
- [x] Stage 2 strict 2+ leg hourly matcher eklendi: stale carry-forward yok, explicit skip-bar var, eligible-hour denominator kullanılıyor, liquidity coverage ratio ve per-leg missing/observed metrikleri üretiliyor, duplicate/misaligned/instrument mismatch girdileri reddediliyor; 8 matcher testi ve toplam 54 test PASS.
- [ ] Direct historical `btc_usd` index OHLC veri hattı gerekiyorsa ayrıca doğrulanacak.
- [ ] Full 360 günlük underlying/index download ve completeness henüz PROVEN değil.
- [ ] Recurring event loop, entegrasyon akışı ve uçtan uca çalışma doğrulanacak.
- [ ] İlk gerçek strateji tanımı ve sonuç raporu oluşturulacak.
- [ ] GitHub oturumu açılacak, depo oluşturulacak ve yerel katalog yüklenecek.
- [ ] Proje kapanışı veya limit daralması halinde ikinci beyin özeti güncellenecek.
