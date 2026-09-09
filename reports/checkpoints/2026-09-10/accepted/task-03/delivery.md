# Görev 3 Teslim Raporu — Tarihsel Kontrat Kataloğu ve ATM Seçimi (QC Re-audit 2026-09-10)

**Tarih:** 2026-09-10  
**Durum:** DRAFT / PENDING ORCHESTRATOR APPROVAL (QC Bulguları Düzeltildi)  
**Base Commit:** `61101f2` (cloned from `C:\Users\user\Documents\Codex\2026-09-08\tes\deribit-options-backtest-catalog`)  
**Test Durumu:** 22/22 Geçti (`tests.test_contract_catalog`) | 262/262 Geçti (Tüm Test Paketi)

---

## 1. QC Re-Audit (2026-09-10) Kapsamında Çözülen Bulgular

### G3-A: Doğrudan `ContractSpec` Constructor ve `dataclasses.replace` Doğrulaması
- `ContractSpec.__post_init__` içine `_parse_contract` ile birebir aynı tutarlılık kontrolleri eklendi:
  - Kontrat adındaki strike ile sayısal `strike` alanı eşleşmesi (`abs(strike - name_strike) <= 1e-4`).
  - Kontrat adındaki C/P eki ile `option_type` ('call' / 'put') eşleşmesi.
  - `underlying` alanının "BTC" olması ve kontrat adı ön eki ile eşleşmesi.
  - `creation_timestamp >= 0` kontrolü (negatif zaman damgaları kesin olarak reddedilir).
  - `dataclasses.replace` ile constructor atlatma girişimleri test edilerek (`strike=1.0`, `option_type='put'`, `underlying='ETH'`, `creation_timestamp=-1`) dördünün de `ContractCatalogError` ile reddedildiği doğrulandı.

### G3-B: Sıkı Deribit BTC Inverse Opsiyon Para Birimi Kuralları
- Kanonik para birimleri zorunlu kılındı:
  - `base_currency`: `"BTC"`
  - `quote_currency`: `"BTC"`
  - `counter_currency`: `"USD"`
  - `settlement_currency`: `"BTC"`
- `_parse_contract` içinde ters kombinasyon (`quote=USD, counter=BTC`), yanlış quote veya yanlış counter kombinasyonları kesin olarak `ContractCatalogError` ile reddedildi.
- `row()` test fikstürü kanonik `quote_currency="BTC"` ve `counter_currency="USD"` olarak güncellendi.
- `test_strict_currency_validation_and_rejection_g3_b` testi ile ret yolları doğrulandı.

### G3-C: Taşınabilir Gerçek Pilot Kayıt Regresyonu
- `tests/test_contract_catalog.py` içerisine 4 adet gerçek Deribit API pilot kaydı (`BTC-15JUL16-600-C`, `BTC-15JUL16-600-P`, `BTC-15JUL16-610-C`, `BTC-15JUL16-610-P`) doğrudan gömüldü (`REAL_DERIBIT_PILOT_SAMPLES`).
- `test_real_deribit_pilot_sample_records_parsed_successfully` testi güncellendi:
  - Harici dosya (`reports/pilot/catalog_sample.json`) bulunamadığında gömülü kayıtlara yedeklenir.
  - Test ASLA `skipTest` çağrısı yapmaz, temiz kopyalarda 0 atlama (skipped=0) ile çalışır.

---

## 2. Test Sonuçları

```
python -m unittest tests.test_contract_catalog -v
Ran 22 tests in 10.119s
OK (failures=0, errors=0, skipped=0)
```

---

## 3. Değiştirilen Dosyalar ve SHA256 Değerleri

| Dosya | Durum | Güncel SHA256 |
|---|---|---|
| `ingestion/contract_catalog.py` | modified | `7c11e309ac2ce1e01c035af71a7de50700a48441f3726abb4b9bf7d8396906f5` |
| `tests/test_contract_catalog.py` | modified | `f97a496dd1709641a71c8fcdb3945bc90301ac0b64313e1eedb61e893c8d5810` |

*Not: Nihai kabul ana orkestratör onayına tabidir.*
