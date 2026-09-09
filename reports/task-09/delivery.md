# Görev 9 Teslimat Raporu — Genel Çok Bacaklı Kontrat Seçimi

- **Görev:** 9 — Genel çok-bacaklı kontrat seçimi
- **Durum:** READY_FOR_REVIEW
- **Base HEAD:** ea62db2439f801de7ef247ffe3b6047f4dec33fa (Aktif: d804d811eca7ce589509d4fe8f5bdc2bc23b423d)
- **Baseline Manifest Hash:** `reports/baseline/manifest.json`
- **Önceki Görev Teslimleri:**
  - Görev 1..8: `reports/task-01/` .. `reports/task-08/` kabul edildi.

---

## 1. Uygulama Özeti ve Alınan Tasarım Kararları

### A. Genel Çok Bacaklı `select_legs` ve `LegSelectorProtocol` Uyumu (`strategies/selection.py`)
- **Protokol Sözleşmesi:** `select_legs(strategy, asof_view, open_position=None) -> SelectionDecision` imzası, `core.contracts.LegSelectorProtocol` kilitli Protocol arayüzünü tam karşılar (`@runtime_checkable` doğrulaması geçer).
- **1..N Bacak Desteği:** Yalnızca 2 bacaklı straddle'a kilitli kalmayıp; tek bacak, straddle, strangle, call debit spread, put debit spread ve 4 bacaklı iron condor dahil olmak üzere 1..N bacaklı tüm stratejileri genel olarak seçer.
- **Katalog ve AsOf Durumu:** `AsOfView` veri yapısı ile anlık `decision_at_ms`, `underlying_price_usd`, `instruments`, `observations_by_instrument`, `tie_break` ve `open_position` parametreleri tam doğrulanır.

### B. Anti-Lookahead ve Zamanlama Değerlendirme Kuralları
- **Kesin Uygunluk Sınırı:** Bir kontratın değerlendirmeye girebilmesi için `creation_ms <= T < expiry_ms` ve (varsa) `available_at_ms <= T` koşullarını sağlaması zorunludur.
- **Future Strike Koruması:** `T` karar anından sonra listelenmiş veya oluşturulmuş (`creation_ms > T`) hiçbir enstrüman seçilemez (spot'a daha yakın olsa bile elenir).
- **Zero DTE / Sona Ermiş Kontrat Koruması:** `expiry_ms <= T` olan kontratlar kesinlikle hariç tutulur.

### C. Strike ve Vade Seçicileri ile Bacak İlişkileri
- **Strike Seçicileri:**
  - `exact_usd`: Belirtilen kesin kullanım fiyatını arar.
  - `atm`: Spot fiyata en yakın strike'ı seçer.
  - `moneyness_percent`: `spot * (percent / 100)` hedef strike'ına en yakın kontratı bulur.
  - `same_strike_as`: Başka bir bacağın seçilen strike'ını tam referans alır.
  - `strike_offset_usd`: Temel strike üzerine artı/eksi fark uygular.
- **Vade Seçicileri ve Aday Vade Fallback:**
  - `exact_date`, `days_to_expiry`, `nearest_days_to_expiry`, `same_expiry_as` desteklenir.
  - Vade sıralaması: Hedef DTE'ye en yakın vadeler denenir. Eğer tercih edilen vadede tüm bacaklar için geçerli aday bulunamazsa (örn. bacaklardan biri eksikse), bir sonraki aday vadeye geri çekilir (fallback).

### D. Delta Yetenek ve Gözlem Kontrolü (Missing Delta)
- **Güvenilir Gözlem Zorunluluğu:** V1 kapsamında tarihsel işlem verilerinde Grekler bulunmadığından; eğer strateji `delta_target` isterse ve `T` anında güvenilir kotasyon/delta gözlemi mevcut değilse, seçim kesinlikle iptal edilir ve `ReasonCode.DATA_MISSING` / `ReasonCode.NO_CANDIDATE_FOUND` ile aday izi metadata'ya yazılır.

### E. Determinizm ve Girdi Permütasyon Bağımsızlığı
- **Eşitlik Çözümü (Tie-Break Policy):** İki strike fiyata eşit uzaklıktaysa `tie_break == "lower"` küçük strike'ı, `tie_break == "higher"` büyük strike'ı seçer.
- **Kanonik Permütasyon Değişmezliği:** Sıralama anahtarı `(dist, directional, expiry_ms, instrument_name)` olarak belirlendiğinden, girdi listesi nasıl karıştırılırsa karıştırılsın (permutation / shuffle), her defasında birebir aynı enstrümanlar deterministik olarak seçilir.

### F. Açık Pozisyon Sabitliği (Position Instrument Invariance)
- **Saatlik Fiyat Değişiminde Sabit Kalma:** Bir pozisyon açıldıktan sonra piyasa spot fiyatı nereye giderse gitsin (+%20, -%20 vb.), pozisyonun bacakları vadeye/kapanışa kadar sabit kalır; açık pozisyon saat başı yeni ATM'ye kaydırılmaz (`position_status="held_open"`, `immutability_preserved="true"`).

### G. Çift Yok / Eksik Bacak Durumu
- Çok bacaklı stratejide herhangi bir bacak için uygun kontrat bulunamazsa, kısmi seçim yapılmaz; `selected_instruments = ()` ve `reason_code = ReasonCode.NO_CANDIDATE_FOUND` döner.

---

## 2. Değişen Dosyalar ve SHA-256

```json
[
  {
    "path": "strategies/selection.py",
    "before_sha256": null,
    "after_sha256": "51c54f999b633be61322a4d1df3c84716d1471fa71085197c2ece1a925013513"
  },
  {
    "path": "tests/test_selection.py",
    "before_sha256": null,
    "after_sha256": "48c605ed2ac7b132d9a713410c799793f378ccc28ad3438e782bef962828c18f"
  }
]
```

---

## 3. Kesin Kabul Testleri ve Doğrulama

Tüm kesin kabul testleri `tests/test_selection.py` altında uygulanmış ve doğrulanmıştır:
1. **Long straddle metadata seçimi:** `test_long_straddle_fixture_selection` ile ATM Call + Put aynı strike ve vadede seçildi.
2. **Strangle metadata seçimi:** `test_strangle_fixture_selection` ile OTM Call (105%) ve OTM Put (95%) seçildi.
3. **Call debit spread seçimi:** `test_call_debit_spread_fixture_selection` ile Long ATM Call + Short OTM Call seçildi.
4. **Put debit spread seçimi:** `test_put_debit_spread_fixture_selection` ile Long ATM Put + Short OTM Put seçildi.
5. **Iron condor seçimi:** `test_iron_condor_fixture_selection` ile 4 bacaklı kanat ve gövde kontratları başarıyla seçildi.
6. **Future strike koruması:** `test_future_strike_exclusion` ile `creation_ms > T` olan enstrümanların elendiği kanıtlandı.
7. **Missing delta koruması:** `test_missing_delta_observation` ile gözlem eksikliğinde seçimin `NO_CANDIDATE_FOUND` / `DATA_MISSING` verdiği ve güvenilir delta sağlandığında seçildiği kanıtlandı.
8. **Çift yok (No matching pair):** `test_no_matching_pair_found` ile bacaklardan biri eksik olduğunda seçimin iptal edildiği doğrulandı.
9. **Girdi permütasyon determinizmi:** `test_input_permutation_determinism` ile 25 farklı rastgele katalog karıştırmasında her defasında özdeş sonuç alındığı doğrulandı.
10. **Pozisyon sabitliği:** `test_open_position_instruments_remain_immutable_despite_spot_change` ile spot sert hareket etse bile açık pozisyon enstrümanlarının sabit kaldığı kanıtlandı.
11. **Tie-break doğrulaması:** `test_tie_break_lower_vs_higher` ile eşit uzaklıktaki strike'ların 'lower' ve 'higher' politikalarına tam uyduğu doğrulandı.
12. **Protokol uyumu:** `test_protocol_conformance` ile `LegSelectorProtocol` uyumu kanıtlandı.

### Test Çıktısı:
```
python -m unittest discover -s tests -v
----------------------------------------------------------------------
Ran 187 tests in 5.193s

OK
```
Tüm 187 test eksiksiz ve sıfır hata ile tamamlanmıştır.

---

## 4. Kalan Eksikler / Sonraki Adım
- Görev 9 yazma sınırları dahilinde eksiksiz tamamlanmıştır.
- Bir sonraki adım: Görev 10 — Tekrarlayan işlem simülasyonu ve pozisyon durum geçişleri.
