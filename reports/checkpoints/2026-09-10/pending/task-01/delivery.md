# Görev 1 Teslim Raporu — Ortak Sözleşmeler ve Değiştirilemez Arayüzler (QC Re-audit 2026-09-10)

**Tarih:** 2026-09-10  
**Durum:** DRAFT / PENDING ORCHESTRATOR APPROVAL (QC Bulguları Düzeltildi)  
**Base Commit:** `61101f2` (cloned from `C:\Users\user\Documents\Codex\2026-09-08\tes\deribit-options-backtest-catalog`)  
**Test Durumu:** 19/19 Geçti (`tests.test_contracts_v1`) | 262/262 Geçti (Tüm Test Paketi)

---

## 1. QC Re-Audit (2026-09-10) Kapsamında Çözülen Bulgular

### G1-A: Kesin Tip Tanımlı Sözleşmeler ve Çıplak Any Açıklarının Kapatılması
- `HistoricalTradeProvider.fetch`: `plan: DownloadPlanProtocol -> DownloadResult` olarak kesin tiplendirildi (`Union[..., Any]` tamamen kaldırıldı).
- `InstrumentProvider.load`: `plan: DownloadPlanProtocol -> Sequence[Instrument]` olarak kesin tiplendirildi (`Any` tamamen kaldırıldı).
- `BacktestRunnerProtocol.__call__`: `data_bundle: DataBundleProtocol` ve `execution_config: ExecutionConfigProtocol` ile kesin tiplendirildi (`Union[..., Any]` tamamen kaldırıldı).
- `DownloadPlanProtocol` ve somut `DownloadPlan` dataclass'ı (`instrument_name`, `start_timestamp`, `end_timestamp`, `start_ms`, `end_ms`) tanımlandı.
- `StrategyLegProtocol` ve `StrategyLegSpec` (`name`, `option_type`, `side`, `quantity`) tanımlandı.
- `StrategyProtocol` ve `StrategySpec` (`name`, `legs`) tanımlandı.
- `HistoryAsOfProtocol` ve `HistoryAsOfView` (`decision_at_ms`, `completed_bars`, `candidate_instruments`) tanımlandı.
- `tests/test_contracts_v1.py` içerisine `inspect.signature` üzerinden reflection kontrolü ve protokol doğrulama testleri eklendi.

### G1-B: Yürütme Modeli (Execution Model) Sözlüğü ve Geriye Uyumluluk Geçiş Tablosu
- Kanonik modeller:
  - `MODEL_A_TRADE_ESTIMATED = "model_a_trade_estimated"` (Task 11 emriyle birebir uyumlu: trade-based estimated fill; karar anından önceki bar kapanışına geriye dönük eşleşme yasak).
  - `MODEL_B_BID_ASK_REPLAY = "model_b_bid_ask_replay"` (Tarihsel bid/ask replay; quote yoksa Model A'ya sessiz düşüş yasak).
- Eski legacy alias'lar korundu (`MODEL_A_BID_ASK`, `MODEL_B_MID_SLIPPAGE`, `MODEL_C_TRADE_BAR_CLOSE`, `MODEL_D_QUOTE_L2`).
- `EXECUTION_MODEL_MIGRATION_TABLE` ve `resolve_canonical_execution_model` yardımcı fonksiyonu eklendi; bilinmeyen modeller için `ValidationError` fırlatılır.
- `specs/contracts-v1.md` ve `specs/product-v1.md` dokümanlarında kanonik modeller ve geçiş tablosu açıkça belgelendi.

### G1-C: TickSchedule ve Ayrı Spread / Slippage Maliyetleri
- `TickScheduleStep` (`threshold_price_btc`, `tick_size_btc`) ve `TickSchedule` (`steps`, `default_tick_size_btc`, `get_tick_size()`) eklendi.
  - Basamakların pozitif, sıralı ve çakışmasız olması `ValidationError` ile güvenceye alındı.
- `Instrument` sınıfına `tick_schedule: Optional[TickSchedule] = None` alanı eklendi ve JSON roundtrip test edildi.
- `Fill` sınıfına `spread_cost_btc: Optional[Decimal] = None` ve `slippage_cost_btc: Optional[Decimal] = None` alanları eklendi.
  - İkisi birden sağlandığında toplamın `spread_slippage_cost_btc` ile birebir eşleşmesi zorunlu kılındı.

### G1-D: Modül Matrisi Uzlaştırması
- `specs/contracts-v1.md` Bölüm 2 matrisindeki `compute_metrics` fonksiyonu `engine/metrics.py` yerine Görev 15 yazma sınırı olan `reporting/metrics.py` ile birebir uzlaştırıldı.

---

## 2. Test Sonuçları

```
python -m unittest tests.test_contracts_v1 -v
Ran 19 tests in 0.053s
OK (failures=0, errors=0, skipped=0)
```

---

## 3. Değiştirilen Dosyalar ve SHA256 Değerleri

| Dosya | Durum | Güncel SHA256 |
|---|---|---|
| `core/__init__.py` | modified | `5dc3506554c200a040320a97e19c11c5584f24e7ffdabb30f08edb429a46c6b3` |
| `core/contracts.py` | modified | `bf59db8a790125ccfc6ecc20df94cb13616f4c0f1988e3145464f5944ea47783` |
| `specs/contracts-v1.md` | modified | `6a6329d77bbf4c6657d55916087fc02a4a77b3379c9dbb379537cfb6a40964e0` |
| `specs/product-v1.md` | modified | `4c9a08debea52cfa4e00d819382e7be6916bd1a8088be3e86ee6c5cac2739807` |
| `tests/test_contracts_v1.py` | modified | `ba676872136204b8f2b33d172ae3d33c6e625abd6caee8d9d98b2ce51fa888a6` |

*Not: Nihai kabul ana orkestratör onayına tabidir.*
