# Görev 13 Teslimat Raporu — Çoklu İşlem Olay Motoru

- **Görev:** 13 — Çoklu işlem olay motoru (Chronological Multi-Trade Event Engine)
- **Durum:** READY_FOR_REVIEW
- **Base HEAD:** ea62db2439f801de7ef247ffe3b6047f4dec33fa (Aktif: d804d811eca7ce589509d4fe8f5bdc2bc23b423d)
- **Baseline Manifest Hash:** `reports/baseline/manifest.json`
- **Önceki Görev Teslimleri:**
  - Görev 1..12: `reports/task-01/` .. `reports/task-12/` kabul edildi.

---

## 1. Uygulama Özeti ve Alınan Mimari Kararlar

### A. Ortak Sözleşme Protokolü ve Tip Uyumu (`engine/`)
- `run(config, data_bundle) -> RunResult` fonksiyonu `core.contracts.BacktestRunnerProtocol` kilitli Protocol sözleşmesini tam karşılar (`@runtime_checkable`, `isinstance(run, BacktestRunnerProtocol) == True`).
- `DataBundle` sınıfı (`engine/data_bundle.py`) tarihsel enstrümanları, fiyat gözlemlerini (`PriceObservation`), ve vade uzlaşma verilerini (`SettlementObservation`) immutable tuple yapılarıyla paketler. `get_underlying_price_at(t)` metoduyla T anındaki as-of spot fiyatı güvenli ve anti-lookahead biçimde sunar.
- `compute_run_metrics(trades, equity_curve, initial_capital_btc)` (`engine/metrics.py`) tamamlanan pozisyonlar (roundtrip trades) üzerinden Sharpe, Sortino, Max Drawdown, Win Rate, Profit Factor, Net PnL metriklerini Decimal hassasiyetiyle üretir.

### B. Katı Kronolojik Döngü Sırası (Strict Chronological Loop Order)
UTC grid adımı $T$ üzerinde olay motoru her adımda şu kesin sırayı takip eder:
1. **Piyasa Verisi Görünürlüğü (Step 1):** Yalnızca `available_at_ms <= T` koşulunu sağlayan gözlemler görünür yapılır. Gelecek verisi veya karar anından sonraki barlar görünmez.
2. **Vade Sonu Uzlaşması (Step 2 - Expiry Settlement):** $T$ anında vadesi dolan enstrümanlar için `settle` fonksiyonu çağrılır. Çok bacaklı (multi-leg) pozisyonlarda her iki bacak için nakit uzlaşma (`SETTLEMENT` payoff) ve teslimat komisyonu (`FEE`) deftere işlenir, net kâr/zarar realized PnL'e yansıtılır.
3. **Önceki Uygun Emirlerin İşlenmesi (Step 3 - Fills with Anti-Lookahead):** Önceki adımlarda kuyruğa alınmış ve `eligible_after_ms <= T` olan emirler simüle edilir (`simulate_fill`). Karar anı barı ile aynı barda dolum engellenir (`sonraki eligible observation'da fill`). Her dolum için `apply_fill` çağrılarak nakit defteri ve pozisyon durumu güncellenir.
4. **Portföy Değerlemesi ve Çıkış Kararları (Step 4 - Valuation & Exit Decisions):** Portföy özkaynağı `evaluate_equity` ile hesaplanır. Eksik bar durumunda pozisyon korunur ve `is_valuation_reliable=False` (`valuation_unknown`) işaretlenir. Stop-Loss ve Take-Profit kararları bar-close net liquidation estimate ile tetiklenir; garantili eşik fiyatı uydurulmaz, sonraki adımdaki gerçek piyasa fiyatından dolum emri kuyruğa alınır (Gap pricing).
5. **Yeni Giriş Sinyalleri (Step 5 - New Entry Signals):** Portföyün açık pozisyon sayısı `max_open_positions` limitinin altındaysa sinyaller değerlendirilir. Karar anı $T$ için üretilen emirler `eligible_after_ms = T + 1` olarak işaretlenir.
6. **Veri Sonu Açık Pozisyon Politikası (Step 6 - Open Positions at End):** `end_ms` sınırına ulaşıldığında açık kalan pozisyonlar `open_positions_at_end` listesinde korunur; tamamlanmamış pozisyonlar kazanma/kaybetme işlem metriklerine ve trade_count'a kesinlikle dâhil edilmez.

### C. Roundtrip İşlem Muhasebesi (Single Trade Counting)
- Çok bacaklı pozisyonlar (ör. Long Straddle = Call + Put) tek bir `Position` nesnesidir ve kapanışta veya uzlaşmada tek bir `Trade` (roundtrip) sayılır. Bacaklar ayrı trade olarak sayılarak kazanma oranı yapay şişirilmez.

### D. Gap Stop-Loss ve Gerçek Dolum Kanıtı
- Sert fiyat boşluklarında (gap down), stop emri tetiklendiğinde garantili stop seviyesinden değil, sonraki adımda gerçekleşen gerçek piyasa fiyatından dolar. Örnek: Alış 0.08 BTC, Stop eşiği -0.0100 BTC; fiyat 0.02 BTC'ye düştüğünde çıkış 0.07'den değil, 0.02'den gerçekleşir ve gerçekleşen zarar -0.0606 BTC olur.

### E. Prefix Invariance ve Determinizm
- $[T_0, T_1]$ zaman aralığında koşan motorun ürettiği event log ve ledger kayıtları, $[T_0, T_2]$ ($T_2 > T_1$) aralığında koşan motorun çıktılarının birebir prefix'idir.
- Aynı konfigürasyon ve veri girdisiyle iki kez çalıştırıldığında konfigürasyon hash'i, veri hash'i, ledger girdileri, dolumlar ve metrikler %100 özdeş üretilir.

---

## 2. Değişen Dosyalar ve SHA-256

```json
[
  {
    "path": "engine/__init__.py",
    "before_sha256": null,
    "after_sha256": "c311f3511fabe380f73bc7f2ffacbf65b5b88979920d86900c10611a064da414"
  },
  {
    "path": "engine/data_bundle.py",
    "before_sha256": null,
    "after_sha256": "a06cf5d13e490262967f4e7755a9d664251a4fedb2a2e65c0f2747f765b8780c"
  },
  {
    "path": "engine/metrics.py",
    "before_sha256": null,
    "after_sha256": "101769a4ffab7cd038e7f868b5744a6931c10c833dd2f53657806afbd88a031f"
  },
  {
    "path": "engine/event_engine.py",
    "before_sha256": null,
    "after_sha256": "b8290684f7115de0951f3c7f15f85e6d6a3057ecd14bfa7cbbb0ff0ddaf58bdd"
  },
  {
    "path": "tests/test_event_engine.py",
    "before_sha256": null,
    "after_sha256": "7264e4f9d9328646185c0bb7df9aa34ae63703630a2df16f690f9e77e0f103ba"
  }
]
```

---

## 3. Doğrulama ve Test Kanıtı

Modül kabul testleri (`tests/test_event_engine.py`) 12 zorlu senaryoyu eksiksiz karşılamış ve tüm repo genelinde 237 test sıfır hata ile tamamlanmıştır:

```
=== TASK 13 MODULE UNIT TESTS ===
test_concurrent_two_open_positions (tests.test_event_engine.EventEngineAcceptanceTests.test_concurrent_two_open_positions)
Verify engine correctly holds 2 concurrent open positions when max_open_positions=2. ... ok
test_contract_roll_only_on_new_positions (tests.test_event_engine.EventEngineAcceptanceTests.test_contract_roll_only_on_new_positions)
Active open positions keep original strikes; contract roll occurs only when opening new position. ... ok
test_deterministic_reproducibility (tests.test_event_engine.EventEngineAcceptanceTests.test_deterministic_reproducibility)
Running the engine twice on identical inputs produces identical outputs and hashes. ... ok
test_fee_single_charge (tests.test_event_engine.EventEngineAcceptanceTests.test_fee_single_charge)
Every fill in the event loop charges fee exactly once on the ledger. ... ok
test_gap_stop_loss_fills_at_market_gap_price (tests.test_event_engine.EventEngineAcceptanceTests.test_gap_stop_loss_fills_at_market_gap_price)
Stop-loss triggered during a severe price gap fills at the actual gap price, not the threshold. ... ok
test_missing_put_bars_settlement_at_expiry (tests.test_event_engine.EventEngineAcceptanceTests.test_missing_put_bars_settlement_at_expiry)
If a put option had zero trade bars during intermediate hours, it settles cleanly at expiry. ... ok
test_multi_leg_straddle_is_one_trade (tests.test_event_engine.EventEngineAcceptanceTests.test_multi_leg_straddle_is_one_trade)
A multi-leg straddle (Call + Put) is counted as exactly 1 trade, not 2. ... ok
test_no_trade_run (tests.test_event_engine.EventEngineAcceptanceTests.test_no_trade_run)
When no signals trigger, engine returns a clean RunResult with 0 trades and matching hashes. ... ok
test_open_at_end_excluded_from_completed_metrics (tests.test_event_engine.EventEngineAcceptanceTests.test_open_at_end_excluded_from_completed_metrics)
Positions still open at end_ms remain in open_positions_at_end and are excluded from completed metrics. ... ok
test_prefix_invariance (tests.test_event_engine.EventEngineAcceptanceTests.test_prefix_invariance)
Running over [T0, T1] produces an event log and ledger that is an exact prefix of [T0, T2]. ... ok
test_protocol_conformance (tests.test_event_engine.EventEngineAcceptanceTests.test_protocol_conformance)
run function strictly satisfies BacktestRunnerProtocol. ... ok
test_three_consecutive_trades_hand_calculated (tests.test_event_engine.EventEngineAcceptanceTests.test_three_consecutive_trades_hand_calculated)
Execute at least 3 consecutive trades with hand-calculated PnL verification. ... ok

----------------------------------------------------------------------
Ran 12 tests in 0.012s

OK

=== FULL REPO REGRESSION TESTS ===
Ran 237 tests in 1.403s

OK
```

---

## 4. Kalan Eksikler / Notlar
- Hiçbir eksik veya blokaj bulunmamaktadır.
- Yazma sınırına (`engine/**`, `tests/test_event_engine.py`, `reports/task-13/**`) kesinlikle sadık kalınmıştır.
- Görev 13 kabul kriterleri tam olarak sağlanmış ve teslimata hazırdır.
