# Görev 11 Teslimat Raporu — Açık Fill Modeli ve Maliyet Senaryoları

- **Görev:** 11 — Açık fill modeli ve maliyet senaryoları
- **Durum:** READY_FOR_REVIEW
- **Base HEAD:** ea62db2439f801de7ef247ffe3b6047f4dec33fa (Aktif: d804d811eca7ce589509d4fe8f5bdc2bc23b423d)
- **Baseline Manifest Hash:** `reports/baseline/manifest.json`
- **Önceki Görev Teslimleri:**
  - Görev 1..10: `reports/task-01/` .. `reports/task-10/` kabul edildi.

---

## 1. Uygulama Özeti ve Mimari Kararlar

### A. Protokol Sözleşmesi ve Tip Doğrulaması (`execution/`)
- `simulate_fill(order, eligible_observations, execution_config, fee_schedule) -> FillDecision` fonksiyonu, `core.contracts.FillSimulatorProtocol` kilitli Protocol sözleşmesini tam karşılar (`@runtime_checkable`, `isinstance(simulate_fill, FillSimulatorProtocol) == True`).
- Model ve durum veri sınıfları:
  - `ExecutionConfig`: Model türü, tick boyutu, sözleşme boyutu, min_qty, qty_step, derinlik denetimi, fallback anahtarı ve maliyet senaryosu parametrelerini doğrular. `RunConfig` veya `dict` üzerinden de şeffaf şekilde üretilebilir.
  - `MultiLegFillDecision`: Çok bacaklı emir paketleri için All-Or-None sonucunu ve ayrı bacak dolumlarını taşır.
  - `CostScenarioConfig`: 3 standart maliyet senaryosu (Optimistic, Baseline, Pessimistic) için parametreleri barındırır.

### B. İki Temel Dolum Modeli (Model A ve Model B)
1. **Model A (Trade-Based Estimated):**
   - Karar anından (`decision_at_ms`) önceki bar kapanış fiyatını (`interval_end_ms <= decision_at_ms`) kullanmaz; emrin uygun olduğu andan (`eligible_after_ms`) sonraki ilk uygun trade fiyatını veya sonraki bar açılışını referans alır.
   - Zaman hassasiyeti etiketlemesi: Gerçek trade zaman damgası varsa saklanır (`tick_resolution`); yalnızca bar zaman damgası biliniyorsa `bar_resolution` olarak etiketlenir.
   - Fiyatlama: Referans trade fiyatı üzerine spread yarı payı (`spread_btc / 2`) ve kayma (`slippage_btc`) eklenir/çıkarılır, ardından tick yuvarlama uygulanır.
2. **Model B (Historical Bid/Ask Replay):**
   - Alışlar Ask fiyatından (`Side.BUY -> ask_price_btc`), satışlar Bid fiyatından (`Side.SELL -> bid_price_btc`) dolum simüle edilir.
   - **Spread ve Slippage Ayrı Parametre:** Model B'de teklif (quote) zaten piyasa spread'ini içerdiğinden spread tekrar eklenmez; yalnızca yapılandırılan kayma (`slippage_btc`) eklenir/çıkarılır. Böylece çifte sayım (double-counting) kesinlikle önlenir.
   - **Otomatik Fallback (A'ya Düşme):** Geçerli bir alış için ask veya satış için bid kotasyonu bulunamadığında, sistem otomatik olarak Model A mantığına (ilgili gözlemin trade verisine) düşer ve `source_ref` alanında `fallback_model_a` notunu saklar.

### C. Ters (Adverse) Tick Yuvarlama ve Boyut Denetimleri
- **Tick Rounding (Adverse Rounding):**
  - Alış yönünde yukarı tavan yuvarlama (`Side.BUY` -> `math.ceil(price / tick_size) * tick_size`).
  - Satış yönünde aşağı taban yuvarlama (`Side.SELL` -> `math.floor(price / tick_size) * tick_size`, minimum 0.0 BTC).
- **Miktar Kısıtları (min_qty ve qty_step):**
  - `order.quantity < min_qty` durumunda `MIN_QTY_VIOLATION` ile emir reddedilir.
  - `order.quantity % qty_step != 0` durumunda `QTY_STEP_VIOLATION` ile emir reddedilir.
- **Tek İşlem Fiyatı ve Derinlik (Oversized Qty):**
  - Tek bir işlem fiyatı kotasyon derinliğinin kanıtı değildir.
  - Model B'de `order.quantity > ask_size` (alış) veya `order.quantity > bid_size` (satış) olduğunda emir `OVERSIZED_QTY` ile reddedilir.
  - **Kısmi Dolum (Partial Fill) V1'de Desteklenmez:** Kısmi dolum v1 sürümünde kesin olarak devre dışıdır (`allow_partial_fill=True` yapılandırıldığında `UnsupportedPartialFillError` fırlatılır).

### D. Ücret Ayrıştırması ve Deribit Kuralı (`FeeSchedule`)
- İşlem ücreti (`fee_btc`) ile spread/kayma maliyeti (`spread_slippage_cost_btc`) `Fill` nesnesinde ayrı alanlarda raporlanır.
- Deribit ters opsiyon kuralına uygun olarak:
  $$\text{fee} = \min(q \times \text{contract\_size} \times 0.0003, q \times \text{contract\_size} \times \text{price} \times 0.125)$$
  Derin OTM ve ucuz opsiyonlarda %12.5 prim tavanı (cap) uygulanır.

### E. Çok Bacaklı Emirler (Multi-Leg All-Or-None Model)
- Çok bacaklı yapılarda (straddle, strangle, spreads) `simulate_multi_leg_fill` fonksiyonu kullanılır.
- **All-Or-None İlkesi:** Bacaklardan herhangi biri dolmazsa (veri eksikliği, kotasyon yokluğu, yetersiz derinlik, süre aşımı), tüm paket reddedilir ve açıkta bacak bırakılmaz.
- **Ayrı Bacak Zaman Damgaları:** Tüm bacaklar dolduğunda her bacağın kendi gerçekleşme zaman damgası (`actual_ms`), enstrüman adı ve fill ID'si bağımsız olarak raporlanır.

### F. 3 Maliyet Senaryosu ve Monotoniklik Kanıtı (`execution/scenarios.py`)
- Üç standart senaryo tanımlandı:
  1. `optimistic`: 0 tick slippage, 0 tick spread, `calibrated=False`.
  2. `baseline`: 1 tick slippage (0.0005 BTC), 1 tick spread (0.0005 BTC), `calibrated=False`.
  3. `pessimistic`: 3 tick slippage (0.0015 BTC), 3 tick spread (0.0015 BTC), `calibrated=False`.
- **Sabit Emir Listesi Monotonikliği:** Aynı sabit emir listesi üzerinden artan maliyet net PnL'yi artırmaz:
  $$\text{Net PnL}(\text{Optimistic}) \ge \text{Net PnL}(\text{Baseline}) \ge \text{Net PnL}(\text{Pessimistic})$$
  Bu kural `verify_monotonic_fixed_orders` ile doğrulanır ve test edilir.
- **Dinamik Yeniden Üretimde Non-Monotonic Sonuç:** Maliyetler değiştiğinde sinyal/çıkış koşulları (örn. stop-loss) yeniden değerlendirildiğinde erken stop olan kötümser senaryonun daha sonraki piyasa çöküşünden korunarak iyimser senaryodan daha az zarar edebileceği testle kanıtlanmıştır.

---

## 2. Değişen Dosyalar ve SHA-256

```json
[
  {
    "path": "execution/__init__.py",
    "before_sha256": null,
    "after_sha256": "ad4f98527164976ae407fb18015021f8c081b15a25a91077140644b379cdff65"
  },
  {
    "path": "execution/models.py",
    "before_sha256": null,
    "after_sha256": "ab2a29580709d181e8017b42ec2fbb1ff6e62147f57ec6acb1740546d70f34e8"
  },
  {
    "path": "execution/fill_simulator.py",
    "before_sha256": null,
    "after_sha256": "ff0597ccd5294e0f4e5211a7c53779643310c7acac6b1515b395a84e25c2823c"
  },
  {
    "path": "execution/scenarios.py",
    "before_sha256": null,
    "after_sha256": "8907fce0ed6896573df60d15c43e1a976b11a611648e17841b1e654d07cadaeb"
  },
  {
    "path": "tests/test_execution_v1.py",
    "before_sha256": null,
    "after_sha256": "45695de4530045896f6ed6a9360dac78b176a33d219f2a332b7cc2be13fd8844"
  }
]
```

---

## 3. Doğrulama ve Test Kanıtı

Test paketi 18 yeni kabul testini ve toplam 214 regresyon testini hatasız tamamlamıştır:

```
python -m unittest tests.test_execution_v1 -v
test_anti_lookahead_prior_bar_close_excluded ... ok
test_dynamic_regeneration_can_break_monotonicity ... ok
test_fee_cap_on_deep_otm_cheap_option ... ok
test_fee_decomposition_and_deribit_cap ... ok
test_four_trade_directions_model_b ... ok
test_max_wait_expiry_rejection ... ok
test_min_qty_rejection ... ok
test_missing_quote_and_missing_trade_data_rejection ... ok
test_missing_quote_automatic_fallback_to_model_a ... ok
test_monotonic_pnl_on_fixed_order_list ... ok
test_multi_leg_all_or_none_failure_on_single_leg_miss ... ok
test_multi_leg_all_or_none_success ... ok
test_oversized_qty_rejected_partial_fill_unsupported ... ok
test_partial_fill_configuration_strictly_unsupported ... ok
test_protocol_conformance ... ok
test_qty_step_violation_rejection ... ok
test_three_scenarios_calibrated_false_default ... ok
test_tick_rounding_rules ... ok

----------------------------------------------------------------------
Ran 18 tests in 0.007s
OK

python -m unittest discover -s tests -v
----------------------------------------------------------------------
Ran 214 tests in 1.445s
OK
```

---

## 4. Kalan Eksikler
- Yok. Görev 11 tüm kabul kriterleriyle eksiksiz tamamlandı.
