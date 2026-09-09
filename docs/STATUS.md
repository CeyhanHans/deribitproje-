# Güncel durum — 2026-09-09

## Gerçek durum
Çalışan bir Python çekirdeği ve 24 görevlik uygulama planı var. Tam genel amaçlı backtest ürünü henüz yok. Önceki %55/%62 oranları eski tahminlerdir; güncel geniş kapsamın ölçüsü değildir.

Python 3.11+ gerektiren standart kütüphane kodu. Bu yayın hazırlığında 64/64 unittest geçti. Testlerin çoğu sentetik/mocked girdiler kullanır; tüm dönem gerçek veri kabulü yapılmadı.

## Mevcut kod
- ingestion/deribit_history.py: history host, bounded trade indirme.
- ingestion/historical_windows.py: saatlik trade barları ve dönem planları.
- ingestion/deribit_underlying.py: BTC-PERPETUAL kline proxy; doğrudan index OHLC değildir.
- ingestion/deribit_snapshot.py: mevcut order book snapshot; geçmiş defter değildir.
- ingestion/contract_catalog.py: listing/expiry filtreli aynı-strike/vade çift seçimi; 10 test eklendi.
- strategies/strategy_schema.py: çok-bacaklı tanım validasyonu.
- strategies/leg_matcher.py: eşleşen saatler ve veri kapsamı; stale fill yok.
- strategies/backtest_engine.py: tek pozisyon prototipi, maliyet/çıkış hesapları.

## Bilinen doğruluk açıkları — henüz düzeltilmedi
1. backtest_engine.py: exit tetiklenmezse exit_observations girişte kalır; yanlış final PnL riski.
2. trade_count=1; entry_rules motor tarafından değerlendirilmez. Tekrarlı işlem döngüsü yok.
3. deribit_history.py: min(timestamp)-1 sayfalaması aynı ms'de kalan trade'leri kaybedebilir.
4. Katalog: sıfır DTE expiry sınırı, metadata tutarlılığı, NaN/Inf, duplicate ve fallback vade kontrolü eksik.
5. Bar aggregation: dedup ve aynı timestamp deterministik sıra geliştirilmesi gerekir.
6. Açık pozisyon veri boşlukları, BTC ledger, settlement, sermaye/margin, genel seçim/sinyaller, rapor ve E2E entegrasyon eksik.

## Görev durumu
1–24 görevlerinin tümü PLANNED; hiçbiri bu paket kapsamında tamamlandı sayılmaz. Katalog prototipi görev3 için başlangıçtır, görev3'ün kabulü değildir.
1–20 temel ürün/QC/teslim; 21–24 ileri veri/execution/margin/strateji uzantıları.
Sonraki iş: Görev1 ortak arayüz ve baseline paketi. Uygulama ve dosya sahiplikleri orchestration/tasks içindedir.

## Veri kanıtı
reports/history-feasibility-2026-09-09.md küçük eski kontrat örneklerini belgeler; tüm tarih aralığı/evren kapsamını kanıtlamaz. Önceki 121497 metadata ve 25 gelecek vadeli kayıt sayıları eski ölçümdür, sabit değildir. Tam ham veri arşivi bu depoda yok; collector ile doğrulanmalı.
