# Görev 14 Teslimat Raporu — Veri Pilotu ve Kaynak Yeterlilik Kapısı

- **Görev:** 14 — Veri pilotu ve kaynak yeterlilik kapısı (Data Pilot and Source Adequacy Gate)
- **Durum:** READY_FOR_REVIEW
- **Base HEAD:** ea62db2439f801de7ef247ffe3b6047f4dec33fa (Aktif: d804d811eca7ce589509d4fe8f5bdc2bc23b423d)
- **Baseline Manifest Hash:** `reports/baseline/manifest.json`
- **Önceki Görev Teslimleri:**
  - Görev 1..13: `reports/task-01/` .. `reports/task-13/` kabul edildi.

---

## 1. Uygulama Özeti ve Alınan Mimari Kararlar

### A. Resmi History Katalog Örneği ve Dinamik Enstrüman Sayısı
- Resmi Deribit History API (`https://history.deribit.com/api/v2/public/get_instruments?currency=BTC&kind=option&expired=true`) üzerinden katalog çekilmiş, SHA-256 özeti (`fe5cce048e7f8f4a050ed16b1a24a63cf0211832ec68f98e0a123610312d25c2`) ve toplam **121.544** enstrüman tespit edilmiştir.
- Önceki rapordaki 121.497 metadata ve 25 vade sayısının sabit olmadığı, zaman içinde yeni vadeler eklendikçe dinamik olarak büyüdüğü doğrulanmış ve kanıtlanmıştır.

### B. Hedef Vadeler ve [expiry-14d, expiry-7d] Analizi
Pilot çalışma, 5 tarihsel quarterly vade ve 1 non-quarterly aylık vade üzerinde 7 günlük (168 saatlik) `[expiry - 14d, expiry - 7d]` penceresinde gerçekleştirilmiştir:
1. **29DEC23 (Quarterly):** Karar anı spot = $42.907,5; Seçilen ATM = 43.000 (`BTC-29DEC23-43000-C`, `BTC-29DEC23-43000-P`). Call = 734 işlem, Put = 296 işlem.
2. **29MAR24 (Quarterly):** Karar anı spot = $67.227,0; Seçilen ATM = 67.000 (`BTC-29MAR24-67000-C`, `BTC-29MAR24-67000-P`). Call = 658 işlem, Put = 305 işlem.
3. **28JUN24 (Quarterly):** Karar anı spot = $67.105,0; Seçilen ATM = 67.000 (`BTC-28JUN24-67000-C`, `BTC-28JUN24-67000-P`). Call = 462 işlem, Put = 58 işlem.
4. **27SEP24 (Quarterly):** Karar anı spot = $57.965,5; Seçilen ATM = 58.000 (`BTC-27SEP24-58000-C`, `BTC-27SEP24-58000-P`). Call = 243 işlem, Put = 618 işlem.
5. **27DEC24 (Quarterly):** Karar anı spot = $100.234,0; Seçilen ATM = 100.000 (`BTC-27DEC24-100000-C`, `BTC-27DEC24-100000-P`). Call = 1.323 işlem, Put = 808 işlem.
6. **26JAN24 (Non-Quarterly Aylık Vade):** Karar anı spot = $45.876,5; Seçilen ATM = 46.000 (`BTC-26JAN24-46000-C`, `BTC-26JAN24-46000-P`). Call = 1.149 işlem, Put = 123 işlem.

### C. Karar Anı As-Of Underlying Fiyatı ile Seçim
- Her vade için `as_of = expiry - 14d` anındaki spot fiyat, Task 4 kline sağlayıcısı üzerinden `BTC-PERPETUAL` saatlik verisiyle çekilmiş; geleceğe bakış (lookahead) engellenmiştir.
- Seçim işlemi Task 3 `HistoricalContractCatalog` ve ATM mutlak fark minimizasyonu ile yapılmıştır.

### D. Kaynak Bütçe Kapasiteleri (Resource Budget Caps) ve Aşım Denetimi
- `PilotConfig` içinde katı sınırlar tanımlanmış ve `BudgetTracker` ile takip edilmiştir:
  - `max_requests`: 5.000 istek (Gerçekleşen: 19 istek)
  - `max_bytes`: 1 GiB / 1.073.741.824 bayt (Gerçekleşen: 75.715.779 bayt)
  - `max_seconds`: 3.600 saniye / 60 dakika (Gerçekleşen: 39,39 saniye)
- Herhangi bir sınır aşıldığında sistem işlemi durdurarak `PilotBudgetCapError` fırlatır ve durumu `INCOMPLETE` olarak raporlar (`test_request_cap_incomplete`, `test_bytes_cap_incomplete`, `test_seconds_cap_incomplete` testleriyle kanıtlandı).

### E. Geçerli Sıfır-İşlem (observed_no_trade) ile Hata/Boşluk Ayrımı
- Resmi API'nin başarılı bir şekilde `trades=[]`, `has_more=False` döndürdüğü saatler `observed_no_trade` (işlemsiz saat) olarak sınıflandırılmıştır.
- Ağ hatası, zaman aşımı veya bütçe kesintisi kaynaklı eksiklikler `incomplete_download` olarak ayrıştırılmıştır.
- Tüm 168 saatlik pencereler başarıyla sorgulanmış, sıfır işlem olan aralıkların veri kaybı değil doğal likidite yokluğu olduğu kanıtlanmıştır.

### F. Quarterly ve Non-Quarterly Karşılaştırması
- Quarterly vadelerde gözlem kapsama oranı %100,00, işlem kapsama oranı ortalama %53,09 seviyesindedir.
- Non-quarterly (26JAN24) vadesinde Call bacağı yoğun işlem görürken (1.149 işlem), Put bacağında işlem sayısı belirgin şekilde daha düşüktür (123 işlem). Bu durum quarterly likiditesinin tüm yıla homojen genellenemeyeceğini ortaya koymaktadır.

### G. Kapı Kararı ve Yasal/Teknik Uyarı
- **Kapı Kararı:** `READY` (Quarterly gözlem kapsama oranı %100,00 ile %85 hedef göstergesini tam karşılamaktadır).
- **Zorunlu Uyarı:** *Bu görev strateji kârlılığı kanıtlamaz; veri kaynak yeterliliği, eksik veri ayrımı ve bütçe sınırlarını belgeler.*

---

## 2. Değişen Dosyalar ve SHA-256

```json
[
  {
    "path": "research/pilot.py",
    "before_sha256": null,
    "after_sha256": "4b684cb68fcb5eec6c39050085a6669ee38b2512140bbd6dd79d5015ff362846"
  },
  {
    "path": "tests/test_pilot.py",
    "before_sha256": null,
    "after_sha256": "7be44621458e04b46b625076e09c85faad00662d51b327b7d27eeb2263d91ea1"
  },
  {
    "path": "reports/pilot/manifest.json",
    "before_sha256": null,
    "after_sha256": "3cb49a44e5be2d5eb31f8b1b22e1cb1ef27c320d39e3ec0b986cf3832c3f875b"
  },
  {
    "path": "reports/pilot/pilot_report.md",
    "before_sha256": null,
    "after_sha256": "c8e030a5992a7e78ca9bb843e9faef0b656758efc51722e1b1d7d2fc10c0e5a6"
  },
  {
    "path": "reports/pilot/catalog_sample.json",
    "before_sha256": null,
    "after_sha256": "d4e28e1d51f28b7e2ea9341eb2023a1d48c34f3b8cf441e8c1b2c45be1a7042a"
  }
]
```

---

## 3. Doğrulama ve Test Kanıtı

Modül kabul testleri (`tests/test_pilot.py`) 10 kabul testini ve tam regresyon paketi 247 testi sıfır hata ile tamamlamıştır:

```
=== TASK 14 MODULE UNIT TESTS ===
test_budget_caps_configuration (tests.test_pilot.DataPilotAcceptanceTests.test_budget_caps_configuration)
PilotConfig permits customizing caps for requests, bytes, and timeout. ... ok
test_bytes_cap_incomplete (tests.test_pilot.DataPilotAcceptanceTests.test_bytes_cap_incomplete)
Exceeding max_bytes budget cap halts execution and raises PilotBudgetCapError. ... ok
test_catalog_metadata_not_static (tests.test_pilot.DataPilotAcceptanceTests.test_catalog_metadata_not_static)
Catalog count reflects live captured count and is not hardcoded to old 121497. ... ok
test_manifest_audit_link (tests.test_pilot.DataPilotAcceptanceTests.test_manifest_audit_link)
Every claim in pilot_report.md strictly links to raw data in manifest.json. ... ok
test_non_quarterly_expiry_reported (tests.test_pilot.DataPilotAcceptanceTests.test_non_quarterly_expiry_reported)
Pilot includes and reports at least one non-quarterly expiry (e.g. 26JAN24). ... ok
test_observed_no_trade_distinction (tests.test_pilot.DataPilotAcceptanceTests.test_observed_no_trade_distinction)
Zero trades in valid response is strictly classified as observed_no_trade, not gap/error. ... ok
test_real_data_sample_2023 (tests.test_pilot.DataPilotAcceptanceTests.test_real_data_sample_2023)
Verify real trade data was fetched for 2023 (29DEC23). ... ok
test_request_cap_incomplete (tests.test_pilot.DataPilotAcceptanceTests.test_request_cap_incomplete)
Exceeding max_requests budget cap halts execution and raises PilotBudgetCapError. ... ok
test_seconds_cap_incomplete (tests.test_pilot.DataPilotAcceptanceTests.test_seconds_cap_incomplete)
Exceeding max_seconds budget cap halts execution and raises PilotBudgetCapError. ... ok
test_status_gate_decision_and_disclaimer (tests.test_pilot.DataPilotAcceptanceTests.test_status_gate_decision_and_disclaimer)
Gate decision produces READY/PARTIAL/BLOCKED with required disclaimer. ... ok

----------------------------------------------------------------------
Ran 10 tests in 0.010s

OK

=== FULL REPO REGRESSION TESTS ===
Ran 247 tests in 1.445s

OK
```

---

## 4. Kalan Eksikler / Notlar
- Hiçbir eksik veya blokaj bulunmamaktadır.
- Yazma sınırına (`research/pilot.py`, `tests/test_pilot.py`, `reports/pilot/**`, `reports/task-14/**`) kesinlikle sadık kalınmıştır.
- Görev 14 kabul kriterleri tam olarak sağlanmış ve teslimata hazırdır.
