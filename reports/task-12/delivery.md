# Görev 12 Teslimat Raporu — BTC Hesap Defteri ve Pozisyon Sermayesi

- **Görev:** 12 — BTC hesap defteri ve pozisyon sermayesi
- **Durum:** READY_FOR_REVIEW
- **Base HEAD:** ea62db2439f801de7ef247ffe3b6047f4dec33fa (Aktif: d804d811eca7ce589509d4fe8f5bdc2bc23b423d)
- **Baseline Manifest Hash:** `reports/baseline/manifest.json`
- **Önceki Görev Teslimleri:**
  - Görev 1..11: `reports/task-01/` .. `reports/task-11/` kabul edildi.

---

## 1. Uygulama Özeti ve Alınan Mimari Kararlar

### A. Protokol Sözleşmeleri ve Tip Doğrulaması (`portfolio/`)
- `apply_fill(state, fill) -> PortfolioState` fonksiyonu `core.contracts.FillApplicatorProtocol` kilitli Protocol sözleşmesini tam karşılar (`@runtime_checkable`).
- `settle(state, settlement) -> PortfolioState` fonksiyonu `core.contracts.SettlerProtocol` kilitli Protocol sözleşmesini tam karşılar (`@runtime_checkable`).
- `evaluate_equity(state, timestamp_ms, underlying_price_usd, benchmark_initial_btc, ...) -> EquityPoint` fonksiyonu portföy özkaynak durumunu tip doğrulamalı hesaplar.

### B. BTC Esaslı Nakit Defteri (Cash Ledger) ve Çift Taraflı Kayıt (Double-Entry)
- Hesap defteri kesin olarak BTC para birimi (`Currency.BTC`) esaslıdır; tüm primler, komisyonlar ve teslimat ödemeleri BTC cinsinden muhasebeleştirilir.
- **Çift Taraflı Kayıt ve Net Korunum (Double-Entry / Net Conservation):**
  - Her nakit hareketi için bir `LedgerEntry` üretilir (`PREMIUM`, `FEE`, `SETTLEMENT`, `RESERVE_ADJUSTMENT`).
  - Her girdide `balance_after == balance_before + amount` eşitliği kesin olarak korunur.
  - $\text{Başlangıç Nakdi} + \sum \text{Ledger Miktarları} = \text{Mevcut Nakit Dengesi}$ kuralı %100 sağlanır.
- **Yetersiz Bakiye Denetimi (Insufficient Capital):**
  - Long alışlarda `premium + fee > available_cash` durumunda işlem `InsufficientCapitalError` ile reddedilir.
  - Short yazımlarda `required_reserve > available_cash + net_premium` durumunda işlem `InsufficientCapitalError` ile reddedilir.

### C. Kur Ayrıştırması ve 4 Temel Muhasebe Metriği (`portfolio/valuation.py`)
Aynı BTC fiyatıyla yapılan işlemlerin farklı BTC/USD kurlarında farklı USD kârı üretmesi ve portföy nakit bakiyesindeki kur dalgalanması birbirinden kesin olarak ayrıştırılmıştır:
1. **Realized Trade PnL (BTC):** Kapanan/uzlaşılan işlemlerin BTC cinsinden net kazanç/kaybı.
2. **Realized Trade PnL (USD):** Kapanan işlemlerin kendi kapanış/uzlaşma anındaki BTC/USD kuruyla çevrilmiş net işlem kârı.
3. **Portfolio USD Değişimi (Başlangıçtan Beri):** Başlangıç BTC sermayesinin USD değerinden mevcut portföy USD özsermayesine olan toplam değişim (nakit üzerindeki kur etkisi dahil).
4. **Benchmark Buy-and-Hold (BTC / USD):** Başlangıç sermayesinin hiçbir işlem yapılmadan BTC olarak tutulması durumundaki değeri ($Q_{\text{init}} \times S_t$).

### D. Short Pozisyonlar ve V1 Conservative Stress Reserve Modeli
- **Açık Model Zorunluluğu:** Açık bir marjin/stres modeli bulunmayan (`margin_config=None` veya `allow_naked_short=False`) portföylerde short emirler kesinlikle reddedilir (`ShortPositionRejectedError`).
- **Belgeli Model (exchange_margin_equivalent=False):** V1 stres rezervi modeli (`conservative_stress_reserve`) resmi Deribit borsa marjini yerine geçen bir model değildir (`exchange_margin_equivalent=False`). Belirlenen oran (`stress_reserve_ratio`, varsayılan 0.5 BTC/kontrat) üzerinden nakit rezerve eder.
- **Rezerv Kilitlenmesi ve Çözülmesi:** Rezervler açılışta `reserved_balance_btc` alanına kilitlenir; yalnızca pozisyon kapandığında veya uzlaşma gerçekleştiğinde serbest bırakılır (`reserve release`).
- **Eksik Çıkış Durumu:** Çıkış verisi eksik olduğunda pozisyon silinmez, risk ve rezerv varlığı açık kalmaya devam eder.

### E. Risk İhlali (Risk Breach) Raporlaması ve Uydurma Likidasyon Yasağı
- Aşırı piyasa hareketlerinde short opsiyon yükümlülüğü (intrinsic liability) ayrılan stres rezervini aşarsa `check_risk_breaches` fonksiyonu ile `RiskBreach` kaydı üretilir.
- **Kesin Kural:** Resmi likidasyon mekanizması uydurulmaz; pozisyon yapay işlemlerle kapatılmaz, ihlal net bir risk uyarısı olarak raporlanır.

### F. Idempotency (Yinelenen İşlem Koruması)
- **Yinelenen Fill (Duplicate Fill):** Aynı fill ID'ye sahip bir dolum tekrar `apply_fill` fonksiyonuna verildiğinde nakit mükerrer düşülmez, defter kaydı yinelenmez; durum değiştirilmeden döner.
- **Settlement Tek Kez:** Aynı enstrüman ve vade için `settle` fonksiyonu birden fazla çağrıldığında ilk uzlaşma korunur, sonraki çağrılar idempotent olarak no-op döner.

---

## 2. Değişen Dosyalar ve SHA-256

```json
[
  {
    "path": "portfolio/__init__.py",
    "before_sha256": null,
    "after_sha256": "be2b94d9ca199b318494734820bd2960cfd960d3d91ca35580c7d19a67cd842d"
  },
  {
    "path": "portfolio/models.py",
    "before_sha256": null,
    "after_sha256": "499347d34a63a7850621f2529f1c6ed54a42e6cd3cdf30a56cea6ef90a443351"
  },
  {
    "path": "portfolio/ledger.py",
    "before_sha256": null,
    "after_sha256": "88a35831cdbf875c0d4054ff184b917dcdf646df25f1767f0d59ab8bbb038833"
  },
  {
    "path": "portfolio/settlement_service.py",
    "before_sha256": null,
    "after_sha256": "f38cb741801c8da6864b8d2ada49fc980c917fe2d24eb7954b611db3521b0b66"
  },
  {
    "path": "portfolio/valuation.py",
    "before_sha256": null,
    "after_sha256": "3f1f1e8095f754041cfa35e4ba3d4b7d329880006ed310ad1ee7ae0ffc34621f"
  },
  {
    "path": "tests/test_portfolio_v1.py",
    "before_sha256": null,
    "after_sha256": "d1e20c396bea57899152f7373803f90a67c990d6101e9777e037ab6af02d26dd"
  }
]
```

---

## 3. Doğrulama ve Test Kanıtı

Test paketi 11 yeni kabul testini ve toplam 225 regresyon testini hatasız tamamlamıştır:

```
python -m unittest tests.test_portfolio_v1 -v
test_double_entry_net_conservation ... ok
test_duplicate_fill_idempotency ... ok
test_insufficient_capital_long_rejected ... ok
test_insufficient_capital_short_reserve_rejected ... ok
test_missing_exit_preserves_position ... ok
test_protocol_conformance ... ok
test_same_btc_price_different_usd_rates_accounting_separation ... ok
test_settlement_single_execution_and_idempotency ... ok
test_short_rejected_without_explicit_margin_model ... ok
test_short_stress_reserve_lock_and_release ... ok
test_short_stress_risk_breach_reported_no_fake_liquidation ... ok

----------------------------------------------------------------------
Ran 11 tests in 0.004s
OK

python -m unittest discover -s tests -v
----------------------------------------------------------------------
Ran 225 tests in 1.427s
OK
```

---

## 4. Kalan Eksikler
- Yok. Görev 12 tüm kabul kriterleriyle eksiksiz tamamlandı.
