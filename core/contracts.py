"""Immutable core contracts, schemas, and protocols for Deribit options backtest catalog.

Schema Version: 1.0
Authoritative specification for all downstream tasks (Tasks 2-24).
"""

from __future__ import annotations

import decimal
import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
    get_type_hints,
    runtime_checkable,
)

SCHEMA_VERSION: str = "1.0"


# ==============================================================================
# 1. Custom Exceptions
# ==============================================================================

class ContractError(Exception):
    """Base error for all contract and validation failures."""
    pass


class ValidationError(ContractError, ValueError):
    """Raised when data fails strict validation rules."""
    pass


class SchemaVersionMismatchError(ValidationError):
    """Raised when data has an incompatible schema_version."""
    pass


# ==============================================================================
# 2. Enumerations
# ==============================================================================

class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Currency(str, Enum):
    BTC = "BTC"
    USD = "USD"


class PriceBasis(str, Enum):
    TRADE = "trade"
    BID = "bid"
    ASK = "ask"
    MID = "mid"
    MARK = "mark"
    INDEX = "index"
    SETTLEMENT = "settlement"
    SYNTHETIC_PROXY = "synthetic_proxy"


class DataQuality(str, Enum):
    COMPLETE_DOWNLOAD = "complete_download"
    OBSERVED_TRADE = "observed_trade"
    OBSERVED_NO_TRADE = "observed_no_trade"
    NOT_LISTED = "not_listed"
    INCOMPLETE_DOWNLOAD = "incomplete_download"
    MALFORMED = "malformed"


class ExecutionModelId(str, Enum):
    MODEL_A_BID_ASK = "model_a_bid_ask"
    MODEL_B_MID_SLIPPAGE = "model_b_mid_slippage"
    MODEL_C_TRADE_BAR_CLOSE = "model_c_trade_bar_close"
    MODEL_D_QUOTE_L2 = "model_d_quote_l2"


class EntryType(str, Enum):
    PREMIUM = "premium"
    FEE = "fee"
    SETTLEMENT = "settlement"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    RESERVE_ADJUSTMENT = "reserve_adjustment"


class PositionStatus(str, Enum):
    OPEN = "open"
    PENDING = "pending"
    CLOSED = "closed"
    SETTLED = "settled"
    UNRESOLVED = "unresolved"


class ReasonCode(str, Enum):
    ENTRY_SIGNAL = "entry_signal"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TIME_EXPIRY = "time_expiry"
    CONTRACT_EXPIRY = "contract_expiry"
    MARKET_CLOSE = "market_close"
    DATA_MISSING = "data_missing"
    NO_CANDIDATE_FOUND = "no_candidate_found"
    STRESS_RESERVE_EXCEEDED = "stress_reserve_exceeded"
    UNSPECIFIED = "unspecified"


class TieBreakPolicy(str, Enum):
    LOWER = "lower"
    HIGHER = "higher"


# ==============================================================================
# 3. Validation Helpers
# ==============================================================================

def validate_not_bool(val: Any, name: str) -> None:
    """Ensure val is not a boolean (since bool inherits from int in Python)."""
    if isinstance(val, bool):
        raise ValidationError(f"{name} cannot be a boolean (got {val!r})")


def validate_schema_version(val: Any) -> str:
    """Validate schema version matches SCHEMA_VERSION."""
    if val != SCHEMA_VERSION:
        raise SchemaVersionMismatchError(
            f"Schema version mismatch: expected {SCHEMA_VERSION!r}, got {val!r}"
        )
    return SCHEMA_VERSION


def validate_decimal(
    val: Any,
    name: str,
    *,
    allow_none: bool = False,
    non_negative: bool = False,
    strictly_positive: bool = False,
) -> Optional[Decimal]:
    """Strictly convert and validate a Decimal value. Rejects bool, NaN, and Infinity."""
    if val is None:
        if allow_none:
            return None
        raise ValidationError(f"{name} cannot be None")
    validate_not_bool(val, name)
    try:
        if isinstance(val, float):
            if math.isnan(val) or math.isinf(val):
                raise ValidationError(f"{name} cannot be NaN or Infinity")
            # Convert float via str to prevent float binary representation artifacts
            d = Decimal(str(val))
        elif isinstance(val, (int, str)):
            d = Decimal(str(val))
        elif isinstance(val, Decimal):
            d = val
        else:
            raise ValidationError(f"{name} must be a Decimal, str, or int (got {type(val).__name__})")
    except decimal.InvalidOperation as exc:
        raise ValidationError(f"Invalid decimal value for {name}: {val!r}") from exc

    if d.is_nan():
        raise ValidationError(f"{name} cannot be NaN")
    if d.is_infinite():
        raise ValidationError(f"{name} cannot be Infinity")
    if non_negative and d < Decimal("0"):
        raise ValidationError(f"{name} must be non-negative (got {d})")
    if strictly_positive and d <= Decimal("0"):
        raise ValidationError(f"{name} must be strictly positive (got {d})")
    return d


def validate_int(
    val: Any,
    name: str,
    *,
    allow_none: bool = False,
    non_negative: bool = False,
    strictly_positive: bool = False,
) -> Optional[int]:
    """Strictly validate an integer. Rejects bool and non-integer types."""
    if val is None:
        if allow_none:
            return None
        raise ValidationError(f"{name} cannot be None")
    validate_not_bool(val, name)
    if not isinstance(val, int):
        raise ValidationError(f"{name} must be an int (got {type(val).__name__})")
    if non_negative and val < 0:
        raise ValidationError(f"{name} must be non-negative (got {val})")
    if strictly_positive and val <= 0:
        raise ValidationError(f"{name} must be strictly positive (got {val})")
    return val


def validate_timestamp_ms(val: Any, name: str, *, allow_none: bool = False) -> Optional[int]:
    """Validate UTC integer timestamp in milliseconds."""
    return validate_int(val, name, allow_none=allow_none, non_negative=True)


def validate_enum(val: Any, enum_cls: Type[Enum], name: str) -> Any:
    """Validate and convert value to the target Enum."""
    if isinstance(val, enum_cls):
        return val
    try:
        return enum_cls(val)
    except (ValueError, KeyError) as exc:
        allowed = [e.value for e in enum_cls]
        raise ValidationError(
            f"Invalid {name}: {val!r}. Allowed values: {allowed}"
        ) from exc


# ==============================================================================
# 4. Frozen Core Dataclasses
# ==============================================================================

@dataclass(frozen=True)
class RunConfig:
    """Configuration for a backtest run."""
    strategy_name: str
    start_ms: int
    end_ms: int
    initial_capital_btc: Decimal
    schema_version: str = SCHEMA_VERSION
    decision_resolution_hours: int = 1
    execution_model_id: ExecutionModelId = ExecutionModelId.MODEL_A_BID_ASK
    fee_rate_amount: Decimal = Decimal("0.0003")
    fee_cap_ratio: Decimal = Decimal("0.125")
    slippage_btc: Decimal = Decimal("0")
    stress_reserve_ratio: Decimal = Decimal("0.5")
    strategy_params: Tuple[Tuple[str, str], ...] = ()
    deterministic_seed: int = 42

    def __post_init__(self) -> None:
        validate_schema_version(self.schema_version)
        if not self.strategy_name or not isinstance(self.strategy_name, str):
            raise ValidationError("strategy_name must be a non-empty string")
        object.__setattr__(self, "start_ms", validate_timestamp_ms(self.start_ms, "start_ms"))
        object.__setattr__(self, "end_ms", validate_timestamp_ms(self.end_ms, "end_ms"))
        if self.start_ms >= self.end_ms:
            raise ValidationError(
                f"Time range must satisfy start_ms < end_ms (got start_ms={self.start_ms}, end_ms={self.end_ms})"
            )
        object.__setattr__(
            self,
            "initial_capital_btc",
            validate_decimal(self.initial_capital_btc, "initial_capital_btc", strictly_positive=True),
        )
        res_hours = validate_int(self.decision_resolution_hours, "decision_resolution_hours", strictly_positive=True)
        if res_hours not in (1, 24):
            raise ValidationError(
                f"decision_resolution_hours must be 1 (hourly) or 24 (daily), got {res_hours}"
            )
        object.__setattr__(self, "decision_resolution_hours", res_hours)
        object.__setattr__(
            self,
            "execution_model_id",
            validate_enum(self.execution_model_id, ExecutionModelId, "execution_model_id"),
        )
        object.__setattr__(
            self, "fee_rate_amount", validate_decimal(self.fee_rate_amount, "fee_rate_amount", non_negative=True)
        )
        object.__setattr__(
            self, "fee_cap_ratio", validate_decimal(self.fee_cap_ratio, "fee_cap_ratio", non_negative=True)
        )
        object.__setattr__(
            self, "slippage_btc", validate_decimal(self.slippage_btc, "slippage_btc", non_negative=True)
        )
        object.__setattr__(
            self,
            "stress_reserve_ratio",
            validate_decimal(self.stress_reserve_ratio, "stress_reserve_ratio", non_negative=True),
        )
        object.__setattr__(self, "deterministic_seed", validate_int(self.deterministic_seed, "deterministic_seed"))


@dataclass(frozen=True)
class Instrument:
    """Specification of an options contract."""
    instrument_name: str
    option_type: OptionType
    strike_usd: Decimal
    creation_ms: int
    expiry_ms: int
    base_currency: Currency = Currency.BTC
    quote_currency: Currency = Currency.BTC
    counter_currency: Currency = Currency.USD
    settlement_currency: Currency = Currency.BTC
    contract_size: Decimal = Decimal("1.0")
    min_qty: Decimal = Decimal("0.1")
    qty_step: Optional[Decimal] = None
    qty_step_known: bool = False
    tick_size: Decimal = Decimal("0.0005")
    source_ref: str = ""

    def __post_init__(self) -> None:
        if not self.instrument_name or not isinstance(self.instrument_name, str):
            raise ValidationError("instrument_name must be a non-empty string")
        object.__setattr__(self, "option_type", validate_enum(self.option_type, OptionType, "option_type"))
        object.__setattr__(
            self, "strike_usd", validate_decimal(self.strike_usd, "strike_usd", strictly_positive=True)
        )
        object.__setattr__(self, "creation_ms", validate_timestamp_ms(self.creation_ms, "creation_ms"))
        object.__setattr__(self, "expiry_ms", validate_timestamp_ms(self.expiry_ms, "expiry_ms"))
        if self.creation_ms >= self.expiry_ms:
            raise ValidationError(
                f"Instrument creation_ms must precede expiry_ms (got creation_ms={self.creation_ms}, expiry_ms={self.expiry_ms})"
            )
        object.__setattr__(self, "base_currency", validate_enum(self.base_currency, Currency, "base_currency"))
        object.__setattr__(self, "quote_currency", validate_enum(self.quote_currency, Currency, "quote_currency"))
        object.__setattr__(
            self, "counter_currency", validate_enum(self.counter_currency, Currency, "counter_currency")
        )
        object.__setattr__(
            self, "settlement_currency", validate_enum(self.settlement_currency, Currency, "settlement_currency")
        )
        object.__setattr__(
            self, "contract_size", validate_decimal(self.contract_size, "contract_size", strictly_positive=True)
        )
        object.__setattr__(self, "min_qty", validate_decimal(self.min_qty, "min_qty", strictly_positive=True))
        object.__setattr__(self, "tick_size", validate_decimal(self.tick_size, "tick_size", strictly_positive=True))
        if self.qty_step is not None:
            object.__setattr__(self, "qty_step", validate_decimal(self.qty_step, "qty_step", strictly_positive=True))
        if not isinstance(self.qty_step_known, bool):
            raise ValidationError("qty_step_known must be a boolean")


Contract = Instrument  # Alias for Contract


@dataclass(frozen=True)
class TradeTick:
    """Historical trade tick from public/history API."""
    instrument_name: str
    trade_id: str
    trade_seq: int
    timestamp_ms: int
    price_btc: Decimal
    quantity_contracts: Decimal
    source_ref: str = ""

    def __post_init__(self) -> None:
        if not self.instrument_name:
            raise ValidationError("instrument_name cannot be empty")
        if not self.trade_id:
            raise ValidationError("trade_id cannot be empty")
        object.__setattr__(self, "trade_seq", validate_int(self.trade_seq, "trade_seq", non_negative=True))
        object.__setattr__(self, "timestamp_ms", validate_timestamp_ms(self.timestamp_ms, "timestamp_ms"))
        object.__setattr__(
            self, "price_btc", validate_decimal(self.price_btc, "price_btc", strictly_positive=True)
        )
        object.__setattr__(
            self,
            "quantity_contracts",
            validate_decimal(self.quantity_contracts, "quantity_contracts", strictly_positive=True),
        )


@dataclass(frozen=True)
class PriceObservation:
    """Price observation at a given point or interval in time."""
    instrument_name: str
    observed_at_ms: int
    available_at_ms: int
    price_basis: PriceBasis
    trade_price_btc: Optional[Decimal] = None
    bid_price_btc: Optional[Decimal] = None
    ask_price_btc: Optional[Decimal] = None
    mark_price_btc: Optional[Decimal] = None
    trade_size: Optional[Decimal] = None
    bid_size: Optional[Decimal] = None
    ask_size: Optional[Decimal] = None
    interval_start_ms: Optional[int] = None
    interval_end_ms: Optional[int] = None
    underlying_price_usd: Optional[Decimal] = None
    index_price_usd: Optional[Decimal] = None
    quality: DataQuality = DataQuality.OBSERVED_TRADE
    source_ref: str = ""

    def __post_init__(self) -> None:
        if not self.instrument_name:
            raise ValidationError("instrument_name cannot be empty")
        object.__setattr__(self, "observed_at_ms", validate_timestamp_ms(self.observed_at_ms, "observed_at_ms"))
        object.__setattr__(self, "available_at_ms", validate_timestamp_ms(self.available_at_ms, "available_at_ms"))
        if self.available_at_ms < self.observed_at_ms:
            raise ValidationError(
                f"available_at_ms ({self.available_at_ms}) cannot be earlier than observed_at_ms ({self.observed_at_ms})"
            )
        object.__setattr__(self, "price_basis", validate_enum(self.price_basis, PriceBasis, "price_basis"))
        object.__setattr__(self, "quality", validate_enum(self.quality, DataQuality, "quality"))
        if self.trade_price_btc is not None:
            object.__setattr__(
                self, "trade_price_btc", validate_decimal(self.trade_price_btc, "trade_price_btc", non_negative=True)
            )
        if self.bid_price_btc is not None:
            object.__setattr__(
                self, "bid_price_btc", validate_decimal(self.bid_price_btc, "bid_price_btc", non_negative=True)
            )
        if self.ask_price_btc is not None:
            object.__setattr__(
                self, "ask_price_btc", validate_decimal(self.ask_price_btc, "ask_price_btc", non_negative=True)
            )
        if self.mark_price_btc is not None:
            object.__setattr__(
                self, "mark_price_btc", validate_decimal(self.mark_price_btc, "mark_price_btc", non_negative=True)
            )
        if self.trade_size is not None:
            object.__setattr__(
                self, "trade_size", validate_decimal(self.trade_size, "trade_size", non_negative=True)
            )
        if self.bid_size is not None:
            object.__setattr__(
                self, "bid_size", validate_decimal(self.bid_size, "bid_size", non_negative=True)
            )
        if self.ask_size is not None:
            object.__setattr__(
                self, "ask_size", validate_decimal(self.ask_size, "ask_size", non_negative=True)
            )
        if self.interval_start_ms is not None:
            object.__setattr__(
                self, "interval_start_ms", validate_timestamp_ms(self.interval_start_ms, "interval_start_ms")
            )
        if self.interval_end_ms is not None:
            object.__setattr__(
                self, "interval_end_ms", validate_timestamp_ms(self.interval_end_ms, "interval_end_ms")
            )
        if self.interval_start_ms is not None and self.interval_end_ms is not None:
            if self.interval_start_ms > self.interval_end_ms:
                raise ValidationError("interval_start_ms cannot exceed interval_end_ms")
            if self.observed_at_ms < self.interval_start_ms:
                raise ValidationError(
                    f"observed_at_ms ({self.observed_at_ms}) cannot precede interval_start_ms ({self.interval_start_ms})"
                )
        if self.interval_end_ms is not None:
            if self.available_at_ms < self.interval_end_ms:
                raise ValidationError(
                    f"available_at_ms ({self.available_at_ms}) cannot be earlier than interval_end_ms ({self.interval_end_ms}) "
                    f"for interval observations; bucket lookahead is prohibited."
                )
        if self.underlying_price_usd is not None:
            object.__setattr__(
                self,
                "underlying_price_usd",
                validate_decimal(self.underlying_price_usd, "underlying_price_usd", strictly_positive=True),
            )
        if self.index_price_usd is not None:
            object.__setattr__(
                self,
                "index_price_usd",
                validate_decimal(self.index_price_usd, "index_price_usd", strictly_positive=True),
            )


@dataclass(frozen=True)
class CoverageReport:
    """Coverage report for an instrument over an evaluation window."""
    instrument_name: str
    start_ms: int
    end_ms: int
    expected_intervals: int
    observed_intervals: int
    missing_intervals: int
    coverage_ratio: Decimal
    quality_breakdown: Tuple[Tuple[str, int], ...] = ()
    status: str = ""

    def __post_init__(self) -> None:
        if not self.instrument_name:
            raise ValidationError("instrument_name cannot be empty")
        object.__setattr__(self, "start_ms", validate_timestamp_ms(self.start_ms, "start_ms"))
        object.__setattr__(self, "end_ms", validate_timestamp_ms(self.end_ms, "end_ms"))
        if self.start_ms >= self.end_ms:
            raise ValidationError("start_ms must be less than end_ms")
        object.__setattr__(
            self, "expected_intervals", validate_int(self.expected_intervals, "expected_intervals", non_negative=True)
        )
        object.__setattr__(
            self, "observed_intervals", validate_int(self.observed_intervals, "observed_intervals", non_negative=True)
        )
        object.__setattr__(
            self, "missing_intervals", validate_int(self.missing_intervals, "missing_intervals", non_negative=True)
        )
        c_ratio = validate_decimal(self.coverage_ratio, "coverage_ratio", non_negative=True)
        if c_ratio < Decimal("0") or c_ratio > Decimal("1"):
            raise ValidationError(f"coverage_ratio must be between 0 and 1, got {c_ratio}")
        object.__setattr__(self, "coverage_ratio", c_ratio)


@dataclass(frozen=True)
class SelectionDecision:
    """Decision made when selecting instruments for a strategy."""
    decision_at_ms: int
    strategy_name: str
    selected_instruments: Tuple[Instrument, ...]
    underlying_price_usd: Decimal
    target_expiry_ms: Optional[int] = None
    common_strike_usd: Optional[Decimal] = None
    reason_code: ReasonCode = ReasonCode.ENTRY_SIGNAL
    metadata: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_at_ms", validate_timestamp_ms(self.decision_at_ms, "decision_at_ms"))
        if not self.strategy_name:
            raise ValidationError("strategy_name cannot be empty")
        object.__setattr__(
            self,
            "underlying_price_usd",
            validate_decimal(self.underlying_price_usd, "underlying_price_usd", strictly_positive=True),
        )
        if self.target_expiry_ms is not None:
            object.__setattr__(self, "target_expiry_ms", validate_timestamp_ms(self.target_expiry_ms, "target_expiry_ms"))
        if self.common_strike_usd is not None:
            object.__setattr__(
                self,
                "common_strike_usd",
                validate_decimal(self.common_strike_usd, "common_strike_usd", strictly_positive=True),
            )
        object.__setattr__(self, "reason_code", validate_enum(self.reason_code, ReasonCode, "reason_code"))


@dataclass(frozen=True)
class Signal:
    """Trade signal emitted by strategy evaluation."""
    signal_id: str
    decision_at_ms: int
    available_at_ms: int
    strategy_name: str
    signal_type: str
    instrument_names: Tuple[str, ...]
    target_quantities: Tuple[Decimal, ...]
    sides: Tuple[Side, ...]
    reason_code: ReasonCode
    metadata: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.signal_id:
            raise ValidationError("signal_id cannot be empty")
        object.__setattr__(self, "decision_at_ms", validate_timestamp_ms(self.decision_at_ms, "decision_at_ms"))
        object.__setattr__(self, "available_at_ms", validate_timestamp_ms(self.available_at_ms, "available_at_ms"))
        if self.available_at_ms < self.decision_at_ms:
            raise ValidationError("available_at_ms cannot be earlier than decision_at_ms")
        if not self.strategy_name:
            raise ValidationError("strategy_name cannot be empty")
        if not self.signal_type:
            raise ValidationError("signal_type cannot be empty")
        object.__setattr__(self, "reason_code", validate_enum(self.reason_code, ReasonCode, "reason_code"))
        n = len(self.instrument_names)
        if len(self.target_quantities) != n or len(self.sides) != n:
            raise ValidationError(
                f"Signal arrays length mismatch: instruments={n}, quantities={len(self.target_quantities)}, sides={len(self.sides)}"
            )
        object.__setattr__(self, "sides", tuple(validate_enum(s, Side, "side") for s in self.sides))
        object.__setattr__(
            self,
            "target_quantities",
            tuple(validate_decimal(q, "target_quantity", strictly_positive=True) for q in self.target_quantities),
        )


@dataclass(frozen=True)
class OrderIntent:
    """Actionable order intent generated from a signal."""
    order_id: str
    position_id: str
    leg_id: str
    instrument_name: str
    side: Side
    quantity: Decimal
    decision_at_ms: int
    eligible_after_ms: int
    expires_at_ms: int
    reason: ReasonCode = ReasonCode.ENTRY_SIGNAL

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValidationError("order_id cannot be empty")
        if not self.position_id:
            raise ValidationError("position_id cannot be empty")
        if not self.leg_id:
            raise ValidationError("leg_id cannot be empty")
        if not self.instrument_name:
            raise ValidationError("instrument_name cannot be empty")
        object.__setattr__(self, "side", validate_enum(self.side, Side, "side"))
        object.__setattr__(self, "quantity", validate_decimal(self.quantity, "quantity", strictly_positive=True))
        object.__setattr__(self, "decision_at_ms", validate_timestamp_ms(self.decision_at_ms, "decision_at_ms"))
        object.__setattr__(self, "eligible_after_ms", validate_timestamp_ms(self.eligible_after_ms, "eligible_after_ms"))
        object.__setattr__(self, "expires_at_ms", validate_timestamp_ms(self.expires_at_ms, "expires_at_ms"))
        if self.decision_at_ms > self.eligible_after_ms:
            raise ValidationError("decision_at_ms cannot be after eligible_after_ms")
        if self.eligible_after_ms >= self.expires_at_ms:
            raise ValidationError("eligible_after_ms must be less than expires_at_ms")
        object.__setattr__(self, "reason", validate_enum(self.reason, ReasonCode, "reason"))


@dataclass(frozen=True)
class Fill:
    """Execution fill details."""
    fill_id: str
    order_id: str
    position_id: str
    leg_id: str
    instrument_name: str
    side: Side
    actual_ms: int
    model_timestamp_ms: int
    quantity: Decimal
    price_btc: Decimal
    fee_btc: Decimal
    reference_price_btc: Decimal
    spread_slippage_cost_btc: Decimal
    execution_model_id: ExecutionModelId
    source_ref: str = ""

    def __post_init__(self) -> None:
        if not self.fill_id:
            raise ValidationError("fill_id cannot be empty")
        if not self.order_id:
            raise ValidationError("order_id cannot be empty")
        if not self.position_id:
            raise ValidationError("position_id cannot be empty")
        if not self.leg_id:
            raise ValidationError("leg_id cannot be empty")
        if not self.instrument_name:
            raise ValidationError("instrument_name cannot be empty")
        object.__setattr__(self, "side", validate_enum(self.side, Side, "side"))
        object.__setattr__(self, "actual_ms", validate_timestamp_ms(self.actual_ms, "actual_ms"))
        object.__setattr__(self, "model_timestamp_ms", validate_timestamp_ms(self.model_timestamp_ms, "model_timestamp_ms"))
        object.__setattr__(self, "quantity", validate_decimal(self.quantity, "quantity", strictly_positive=True))
        object.__setattr__(self, "price_btc", validate_decimal(self.price_btc, "price_btc", non_negative=True))
        object.__setattr__(self, "fee_btc", validate_decimal(self.fee_btc, "fee_btc", non_negative=True))
        object.__setattr__(self, "reference_price_btc", validate_decimal(self.reference_price_btc, "reference_price_btc", non_negative=True))
        object.__setattr__(self, "spread_slippage_cost_btc", validate_decimal(self.spread_slippage_cost_btc, "spread_slippage_cost_btc", non_negative=True))
        object.__setattr__(self, "execution_model_id", validate_enum(self.execution_model_id, ExecutionModelId, "execution_model_id"))


@dataclass(frozen=True)
class FillDecision:
    """Outcome of attempting to fill an order against observations."""
    order_id: str
    filled: bool
    fill: Optional[Fill] = None
    rejection_reason: Optional[str] = None
    observation_used: Optional[PriceObservation] = None

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValidationError("order_id cannot be empty")
        if not isinstance(self.filled, bool):
            raise ValidationError("filled must be a boolean")
        if self.filled and self.fill is None:
            raise ValidationError("fill cannot be None when filled is True")
        if not self.filled and self.fill is not None:
            raise ValidationError("fill must be None when filled is False")


@dataclass(frozen=True)
class LedgerEntry:
    """Financial transaction entry in the BTC ledger."""
    event_id: str
    position_id: Optional[str]
    timestamp_ms: int
    currency: Currency
    amount: Decimal
    entry_type: EntryType
    balance_after: Decimal
    description: str = ""

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValidationError("event_id cannot be empty")
        object.__setattr__(self, "timestamp_ms", validate_timestamp_ms(self.timestamp_ms, "timestamp_ms"))
        object.__setattr__(self, "currency", validate_enum(self.currency, Currency, "currency"))
        object.__setattr__(self, "amount", validate_decimal(self.amount, "amount"))
        object.__setattr__(self, "entry_type", validate_enum(self.entry_type, EntryType, "entry_type"))
        object.__setattr__(self, "balance_after", validate_decimal(self.balance_after, "balance_after"))


@dataclass(frozen=True)
class Position:
    """Multi-leg position state."""
    position_id: str
    strategy_name: str
    status: PositionStatus
    legs: Tuple[Instrument, ...]
    leg_quantities: Tuple[Decimal, ...]
    opened_at_ms: int
    closed_at_ms: Optional[int] = None
    open_fills: Tuple[Fill, ...] = ()
    close_fills: Tuple[Fill, ...] = ()
    realized_pnl_btc: Decimal = Decimal("0")
    realized_pnl_usd: Decimal = Decimal("0")
    exit_reason: Optional[ReasonCode] = None

    def __post_init__(self) -> None:
        if not self.position_id:
            raise ValidationError("position_id cannot be empty")
        if not self.strategy_name:
            raise ValidationError("strategy_name cannot be empty")
        object.__setattr__(self, "status", validate_enum(self.status, PositionStatus, "status"))
        object.__setattr__(self, "opened_at_ms", validate_timestamp_ms(self.opened_at_ms, "opened_at_ms"))
        if self.closed_at_ms is not None:
            object.__setattr__(self, "closed_at_ms", validate_timestamp_ms(self.closed_at_ms, "closed_at_ms"))
            if self.closed_at_ms < self.opened_at_ms:
                raise ValidationError("closed_at_ms cannot be earlier than opened_at_ms")
        if len(self.legs) != len(self.leg_quantities):
            raise ValidationError("Length of legs must equal length of leg_quantities")
        object.__setattr__(
            self,
            "leg_quantities",
            tuple(validate_decimal(q, "leg_quantity", strictly_positive=True) for q in self.leg_quantities),
        )
        object.__setattr__(self, "realized_pnl_btc", validate_decimal(self.realized_pnl_btc, "realized_pnl_btc"))
        object.__setattr__(self, "realized_pnl_usd", validate_decimal(self.realized_pnl_usd, "realized_pnl_usd"))
        if self.exit_reason is not None:
            object.__setattr__(self, "exit_reason", validate_enum(self.exit_reason, ReasonCode, "exit_reason"))


@dataclass(frozen=True)
class EquityPoint:
    """Portfolio valuation snapshot at a point in time."""
    timestamp_ms: int
    cash_balance_btc: Decimal
    reserved_balance_btc: Decimal
    unrealized_pnl_btc: Decimal
    total_equity_btc: Decimal
    underlying_price_usd: Decimal
    total_equity_usd: Decimal
    benchmark_buy_and_hold_btc: Decimal
    is_valuation_reliable: bool = True
    quality_note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_ms", validate_timestamp_ms(self.timestamp_ms, "timestamp_ms"))
        object.__setattr__(self, "cash_balance_btc", validate_decimal(self.cash_balance_btc, "cash_balance_btc"))
        object.__setattr__(self, "reserved_balance_btc", validate_decimal(self.reserved_balance_btc, "reserved_balance_btc", non_negative=True))
        object.__setattr__(self, "unrealized_pnl_btc", validate_decimal(self.unrealized_pnl_btc, "unrealized_pnl_btc"))
        object.__setattr__(self, "total_equity_btc", validate_decimal(self.total_equity_btc, "total_equity_btc"))
        object.__setattr__(self, "underlying_price_usd", validate_decimal(self.underlying_price_usd, "underlying_price_usd", strictly_positive=True))
        object.__setattr__(self, "total_equity_usd", validate_decimal(self.total_equity_usd, "total_equity_usd"))
        object.__setattr__(self, "benchmark_buy_and_hold_btc", validate_decimal(self.benchmark_buy_and_hold_btc, "benchmark_buy_and_hold_btc", non_negative=True))
        if not isinstance(self.is_valuation_reliable, bool):
            raise ValidationError("is_valuation_reliable must be a boolean")


@dataclass(frozen=True)
class PortfolioState:
    """State of the backtest portfolio."""
    timestamp_ms: int
    cash_balance_btc: Decimal
    reserved_balance_btc: Decimal
    open_positions: Tuple[Position, ...] = ()
    closed_positions: Tuple[Position, ...] = ()
    ledger: Tuple[LedgerEntry, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_ms", validate_timestamp_ms(self.timestamp_ms, "timestamp_ms"))
        object.__setattr__(self, "cash_balance_btc", validate_decimal(self.cash_balance_btc, "cash_balance_btc"))
        object.__setattr__(self, "reserved_balance_btc", validate_decimal(self.reserved_balance_btc, "reserved_balance_btc", non_negative=True))


@dataclass(frozen=True)
class SettlementObservation:
    """Official delivery/settlement observation at contract expiry."""
    instrument_name: str
    expiry_ms: int
    settlement_price_usd: Decimal
    settlement_price_btc: Decimal
    is_official_delivery: bool
    source_ref: str = ""

    def __post_init__(self) -> None:
        if not self.instrument_name:
            raise ValidationError("instrument_name cannot be empty")
        object.__setattr__(self, "expiry_ms", validate_timestamp_ms(self.expiry_ms, "expiry_ms"))
        object.__setattr__(self, "settlement_price_usd", validate_decimal(self.settlement_price_usd, "settlement_price_usd", strictly_positive=True))
        object.__setattr__(self, "settlement_price_btc", validate_decimal(self.settlement_price_btc, "settlement_price_btc", non_negative=True))
        if not isinstance(self.is_official_delivery, bool):
            raise ValidationError("is_official_delivery must be a boolean")


@dataclass(frozen=True)
class Metrics:
    """Performance and risk metrics."""
    total_trades: int
    winning_trades: int
    losing_trades: int
    break_even_trades: int
    win_rate: Optional[Decimal] = None
    net_pnl_btc: Decimal = Decimal("0")
    net_pnl_usd: Decimal = Decimal("0")
    max_drawdown_btc: Decimal = Decimal("0")
    max_drawdown_pct: Decimal = Decimal("0")
    profit_factor: Optional[Decimal] = None
    sharpe_ratio: Optional[Decimal] = None
    coverage_summary: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "total_trades", validate_int(self.total_trades, "total_trades", non_negative=True))
        object.__setattr__(self, "winning_trades", validate_int(self.winning_trades, "winning_trades", non_negative=True))
        object.__setattr__(self, "losing_trades", validate_int(self.losing_trades, "losing_trades", non_negative=True))
        object.__setattr__(self, "break_even_trades", validate_int(self.break_even_trades, "break_even_trades", non_negative=True))
        if self.total_trades != (self.winning_trades + self.losing_trades + self.break_even_trades):
            raise ValidationError("total_trades must equal winning + losing + break_even trades")
        if self.total_trades == 0:
            if self.win_rate is not None:
                w = validate_decimal(self.win_rate, "win_rate", non_negative=True)
                if w < Decimal("0") or w > Decimal("1"):
                    raise ValidationError(f"win_rate must be between 0 and 1, got {w}")
                object.__setattr__(self, "win_rate", w)
        else:
            if self.win_rate is None:
                raise ValidationError("win_rate cannot be None when total_trades > 0")
            w = validate_decimal(self.win_rate, "win_rate", non_negative=True)
            if w < Decimal("0") or w > Decimal("1"):
                raise ValidationError(f"win_rate must be between 0 and 1, got {w}")
            object.__setattr__(self, "win_rate", w)
        object.__setattr__(self, "net_pnl_btc", validate_decimal(self.net_pnl_btc, "net_pnl_btc"))
        object.__setattr__(self, "net_pnl_usd", validate_decimal(self.net_pnl_usd, "net_pnl_usd"))
        object.__setattr__(self, "max_drawdown_btc", validate_decimal(self.max_drawdown_btc, "max_drawdown_btc", non_negative=True))
        object.__setattr__(self, "max_drawdown_pct", validate_decimal(self.max_drawdown_pct, "max_drawdown_pct", non_negative=True))
        if self.profit_factor is not None:
            object.__setattr__(self, "profit_factor", validate_decimal(self.profit_factor, "profit_factor", non_negative=True))
        if self.sharpe_ratio is not None:
            object.__setattr__(self, "sharpe_ratio", validate_decimal(self.sharpe_ratio, "sharpe_ratio"))


@dataclass(frozen=True)
class ReportManifest:
    """Manifest of generated report artifacts."""
    report_id: str
    generated_at_ms: int
    files: Tuple[str, ...]
    summary: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.report_id:
            raise ValidationError("report_id cannot be empty")
        validate_timestamp_ms(self.generated_at_ms, "generated_at_ms")


@dataclass(frozen=True)
class RunResult:
    """Immutable result of an entire backtest run."""
    config_hash: str
    data_hash: str
    code_hash: str
    run_timestamp_ms: int
    start_ms: int
    end_ms: int
    schema_version: str = SCHEMA_VERSION
    orders: Tuple[OrderIntent, ...] = ()
    fills: Tuple[Fill, ...] = ()
    ledger: Tuple[LedgerEntry, ...] = ()
    trades: Tuple[Position, ...] = ()
    equity_curve: Tuple[EquityPoint, ...] = ()
    coverage_reports: Tuple[CoverageReport, ...] = ()
    metrics: Optional[Metrics] = None
    open_positions_at_end: Tuple[Position, ...] = ()
    skipped_reasons: Tuple[Tuple[str, int], ...] = ()
    warnings: Tuple[str, ...] = ()
    capabilities: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_schema_version(self.schema_version)
        if not self.config_hash or not self.data_hash or not self.code_hash:
            raise ValidationError("config_hash, data_hash, and code_hash must be non-empty strings")
        validate_timestamp_ms(self.run_timestamp_ms, "run_timestamp_ms")
        validate_timestamp_ms(self.start_ms, "start_ms")
        validate_timestamp_ms(self.end_ms, "end_ms")
        if self.start_ms >= self.end_ms:
            raise ValidationError("start_ms must be less than end_ms")


@dataclass(frozen=True)
class DownloadResult:
    """Result of downloading historical data."""
    success: bool
    ticks_count: int
    start_ms: int
    end_ms: int
    output_path: Optional[str] = None
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise ValidationError("success must be a boolean")
        validate_int(self.ticks_count, "ticks_count", non_negative=True)
        validate_timestamp_ms(self.start_ms, "start_ms")
        validate_timestamp_ms(self.end_ms, "end_ms")


# ==============================================================================
# 5. Domain Object Protocols
# ==============================================================================

@runtime_checkable
class StrategyProtocol(Protocol):
    """Protocol representing a strategy definition."""
    @property
    def name(self) -> str:
        ...


@runtime_checkable
class AsOfViewProtocol(Protocol):
    """Protocol representing an as-of market view for contract selection."""
    @property
    def decision_at_ms(self) -> int:
        ...

    @property
    def underlying_price_usd(self) -> Decimal:
        ...

    @property
    def instruments(self) -> Sequence[Instrument]:
        ...


@runtime_checkable
class HistoryAsOfProtocol(Protocol):
    """Protocol representing historical observations up to decision time."""
    @property
    def decision_at_ms(self) -> int:
        ...


@runtime_checkable
class ExecutionConfigProtocol(Protocol):
    """Protocol representing execution model configuration."""
    @property
    def execution_model_id(self) -> ExecutionModelId:
        ...


@runtime_checkable
class FeeScheduleProtocol(Protocol):
    """Protocol representing exchange fee schedule."""
    @property
    def rate(self) -> Decimal:
        ...


@runtime_checkable
class DataBundleProtocol(Protocol):
    """Protocol representing data bundle for backtesting."""
    @property
    def instruments(self) -> Sequence[Instrument]:
        ...

    @property
    def observations(self) -> Sequence[PriceObservation]:
        ...

    @property
    def settlements(self) -> Sequence[SettlementObservation]:
        ...


@runtime_checkable
class DownloadPlanProtocol(Protocol):
    """Protocol representing history download plan."""
    @property
    def instrument_name(self) -> str:
        ...


# ==============================================================================
# 6. Provider Protocols
# ==============================================================================

@runtime_checkable
class HistoricalTradeProvider(Protocol):
    """Protocol for fetching historical trade data."""
    def fetch(self, plan: Union[DownloadPlanProtocol, Mapping[str, Any], Any]) -> DownloadResult:
        ...


@runtime_checkable
class InstrumentProvider(Protocol):
    """Protocol for loading instruments / active universe."""
    def load(
        self, snapshot: Union[Path, str, Sequence[Mapping[str, Any]], Sequence[Instrument], Any]
    ) -> Sequence[Instrument]:
        ...


@runtime_checkable
class PriceProvider(Protocol):
    """Protocol for iterating price observations across time."""
    def iter_events(
        self, start_ms: int, end_ms: int, instruments: Sequence[str]
    ) -> Iterator[PriceObservation]:
        ...


@runtime_checkable
class SettlementProvider(Protocol):
    """Protocol for fetching settlement observations."""
    def get(self, instrument: str, expiry_ms: int) -> Optional[SettlementObservation]:
        ...


@runtime_checkable
class Store(Protocol):
    """Protocol for persistence and cache storage with resume capability."""
    def put(self, key: str, value: bytes) -> None:
        ...

    def get(self, key: str) -> Optional[bytes]:
        ...

    def query(self, prefix: str) -> Iterator[Tuple[str, bytes]]:
        ...

    def resume_state(self) -> Dict[str, Any]:
        ...


# ==============================================================================
# 7. Module Function Protocols (Locked Signatures)
# ==============================================================================

@runtime_checkable
class LegSelectorProtocol(Protocol):
    """Protocol for selecting legs for option strategies."""
    def __call__(
        self,
        strategy: Union[StrategyProtocol, Mapping[str, Any]],
        asof_view: Union[AsOfViewProtocol, Mapping[str, Any]],
    ) -> SelectionDecision:
        ...


@runtime_checkable
class SignalEvaluatorProtocol(Protocol):
    """Protocol for evaluating trading signals at decision time."""
    def __call__(
        self,
        config: Union[RunConfig, Mapping[str, Any]],
        history_asof: Union[HistoryAsOfProtocol, Sequence[PriceObservation], Mapping[str, Any]],
        portfolio_view: PortfolioState,
    ) -> Tuple[Signal, ...]:
        ...


@runtime_checkable
class FillSimulatorProtocol(Protocol):
    """Protocol for simulating order execution against eligible observations."""
    def __call__(
        self,
        order: OrderIntent,
        eligible_observations: Sequence[PriceObservation],
        execution_config: Optional[Union[ExecutionConfigProtocol, RunConfig, Mapping[str, Any]]] = None,
        fee_schedule: Optional[Union[FeeScheduleProtocol, Mapping[str, Any]]] = None,
    ) -> FillDecision:
        ...


@runtime_checkable
class FillApplicatorProtocol(Protocol):
    """Protocol for applying an executed fill to portfolio state."""
    def __call__(self, state: PortfolioState, fill: Fill) -> PortfolioState:
        ...


@runtime_checkable
class SettlerProtocol(Protocol):
    """Protocol for settling expiring contracts."""
    def __call__(
        self, state: PortfolioState, settlement: SettlementObservation
    ) -> PortfolioState:
        ...


@runtime_checkable
class BacktestRunnerProtocol(Protocol):
    """Protocol for running a backtest simulation."""
    def __call__(
        self, config: RunConfig, data_bundle: Union[DataBundleProtocol, Mapping[str, Any], Any]
    ) -> RunResult:
        ...


@runtime_checkable
class MetricsCalculatorProtocol(Protocol):
    """Protocol for calculating performance and risk metrics."""
    def __call__(self, result: RunResult) -> Metrics:
        ...


@runtime_checkable
class ReportWriterProtocol(Protocol):
    """Protocol for writing backtest reports to disk."""
    def __call__(self, result: RunResult, path: Union[Path, str]) -> ReportManifest:
        ...


# ==============================================================================
# 7. Serialization and Deserialization (Strict Roundtrip)
# ==============================================================================

T = TypeVar("T")

def _to_json_compatible(obj: Any) -> Any:
    """Convert dataclasses, Decimals, Enums, and tuples into JSON-safe types."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        result: Dict[str, Any] = {}
        for f_name in obj.__dataclass_fields__:
            result[f_name] = _to_json_compatible(getattr(obj, f_name))
        return result
    if isinstance(obj, (list, tuple)):
        return [_to_json_compatible(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _to_json_compatible(v) for k, v in obj.items()}
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ValidationError(f"Cannot serialize NaN or Infinity float: {obj}")
        return obj
    return obj


def to_dict(obj: Any) -> Any:
    """Convert a contract dataclass to a dictionary with Decimal as strings."""
    return _to_json_compatible(obj)


def to_json(obj: Any, *, indent: Optional[int] = None) -> str:
    """Serialize a contract dataclass to strict JSON string."""
    data = to_dict(obj)
    return json.dumps(data, indent=indent, ensure_ascii=False)


def from_dict(cls: Type[T], data: Dict[str, Any]) -> T:
    """Reconstruct a contract dataclass from a dictionary with strict validation."""
    if not isinstance(data, dict):
        raise ValidationError(f"Expected dict for {cls.__name__}, got {type(data).__name__}")

    # Check schema_version if the target class has a schema_version field
    if hasattr(cls, "__dataclass_fields__") and "schema_version" in cls.__dataclass_fields__:
        if "schema_version" in data:
            validate_schema_version(data["schema_version"])

    try:
        type_hints = get_type_hints(cls)
    except Exception:
        type_hints = getattr(cls, "__annotations__", {})

    # Strict field validation: reject unrecognized fields
    if hasattr(cls, "__dataclass_fields__"):
        allowed_fields = set(cls.__dataclass_fields__.keys())
        unknown_fields = set(data.keys()) - allowed_fields
        if unknown_fields:
            raise ValidationError(
                f"Unknown field(s) for {cls.__name__}: {sorted(unknown_fields)}"
            )

    kwargs: Dict[str, Any] = {}
    for f_name, f_type in type_hints.items():
        if f_name not in data:
            continue
        val = data[f_name]
        kwargs[f_name] = _reconstruct_field(f_type, val, f_name)

    return cls(**kwargs)  # __post_init__ runs validation


def from_json(cls: Type[T], json_str: str) -> T:
    """Reconstruct a contract dataclass from a JSON string."""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Malformed JSON: {exc}") from exc
    return from_dict(cls, data)


def _reconstruct_field(target_type: Any, val: Any, field_name: str) -> Any:
    """Internal helper to reconstruct nested types recursively."""
    if val is None:
        return None

    # Resolve Optional / Union
    origin = getattr(target_type, "__origin__", None)
    args = getattr(target_type, "__args__", ())

    if origin is Union:
        non_none_args = [a for a in args if a is not type(None)]
        if len(non_none_args) == 1:
            return _reconstruct_field(non_none_args[0], val, field_name)
        return val

    if origin in (Tuple, tuple):
        if not isinstance(val, (list, tuple)):
            raise ValidationError(f"{field_name} must be a list or tuple, got {type(val).__name__}")
        if args and len(args) == 2 and args[1] is Ellipsis:
            elem_type = args[0]
            return tuple(_reconstruct_field(elem_type, item, f"{field_name}[]") for item in val)
        if args and Ellipsis not in args:
            return tuple(
                _reconstruct_field(args[i], item, f"{field_name}[{i}]")
                for i, item in enumerate(val)
            )
        return tuple(val)

    if origin in (List, list):
        if not isinstance(val, (list, tuple)):
            raise ValidationError(f"{field_name} must be a list, got {type(val).__name__}")
        elem_type = args[0] if args else Any
        return [_reconstruct_field(elem_type, item, f"{field_name}[]") for item in val]

    if origin in (Dict, dict):
        if not isinstance(val, dict):
            raise ValidationError(f"{field_name} must be a dict, got {type(val).__name__}")
        return val

    # Direct classes
    if target_type is Decimal:
        return validate_decimal(val, field_name)

    if target_type is int:
        return validate_int(val, field_name)

    if target_type is str:
        if not isinstance(val, str):
            raise ValidationError(f"{field_name} must be a str, got {type(val).__name__}")
        return val

    if target_type is bool:
        if not isinstance(val, bool):
            raise ValidationError(f"{field_name} must be a bool, got {type(val).__name__}")
        return val

    if isinstance(target_type, type) and issubclass(target_type, Enum):
        return validate_enum(val, target_type, field_name)

    if hasattr(target_type, "__dataclass_fields__"):
        if isinstance(val, dict):
            return from_dict(target_type, val)
        return val

    return val


# ==============================================================================
# 8. Contract Fixtures (Test Doubles / Defaults)
# ==============================================================================

def make_sample_instrument(
    instrument_name: str = "BTC-27DEC24-90000-C",
    option_type: OptionType = OptionType.CALL,
    strike_usd: Decimal = Decimal("90000.0"),
    creation_ms: int = 1703836800000,
    expiry_ms: int = 1735286400000,
    base_currency: Currency = Currency.BTC,
    quote_currency: Currency = Currency.BTC,
    counter_currency: Currency = Currency.USD,
    settlement_currency: Currency = Currency.BTC,
) -> Instrument:
    return Instrument(
        instrument_name=instrument_name,
        option_type=option_type,
        strike_usd=strike_usd,
        creation_ms=creation_ms,
        expiry_ms=expiry_ms,
        base_currency=base_currency,
        quote_currency=quote_currency,
        counter_currency=counter_currency,
        settlement_currency=settlement_currency,
        contract_size=Decimal("1.0"),
        min_qty=Decimal("0.1"),
        qty_step=None,
        qty_step_known=False,
        tick_size=Decimal("0.0005"),
        source_ref="test_fixture",
    )


def make_sample_run_config(
    strategy_name: str = "long_straddle_v1",
    start_ms: int = 1711699200000,
    end_ms: int = 1711785600000,
    initial_capital_btc: Decimal = Decimal("10.0"),
) -> RunConfig:
    return RunConfig(
        strategy_name=strategy_name,
        start_ms=start_ms,
        end_ms=end_ms,
        initial_capital_btc=initial_capital_btc,
        decision_resolution_hours=1,
        execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        fee_rate_amount=Decimal("0.0003"),
        fee_cap_ratio=Decimal("0.125"),
        slippage_btc=Decimal("0"),
        stress_reserve_ratio=Decimal("0.5"),
        strategy_params=(("target_dte_days", "10.0"),),
        deterministic_seed=42,
    )


def make_sample_trade_tick(
    instrument_name: str = "BTC-27DEC24-90000-C",
    trade_id: str = "trade_001",
    trade_seq: int = 1,
    timestamp_ms: int = 1711700000000,
    price_btc: Decimal = Decimal("0.0850"),
    quantity_contracts: Decimal = Decimal("1.0"),
) -> TradeTick:
    return TradeTick(
        instrument_name=instrument_name,
        trade_id=trade_id,
        trade_seq=trade_seq,
        timestamp_ms=timestamp_ms,
        price_btc=price_btc,
        quantity_contracts=quantity_contracts,
        source_ref="test_fixture",
    )


def make_sample_price_observation(
    instrument_name: str = "BTC-27DEC24-90000-C",
    observed_at_ms: int = 1711700000000,
    available_at_ms: int = 1711703600000,
    price_basis: PriceBasis = PriceBasis.TRADE,
) -> PriceObservation:
    return PriceObservation(
        instrument_name=instrument_name,
        observed_at_ms=observed_at_ms,
        available_at_ms=available_at_ms,
        price_basis=price_basis,
        trade_price_btc=Decimal("0.0850"),
        bid_price_btc=Decimal("0.0840"),
        ask_price_btc=Decimal("0.0860"),
        mark_price_btc=Decimal("0.0850"),
        underlying_price_usd=Decimal("69000.0"),
        quality=DataQuality.OBSERVED_TRADE,
        source_ref="test_fixture",
    )


def make_sample_coverage_report(
    instrument_name: str = "BTC-27DEC24-90000-C",
    start_ms: int = 1711699200000,
    end_ms: int = 1711785600000,
    expected_intervals: int = 24,
    observed_intervals: int = 22,
) -> CoverageReport:
    return CoverageReport(
        instrument_name=instrument_name,
        start_ms=start_ms,
        end_ms=end_ms,
        expected_intervals=expected_intervals,
        observed_intervals=observed_intervals,
        missing_intervals=expected_intervals - observed_intervals,
        coverage_ratio=Decimal("0.9167"),
        quality_breakdown=(("observed_trade", 22), ("missing", 2)),
        status="adequate",
    )


def make_sample_selection_decision(
    decision_at_ms: int = 1711700000000,
    strategy_name: str = "long_straddle_v1",
) -> SelectionDecision:
    call = make_sample_instrument("BTC-27DEC24-90000-C", OptionType.CALL)
    put = make_sample_instrument("BTC-27DEC24-90000-P", OptionType.PUT)
    return SelectionDecision(
        decision_at_ms=decision_at_ms,
        strategy_name=strategy_name,
        selected_instruments=(call, put),
        underlying_price_usd=Decimal("69000.0"),
        target_expiry_ms=1735286400000,
        common_strike_usd=Decimal("90000.0"),
        reason_code=ReasonCode.ENTRY_SIGNAL,
        metadata=(("dte_days", "270.0"),),
    )


def make_sample_signal(
    signal_id: str = "sig_001",
    decision_at_ms: int = 1711700000000,
) -> Signal:
    return Signal(
        signal_id=signal_id,
        decision_at_ms=decision_at_ms,
        available_at_ms=decision_at_ms,
        strategy_name="long_straddle_v1",
        signal_type="enter_long_straddle",
        instrument_names=("BTC-27DEC24-90000-C", "BTC-27DEC24-90000-P"),
        target_quantities=(Decimal("1.0"), Decimal("1.0")),
        sides=(Side.BUY, Side.BUY),
        reason_code=ReasonCode.ENTRY_SIGNAL,
    )


def make_sample_order_intent(
    order_id: str = "ord_001",
    decision_at_ms: int = 1711700000000,
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        position_id="pos_001",
        leg_id="leg_call",
        instrument_name="BTC-27DEC24-90000-C",
        side=Side.BUY,
        quantity=Decimal("1.0"),
        decision_at_ms=decision_at_ms,
        eligible_after_ms=decision_at_ms,
        expires_at_ms=decision_at_ms + 3600000,
        reason=ReasonCode.ENTRY_SIGNAL,
    )


def make_sample_fill(
    fill_id: str = "fill_001",
    order_id: str = "ord_001",
    actual_ms: int = 1711700000000,
    price_btc: Decimal = Decimal("0.0860"),
    fee_btc: Decimal = Decimal("0.0003"),
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=order_id,
        position_id="pos_001",
        leg_id="leg_call",
        instrument_name="BTC-27DEC24-90000-C",
        side=Side.BUY,
        actual_ms=actual_ms,
        model_timestamp_ms=actual_ms,
        quantity=Decimal("1.0"),
        price_btc=price_btc,
        fee_btc=fee_btc,
        reference_price_btc=Decimal("0.0850"),
        spread_slippage_cost_btc=Decimal("0.0010"),
        execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        source_ref="test_fixture",
    )


def make_sample_fill_decision(order_id: str = "ord_001") -> FillDecision:
    fill = make_sample_fill("fill_001", order_id)
    return FillDecision(
        order_id=order_id,
        filled=True,
        fill=fill,
        rejection_reason=None,
        observation_used=None,
    )


def make_sample_ledger_entry(
    event_id: str = "led_001",
    timestamp_ms: int = 1711700000000,
) -> LedgerEntry:
    return LedgerEntry(
        event_id=event_id,
        position_id="pos_001",
        timestamp_ms=timestamp_ms,
        currency=Currency.BTC,
        amount=Decimal("-0.0863"),  # premium + fee
        entry_type=EntryType.PREMIUM,
        balance_after=Decimal("9.9137"),
        description="Option purchase debit",
    )


def make_sample_position(
    position_id: str = "pos_001",
    opened_at_ms: int = 1711700000000,
) -> Position:
    call = make_sample_instrument("BTC-27DEC24-90000-C", OptionType.CALL)
    put = make_sample_instrument("BTC-27DEC24-90000-P", OptionType.PUT)
    fill_call = make_sample_fill("fill_c", "ord_c", opened_at_ms)
    fill_put = make_sample_fill("fill_p", "ord_p", opened_at_ms)
    return Position(
        position_id=position_id,
        strategy_name="long_straddle_v1",
        status=PositionStatus.OPEN,
        legs=(call, put),
        leg_quantities=(Decimal("1.0"), Decimal("1.0")),
        opened_at_ms=opened_at_ms,
        closed_at_ms=None,
        open_fills=(fill_call, fill_put),
        close_fills=(),
        realized_pnl_btc=Decimal("0"),
        realized_pnl_usd=Decimal("0"),
        exit_reason=None,
    )


def make_sample_equity_point(timestamp_ms: int = 1711700000000) -> EquityPoint:
    return EquityPoint(
        timestamp_ms=timestamp_ms,
        cash_balance_btc=Decimal("9.9137"),
        reserved_balance_btc=Decimal("0"),
        unrealized_pnl_btc=Decimal("0.05"),
        total_equity_btc=Decimal("9.9637"),
        underlying_price_usd=Decimal("69000.0"),
        total_equity_usd=Decimal("687495.3"),
        benchmark_buy_and_hold_btc=Decimal("10.0"),
        is_valuation_reliable=True,
        quality_note="",
    )


def make_sample_portfolio_state(timestamp_ms: int = 1711700000000) -> PortfolioState:
    pos = make_sample_position("pos_001", timestamp_ms)
    entry = make_sample_ledger_entry("led_001", timestamp_ms)
    return PortfolioState(
        timestamp_ms=timestamp_ms,
        cash_balance_btc=Decimal("9.9137"),
        reserved_balance_btc=Decimal("0"),
        open_positions=(pos,),
        closed_positions=(),
        ledger=(entry,),
    )


def make_sample_settlement_observation(
    instrument_name: str = "BTC-27DEC24-90000-C",
    expiry_ms: int = 1735286400000,
) -> SettlementObservation:
    return SettlementObservation(
        instrument_name=instrument_name,
        expiry_ms=expiry_ms,
        settlement_price_usd=Decimal("95000.0"),
        settlement_price_btc=Decimal("0.05263158"),
        is_official_delivery=True,
        source_ref="official_deribit_settlement",
    )


def make_sample_metrics() -> Metrics:
    return Metrics(
        total_trades=10,
        winning_trades=6,
        losing_trades=3,
        break_even_trades=1,
        win_rate=Decimal("0.60"),
        net_pnl_btc=Decimal("0.4500"),
        net_pnl_usd=Decimal("31050.0"),
        max_drawdown_btc=Decimal("0.1200"),
        max_drawdown_pct=Decimal("0.024"),
        profit_factor=Decimal("1.85"),
        sharpe_ratio=Decimal("1.42"),
        coverage_summary=(("coverage_ratio", "0.92"),),
    )


def make_sample_report_manifest() -> ReportManifest:
    return ReportManifest(
        report_id="rep_20260909_001",
        generated_at_ms=1711785600000,
        files=("summary.json", "equity_curve.csv", "trades.json"),
        summary=(("net_pnl_btc", "0.4500"),),
    )


def make_sample_run_result(
    config_hash: str = "cfg_hash_abc",
    data_hash: str = "data_hash_def",
    code_hash: str = "code_hash_123",
    run_timestamp_ms: int = 1711785600000,
    start_ms: int = 1711699200000,
    end_ms: int = 1711785600000,
) -> RunResult:
    return RunResult(
        config_hash=config_hash,
        data_hash=data_hash,
        code_hash=code_hash,
        run_timestamp_ms=run_timestamp_ms,
        start_ms=start_ms,
        end_ms=end_ms,
        orders=(),
        fills=(),
        ledger=(),
        trades=(),
        equity_curve=(),
        coverage_reports=(),
        metrics=make_sample_metrics(),
        open_positions_at_end=(),
        skipped_reasons=(("no_trade_bar", 2),),
        warnings=(),
        capabilities=("hourly_backtest", "inverse_btc"),
    )
