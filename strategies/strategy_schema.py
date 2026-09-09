"""Strategy and RunConfig schema definitions and validation for Deribit BTC inverse options."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from core.contracts import RunConfig


class StrategyValidationError(ValueError):
    """Raised when a strategy or run configuration definition is incomplete, inconsistent, or unsupported."""


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class StrikeSelectorType(str, Enum):
    EXACT_USD = "exact_usd"
    ATM = "atm"
    DELTA_TARGET = "delta_target"
    MONEYNESS_PERCENT = "moneyness_percent"
    NEAREST_PREMIUM_USD = "nearest_premium_usd"
    SAME_STRIKE_AS = "same_strike_as"


class ExpirySelectorType(str, Enum):
    EXACT_DATE = "exact_date"
    DAYS_TO_EXPIRY = "days_to_expiry"
    NEAREST_DAYS_TO_EXPIRY = "nearest_days_to_expiry"
    SAME_EXPIRY_AS = "same_expiry_as"


class ExitType(str, Enum):
    NET_USD_STOP_LOSS = "net_usd_stop_loss"
    NET_USD_TAKE_PROFIT = "net_usd_take_profit"
    TIME_EXIT = "time_exit"
    EXPIRY_EXIT = "expiry_exit"


class SizingType(str, Enum):
    FIXED_QUANTITY = "fixed_quantity"
    PREMIUM_BUDGET_BTC = "premium_budget_btc"
    CAPITAL_FRACTION = "capital_fraction"


class MarginModel(str, Enum):
    CONSERVATIVE_STRESS_RESERVE = "conservative_stress_reserve"
    CASH_SECURED = "cash_secured"


ALLOWED_FEE_SPREAD_MODELS = {
    "deribit_inverse_option_bid_ask",
    "deribit_usdc_option_bid_ask",
    "custom_bid_ask_slippage",
}

ALLOWED_MARGIN_MODELS = {
    "conservative_stress_reserve",
    "cash_secured",
}

UNSUPPORTED_MARGIN_MODELS = {
    "deribit_portfolio_margin",
    "portfolio_margin",
    "cross_margin",
    "naked_short_unreserved",
    "unreserved",
}

KNOWN_ENTRY_RULES = {
    "enter_when_chain_has_required_legs",
    "periodic_calendar_entry",
    "dte_target_entry",
    "signal_rule_entry",
    "rsi_oversold_filter",
}

REQUIRED_METRICS = {
    "trade_count",
    "net_pnl_usd",
    "max_drawdown_usd",
    "win_rate",
}

FORBIDDEN_METRICS = {"prediction_accuracy"}

ALLOWED_STRATEGY_FIELDS = {
    "name",
    "version",
    "legs",
    "entry_rules",
    "exit_rules",
    "fee_spread_model",
    "result_metrics",
    "notes",
}

ALLOWED_LEG_FIELDS = {
    "name",
    "option_type",
    "side",
    "quantity",
    "strike",
    "expiry",
    "same_expiry_as",
    "same_strike_as",
    "strike_offset_usd",
}

ALLOWED_SELECTOR_FIELDS = {"type", "value"}

ALLOWED_ENTRY_RULE_FIELDS = {"name", "parameters"}

ALLOWED_EXIT_RULE_FIELDS = {"type", "value"}

ALLOWED_METRIC_FIELDS = {"name", "description"}

ALLOWED_SIZING_FIELDS = {"type", "value", "sizing_type"}

ALLOWED_EXECUTION_FIELDS = {
    "execution_model_id",
    "fee_rate_amount",
    "fee_cap_ratio",
    "slippage_btc",
}

ALLOWED_DATA_POLICY_FIELDS = {"allow_missing_bars", "price_basis"}

ALLOWED_RUN_CONFIG_FIELDS = {
    "run_name",
    "strategy_name",
    "version",
    "start_time",
    "end_time",
    "start_date",
    "end_date",
    "start_ms",
    "end_ms",
    "time_range",
    "date_range",
    "decision_resolution",
    "decision_resolution_hours",
    "underlying",
    "settlement_currency",
    "contract_type",
    "initial_cash_btc",
    "initial_capital_btc",
    "sizing",
    "sizing_type",
    "sizing_value",
    "quantity",
    "max_open_positions",
    "margin_model",
    "stress_reserve_ratio",
    "execution_profile",
    "data_policy",
    "strategy",
    "strategy_params",
    "deterministic_seed",
    "schema_version",
    "legs",
    "entry_rules",
    "exit_rules",
    "fee_spread_model",
    "result_metrics",
    "notes",
}


def _is_nan(val: Any) -> bool:
    """Check if value is NaN (float, Decimal, or string NaN)."""
    if isinstance(val, (float, int)):
        if isinstance(val, bool):
            return False
        return math.isnan(val)
    if isinstance(val, Decimal):
        return val.is_nan()
    if isinstance(val, str):
        val_clean = val.strip().lower()
        if val_clean in ("nan", "+nan", "-nan", "snan", "+snan", "-snan"):
            return True
        try:
            d = Decimal(val)
            return d.is_nan()
        except Exception:
            return False
    return False


def _is_infinite(val: Any) -> bool:
    """Check if value is infinite."""
    if isinstance(val, (float, int)):
        if isinstance(val, bool):
            return False
        return math.isinf(val)
    if isinstance(val, Decimal):
        return val.is_infinite()
    if isinstance(val, str):
        val_clean = val.strip().lower()
        if val_clean in ("inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"):
            return True
    return False


def parse_utc_timestamp_ms(val: Any, field_name: str = "timestamp") -> int:
    """Parse an ISO 8601 string or integer timestamp into UTC milliseconds.

    Handles full ISO strings (with or without 'Z', with offsets), dates ('YYYY-MM-DD'),
    leap days (e.g. 2024-02-29), and millisecond integers.
    """
    if val is None or val == "":
        raise StrategyValidationError(f"{field_name} is required")
    if _is_nan(val):
        raise StrategyValidationError(f"{field_name} must not be NaN")
    if _is_infinite(val):
        raise StrategyValidationError(f"{field_name} must not be infinite")

    if isinstance(val, (int, float)):
        int_val = int(val)
        if int_val < 0:
            raise StrategyValidationError(f"{field_name} must not be negative (got {int_val})")
        return int_val

    if isinstance(val, str):
        s = val.strip()
        if not s:
            raise StrategyValidationError(f"{field_name} must not be empty")
        if s.lower() == "nan":
            raise StrategyValidationError(f"{field_name} must not be NaN")
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            int_val = int(s)
            if int_val < 0:
                raise StrategyValidationError(f"{field_name} must not be negative (got {int_val})")
            return int_val

        # Normalize trailing 'Z' or 'z'
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        # If bare date (YYYY-MM-DD), append start-of-day UTC
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            s = s + "T00:00:00+00:00"

        try:
            dt = datetime.fromisoformat(s)
        except ValueError as exc:
            raise StrategyValidationError(f"invalid UTC date string for {field_name}: {val!r}") from exc

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return int(dt.timestamp() * 1000)

    raise StrategyValidationError(
        f"{field_name} must be an ISO string or millisecond integer, got {type(val).__name__}"
    )


def _check_allowed_fields(raw: Mapping[str, Any], allowed: set[str], context: str) -> None:
    for key in raw.keys():
        if key not in allowed:
            raise StrategyValidationError(f"unexpected field in {context}: {key!r}")


@dataclass(frozen=True)
class Selector:
    selector_type: str
    value: Any = None


@dataclass(frozen=True)
class StrategyLeg:
    name: str
    option_type: str
    side: str
    quantity: float | Decimal = 1.0
    strike: Selector = field(default_factory=lambda: Selector("atm", 0))
    expiry: Selector = field(default_factory=lambda: Selector("nearest_days_to_expiry", 30))
    same_expiry_as: str | None = None
    same_strike_as: str | None = None
    strike_offset_usd: float | Decimal | None = None


@dataclass(frozen=True)
class EntryRule:
    name: str
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExitRule:
    exit_type: str
    value: Any = None


@dataclass(frozen=True)
class ResultMetric:
    name: str
    description: str


@dataclass(frozen=True)
class StrategyDefinition:
    name: str
    version: str
    legs: Sequence[StrategyLeg]
    entry_rules: Sequence[EntryRule]
    exit_rules: Sequence[ExitRule]
    fee_spread_model: str
    result_metrics: Sequence[ResultMetric]
    notes: str = ""


@dataclass(frozen=True)
class SizingConfig:
    sizing_type: str = SizingType.FIXED_QUANTITY.value
    value: Decimal = Decimal("1.0")


@dataclass(frozen=True)
class ExecutionProfileConfig:
    execution_model_id: str = "model_a_bid_ask"
    fee_rate_amount: Decimal = Decimal("0.0003")
    fee_cap_ratio: Decimal = Decimal("0.125")
    slippage_btc: Decimal = Decimal("0")


@dataclass(frozen=True)
class DataPolicyConfig:
    allow_missing_bars: bool = False
    price_basis: str = "trade_and_quotes"


@dataclass(frozen=True)
class RunConfigDefinition:
    strategy_name: str
    start_time: str
    end_time: str
    start_ms: int
    end_ms: int
    decision_resolution: str = "1h"
    decision_resolution_hours: int = 1
    underlying: str = "BTC"
    settlement_currency: str = "BTC"
    contract_type: str = "inverse"
    initial_cash_btc: Decimal = Decimal("10.0")
    sizing: SizingConfig = field(default_factory=SizingConfig)
    max_open_positions: int = 1
    margin_model: str = "conservative_stress_reserve"
    stress_reserve_ratio: Decimal = Decimal("0.5")
    execution_profile: ExecutionProfileConfig = field(default_factory=ExecutionProfileConfig)
    data_policy: DataPolicyConfig = field(default_factory=DataPolicyConfig)
    strategy: StrategyDefinition | None = None
    strategy_params: Sequence[tuple[str, str]] = ()
    deterministic_seed: int = 42
    schema_version: str = "1.0"
    run_name: str = ""

    @property
    def initial_capital_btc(self) -> Decimal:
        return self.initial_cash_btc

    def to_core_run_config(self) -> "RunConfig":
        return to_core_run_config(self)


def validate_strategy(strategy: StrategyDefinition) -> None:
    """Validate that a strategy definition complies with V1 backtest specifications."""
    _require_text(strategy.name, "name")
    _require_text(strategy.version, "version")
    _require_non_empty(strategy.legs, "legs")
    _require_non_empty(strategy.entry_rules, "entry_rules")
    _require_non_empty(strategy.exit_rules, "exit_rules")
    _validate_fee_spread_model(strategy.fee_spread_model)
    _validate_result_metrics(strategy.result_metrics)

    leg_names: set[str] = set()
    all_names = [leg.name for leg in strategy.legs]

    for index, leg in enumerate(strategy.legs):
        if leg.name in leg_names:
            raise StrategyValidationError(f"duplicate leg name: {leg.name!r}")
        leg_names.add(leg.name)
        _validate_leg(leg, index, all_names)

    for index, entry_rule in enumerate(strategy.entry_rules):
        _validate_entry_rule(entry_rule, index)

    for index, exit_rule in enumerate(strategy.exit_rules):
        _validate_exit_rule(exit_rule, index)


def strategy_from_mapping(raw: Mapping[str, Any]) -> StrategyDefinition:
    """Parse a StrategyDefinition from a mapping, validating all fields strictly."""
    _check_allowed_fields(raw, ALLOWED_STRATEGY_FIELDS, "strategy")
    strategy = StrategyDefinition(
        name=raw.get("name", ""),
        version=raw.get("version", ""),
        legs=tuple(_leg_from_mapping(item) for item in raw.get("legs", ())),
        entry_rules=tuple(_entry_rule_from_mapping(item) for item in raw.get("entry_rules", ())),
        exit_rules=tuple(_exit_rule_from_mapping(item) for item in raw.get("exit_rules", ())),
        fee_spread_model=raw.get("fee_spread_model", ""),
        result_metrics=tuple(_metric_from_mapping(item) for item in raw.get("result_metrics", ())),
        notes=raw.get("notes", ""),
    )
    validate_strategy(strategy)
    return strategy


def _leg_from_mapping(raw: Mapping[str, Any]) -> StrategyLeg:
    _check_allowed_fields(raw, ALLOWED_LEG_FIELDS, "leg")

    same_expiry_as = raw.get("same_expiry_as")
    same_strike_as = raw.get("same_strike_as")
    strike_offset_usd = raw.get("strike_offset_usd")

    # Strike handling
    if "strike" in raw:
        strike = _selector_from_mapping(raw["strike"])
    elif same_strike_as is not None:
        strike = Selector(selector_type=StrikeSelectorType.SAME_STRIKE_AS.value, value=same_strike_as)
    else:
        strike = _selector_from_mapping({})

    # Expiry handling
    if "expiry" in raw:
        expiry = _selector_from_mapping(raw["expiry"])
    elif same_expiry_as is not None:
        expiry = Selector(selector_type=ExpirySelectorType.SAME_EXPIRY_AS.value, value=same_expiry_as)
    else:
        expiry = _selector_from_mapping({})

    return StrategyLeg(
        name=raw.get("name", ""),
        option_type=raw.get("option_type", ""),
        side=raw.get("side", ""),
        quantity=raw.get("quantity", 0),
        strike=strike,
        expiry=expiry,
        same_expiry_as=same_expiry_as,
        same_strike_as=same_strike_as,
        strike_offset_usd=strike_offset_usd,
    )


def _selector_from_mapping(raw: Mapping[str, Any]) -> Selector:
    _check_allowed_fields(raw, ALLOWED_SELECTOR_FIELDS, "selector")
    return Selector(selector_type=raw.get("type", ""), value=raw.get("value"))


def _entry_rule_from_mapping(raw: Mapping[str, Any]) -> EntryRule:
    _check_allowed_fields(raw, ALLOWED_ENTRY_RULE_FIELDS, "entry_rule")
    return EntryRule(name=raw.get("name", ""), parameters=raw.get("parameters", {}))


def _exit_rule_from_mapping(raw: Mapping[str, Any]) -> ExitRule:
    _check_allowed_fields(raw, ALLOWED_EXIT_RULE_FIELDS, "exit_rule")
    return ExitRule(exit_type=raw.get("type", ""), value=raw.get("value"))


def _metric_from_mapping(raw: Mapping[str, Any]) -> ResultMetric:
    _check_allowed_fields(raw, ALLOWED_METRIC_FIELDS, "result_metric")
    return ResultMetric(name=raw.get("name", ""), description=raw.get("description", ""))


def _validate_leg(leg: StrategyLeg, index: int, all_leg_names: Sequence[str] = ()) -> None:
    prefix = f"legs[{index}]"
    _require_text(leg.name, f"{prefix}.name")
    _require_choice(leg.option_type, OptionType, f"{prefix}.option_type")
    _require_choice(leg.side, Side, f"{prefix}.side")
    _require_positive_number(leg.quantity, f"{prefix}.quantity")

    _validate_strike_selector(leg.strike, f"{prefix}.strike")
    _validate_expiry_selector(leg.expiry, f"{prefix}.expiry")

    # Validate relations
    if leg.same_expiry_as is not None:
        if not isinstance(leg.same_expiry_as, str) or not leg.same_expiry_as.strip():
            raise StrategyValidationError(f"{prefix}.same_expiry_as must be non-empty text")
        if leg.same_expiry_as == leg.name:
            raise StrategyValidationError(f"{prefix}.same_expiry_as cannot reference itself ({leg.name!r})")
        if all_leg_names and leg.same_expiry_as not in all_leg_names:
            raise StrategyValidationError(
                f"{prefix}.same_expiry_as references unknown leg {leg.same_expiry_as!r}"
            )

    if leg.same_strike_as is not None:
        if not isinstance(leg.same_strike_as, str) or not leg.same_strike_as.strip():
            raise StrategyValidationError(f"{prefix}.same_strike_as must be non-empty text")
        if leg.same_strike_as == leg.name:
            raise StrategyValidationError(f"{prefix}.same_strike_as cannot reference itself ({leg.name!r})")
        if all_leg_names and leg.same_strike_as not in all_leg_names:
            raise StrategyValidationError(
                f"{prefix}.same_strike_as references unknown leg {leg.same_strike_as!r}"
            )

    if leg.strike_offset_usd is not None:
        if _is_nan(leg.strike_offset_usd):
            raise StrategyValidationError(f"{prefix}.strike_offset_usd must not be NaN")
        if not isinstance(leg.strike_offset_usd, (int, float, Decimal)) or isinstance(leg.strike_offset_usd, bool):
            raise StrategyValidationError(f"{prefix}.strike_offset_usd must be a number")


def _validate_strike_selector(selector: Selector, field_name: str) -> None:
    _require_choice(selector.selector_type, StrikeSelectorType, f"{field_name}.type")

    # Capability check for delta
    if selector.selector_type == StrikeSelectorType.DELTA_TARGET.value:
        raise StrategyValidationError(
            "unsupported capability: delta strike selection ('delta_target') is not supported in V1; "
            "historical Greek/vol surface quotes are unavailable. Use exact_usd, atm, moneyness_percent, or nearest_premium_usd."
        )

    if selector.selector_type == StrikeSelectorType.ATM.value:
        if selector.value is not None and selector.value != "" and selector.value != "atm":
            if _is_nan(selector.value):
                raise StrategyValidationError(f"{field_name}.value must not be NaN")
            if not isinstance(selector.value, (int, float, Decimal)) or isinstance(selector.value, bool):
                raise StrategyValidationError(f"{field_name}.value for atm must be None, 'atm', or an offset number")
        return

    if selector.selector_type == StrikeSelectorType.SAME_STRIKE_AS.value:
        _require_text(selector.value, f"{field_name}.value")
        return

    if selector.value in (None, ""):
        raise StrategyValidationError(f"{field_name}.value is required")

    if _is_nan(selector.value):
        raise StrategyValidationError(f"{field_name}.value must not be NaN")

    if not isinstance(selector.value, (int, float, Decimal)) or isinstance(selector.value, bool):
        raise StrategyValidationError(f"{field_name}.value must be a numeric value")

    if selector.selector_type in (StrikeSelectorType.EXACT_USD.value, StrikeSelectorType.NEAREST_PREMIUM_USD.value):
        if selector.value <= 0:
            raise StrategyValidationError(f"{field_name}.value must be positive for {selector.selector_type}")


def _validate_expiry_selector(selector: Selector, field_name: str) -> None:
    _require_choice(selector.selector_type, ExpirySelectorType, f"{field_name}.type")

    if selector.value in (None, ""):
        raise StrategyValidationError(f"{field_name}.value is required")

    if selector.selector_type == ExpirySelectorType.SAME_EXPIRY_AS.value:
        _require_text(selector.value, f"{field_name}.value")
        return

    if selector.selector_type == ExpirySelectorType.EXACT_DATE.value:
        _require_text(selector.value, f"{field_name}.value")
        try:
            parse_utc_timestamp_ms(selector.value, field_name)
        except StrategyValidationError as exc:
            raise StrategyValidationError(f"invalid exact_date in {field_name}: {exc}") from exc
        return

    # Days to expiry checks
    if _is_nan(selector.value):
        raise StrategyValidationError(f"invalid DTE: {field_name}.value must not be NaN")

    if not isinstance(selector.value, (int, float, Decimal)) or isinstance(selector.value, bool):
        raise StrategyValidationError(
            f"invalid DTE: {field_name}.value must be a positive number of days (got {selector.value!r})"
        )

    if selector.value <= 0:
        raise StrategyValidationError(
            f"invalid DTE: {field_name}.value must be a positive number of days (got {selector.value})"
        )


def _validate_selector(selector: Selector, enum_cls: type[Enum], field_name: str) -> None:
    _require_choice(selector.selector_type, enum_cls, f"{field_name}.type")
    if selector.value in (None, ""):
        raise StrategyValidationError(f"{field_name}.value is required")


def _validate_entry_rule(entry_rule: EntryRule, index: int) -> None:
    prefix = f"entry_rules[{index}]"
    _require_text(entry_rule.name, f"{prefix}.name")
    if entry_rule.name not in KNOWN_ENTRY_RULES:
        allowed = ", ".join(sorted(KNOWN_ENTRY_RULES))
        raise StrategyValidationError(
            f"unknown entry rule: {entry_rule.name!r}; must be one of: {allowed}"
        )
    if not isinstance(entry_rule.parameters, Mapping):
        raise StrategyValidationError(f"{prefix}.parameters must be a mapping")


def _validate_exit_rule(exit_rule: ExitRule, index: int) -> None:
    field_name = f"exit_rules[{index}]"
    _require_choice(exit_rule.exit_type, ExitType, f"{field_name}.type")

    if exit_rule.exit_type in {
        ExitType.NET_USD_STOP_LOSS.value,
        ExitType.NET_USD_TAKE_PROFIT.value,
    }:
        _require_positive_number(exit_rule.value, f"{field_name}.value")
    elif exit_rule.exit_type == ExitType.TIME_EXIT.value:
        _require_text(exit_rule.value, f"{field_name}.value")
    elif exit_rule.exit_type == ExitType.EXPIRY_EXIT.value and exit_rule.value not in (None, "at_expiry"):
        raise StrategyValidationError(f"{field_name}.value must be omitted or 'at_expiry'")


def _validate_fee_spread_model(fee_spread_model: str) -> None:
    if fee_spread_model not in ALLOWED_FEE_SPREAD_MODELS:
        allowed = ", ".join(sorted(ALLOWED_FEE_SPREAD_MODELS))
        raise StrategyValidationError(f"fee_spread_model must be one of: {allowed}")


def _validate_result_metrics(result_metrics: Sequence[ResultMetric]) -> None:
    _require_non_empty(result_metrics, "result_metrics")
    names = {metric.name for metric in result_metrics}
    forbidden = names & FORBIDDEN_METRICS
    if forbidden:
        raise StrategyValidationError("result_metrics must use win_rate, not prediction_accuracy")

    missing = REQUIRED_METRICS - names
    if missing:
        raise StrategyValidationError(f"result_metrics missing required metrics: {', '.join(sorted(missing))}")

    for index, metric in enumerate(result_metrics):
        _require_text(metric.name, f"result_metrics[{index}].name")
        _require_text(metric.description, f"result_metrics[{index}].description")


def _require_text(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise StrategyValidationError(f"{field_name} must be non-empty text")


def _require_non_empty(value: Sequence[Any], field_name: str) -> None:
    if not value:
        raise StrategyValidationError(f"{field_name} must not be empty")


def _require_choice(value: Any, enum_cls: type[Enum], field_name: str) -> None:
    allowed = {item.value for item in enum_cls}
    if value not in allowed:
        raise StrategyValidationError(f"{field_name} must be one of: {', '.join(sorted(allowed))}")


def _require_positive_number(value: Any, field_name: str) -> None:
    if _is_nan(value):
        raise StrategyValidationError(f"{field_name} must not be NaN")
    if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool) or value <= 0:
        raise StrategyValidationError(f"{field_name} must be a positive number")


def parse_run_config_dict(raw: Mapping[str, Any]) -> RunConfigDefinition:
    """Parse and validate a RunConfigDefinition from a dictionary representation."""
    _check_allowed_fields(raw, ALLOWED_RUN_CONFIG_FIELDS, "run config")

    # Time range parsing: dates are never hardcoded and dynamically parsed
    start_raw = None
    end_raw = None

    for range_key in ("time_range", "date_range"):
        if range_key in raw:
            range_val = raw[range_key]
            if isinstance(range_val, str):
                cleaned = range_val.strip("[]()")
                parts = [p.strip() for p in cleaned.split(",") if p.strip()]
                if len(parts) != 2:
                    raise StrategyValidationError(
                        f"{range_key} must contain exactly two boundary timestamps, got {range_val!r}"
                    )
                start_raw, end_raw = parts[0], parts[1]
            elif isinstance(range_val, (list, tuple)):
                if len(range_val) != 2:
                    raise StrategyValidationError(
                        f"{range_key} must contain exactly two elements, got {len(range_val)}"
                    )
                start_raw, end_raw = range_val[0], range_val[1]
            break

    if start_raw is None:
        start_raw = raw.get("start_time", raw.get("start_date", raw.get("start_ms")))
    if end_raw is None:
        end_raw = raw.get("end_time", raw.get("end_date", raw.get("end_ms")))

    if start_raw is None or end_raw is None:
        raise StrategyValidationError("RunConfig requires explicit start and end times (or time_range)")

    start_ms = parse_utc_timestamp_ms(start_raw, "start_time")
    end_ms = parse_utc_timestamp_ms(end_raw, "end_time")

    if start_ms == end_ms:
        raise StrategyValidationError(
            f"Invalid time range: start_time and end_time cannot be identical ({start_ms})"
        )
    if start_ms > end_ms:
        raise StrategyValidationError(
            f"Invalid time range: start_time ({start_ms}) must be strictly earlier than end_time ({end_ms})"
        )

    # Resolution handling
    res_raw = raw.get("decision_resolution", raw.get("decision_resolution_hours", "1h"))
    if isinstance(res_raw, str):
        res_str = res_raw.strip().lower()
        if res_str in ("1h", "1"):
            resolution_str = "1h"
            resolution_hours = 1
        elif res_str in ("1d", "24", "24h"):
            resolution_str = "1d"
            resolution_hours = 24
        else:
            raise StrategyValidationError(
                f"unsupported decision resolution: {res_raw!r}; only '1h' (1) and '1d' (24) are supported in V1"
            )
    elif isinstance(res_raw, int) and not isinstance(res_raw, bool):
        if res_raw == 1:
            resolution_str = "1h"
            resolution_hours = 1
        elif res_raw == 24:
            resolution_str = "1d"
            resolution_hours = 24
        else:
            raise StrategyValidationError(
                f"unsupported decision resolution: {res_raw!r}; only 1 and 24 are supported in V1"
            )
    else:
        raise StrategyValidationError(
            f"decision resolution must be string ('1h'/'1d') or integer (1/24), got {type(res_raw).__name__}"
        )

    # Product and asset verification
    underlying = str(raw.get("underlying", "BTC")).strip().upper()
    if underlying != "BTC":
        raise StrategyValidationError(f"unsupported underlying: {underlying!r}; V1 only supports 'BTC'")

    settlement_currency = str(raw.get("settlement_currency", "BTC")).strip().upper()
    if settlement_currency != "BTC":
        raise StrategyValidationError(
            f"unsupported settlement_currency: {settlement_currency!r}; V1 only supports 'BTC'"
        )

    contract_type = str(raw.get("contract_type", "inverse")).strip().lower()
    if contract_type != "inverse":
        raise StrategyValidationError(f"unsupported contract_type: {contract_type!r}; V1 only supports 'inverse'")

    # Capital verification
    cash_val = raw.get("initial_cash_btc", raw.get("initial_capital_btc", "10.0"))
    if _is_nan(cash_val):
        raise StrategyValidationError("initial_cash_btc must not be NaN")
    try:
        initial_cash_btc = Decimal(str(cash_val))
    except Exception as exc:
        raise StrategyValidationError(f"invalid initial_cash_btc: {cash_val!r}") from exc
    if initial_cash_btc <= 0:
        raise StrategyValidationError(f"initial_cash_btc must be strictly positive (got {initial_cash_btc})")

    # Sizing configuration
    if "sizing" in raw:
        sizing_raw = raw["sizing"]
        if not isinstance(sizing_raw, Mapping):
            raise StrategyValidationError("sizing must be a mapping")
        _check_allowed_fields(sizing_raw, ALLOWED_SIZING_FIELDS, "sizing")
        stype = sizing_raw.get("type", sizing_raw.get("sizing_type", SizingType.FIXED_QUANTITY.value))
        if stype not in {s.value for s in SizingType}:
            allowed = ", ".join(sorted(s.value for s in SizingType))
            raise StrategyValidationError(f"unsupported sizing type: {stype!r}; must be one of: {allowed}")
        sval_raw = sizing_raw.get("value", Decimal("1.0"))
        if _is_nan(sval_raw):
            raise StrategyValidationError("sizing value must not be NaN")
        try:
            sval = Decimal(str(sval_raw))
        except Exception as exc:
            raise StrategyValidationError(f"invalid sizing value: {sval_raw!r}") from exc
        if sval <= 0:
            raise StrategyValidationError(f"sizing value must be positive (got {sval})")
        sizing_config = SizingConfig(sizing_type=stype, value=sval)
    elif "sizing_type" in raw or "sizing_value" in raw:
        stype = raw.get("sizing_type", SizingType.FIXED_QUANTITY.value)
        if stype not in {s.value for s in SizingType}:
            allowed = ", ".join(sorted(s.value for s in SizingType))
            raise StrategyValidationError(f"unsupported sizing type: {stype!r}; must be one of: {allowed}")
        sval_raw = raw.get("sizing_value", raw.get("quantity", Decimal("1.0")))
        if _is_nan(sval_raw):
            raise StrategyValidationError("sizing value must not be NaN")
        try:
            sval = Decimal(str(sval_raw))
        except Exception as exc:
            raise StrategyValidationError(f"invalid sizing value: {sval_raw!r}") from exc
        if sval <= 0:
            raise StrategyValidationError(f"sizing value must be positive (got {sval})")
        sizing_config = SizingConfig(sizing_type=stype, value=sval)
    elif "quantity" in raw:
        q_raw = raw["quantity"]
        if _is_nan(q_raw):
            raise StrategyValidationError("quantity must not be NaN")
        try:
            q = Decimal(str(q_raw))
        except Exception as exc:
            raise StrategyValidationError(f"invalid quantity: {q_raw!r}") from exc
        if q <= 0:
            raise StrategyValidationError(f"quantity must be positive (got {q})")
        sizing_config = SizingConfig(sizing_type=SizingType.FIXED_QUANTITY.value, value=q)
    else:
        sizing_config = SizingConfig(sizing_type=SizingType.FIXED_QUANTITY.value, value=Decimal("1.0"))

    # Max open positions
    mop = raw.get("max_open_positions", 1)
    if _is_nan(mop) or isinstance(mop, bool) or not isinstance(mop, int) or mop <= 0:
        raise StrategyValidationError(f"max_open_positions must be a positive integer (got {mop!r})")

    # Margin model verification (V1 capability check)
    margin_model = str(raw.get("margin_model", "conservative_stress_reserve")).strip()
    if margin_model not in ALLOWED_MARGIN_MODELS:
        raise StrategyValidationError(
            f"unsupported margin model: {margin_model!r}; V1 only supports conservative_stress_reserve or cash_secured (portfolio margin and naked short are not supported)"
        )

    srr_raw = raw.get("stress_reserve_ratio", Decimal("0.5"))
    if _is_nan(srr_raw):
        raise StrategyValidationError("stress_reserve_ratio must not be NaN")
    try:
        stress_reserve_ratio = Decimal(str(srr_raw))
    except Exception as exc:
        raise StrategyValidationError(f"invalid stress_reserve_ratio: {srr_raw!r}") from exc
    if stress_reserve_ratio < 0:
        raise StrategyValidationError(f"stress_reserve_ratio must be non-negative (got {stress_reserve_ratio})")

    # Execution profile
    if "execution_profile" in raw:
        ep_raw = raw["execution_profile"]
        if not isinstance(ep_raw, Mapping):
            raise StrategyValidationError("execution_profile must be a mapping")
        _check_allowed_fields(ep_raw, ALLOWED_EXECUTION_FIELDS, "execution_profile")
        exec_model_id = str(ep_raw.get("execution_model_id", "model_a_bid_ask")).strip().lower()

        fee_rate_raw = ep_raw.get("fee_rate_amount", "0.0003")
        fee_cap_raw = ep_raw.get("fee_cap_ratio", "0.125")
        slip_raw = ep_raw.get("slippage_btc", "0")

        if _is_nan(fee_rate_raw) or _is_nan(fee_cap_raw) or _is_nan(slip_raw):
            raise StrategyValidationError("execution profile fees and slippage must not be NaN")

        execution_profile = ExecutionProfileConfig(
            execution_model_id=exec_model_id,
            fee_rate_amount=Decimal(str(fee_rate_raw)),
            fee_cap_ratio=Decimal(str(fee_cap_raw)),
            slippage_btc=Decimal(str(slip_raw)),
        )
    else:
        execution_profile = ExecutionProfileConfig()

    # Data policy
    if "data_policy" in raw:
        dp_raw = raw["data_policy"]
        if not isinstance(dp_raw, Mapping):
            raise StrategyValidationError("data_policy must be a mapping")
        _check_allowed_fields(dp_raw, ALLOWED_DATA_POLICY_FIELDS, "data_policy")
        data_policy = DataPolicyConfig(
            allow_missing_bars=bool(dp_raw.get("allow_missing_bars", False)),
            price_basis=str(dp_raw.get("price_basis", "trade_and_quotes")),
        )
    else:
        data_policy = DataPolicyConfig()

    # Strategy definition parsing
    strategy: StrategyDefinition | None = None
    if "strategy" in raw:
        strat_raw = raw["strategy"]
        if not isinstance(strat_raw, Mapping):
            raise StrategyValidationError("strategy must be a mapping")
        strategy = strategy_from_mapping(strat_raw)
    elif "legs" in raw:
        strat_dict = {
            "name": raw.get("strategy_name", "unnamed_strategy"),
            "version": raw.get("version", "1.0"),
            "legs": raw.get("legs", ()),
            "entry_rules": raw.get("entry_rules", ()),
            "exit_rules": raw.get("exit_rules", ()),
            "fee_spread_model": raw.get("fee_spread_model", "deribit_inverse_option_bid_ask"),
            "result_metrics": raw.get("result_metrics", ()),
            "notes": raw.get("notes", ""),
        }
        strategy = strategy_from_mapping(strat_dict)

    strategy_name = str(raw.get("strategy_name", strategy.name if strategy else "unnamed_strategy")).strip()
    if not strategy_name:
        raise StrategyValidationError("strategy_name must be non-empty text")

    run_name = str(raw.get("run_name", f"run_{strategy_name}_{resolution_str}")).strip()

    start_iso = (
        start_raw
        if isinstance(start_raw, str) and not start_raw.isdigit()
        else datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat()
    )
    end_iso = (
        end_raw
        if isinstance(end_raw, str) and not end_raw.isdigit()
        else datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).isoformat()
    )

    strategy_params = tuple(raw.get("strategy_params", ()))
    deterministic_seed = int(raw.get("deterministic_seed", 42))
    schema_version = str(raw.get("schema_version", "1.0"))

    return RunConfigDefinition(
        strategy_name=strategy_name,
        start_time=start_iso,
        end_time=end_iso,
        start_ms=start_ms,
        end_ms=end_ms,
        decision_resolution=resolution_str,
        decision_resolution_hours=resolution_hours,
        underlying=underlying,
        settlement_currency=settlement_currency,
        contract_type=contract_type,
        initial_cash_btc=initial_cash_btc,
        sizing=sizing_config,
        max_open_positions=mop,
        margin_model=margin_model,
        stress_reserve_ratio=stress_reserve_ratio,
        execution_profile=execution_profile,
        data_policy=data_policy,
        strategy=strategy,
        strategy_params=strategy_params,
        deterministic_seed=deterministic_seed,
        schema_version=schema_version,
        run_name=run_name,
    )


def parse_run_config_json(json_str: str) -> RunConfigDefinition:
    """Parse and validate RunConfigDefinition from JSON string."""
    try:
        raw = json.loads(json_str)
    except Exception as exc:
        raise StrategyValidationError(f"Invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise StrategyValidationError("RunConfig JSON must be a top-level object")
    return parse_run_config_dict(raw)


def load_run_config_file(file_path: str | Path) -> RunConfigDefinition:
    """Load and validate RunConfigDefinition from a JSON file."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"RunConfig file not found: {file_path}")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return parse_run_config_json(content)


def to_core_run_config(definition: RunConfigDefinition) -> "RunConfig":
    """Convert a validated RunConfigDefinition into a frozen core.contracts.RunConfig."""
    from core.contracts import ExecutionModelId, RunConfig

    exec_map = {
        "model_a_bid_ask": ExecutionModelId.MODEL_A_BID_ASK,
        "model_b_mid_slippage": ExecutionModelId.MODEL_B_MID_SLIPPAGE,
        "model_c_trade_bar_close": ExecutionModelId.MODEL_C_TRADE_BAR_CLOSE,
        "model_c_trade_match": ExecutionModelId.MODEL_C_TRADE_BAR_CLOSE,
        "model_d_quote_l2": ExecutionModelId.MODEL_D_QUOTE_L2,
    }

    model_id_str = definition.execution_profile.execution_model_id.lower()
    exec_id = exec_map.get(model_id_str)
    if exec_id is None:
        try:
            exec_id = ExecutionModelId(definition.execution_profile.execution_model_id)
        except ValueError:
            exec_id = ExecutionModelId.MODEL_A_BID_ASK

    return RunConfig(
        strategy_name=definition.strategy_name,
        start_ms=definition.start_ms,
        end_ms=definition.end_ms,
        initial_capital_btc=definition.initial_cash_btc,
        schema_version=definition.schema_version,
        decision_resolution_hours=definition.decision_resolution_hours,
        execution_model_id=exec_id,
        fee_rate_amount=definition.execution_profile.fee_rate_amount,
        fee_cap_ratio=definition.execution_profile.fee_cap_ratio,
        slippage_btc=definition.execution_profile.slippage_btc,
        stress_reserve_ratio=definition.stress_reserve_ratio,
        strategy_params=tuple(definition.strategy_params),
        deterministic_seed=definition.deterministic_seed,
    )
