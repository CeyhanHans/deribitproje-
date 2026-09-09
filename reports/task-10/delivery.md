# Görev 10 Teslimat Raporu — Sinyal ve Takvim Motoru

- **Görev:** 10 — Sinyal ve takvim motoru
- **Durum:** READY_FOR_REVIEW
- **Base HEAD:** ea62db2439f801de7ef247ffe3b6047f4dec33fa (Aktif: d804d811eca7ce589509d4fe8f5bdc2bc23b423d)
- **Baseline Manifest Hash:** `reports/baseline/manifest.json`
- **Önceki Görev Teslimleri:**
  - Görev 1..9: `reports/task-01/` .. `reports/task-09/` kabul edildi.

---

## 1. Uygulama Özeti ve Alınan Tasarım Kararları

### A. Saf Fonksiyon ve `SignalEvaluatorProtocol` Uyumu (`strategies/signals.py`)
- **Protokol Sözleşmesi:** `evaluate_signals(config, history_asof, portfolio_view) -> Tuple[Signal, ...]` fonksiyonu `core.contracts.SignalEvaluatorProtocol` kilitli Protocol arayüzünü tam karşılar (`@runtime_checkable`).
- **Saf Fonksiyon İlkesi:** Ağ (network) çağrısı veya dosya yazma (file I/O) kesinlikle içermez; deterministiktir.
- **Tarihsel Bağlam Veri Yapısı:** `HistoryAsOf` veri sınıfı ile `decision_at_ms`, `completed_bars`, `current_bar`, `underlying_price_usd`, `candidate_decision` ve `candidate_instruments` alanları tip doğrulamalı olarak sunuldu.

### B. Anti-Lookahead, Prefix Invariance ve Incomplete Bar Dışlama
- **Prefix Invariance:** Gelecekteki barların ve fiyatların değişmesi, önceki zaman damgalarında üretilen sinyalleri asla değiştirmez.
- **Incomplete Bar Dışlama:** Karar anında `bucket_end > T` veya `available_at > T` olan hiçbir tamamlanmamış bar analize dahil edilmez. Bar kapanışları yalnız `bucket_end` anında bilinir ilkesine sıkı sıkıya uyulur.

### C. Takvim ve Zamanlama Motoru (Schedules)
- **UTC Daily:** Belirli UTC saatinde (varsayılan 08:00 UTC) tetiklenir (`daily_calendar_entry` / `schedule_type="daily"`).
- **UTC Weekly:** Belirli UTC gün ve saatinde (örn. Cuma 08:00 UTC) tetiklenir (`weekly_calendar_entry` / `schedule_type="weekly"`).
- **Every N Bars:** Belirli bar periyodunda bir tetiklenir (`every_n_bars` / `interval_bars`).
- **First Eligible Entry:** Koşullar sağlandığında ilk uygun barda giriş yapar (`enter_when_chain_has_required_legs`).

### D. Kapılar (Gates) ve Filtreler
1. **Concurrent Position Limit (Max-Open Gate):** Portföydeki aktif açık pozisyon sayısı `max_open_positions` sınırına ulaştığında yeni sinyal üretimi engellenir.
2. **Cooldown Gate:** Pozisyon açılış veya kapanışından sonra tanımlı bekleme süresi (`cooldown_hours`, `cooldown_bars`, `cooldown_ms`) geçene kadar yeni giriş sinyali engellenir.
3. **Warmup Gate:** Rolling realized volatility veya return filtresi için geçmiş bar sayısı yetersiz olduğunda (`completed_bars < warmup_bars`), giriş sinyali engellenir ve warmup durumu raporlanır.
4. **Rolling Realized Volatility Filtresi:** Yalnızca tamamlanmış barların log-getirilerinin standart sapmasından yıllıklandırılmış volatilite hesaplanır (`min_realized_vol` ve `max_realized_vol` sınırları).
5. **DTE Gate:** Aday enstrümanların vadesi `[min_dte_days, max_dte_days]` dışındaysa sinyal engellenir.
6. **Yinelenen Sinyal Koruması (Deduplication):** Aynı zaman damgasında (`decision_at_ms`) fonksiyon tekrar çağrıldığında deterministik `signal_id` taşır ve portföyde aynı anda açılmış pozisyon varsa tekrar sinyal üretmez.

### E. Desteklenmeyen Filtrelerin Açık Reddi
- Tarihsel verisi V1'de bulunmayan ima edilen volatilite (`implied_volatility`, `iv`) veya açık pozisyon (`open_interest`, `oi`) filtreleri istendiğinde sessizce varsayım yapmak yerine `UnsupportedFilterError` ile açıkça reddedilir.

---

## 2. Değişen Dosyalar ve SHA-256

```json
[
  {
    "path": "strategies/signals.py",
    "before_sha256": null,
    "after_sha256": "cf3676249056fbe7b7598c54b319cf1077347a53f28713da0166ff4c7d3be46b"
  },
  {
    "path": "tests/test_signals.py",
    "before_sha256": null,
    "after_sha256": "ecdc373496823fd2c2f9ba97a94acba1640bf42f04f65d792af543309f542b73"
  }
]
```

---

## 3. Kesin Kabul Testleri ve Doğrulama

Tüm kesin kabul şartları `tests/test_signals.py` altında uygulanmış ve doğrulanmıştır:
1. **Prefix Invariance:** `test_prefix_invariance` ile gelecekteki barlar ve fiyatlar mutasyona uğratıldığında dahi önceki zaman damgalarındaki sinyallerin %100 özdeş kaldığı kanıtlandı.
2. **Incomplete Bar Dışlama:** `test_incomplete_bar_excluded` ile devam eden tamamlanmamış bara aşırı uç fiyat verilmesine rağmen analize sokulmadığı doğrulandı.
3. **Warmup Yetersiz:** `test_warmup_insufficient` ile yeterli geçmiş bar yokken sinyalin engellendiği ve tamamlanınca üretildiği kanıtlandı.
4. **Weekly UTC Schedule:** `test_weekly_utc_schedule` ile haftalık sinyalin yalnız Cuma 08:00 UTC'de tetiklendiği doğrulandı.
5. **Cooldown Gate:** `test_cooldown_gate` ile kapanış sonrası bekleme süresince sinyalin engellendiği, süre dolunca açıldığı kanıtlandı.
6. **Concurrent Position Limit:** `test_concurrent_position_limit` ile açık pozisyon limiti tam test edildi.
7. **Aynı Timestamp Duplicate Koruması:** `test_same_timestamp_no_duplicate_signal` ile deterministik ID ve mükerrer sinyal engellemesi kanıtlandı.
8. **Desteklenmeyen Filtreler:** `test_rejects_unsupported_filters` ile IV ve OI filtrelerinin `UnsupportedFilterError` ile reddedildiği doğrulandı.
9. **Protokol Uyumu:** `test_protocol_conformance` ile `SignalEvaluatorProtocol` uyumu kanıtlandı.

### Test Sonucu:
```
python -m unittest discover -s tests -v
----------------------------------------------------------------------
Ran 196 tests in 1.336s

OK
```
Tüm 196 test eksiksiz ve sıfır hata ile tamamlanmıştır.

---

## 4. Kalan Eksikler / Sonraki Adım
- Görev 10 yazma sınırları dahilinde eksiksiz tamamlanmıştır.
- Bir sonraki görev: Görev 11 — Emir modeli ve deterministik fill simülasyonu.
