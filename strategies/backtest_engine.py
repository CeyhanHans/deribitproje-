"""Legacy single-position USD cashflow backtest engine.

DISCLAIMER & ARCHITECTURAL STATUS:
This module is a historical single-position prototype evaluating legacy USD cashflow
for multi-leg option strategies. It does NOT implement a continuous multi-trade
event loop, advanced signal evaluation, or the authoritative BTC balance ledger
defined in core.contracts (Task 1). It is maintained for baseline compatibility
and single-trade unit verification.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from strategies.strategy_schema import ExitType, Side, StrategyDefinition, validate_strategy


class BacktestEngineError(ValueError):
    """Raised when market data or strategy parameters cannot support a backtest run."""


@dataclass(frozen=True)
class MarketObservation:
    timestamp_utc: datetime
    instrument_name: str
    option_type: str
    underlying_price_usd: float
    trade_price_btc: float
    btc_usd: float
    expiry_utc: datetime | None = None
    data_quality_label: str = "official-historical"
    is_settlement: bool = False


@dataclass(frozen=True)
class ExecutionModel:
    slippage_bps: float = 0.0
    # Deribit inverse-option trading fee per underlying contract. The fee is
    # capped at a fraction of the option premium for each execution.
    fee_rate: float = 0.0003
    min_fee_btc: float = 0.0
    fill_quality_label: str = "estimated"
    premium_fee_cap_rate: float = 0.125


@dataclass(frozen=True)
class BacktestTrade:
    opened_at_utc: datetime
    closed_at_utc: datetime
    net_pnl_usd: float
    exit_reason: str
    data_quality_labels: tuple[str, ...]
    fill_quality_label: str


@dataclass(frozen=True)
class BacktestResult:
    trade_count: int
    net_pnl_usd: float
    max_drawdown_usd: float
    win_rate: float
    trades: tuple[BacktestTrade, ...]
    data_quality_labels: tuple[str, ...]
    fill_quality_label: str
    accounting_model: str = "legacy_usd_cashflow"


def run_backtest(
    strategy: StrategyDefinition,
    observations_by_leg: Mapping[str, Sequence[MarketObservation]],
    execution_model: ExecutionModel | None = None,
) -> BacktestResult:
    """Execute a single-position backtest across common observation timestamps."""
    validate_strategy(strategy)
    execution = execution_model or ExecutionModel()
    _validate_execution_model(execution)
    _validate_leg_data(strategy, observations_by_leg)

    leg_series = {
        leg.name: sorted(observations_by_leg[leg.name], key=lambda observation: observation.timestamp_utc)
        for leg in strategy.legs
    }
    common_timestamps = _common_timestamps(leg_series.values())
    if len(common_timestamps) < 2:
        raise BacktestEngineError("at least two common timestamps are required")

    opened_at = common_timestamps[0]
    entry_observations = _observations_at(leg_series, opened_at)
    entry_cashflow_usd = _portfolio_cashflow_usd(strategy, entry_observations, execution, is_entry=True)

    # Initialize exit state to the final common observation in case no explicit exit rule triggers
    closed_at = common_timestamps[-1]
    exit_observations = _observations_at(leg_series, closed_at)
    exit_reason = "end_of_data"

    equity_curve = [0.0]
    for timestamp in common_timestamps[1:]:
        current_observations = _observations_at(leg_series, timestamp)
        current_pnl = entry_cashflow_usd + _portfolio_cashflow_usd(
            strategy, current_observations, execution, is_entry=False
        )
        equity_curve.append(current_pnl)
        reason = _matching_exit_reason(strategy, current_pnl, timestamp, current_observations)
        if reason:
            exit_observations = current_observations
            closed_at = timestamp
            exit_reason = reason
            break

    net_pnl_usd = entry_cashflow_usd + _portfolio_cashflow_usd(
        strategy, exit_observations, execution, is_entry=False
    )
    labels = tuple(
        sorted({observation.data_quality_label for series in leg_series.values() for observation in series})
    )
    trade = BacktestTrade(
        opened_at_utc=opened_at,
        closed_at_utc=closed_at,
        net_pnl_usd=net_pnl_usd,
        exit_reason=exit_reason,
        data_quality_labels=labels,
        fill_quality_label=execution.fill_quality_label,
    )

    return BacktestResult(
        trade_count=1,
        net_pnl_usd=net_pnl_usd,
        max_drawdown_usd=_max_drawdown(equity_curve),
        win_rate=1.0 if net_pnl_usd > 0 else 0.0,
        trades=(trade,),
        data_quality_labels=labels,
        fill_quality_label=execution.fill_quality_label,
        accounting_model="legacy_usd_cashflow",
    )


def _validate_execution_model(execution: ExecutionModel) -> None:
    for name, val in [
        ("slippage_bps", execution.slippage_bps),
        ("fee_rate", execution.fee_rate),
        ("min_fee_btc", execution.min_fee_btc),
        ("premium_fee_cap_rate", execution.premium_fee_cap_rate),
    ]:
        _require_finite(val, name)
        if val < 0:
            raise BacktestEngineError(f"{name} cannot be negative")


def _validate_leg_data(
    strategy: StrategyDefinition,
    observations_by_leg: Mapping[str, Sequence[MarketObservation]],
) -> None:
    # Explicitly verify entry rules: legacy engine only supports immediate entry without filters/parameters
    for rule in strategy.entry_rules:
        rule_name = getattr(rule, "name", "")
        if rule_name != "enter_when_chain_has_required_legs":
            raise BacktestEngineError(
                f"unsupported entry rule: {rule_name!r}. Legacy engine does not evaluate "
                f"signal rules and only supports 'enter_when_chain_has_required_legs'. "
                f"For general signal evaluation, use Task 10 signal engine."
            )
        rule_params = getattr(rule, "parameters", None)
        if rule_params:
            raise BacktestEngineError(
                f"unsupported entry rule parameters: {rule_params!r}. Legacy engine does not evaluate "
                f"signal or filter parameters on 'enter_when_chain_has_required_legs'. "
                f"Parameters must be empty."
            )

    for leg in strategy.legs:
        observations = observations_by_leg.get(leg.name)
        if not observations:
            raise BacktestEngineError(f"missing observations for leg: {leg.name}")

        seen_timestamps = set()
        expected_instrument = None

        for observation in observations:
            # Reject duplicate timestamps within the same leg series
            if observation.timestamp_utc in seen_timestamps:
                raise BacktestEngineError(
                    f"duplicate timestamp detected in leg {leg.name}: {observation.timestamp_utc}"
                )
            seen_timestamps.add(observation.timestamp_utc)

            # Reject changing instrument names within the same leg series
            if expected_instrument is None:
                expected_instrument = observation.instrument_name
            elif observation.instrument_name != expected_instrument:
                raise BacktestEngineError(
                    f"instrument changed within series for leg {leg.name}: "
                    f"expected {expected_instrument}, got {observation.instrument_name}"
                )

            if observation.option_type != leg.option_type:
                raise BacktestEngineError(f"option type mismatch for leg: {leg.name}")

            # Numerical finiteness & non-negativity checks
            _require_finite(observation.btc_usd, "btc_usd")
            _require_positive(observation.btc_usd, "btc_usd")
            _require_finite(observation.underlying_price_usd, "underlying_price_usd")
            _require_positive(observation.underlying_price_usd, "underlying_price_usd")

            # Trade price validation: settlement observations allow zero payout; standard trades require > 0
            _require_finite(observation.trade_price_btc, "trade_price_btc")
            is_settlement = (
                getattr(observation, "is_settlement", False)
                or observation.data_quality_label == "settlement"
            )
            if is_settlement:
                if observation.trade_price_btc < 0:
                    raise BacktestEngineError("settlement trade_price_btc cannot be negative")
            else:
                _require_positive(observation.trade_price_btc, "trade_price_btc")


def _common_timestamps(series_collection: Sequence[Sequence[MarketObservation]]) -> list[datetime]:
    common: set[datetime] | None = None
    for series in series_collection:
        timestamps = {observation.timestamp_utc for observation in series}
        common = timestamps if common is None else common & timestamps
    return sorted(common or set())


def _observations_at(
    leg_series: Mapping[str, Sequence[MarketObservation]],
    timestamp: datetime,
) -> dict[str, MarketObservation]:
    selected = {}
    for leg_name, series in leg_series.items():
        for observation in series:
            if observation.timestamp_utc == timestamp:
                selected[leg_name] = observation
                break
    return selected


def _portfolio_cashflow_usd(
    strategy: StrategyDefinition,
    observations_by_leg: Mapping[str, MarketObservation],
    execution: ExecutionModel,
    *,
    is_entry: bool,
) -> float:
    total = 0.0
    for leg in strategy.legs:
        observation = observations_by_leg[leg.name]
        fill_price_btc = _fill_price_btc(
            observation.trade_price_btc,
            side=leg.side,
            execution=execution,
            is_entry=is_entry,
        )
        gross_usd = fill_price_btc * observation.btc_usd * leg.quantity
        fee_btc = _inverse_option_trading_fee_btc(fill_price_btc, leg.quantity, execution)
        fee_usd = fee_btc * observation.btc_usd
        total += _cashflow_sign(leg.side, is_entry) * gross_usd - fee_usd
    return total


def _inverse_option_trading_fee_btc(
    premium_price_btc: float,
    amount_contracts: float,
    execution: ExecutionModel,
) -> float:
    """Return the capped BTC trading fee for one inverse-option execution."""
    uncapped_fee_btc = amount_contracts * execution.fee_rate
    premium_cap_btc = premium_price_btc * amount_contracts * execution.premium_fee_cap_rate
    capped_fee_btc = min(uncapped_fee_btc, premium_cap_btc)
    return max(capped_fee_btc, amount_contracts * execution.min_fee_btc)


def _fill_price_btc(price_btc: float, *, side: str, execution: ExecutionModel, is_entry: bool) -> float:
    slip = price_btc * execution.slippage_bps / 10_000
    pays_offer = (side == Side.LONG.value and is_entry) or (side == Side.SHORT.value and not is_entry)
    return price_btc + slip if pays_offer else max(price_btc - slip, 0.0)


def _cashflow_sign(side: str, is_entry: bool) -> int:
    if side == Side.LONG.value:
        return -1 if is_entry else 1
    return 1 if is_entry else -1


def _matching_exit_reason(
    strategy: StrategyDefinition,
    net_pnl_usd: float,
    timestamp: datetime,
    observations_by_leg: Mapping[str, MarketObservation],
) -> str | None:
    for exit_rule in strategy.exit_rules:
        if exit_rule.exit_type == ExitType.NET_USD_STOP_LOSS.value and net_pnl_usd <= -float(exit_rule.value):
            return exit_rule.exit_type
        if exit_rule.exit_type == ExitType.NET_USD_TAKE_PROFIT.value and net_pnl_usd >= float(exit_rule.value):
            return exit_rule.exit_type
        if exit_rule.exit_type == ExitType.TIME_EXIT.value and timestamp >= _parse_utc(exit_rule.value):
            return exit_rule.exit_type
        if exit_rule.exit_type == ExitType.EXPIRY_EXIT.value and _is_at_or_after_expiry(timestamp, observations_by_leg):
            return exit_rule.exit_type
    return None


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_at_or_after_expiry(timestamp: datetime, observations_by_leg: Mapping[str, MarketObservation]) -> bool:
    expiries = [observation.expiry_utc for observation in observations_by_leg.values() if observation.expiry_utc]
    return bool(expiries) and timestamp >= min(expiries)


def _max_drawdown(equity_curve: Sequence[float]) -> float:
    peak = equity_curve[0]
    max_drop = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        max_drop = max(max_drop, peak - value)
    return max_drop


def _require_positive(value: float, field_name: str) -> None:
    if value <= 0:
        raise BacktestEngineError(f"{field_name} must be positive")


def _require_finite(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BacktestEngineError(f"{field_name} must be a valid number")
    if math.isnan(value) or math.isinf(value):
        raise BacktestEngineError(f"{field_name} cannot be NaN or Infinity")
