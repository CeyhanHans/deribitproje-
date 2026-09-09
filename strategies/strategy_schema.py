from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class StrategyValidationError(ValueError):
    """Raised when a strategy definition is incomplete or inconsistent."""


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class StrikeSelectorType(str, Enum):
    EXACT_USD = "exact_usd"
    DELTA_TARGET = "delta_target"
    MONEYNESS_PERCENT = "moneyness_percent"
    NEAREST_PREMIUM_USD = "nearest_premium_usd"


class ExpirySelectorType(str, Enum):
    EXACT_DATE = "exact_date"
    DAYS_TO_EXPIRY = "days_to_expiry"
    NEAREST_DAYS_TO_EXPIRY = "nearest_days_to_expiry"


class ExitType(str, Enum):
    NET_USD_STOP_LOSS = "net_usd_stop_loss"
    NET_USD_TAKE_PROFIT = "net_usd_take_profit"
    TIME_EXIT = "time_exit"
    EXPIRY_EXIT = "expiry_exit"


ALLOWED_FEE_SPREAD_MODELS = {
    "deribit_inverse_option_bid_ask",
    "deribit_usdc_option_bid_ask",
    "custom_bid_ask_slippage",
}

REQUIRED_METRICS = {
    "trade_count",
    "net_pnl_usd",
    "max_drawdown_usd",
    "win_rate",
}

FORBIDDEN_METRICS = {"prediction_accuracy"}


@dataclass(frozen=True)
class Selector:
    selector_type: str
    value: Any


@dataclass(frozen=True)
class StrategyLeg:
    name: str
    option_type: str
    side: str
    quantity: float
    strike: Selector
    expiry: Selector


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


def validate_strategy(strategy: StrategyDefinition) -> None:
    _require_text(strategy.name, "name")
    _require_text(strategy.version, "version")
    _require_non_empty(strategy.legs, "legs")
    _require_non_empty(strategy.entry_rules, "entry_rules")
    _require_non_empty(strategy.exit_rules, "exit_rules")
    _validate_fee_spread_model(strategy.fee_spread_model)
    _validate_result_metrics(strategy.result_metrics)

    for index, leg in enumerate(strategy.legs):
        _validate_leg(leg, index)

    for index, entry_rule in enumerate(strategy.entry_rules):
        _validate_entry_rule(entry_rule, index)

    for index, exit_rule in enumerate(strategy.exit_rules):
        _validate_exit_rule(exit_rule, index)


def strategy_from_mapping(raw: Mapping[str, Any]) -> StrategyDefinition:
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
    return StrategyLeg(
        name=raw.get("name", ""),
        option_type=raw.get("option_type", ""),
        side=raw.get("side", ""),
        quantity=raw.get("quantity", 0),
        strike=_selector_from_mapping(raw.get("strike", {})),
        expiry=_selector_from_mapping(raw.get("expiry", {})),
    )


def _selector_from_mapping(raw: Mapping[str, Any]) -> Selector:
    return Selector(selector_type=raw.get("type", ""), value=raw.get("value"))


def _entry_rule_from_mapping(raw: Mapping[str, Any]) -> EntryRule:
    return EntryRule(name=raw.get("name", ""), parameters=raw.get("parameters", {}))


def _exit_rule_from_mapping(raw: Mapping[str, Any]) -> ExitRule:
    return ExitRule(exit_type=raw.get("type", ""), value=raw.get("value"))


def _metric_from_mapping(raw: Mapping[str, Any]) -> ResultMetric:
    return ResultMetric(name=raw.get("name", ""), description=raw.get("description", ""))


def _validate_leg(leg: StrategyLeg, index: int) -> None:
    prefix = f"legs[{index}]"
    _require_text(leg.name, f"{prefix}.name")
    _require_choice(leg.option_type, OptionType, f"{prefix}.option_type")
    _require_choice(leg.side, Side, f"{prefix}.side")
    _require_positive_number(leg.quantity, f"{prefix}.quantity")
    _validate_selector(leg.strike, StrikeSelectorType, f"{prefix}.strike")
    _validate_selector(leg.expiry, ExpirySelectorType, f"{prefix}.expiry")


def _validate_entry_rule(entry_rule: EntryRule, index: int) -> None:
    _require_text(entry_rule.name, f"entry_rules[{index}].name")
    if not isinstance(entry_rule.parameters, Mapping):
        raise StrategyValidationError(f"entry_rules[{index}].parameters must be a mapping")


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


def _validate_selector(selector: Selector, enum_cls: type[Enum], field_name: str) -> None:
    _require_choice(selector.selector_type, enum_cls, f"{field_name}.type")
    if selector.value in (None, ""):
        raise StrategyValidationError(f"{field_name}.value is required")


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
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise StrategyValidationError(f"{field_name} must be a positive number")
