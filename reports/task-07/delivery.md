# Görev 7 Teslimat Raporu — Bar Üretimi ve Veri Kapsamı

- **Görev:** 7 — Bar üretimi ve veri kapsamı
- **Durum:** READY_FOR_REVIEW
- **Base HEAD:** d804d811eca7ce589509d4fe8f5bdc2bc23b423d
- **Baseline Manifest Hash:** `reports/baseline/manifest.json`
- **Önceki Görev Teslimleri:**
  - Görev 1..6: `reports/task-01/` .. `reports/task-06/` kabul edildi.

---

## 1. Uygulama Özeti ve Kararlar

### A. Bar Üretimi, Sıralama, Dedup ve Adaptör (`ingestion/historical_windows.py`)
- **Çözünürlük Desteği:** UTC `[start, end)` aralığında saatlik (`1h`, `HOUR_MS=3600000`) bar üretiminin yanında günlük (`1d`, `DAY_MS=86400000`) agregasyon eklendi (`SUPPORTED_RESOLUTIONS`).
- **trade_seq ve trade_id Dedup:** `clean_and_dedup_trades` fonksiyonu ile:
  - Aynı zaman damgasındaki işlemler `trade_seq` artan sırasına göre deterministik dizilir.
  - `trade_id` üzerinden tam deduplication yapılarak mükerrer işlemler elenir ve kontrat hacmi (`volume_contracts`) şişirilmez.
- **Bar Zamanlaması ve OHLC Kapanışı:**
  - Her bara `first_trade_at`, `last_trade_at` ve `available_at` alanları eklendi.
  - OHLC kapanışı yalnız `bucket_end` anında bilinir kuralına tam uyularak `available_at = bucket_end` atandı.
- **PriceObservation Adaptörü:** `trade_bar_to_price_observation` fonksiyonu ile `HourlyTradeBar` nesneleri `core.contracts.PriceObservation` tipine dönüştürülür.
- **Sıfır / Negatif / NaN / Inf Koruması:** `price <= 0`, `amount <= 0`, `NaN` veya `Inf` değerleri `HistoricalWindowError` fırlatılarak reddedilir.

### B. Çok Bacaklı Eşleştirme, Takvim Grid'i ve Kapsam (`strategies/leg_matcher.py`)
- **Tek ve N Bacak Desteği:** `match_hourly_trade_bars` artık hem 1 bacak (tek bacak) hem 2 bacak hem de 4+ bacak (örn. Iron Condor) eşleştirmeyi destekler (`min_legs=1` varsayılan).
- **Geriye Dönük Uyumluluk:** Eski 2+ bacak zorunluluğunu korumak için `match_two_plus_legs` uyumluluk fonksiyonu sağlandı.
- **Eksik Saatleri Düşürmeme & Takvim Grid'i:** Açık pozisyondaki eksik saatlerin risk hesabında kaybolmaması için tüm `[window_start, window_end)` aralığı boyunca `calendar_grid` (`CalendarBucket`) ve `missing_events` (`MissingIntervalEvent`) üretilir.
- **Veri Boşluk Nedenleri:** `GapReason` sınıfı ile boşluklar ayrıştırıldı: `observed_no_trade`, `not_listed`, `incomplete_download`, `malformed`.
- **Bağımsız Denominator:** Her bacağın `LegCoverage` nesnesi kendi `eligible_hours`, `observed_hours`, `missing_hours` ve `coverage_ratio` değerlerini bağımsız hesaplar.
- **Eşzamanlı Quote Yanılgısı Yasağı:** Aynı saat içindeki farklı trade zamanları (`first_trade_at`, `last_trade_at`) korunur; farklı dakikalarda gerçekleşen işlemler eşzamanlı kotasyon diye etiketlenmez (`has_simultaneous_trades`).
- **CoverageReport Adaptörü:** `to_contract_coverage_reports` fonksiyonu ile `core.contracts.CoverageReport` nesnelerine tam dönüştürme sağlandı.

---

## 2. Değişen Dosyalar ve SHA-256

```json
[
  {
    "path": "ingestion/historical_windows.py",
    "before_sha256": "b22a6ced974d7fae783a25738be4fa04605892c10b56f8f3979ea26cf889ba1e",
    "after_sha256": "9202ef4311559eec2e880f3bbeccd1fc857d5f2fc43cf55bf0214839e469fe94"
  },
  {
    "path": "strategies/leg_matcher.py",
    "before_sha256": "7c4af0614737f0859974a2700647b52f4a91c8dd45e29ffe21e267fc0395ed6c",
    "after_sha256": "d8d7ed5255369905178825e04abe3e450b1d029d043d19fd847a741f951fc494"
  },
  {
    "path": "tests/test_historical_windows.py",
    "before_sha256": "78ec2c3aa3f66c7b7cf513487c96b165fe37076792a28f428e849263ba1db2fd",
    "after_sha256": "b11a2e76bf85c6198863f249fc270d272cc0fb6563ec46a1b37ff3c8ba7ebd4a"
  },
  {
    "path": "tests/test_leg_matcher.py",
    "before_sha256": "69ee801d383c96ab949706c3ec75b0562076d25c51c462d61dc2879de70f0c81",
    "after_sha256": "95b2335986deab622219826ed9fbcb138162083a8b1eb1827bcd5a00bc319315"
  }
]
```

---

## 3. Test Sonuçları Kanıtı

- **Modül Testleri:**
  - `tests/test_historical_windows.py`: 11 test PASSED
  - `tests/test_leg_matcher.py`: 15 test PASSED
- **Tüm Proje Regresyonu:**
  - `python -m unittest discover -s tests -v`: **152 test PASSED** (1.398s)
  - 0 failure, 0 error.

---

## 4. Kalan Eksikler ve Durum
- Hiçbir eksik veya atlanan kabul kriteri yoktur.
- Durum: **READY_FOR_REVIEW**
