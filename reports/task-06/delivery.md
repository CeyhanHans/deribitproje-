# Görev 6 Teslimat Raporu — Underlying, Tarihsel Fiyatlandırma ve Resmi Settlement

- **Görev:** 6 — Underlying, tarihsel fiyatlandırma ve resmi settlement
- **Durum:** READY_FOR_REVIEW
- **Base HEAD:** d804d811eca7ce589509d4fe8f5bdc2bc23b423d
- **Baseline Manifest Hash:** `reports/baseline/manifest.json`
- **Önceki Görev Teslimleri:**
  - Görev 1: `reports/task-01/delivery.md` (kabul edildi)
  - Görev 2: `reports/task-02/delivery.md` (kabul edildi)
  - Görev 3: `reports/task-03/delivery.md` (kabul edildi)
  - Görev 4: `reports/task-04/delivery.md` (kabul edildi)
  - Görev 5: `reports/task-05/delivery.md` (kabul edildi)

---

## 1. Uygulama Özeti ve Kararlar

### A. Underlying Proxy ve Eksik Interval Analizi (`ingestion/deribit_underlying.py`)
- `DEFAULT_DATA_QUALITY_LABEL = "official-tradingview-perpetual-kline-proxy"`, `DEFAULT_INSTRUMENT = "BTC-PERPETUAL"`, `DEFAULT_INDEX_NAME = "btc_usd"` ve `DEFAULT_SERIES_TYPE = "perpetual_kline_proxy"` etiketleri korunmuştur.
- **Çoklu Chunk Birleştirme & Dedup:** `merge_and_dedup_bars` fonksiyonu ile ardışık veya örtüşen chunk'lar `timestamp_ms` üzerinden deduplicate edilmiş, tutarsız OHLC çelişkisi olduğunda `DeribitUnderlyingError` fırlatacak veri bütünlüğü denetimi eklenmiştir.
- **Eksik Interval Tespiti:** `find_missing_underlying_intervals` ve `compute_underlying_coverage` fonksiyonları ile `[start_timestamp, end_timestamp)` aralığındaki saatlik kontigü interval kapsamı ve eksik pencereler `UnderlyingCoverageReport` olarak hesaplanmaktadır.
- **Çoklu Chunk Çekme:** `fetch_underlying_series` ile otomatik planlanan chunk'lar sırayla çekilip birleştirilmektedir.

### B. Resmi Settlement ve Delivery Fiyatı (`ingestion/settlement.py`)
- `core.contracts.SettlementProvider` protokolüne tam uyumlu `DeribitSettlementProvider` yazılmıştır (`get(instrument, expiry_ms) -> Optional[SettlementObservation]`).
- **Resmi Kurallar & Birincil Kaynak Doğrulaması:**
  - Deribit 08:00 UTC expiry kuralı ve 30-dakikalık TWAP delivery price normalization fonksiyonu `parse_delivery_prices_response` yazılmıştır.
  - Leap-day (2024-02-29 08:00 UTC) desteği dahil UTC integer ms ile hatasız tarihleme doğrulanmıştır.
- **KRİTİK YASAK:** *"Delivery price yoksa perpetual close ile yerine koyma."*
  - Resmi delivery price bulunamadığında kesinlikle perpetual close veya başka bir proxy ikamesi yapılmaz. Eksik fiyatta `None` döner; zorunlu çağrıda `MissingDeliveryPriceError` fırlatır. `DeliveryPriceRecord(is_official_delivery=False)` oluşturma girişimleri reddedilir.
- **Kotasyon Olmasa da Settlement:** Enstrüman kotasyonu veya işlemi olmasa dahi (`test_settlement_provider_works_without_option_quote`), resmi underlying delivery fiyatı ile ITM/ATM/OTM BTC settlement değeri eksiksiz hesaplanır.
- **Inverse BTC Option Payoff:**
  Call = max(S - K, 0) / S (BTC) ve Put = max(K - S, 0) / S (BTC)
  - `calculate_inverse_option_payoff` ve `calculate_inverse_option_payoff_per_contract` fonksiyonları katı `Decimal` aritmetiği ve `validate_decimal` kontrolleriyle uygulanmıştır.
  - Negatif veya sıfır strike/delivery fiyatı, NaN, Infinity ve negatif miktar anında reddedilir.

### C. FeeSchedule ve Tarihsel Doğrulama Statüsü (`catalog/fees-btc-inverse.md` & `ingestion/settlement.py`)
- `FeeSchedule` veri yapısı: `schedule_id`, `effective_start_ms`, `effective_end_ms`, `trading_fee_rate`, `trading_fee_cap_ratio`, `delivery_fee_rate_standard`, `delivery_fee_rate_daily`, `delivery_fee_cap_ratio`, `verification_status`, `source_reference`.
- **Trading Fee:** Standart %0.03 (0.0003 BTC/kontrat), opsiyon priminin %12.5'i ile sınırlandırılmıştır (`premium_fee_cap`).
- **Delivery Fee:** Standart %0.015 (0.00015 BTC/kontrat), delivery değerinin %12.5'i ile sınırlandırılmıştır.
- **Günlük Vade Muafiyeti:** Günlük opsiyonlar için delivery fee kesinlikle `%0.0` (`Decimal("0.0")`) olarak uygulanmıştır.
- **Tarihsel Ücret Doğrulama Kuralı:** Deribit resmi destek sayfasından 2026-08-17 tarihi itibarıyla teyit edilen geçerli model `CONFIRMED` olarak kaydedilmiştir. 2026-08-17 öncesi tarihsel dönemler için birincil kanıt bulunmadığından ücretler geçmişe `CONFIRMED` taşınmamış; katı bir biçimde `CONFIGURED_UNVERIFIED` olarak işaretlenmiştir (`get_fee_schedule_for_timestamp`).

---

## 2. Değişen Dosyalar ve SHA-256

```json
[
  {
    "path": "catalog/fees-btc-inverse.md",
    "before_sha256": "bc5601cffad319aa21da4579443f614f8d45d8d159efa4789f4a96e24fddfb42",
    "after_sha256": "89e5b79586a6bcba77a2b18206bdf5441ed9f91cdd0e9b9bd86954cb95a14a53"
  },
  {
    "path": "ingestion/deribit_underlying.py",
    "before_sha256": "adbdc8b639604fe31dc875f738385838f6ee0cd326e43361d734d36a3093d1fe",
    "after_sha256": "d375f9275d0d4d4f2ed470a3ab5701a6dceb9d39433ba0af5b7e271aada31a6e"
  },
  {
    "path": "ingestion/settlement.py",
    "before_sha256": null,
    "after_sha256": "d97dedb8dc53144c9964a6706110fe41682f0791d4072bb7fbf2f0d16fb3367f"
  },
  {
    "path": "tests/test_deribit_underlying.py",
    "before_sha256": "4bc3f90e21041cf41944da71b72eaf3586f0cc63426b262a21a4798eb78e1528",
    "after_sha256": "513293b9e16e5b594df5ed7a368262f32fce6f9c7d4e4f1c230b9865e7d17d7b"
  },
  {
    "path": "tests/test_settlement.py",
    "before_sha256": null,
    "after_sha256": "7ec3c3341caf09f9edf88a8478949006adb708b1a4f82506b62c9b7faa750958"
  }
]
```

---

## 3. Test Sonuçları Kanıtı

- **Modül Testleri:**
  - `tests/test_deribit_underlying.py`: 22 test PASSED
  - `tests/test_settlement.py`: 14 test PASSED
- **Tüm Proje Regresyonu:**
  - `python -m unittest discover -s tests -v`: **140 test PASSED** (1.284s)
  - 0 failure, 0 error.

---

## 4. Kalan Eksikler ve Durum
- Hiçbir eksik veya atlanan kabul kriteri yoktur.
- Durum: **READY_FOR_REVIEW**
