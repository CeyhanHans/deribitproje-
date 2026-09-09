# Deribit Options Backtest Engine — Data Contracts and System Specification (v1.0)

**Document Version:** 1.0  
**Date:** 2026-09-09  
**Status:** FROZEN / IMMUTABLE BASELINE  
**Associated Code:** [`core/contracts.py`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py)

---

## 1. Executive Summary & Design Principles

This specification establishes the authoritative, immutable contract schemas and protocols for the Deribit BTC Inverse Options Backtest Catalog. All downstream tasks (Tasks 2 through 24) must consume and produce these exact data types and follow the locked module interfaces.

### Core Architectural Axioms
1. **Schema Versioning:** `schema_version = "1.0"`. Any version mismatch will be strictly rejected.
2. **Financial Arithmetic Integrity:** All monetary quantities (strikes, prices, balances, fees, equity) MUST use Python's `decimal.Decimal` in memory and string-encoded decimals in JSON serialization (e.g., `"0.0003"`, `"90000.0"`). `NaN`, `+Infinity`, `-Infinity`, and boolean types passed where numbers are expected are rejected with `ValidationError`.
3. **Temporal Invariants & Lookahead Prevention:**
   - All timestamps are UTC integer milliseconds (`ms`) $\ge 0$.
   - Time ranges follow half-open intervals `[start_ms, end_ms)`.
   - `PriceObservation.available_at_ms >= PriceObservation.observed_at_ms`.
   - `OrderIntent.eligible_after_ms >= OrderIntent.decision_at_ms`.
   - Bar close prices are strictly available only at `bucket_end_ms`. Retroactive back-fills to past bars are strictly prohibited.
4. **Data Completeness vs. Price Reality:** Missing data is never treated as price `0.0`. Explicit quality statuses (`complete_download`, `observed_no_trade`, `not_listed`, `incomplete_download`, `malformed`) separate liquidity absences from download errors.
5. **Standard-Library Only:** Core contracts require zero external runtime dependencies.

---

## 2. Locked Module Responsibility Matrix

The following function signatures and protocols are locked for downstream module implementation:

| Module / Task | Responsible File | Function Signature / Protocol | Input Types | Output Type |
|---|---|---|---|---|
| **Task 3 / 9** | `strategies/selection.py` | `select_legs(strategy, asof_view)` | `Union[StrategyProtocol, Mapping], Union[AsOfViewProtocol, Mapping]` | [`SelectionDecision`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py) |
| **Task 10** | `strategies/signals.py` | `evaluate_signals(config, history_asof, portfolio_view)` | `Union[RunConfig, Mapping], Union[HistoryAsOfProtocol, Sequence[PriceObservation], Mapping], PortfolioState` | `Tuple[Signal, ...]` |
| **Task 11** | `execution/fill_simulator.py` | `simulate_fill(order, eligible_observations, execution_config, fee_schedule)` | `OrderIntent, Sequence[PriceObservation], Optional[Union[ExecutionConfigProtocol, RunConfig, Mapping]], Optional[Union[FeeScheduleProtocol, Mapping]]` | [`FillDecision`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py) |
| **Task 12** | `portfolio/ledger.py` | `apply_fill(state, fill)` | [`PortfolioState`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py), [`Fill`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py) | [`PortfolioState`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py) |
| **Task 6 / 12** | `portfolio/settlement_service.py` | `settle(state, settlement)` | [`PortfolioState`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py), [`SettlementObservation`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py) | [`PortfolioState`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py) |
| **Task 13 / 16** | `engine/event_engine.py` / `strategies/backtest_engine.py` | `run(config, data_bundle)` | [`RunConfig`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py), `Union[DataBundleProtocol, Mapping, Any]` | [`RunResult`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py) |
| **Task 13 / 15** | `engine/metrics.py` | `compute_metrics(result)` | [`RunResult`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py) | [`Metrics`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py) |
| **Task 15** | `reporting/manifest.py` | `write_report(result, path)` | [`RunResult`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py), `Union[Path, str]` | [`ReportManifest`](file:///C:/Users/user/Documents/Codex/2026-09-08/tes/deribit-options-backtest-catalog/core/contracts.py) |

---

## 3. Data Dictionary and Invariant Specifications

### 3.1 Enumerations
* **`OptionType`**: `"call"`, `"put"`.
* **`Side`**: `"buy"`, `"sell"`.
* **`Currency`**: `"BTC"`, `"USD"`.
* **`PriceBasis`**: `"trade"`, `"bid"`, `"ask"`, `"mid"`, `"mark"`, `"index"`, `"settlement"`, `"synthetic_proxy"`.
* **`DataQuality`**:
  * `complete_download`: Data successfully downloaded and verified.
  * `observed_trade`: Real market trade observed in bucket.
  * `observed_no_trade`: Continuous market existed, but zero trades occurred.
  * `not_listed`: Contract was not yet listed or already expired at $T$.
  * `incomplete_download`: Source data missing or pagination severed.
  * `malformed`: Corrupted data rejected by validation.
* **`ExecutionModelId`**:
  * `model_a_bid_ask`: Crosses the historical bid/ask spread.
  * `model_b_mid_slippage`: Mid-price execution with parameterised slippage.
  * `model_c_trade_bar_close`: Fills against trade bar close with liquidity checks.
  * `model_d_quote_l2`: Full L2 order-book queue simulation.
* **`EntryType`**: `"premium"`, `"fee"`, `"settlement"`, `"deposit"`, `"withdrawal"`, `"reserve_adjustment"`.
* **`PositionStatus`**: `"open"`, `"pending"`, `"closed"`, `"settled"`, `"unresolved"`.
* **`ReasonCode`**: `"entry_signal"`, `"stop_loss"`, `"take_profit"`, `"time_expiry"`, `"contract_expiry"`, `"market_close"`, `"data_missing"`, `"no_candidate_found"`, `"stress_reserve_exceeded"`, `"unspecified"`.
* **`TieBreakPolicy`**: `"lower"`, `"higher"`.

---

### 3.2 Data Classes

#### 1. `RunConfig`
* `strategy_name: str` — Identifier of strategy to execute. Non-empty string.
* `start_ms: int` — Start timestamp in UTC milliseconds (inclusive).
* `end_ms: int` — End timestamp in UTC milliseconds (exclusive). Invariant: `start_ms < end_ms`.
* `initial_capital_btc: Decimal` — Initial cash allocated in BTC. Must be $> 0$.
* `schema_version: str` — Defaults to `"1.0"`. Incompatible versions raise `SchemaVersionMismatchError`.
* `decision_resolution_hours: int` — Decision cadence: `1` (hourly) or `24` (daily).
* `execution_model_id: ExecutionModelId` — Execution fill simulation engine.
* `fee_rate_amount: Decimal` — Base fee per contract (Deribit default `0.0003` BTC). $\ge 0$.
* `fee_cap_ratio: Decimal` — Maximum fee cap as a fraction of premium (Deribit default `0.125` = 12.5%). $\ge 0$.
* `slippage_btc: Decimal` — Configured slippage per trade in BTC. $\ge 0$.
* `stress_reserve_ratio: Decimal` — Conservative margin reserve requirement for short options. $\ge 0$.
* `strategy_params: Tuple[Tuple[str, str], ...]` — Immutable key-value pairs for strategy configuration.
* `deterministic_seed: int` — Seed ensuring repeatable simulations.

#### 2. `Instrument` (alias `Contract`)
* `instrument_name: str` — Deribit symbol, e.g. `"BTC-27DEC24-90000-C"`.
* `option_type: OptionType` — `OptionType.CALL` or `OptionType.PUT`.
* `strike_usd: Decimal` — Strike price in USD. Must be $> 0$.
* `creation_ms: int` — Listing timestamp in UTC ms.
* `expiry_ms: int` — Contract expiration timestamp in UTC ms. Invariant: `creation_ms < expiry_ms`.
* `base_currency: Currency` — Base asset (`Currency.BTC`).
* `quote_currency: Currency` — Premium quote currency (`Currency.BTC` for Deribit inverse options, or `Currency.USD`).
* `counter_currency: Currency` — Strike/counter currency (`Currency.USD`).
* `settlement_currency: Currency` — Settlement currency (`Currency.BTC`).
* `contract_size: Decimal` — Number of BTC underlying per contract (standard is `1.0`).
* `min_qty: Decimal` — Minimum order size in contracts (e.g. `0.1`).
* `qty_step: Optional[Decimal]` — Minimum quantity increment. If unknown, must be `None`.
* `qty_step_known: bool` — Flag indicating whether `qty_step` is verified.
* `tick_size: Decimal` — Price tick size in BTC (default `0.0005`).
* `source_ref: str` — Provenance reference or catalog JSON file identifier.

#### 3. `TradeTick`
* `instrument_name: str` — Instrument traded.
* `trade_id: str` — Unique exchange trade ID.
* `trade_seq: int` — Monotonically increasing sequence number $\ge 0$.
* `timestamp_ms: int` — Execution timestamp in UTC ms.
* `price_btc: Decimal` — Trade price in BTC $> 0$.
* `quantity_contracts: Decimal` — Size in contracts $> 0$.
* `source_ref: str` — History file or download batch ID.

#### 4. `PriceObservation`
* `instrument_name: str` — Instrument observed.
* `observed_at_ms: int` — Timestamp when observation occurred.
* `available_at_ms: int` — Earliest timestamp when this observation is legally accessible to decision engines. Invariant: `available_at_ms >= observed_at_ms`.
* `price_basis: PriceBasis` — Valuation source (trade, bid, ask, mark, etc.).
* `trade_price_btc: Optional[Decimal]` — Last trade price in BTC, or `None`.
* `bid_price_btc: Optional[Decimal]` — Best bid price in BTC, or `None`.
* `ask_price_btc: Optional[Decimal]` — Best ask price in BTC, or `None`.
* `mark_price_btc: Optional[Decimal]` — Exchange mark price in BTC, or `None`.
* `trade_size: Optional[Decimal]`, `bid_size: Optional[Decimal]`, `ask_size: Optional[Decimal]`.
* `interval_start_ms: Optional[int]`, `interval_end_ms: Optional[int]` — Bar time bounds.
* `underlying_price_usd: Optional[Decimal]` — Simultaneous BTC underlying price in USD.
* `index_price_usd: Optional[Decimal]` — Deribit index reference price.
* `quality: DataQuality` — Quality classification of this observation.
* `source_ref: str` — Source dataset provenance.

#### 5. `CoverageReport`
* `instrument_name: str` — Instrument examined.
* `start_ms: int`, `end_ms: int` — Evaluation window.
* `expected_intervals: int` — Expected bar count in window.
* `observed_intervals: int` — Valid bars observed.
* `missing_intervals: int` — Gaps where no data was recorded.
* `coverage_ratio: Decimal` — Ratio `observed_intervals / expected_intervals` $\in [0.0, 1.0]$.
* `quality_breakdown: Tuple[Tuple[str, int], ...]` — Counts of observations by DataQuality status.
* `status: str` — Summary label (`"adequate"`, `"insufficient"`, `"untradeable"`).

#### 6. `SelectionDecision`
* `decision_at_ms: int` — Timestamp of selection.
* `strategy_name: str` — Strategy initiating selection.
* `selected_instruments: Tuple[Instrument, ...]` — Selected legs (immutable).
* `underlying_price_usd: Decimal` — Current underlying spot/proxy price.
* `target_expiry_ms: Optional[int]` — Target expiration selected.
* `common_strike_usd: Optional[Decimal]` — Selected common strike.
* `reason_code: ReasonCode` — Selection outcome code.
* `metadata: Tuple[Tuple[str, str], ...]` — Supplementary diagnostics.

#### 7. `Signal`
* `signal_id: str` — Unique signal identifier.
* `decision_at_ms: int` — Time signal was calculated.
* `available_at_ms: int` — Time signal becomes actionable ($\ge \text{decision\_at\_ms}$).
* `strategy_name: str` — Originating strategy.
* `signal_type: str` — Intent descriptor (e.g. `"enter_straddle"`, `"exit_stop_loss"`).
* `instrument_names: Tuple[str, ...]` — Leg instruments targeted.
* `target_quantities: Tuple[Decimal, ...]` — Leg order quantities ($> 0$).
* `sides: Tuple[Side, ...]` — `Side.BUY` or `Side.SELL` for each leg.
* `reason_code: ReasonCode` — Trigger reason.
* `metadata: Tuple[Tuple[str, str], ...]` — Invariant: array lengths of instruments, quantities, and sides must be identical.

#### 8. `OrderIntent`
* `order_id: str` — Unique order ID.
* `position_id: str` — Target position ID.
* `leg_id: str` — Identifier of leg within position.
* `instrument_name: str` — Instrument symbol.
* `side: Side` — Buy / Sell.
* `quantity: Decimal` — Number of contracts $> 0$.
* `decision_at_ms: int` — Time order decision was made.
* `eligible_after_ms: int` — Earliest allowable fill time ($\ge \text{decision\_at\_ms}$).
* `expires_at_ms: int` — Order expiry time ($> \text{eligible\_after\_ms}$).
* `reason: ReasonCode` — Entry or exit reason.

#### 9. `Fill`
* `fill_id: str` — Unique fill ID.
* `order_id: str`, `position_id: str`, `leg_id: str`, `instrument_name: str`, `side: Side`.
* `actual_ms: int` — Execution timestamp.
* `model_timestamp_ms: int` — Simulation clock time.
* `quantity: Decimal` — Executed size $> 0$.
* `price_btc: Decimal` — Execution price in BTC per contract $\ge 0$.
* `fee_btc: Decimal` — Realized exchange fee in BTC $\ge 0$.
* `reference_price_btc: Decimal` — Unadjusted base observation price.
* `spread_slippage_cost_btc: Decimal` — Total friction cost incurred.
* `execution_model_id: ExecutionModelId` — Model that generated this fill.
* `source_ref: str` — Observation provenance.

#### 10. `FillDecision`
* `order_id: str` — Target order.
* `filled: bool` — True if fill was executed.
* `fill: Optional[Fill]` — Fill details (required if `filled=True`, forbidden if `False`).
* `rejection_reason: Optional[str]` — Rejection note if unfilled.
* `observation_used: Optional[PriceObservation]` — Market data used for pricing.

#### 11. `LedgerEntry`
* `event_id: str` — Unique transaction identifier.
* `position_id: Optional[str]` — Associated position, or None for balance events.
* `timestamp_ms: int` — Transaction timestamp.
* `currency: Currency` — Currency (`Currency.BTC` or `Currency.USD`).
* `amount: Decimal` — Amount credited ($> 0$) or debited ($< 0$).
* `entry_type: EntryType` — Classification (`premium`, `fee`, `settlement`, etc.).
* `balance_after: Decimal` — Resulting cash balance.
* `description: str` — Transaction explanation.

#### 12. `Position`
* `position_id: str` — Unique position ID.
* `strategy_name: str` — Strategy owner.
* `status: PositionStatus` — `open`, `closed`, `settled`, `unresolved`.
* `legs: Tuple[Instrument, ...]` — Leg contracts.
* `leg_quantities: Tuple[Decimal, ...]` — Leg sizes.
* `opened_at_ms: int` — Entry timestamp.
* `closed_at_ms: Optional[int]` — Exit timestamp ($\ge \text{opened\_at\_ms}$).
* `open_fills: Tuple[Fill, ...]` — Entry fills.
* `close_fills: Tuple[Fill, ...]` — Exit fills.
* `realized_pnl_btc: Decimal` — Cumulative net profit in BTC (accounting for fees).
* `realized_pnl_usd: Decimal` — Cumulative net profit converted to USD.
* `exit_reason: Optional[ReasonCode]` — Reason position terminated.

#### 13. `EquityPoint`
* `timestamp_ms: int` — Mark-to-market valuation timestamp.
* `cash_balance_btc: Decimal` — Free cash in BTC.
* `reserved_balance_btc: Decimal` — Capital reserved for margin or short risk.
* `unrealized_pnl_btc: Decimal` — Current mark-to-market position value.
* `total_equity_btc: Decimal` — `cash_balance_btc + reserved_balance_btc + unrealized_pnl_btc`.
* `underlying_price_usd: Decimal` — Underlying BTC price in USD $> 0$.
* `total_equity_usd: Decimal` — `total_equity_btc * underlying_price_usd`.
* `benchmark_buy_and_hold_btc: Decimal` — Buy-and-hold BTC benchmark equity.
* `is_valuation_reliable: bool` — Flag indicating whether all leg valuations were fresh.
* `quality_note: str` — Explanation if valuation was degraded by missing bars.

#### 14. `PortfolioState`
* `timestamp_ms: int` — State snapshot timestamp.
* `cash_balance_btc: Decimal` — Current free cash.
* `reserved_balance_btc: Decimal` — Current reserved balance.
* `open_positions: Tuple[Position, ...]` — Active positions.
* `closed_positions: Tuple[Position, ...]` — Historical positions.
* `ledger: Tuple[LedgerEntry, ...]` — Full transaction audit trail.

#### 15. `SettlementObservation`
* `instrument_name: str` — Expiring contract.
* `expiry_ms: int` — Expiry timestamp (08:00 UTC on expiry date).
* `settlement_price_usd: Decimal` — Official Deribit delivery price in USD.
* `settlement_price_btc: Decimal` — Intrinsic settlement payout in BTC.
* `is_official_delivery: bool` — True if sourced from official delivery record.
* `source_ref: str` — Exchange delivery provenance.

#### 16. `Metrics`
* `total_trades: int` — Total closed positions (must equal `winning + losing + break_even`).
* `winning_trades: int`, `losing_trades: int`, `break_even_trades: int`.
* `win_rate: Optional[Decimal]` — `winning_trades / total_trades` $\in [0.0, 1.0]$. Can be `None` when `total_trades == 0`.
* `net_pnl_btc: Decimal`, `net_pnl_usd: Decimal` — Total net profits.
* `max_drawdown_btc: Decimal`, `max_drawdown_pct: Decimal` — Peak-to-trough drawdown.
* `profit_factor: Optional[Decimal]` — Gross profits divided by gross losses.
* `sharpe_ratio: Optional[Decimal]` — Annualized Sharpe ratio.
* `coverage_summary: Tuple[Tuple[str, str], ...]` — Data coverage summary.

#### 17. `ReportManifest`
* `report_id: str` — Unique report batch identifier.
* `generated_at_ms: int` — Timestamp report was produced.
* `files: Tuple[str, ...]` — List of artifact paths produced.
* `summary: Tuple[Tuple[str, str], ...]` — Summary key-value metrics.

#### 18. `RunResult`
* `config_hash: str`, `data_hash: str`, `code_hash: str` — Cryptographic reproducibility fingerprints.
* `run_timestamp_ms: int` — Execution timestamp.
* `start_ms: int`, `end_ms: int` — Simulation period.
* `schema_version: str` — Schema version (`"1.0"`).
* `orders: Tuple[OrderIntent, ...]`, `fills: Tuple[Fill, ...]`, `ledger: Tuple[LedgerEntry, ...]`.
* `trades: Tuple[Position, ...]`, `equity_curve: Tuple[EquityPoint, ...]`, `coverage_reports: Tuple[CoverageReport, ...]`.
* `metrics: Optional[Metrics]`, `open_positions_at_end: Tuple[Position, ...]`.
* `skipped_reasons: Tuple[Tuple[str, int], ...]`, `warnings: Tuple[str, ...]`, `capabilities: Tuple[str, ...]`.

---

## 4. Provider Protocols

```python
class HistoricalTradeProvider(Protocol):
    def fetch(self, plan: Any) -> DownloadResult: ...

class InstrumentProvider(Protocol):
    def load(self, snapshot: Any) -> Sequence[Instrument]: ...

class PriceProvider(Protocol):
    def iter_events(
        self, start_ms: int, end_ms: int, instruments: Sequence[str]
    ) -> Iterator[PriceObservation]: ...

class SettlementProvider(Protocol):
    def get(self, instrument: str, expiry_ms: int) -> Optional[SettlementObservation]: ...

class Store(Protocol):
    def put(self, key: str, value: bytes) -> None: ...
    def get(self, key: str) -> Optional[bytes]: ...
    def query(self, prefix: str) -> Iterator[Tuple[str, bytes]]: ...
    def resume_state(self) -> Dict[str, Any]: ...
```

---

## 5. Acceptance & Validation Rules

1. **Strict Field Types:**
   - Float values passed to Decimal fields are strictly checked for NaN and Infinity.
   - Booleans are rejected for numeric and timestamp fields.
2. **Deterministic Tie-Breaking:** Equal strike distances resolved via `query.tie_break == "lower" | "higher"`.
3. **No Phantom Fills:** Fills require valid, contemporaneous PriceObservations. Stale price carry-forward is prohibited.
