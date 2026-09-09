# Görev 8 Teslimat Raporu — Genel Strateji ve Tarih Aralığı Yapılandırması

- **Görev:** 8 — Genel strateji ve tarih aralığı yapılandırması
- **Durum:** READY_FOR_REVIEW
- **Base HEAD:** ea62db2439f801de7ef247ffe3b6047f4dec33fa (Aktif: d804d811eca7ce589509d4fe8f5bdc2bc23b423d)
- **Baseline Manifest Hash:** `reports/baseline/manifest.json`
- **Önceki Görev Teslimleri:**
  - Görev 1..7: `reports/task-01/` .. `reports/task-07/` kabul edildi.

---

## 1. Uygulama Özeti ve Alınan Tasarım Kararları

### A. RunConfig JSON Parser (`strategies/strategy_schema.py`)
- **Dinamik UTC Tarih Ayrıştırma:** Tarihler kesinlikle hardcode edilmez. `parse_utc_timestamp_ms` fonksiyonu ISO 8601 (tam ISO, 'Z', UTC ofsetleri, 'YYYY-MM-DD' tarih dizeleri ve tamsayı milisaniye) girdilerini dinamik ayrıştırır.
- **Kanonik Dönem Örneği:** Mart 2024 – Mayıs 2025 dahil aralık `[2024-03-01T00:00:00Z, 2025-06-01T00:00:00Z)` -> `start_ms=1709251200000`, `end_ms=1748736000000` olarak tam dönüştürülür. Hem tekil `start_time`/`end_time` hem de `[start, end)` aralık dizeleri desteklenir.
- **Artık Gün (Leap Day) Desteği:** 2024 artık yılı `2024-02-29T08:00:00Z` (`1709193600000`) başarıyla ayrıştırılır. Artık yıl olmayan `2023-02-29` tarihi ise `StrategyValidationError` ile reddedilir.
- **Aynı Start/End ve Ters Tarih Koruması:** `start_time == end_time` veya `start_time > end_time` tespit edildiğinde `StrategyValidationError` fırlatılır.
- **Karar Çözünürlüğü:** `decision_resolution` olarak `1h` (veya integer `1`) ve `1d` (veya integer `24`) kabul edilir. Diğer değerler reddedilir.
- **Dayanak Varlık ve Sözleşme:** Yalnızca Deribit BTC ters opsiyonları (`underlying="BTC"`, `settlement_currency="BTC"`, `contract_type="inverse"`) desteklenir.
- **Sermaye ve Boyutlandırma (Sizing):**
  - Başlangıç sermayesi `initial_cash_btc` / `initial_capital_btc` kesin pozitif Decimal olarak saklanır; negatif, sıfır ve NaN girdiler reddedilir.
  - `sizing` için `fixed_quantity`, `premium_budget_btc` ve `capital_fraction` tipleri desteklenir. Miktar sıfır veya negatif olamaz.
- **Pozisyon Limiti:** `max_open_positions` pozitif tamsayı olmak zorundadır.
- **İcra Profili:** `execution_model_id` (`model_a_bid_ask`, `model_b_mid_slippage`, `model_c_trade_bar_close`), `fee_rate_amount`, `fee_cap_ratio` ve `slippage_btc` parametreleri tam ayrıştırılır.
- **Marjin Modeli Yetenek Kontrolü:** V1 kapsamında yalnızca `conservative_stress_reserve` ve `cash_secured` marjin modelleri geçerlidir. `deribit_portfolio_margin`, `cross_margin` veya `naked_short_unreserved` belirtildiğinde açıklayıcı bir `StrategyValidationError` ile reddedilir.
- **Veri Politikası:** `data_policy` (`allow_missing_bars`, `price_basis`) ayrıştırılır.

### B. StrategyLeg (1..N) ve Bacak İlişkileri
- **1..N Bacak Desteği:** Tek bacaklı opsiyonlardan çok bacaklı karmaşık yapılara (örn. straddle, strangle, iron condor) kadar tüm stratejiler desteklenir.
- **Yinelenen Bacak İsimleri Koruması:** Strateji içinde mükerrer bacak adı bulunduğunda (`duplicate leg name`) hata verilir.
- **Strike Seçicileri:** `exact_usd`, `atm`, `moneyness_percent`, `nearest_premium_usd`, `same_strike_as` tipleri eklendi.
- **Delta Yetenek Kontrolü (Capability Check):** V1 tarihsel işlem verilerinde Grekler ve ima edilen volatilite yüzeyi bulunmadığından, `delta_target` strike seçicisi açıklayıcı bir yetenek ret mesajıyla (`unsupported capability: delta strike selection ('delta_target') is not supported in V1...`) durdurulur.
- **Vade Seçicileri ve DTE Doğrulaması:** `exact_date`, `days_to_expiry`, `nearest_days_to_expiry`, `same_expiry_as` desteklenir. `DTE <= 0` veya `NaN` değerleri `StrategyValidationError` ile reddedilir.
- **Bacak İlişkileri (Relations):**
  - `same_expiry_as`: Başka bir bacağın vadesini referans alır. Kendi bacağını referans göstermesi veya var olmayan bacağı göstermesi engellenir.
  - `same_strike_as`: Başka bir bacağın kullanım fiyatını referans alır.
  - `strike_offset_usd`: Bacaklar arası strike farkını belirtir, sayısal ve sonlu olmak zorundadır.

### C. Entry Rule Registry ve Bilinmeyen Alan (Strict Field) Kontrolü
- **Entry Rule Registry:** Strateji tanımlarındaki giriş kuralları tanımlı kayıt defteri (`KNOWN_ENTRY_RULES`) üzerinden denetlenir (`enter_when_chain_has_required_legs`, `periodic_calendar_entry`, `dte_target_entry`, `signal_rule_entry`, `rsi_oversold_filter`). Registry dışındaki kurallar `StrategyValidationError` ile reddedilir.
- **Bilinmeyen Alanların Reddi:** RunConfig, Strategy, Leg, Selector, EntryRule, ExitRule sözlüklerinde beklenmeyen herhangi bir ekstra alan tespit edildiğinde `StrategyValidationError(f"unexpected field: {key!r}")` fırlatılır.

### D. Kanonik Konfigürasyon Dosyaları (`config/**`)
1. `config/backtest_straddle_2024_2025.json`: Mart 2024 – Mayıs 2025 dahil (`[2024-03-01T00:00:00Z, 2025-06-01T00:00:00Z)`), saatlik (`1h`), 10.0 BTC sermayeli, 1 kontrat ATM Straddle kanonik konfigürasyonu.
2. `config/backtest_strangle_daily.json`: 2024 2. yarıyıl (`[2024-07-01T00:00:00Z, 2025-01-01T00:00:00Z)`), günlük (`1d`), 5.0 BTC sermayeli OTM Strangle konfigürasyonu.
3. `config/schema_run_config.json`: RunConfig parametreleri için JSON Schema taslağı.

### E. `core.contracts.RunConfig` İle Çift Yönlü Uyumluluk
- `RunConfigDefinition.to_core_run_config()` ve `to_core_run_config(definition)` fonksiyonları ile parse edilen yapılandırma doğrudan dondurulmuş `core.contracts.RunConfig` nesnesine dönüştürülür.

---

## 2. Değişen Dosyalar ve SHA-256

```json
[
  {
    "path": "strategies/strategy_schema.py",
    "before_sha256": "15533f4cb7905139fa214f38e6f832a0bd5b71cb2465bddfcc75633c08ce144c",
    "after_sha256": "5192ec5c9714fddf841e32c3c2e53f5172ce4308d214ead515024284eae5b0b7"
  },
  {
    "path": "config/backtest_straddle_2024_2025.json",
    "before_sha256": null,
    "after_sha256": "1be2d08e698c9f2be62991a04c2b03a62e0f9acad9dda89d7ad5dc08c70104d4"
  },
  {
    "path": "config/backtest_strangle_daily.json",
    "before_sha256": null,
    "after_sha256": "949a5c946d1880e71066b60013b9ba74c370a85678c7c93e596e1f8e424f29f2"
  },
  {
    "path": "config/schema_run_config.json",
    "before_sha256": null,
    "after_sha256": "f8c43f3e0cd2c66417963740a7cfd2dd6323ed2745dd3edc6ab79e4cddd04a78"
  },
  {
    "path": "tests/test_strategy_schema.py",
    "before_sha256": "1f134196f0364301e2102fb4a1d8cd123abbe7a0613a4e629ad23f74d75ae9cb",
    "after_sha256": "4cdbe8652091f2ee71d62a0e64bafc837f6bd724c114e5bd3449114603cbb63e"
  },
  {
    "path": "tests/test_run_config.py",
    "before_sha256": null,
    "after_sha256": "1292ff48eb0cf13891a1b1605e424f142dc2e5a8125658da67703880f562fe32"
  }
]
```

---

## 3. Kesin Kabul Testleri ve Doğrulama

Tüm kesin kabul şartları `tests/test_run_config.py` ve `tests/test_strategy_schema.py` altında test edilmiş ve doğrulanmıştır:
1. **İki farklı tarih aralığı:** `test_parses_two_different_date_ranges` ile dinamik parse ve süre farkı kanıtlandı.
2. **Artık gün (Leap day):** `test_leap_day_parsing_and_invalid_leap_day` ile 2024-02-29 doğrulanıp 2023-02-29 reddedildi.
3. **Aynı start/end ve ters aralık:** `test_rejects_identical_and_inverted_start_end` ile test edildi.
4. **Bilinmeyen giriş kuralı:** `test_rejects_unknown_entry_rule` ile registry dışındaki kurallar engellendi.
5. **Yinelenen bacak:** `test_rejects_duplicate_leg_names` ile aynı isimli bacaklar reddedildi.
6. **Negatif / sıfır miktar:** `test_rejects_negative_or_zero_quantity` ile bacak ve sizing miktarları denetlendi.
7. **NaN girdiler:** `test_rejects_nan_inputs` ile sermaye, bacak, strike ve çıkış kurallarında NaN engellendi.
8. **Geçersiz DTE:** `test_rejects_invalid_dte` ile DTE <= 0 ve NaN reddedildi.
9. **Desteklenmeyen delta:** `test_rejects_unsupported_delta_capability` ile V1 veri sınırı yetenek ret mesajı doğrulandı.
10. **Desteklenmeyen marjin modeli:** `test_rejects_unsupported_margin_model` ile portföy marjini ve naked short reddedildi.
11. **Bilinmeyen alanlar:** `test_rejects_unknown_fields_in_config_and_schema` ile beklenmeyen alanlar reddedildi.
12. **Bacak ilişkileri & ATM:** `test_leg_relations_and_atm_selector` ile ATM seçici ve bacak ilişkileri test edildi.
13. **Kanonik konfigürasyon roundtrip:** `test_v1_canonical_configs_parse_and_roundtrip` ile kanonik dosyaların dondurulmuş `RunConfig` nesnesine dönüştüğü doğrulandı.

### Test Sonucu:
```
Ran 175 tests in 1.371s
OK
```
Önceki test sayısı 152'den 175'e çıkmış ve sıfır hata ile tamamlanmıştır.

---

## 4. Kalan Eksikler / Sonraki Adım
- Bu görev yazma sınırları dahilinde eksiksiz tamamlanmıştır.
- Bir sonraki görev: Görev 9 — Tekrarlayan işlem simülasyonu ve portfolio state geçişleri.
