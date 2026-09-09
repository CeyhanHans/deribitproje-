"""
Models and data structures for execution simulation.
Schema Version: 1.0
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional, Sequence, Tuple, Union

from core.contracts import (
    ExecutionModelId,
    Fill,
    FillDecision,
    OrderIntent,
    PriceObservation,
    RunConfig,
    Side,
    ValidationError,
    validate_decimal,
    validate_enum,
    validate_int,
    validate_not_bool,
    validate_timestamp_ms,
)


class ExecutionModelType(str, Enum):
    """Execution simulation model types."""
    MODEL_A_TRADE_ESTIMATED = "model_a_trade_estimated"
    MODEL_B_BID_ASK_REPLAY = "model_b_bid_ask_replay"


class ScenarioName(str, Enum):
    """Standard cost scenario names."""
    OPTIMISTIC = "optimistic"
    BASELINE = "baseline"
    PESSIMISTIC = "pessimistic"
    CUSTOM = "custom"


class ExecutionError(Exception):
    """Base exception for execution simulation errors."""
    pass


class UnsupportedPartialFillError(ExecutionError):
    """Raised when partial fill is attempted (partial fills unsupported in v1)."""
    pass


class OrderExpiredError(ExecutionError):
    """Raised when an order has passed its expiration timestamp."""
    pass


class InvalidOrderQuantityError(ExecutionError):
    """Raised when order quantity violates min_qty or qty_step."""
    pass


@dataclass(frozen=True)
class CostScenarioConfig:
    """Cost scenario parameters.
    
    Default: calibrated=False (explicit assumption unless calibrated from data).
    """
    scenario_name: ScenarioName = ScenarioName.BASELINE
    slippage_btc: Decimal = Decimal("0.0005")
    spread_btc: Decimal = Decimal("0.0005")
    fee_rate: Decimal = Decimal("0.0003")
    fee_cap_ratio: Decimal = Decimal("0.125")
    calibrated: bool = False
    notes: str = "Uncalibrated explicit cost assumption"

    def __post_init__(self) -> None:
        validate_enum(self.scenario_name, ScenarioName, "scenario_name")
        validate_decimal(self.slippage_btc, "slippage_btc", non_negative=True)
        validate_decimal(self.spread_btc, "spread_btc", non_negative=True)
        validate_decimal(self.fee_rate, "fee_rate", non_negative=True)
        validate_decimal(self.fee_cap_ratio, "fee_cap_ratio", non_negative=True)
        validate_not_bool(self.notes, "notes")


@dataclass(frozen=True)
class ExecutionConfig:
    """Configuration for deterministic fill simulation."""
    model_type: ExecutionModelType = ExecutionModelType.MODEL_A_TRADE_ESTIMATED
    execution_model_id: ExecutionModelId = ExecutionModelId.MODEL_C_TRADE_BAR_CLOSE
    slippage_btc: Decimal = Decimal("0.0")
    spread_btc: Decimal = Decimal("0.0")
    tick_size: Decimal = Decimal("0.0005")
    contract_size: Decimal = Decimal("1.0")
    min_qty: Decimal = Decimal("0.1")
    qty_step: Optional[Decimal] = Decimal("0.1")
    qty_step_known: bool = True
    enforce_depth: bool = True
    allow_partial_fill: bool = False  # Strictly False in v1
    fallback_to_model_a: bool = True  # Model B falls back to Model A on missing quote
    max_order_quantity: Optional[Decimal] = None
    calibrated: bool = False
    cost_scenario: Optional[CostScenarioConfig] = None

    def __post_init__(self) -> None:
        validate_enum(self.model_type, ExecutionModelType, "model_type")
        validate_enum(self.execution_model_id, ExecutionModelId, "execution_model_id")
        validate_decimal(self.slippage_btc, "slippage_btc", non_negative=True)
        validate_decimal(self.spread_btc, "spread_btc", non_negative=True)
        validate_decimal(self.tick_size, "tick_size", strictly_positive=True)
        validate_decimal(self.contract_size, "contract_size", strictly_positive=True)
        validate_decimal(self.min_qty, "min_qty", strictly_positive=True)
        if self.qty_step is not None:
            validate_decimal(self.qty_step, "qty_step", strictly_positive=True)
        if self.max_order_quantity is not None:
            validate_decimal(self.max_order_quantity, "max_order_quantity", strictly_positive=True)
        if self.allow_partial_fill:
            raise UnsupportedPartialFillError("Partial fill is unsupported in V1 (all-or-none assumption)")

    @classmethod
    def from_run_config(
        cls,
        run_config: RunConfig,
        model_type: Optional[ExecutionModelType] = None,
        cost_scenario: Optional[CostScenarioConfig] = None,
    ) -> "ExecutionConfig":
        """Build ExecutionConfig from a core RunConfig instance."""
        m_type = model_type
        if m_type is None:
            if run_config.execution_model_id == ExecutionModelId.MODEL_A_BID_ASK:
                m_type = ExecutionModelType.MODEL_B_BID_ASK_REPLAY
            else:
                m_type = ExecutionModelType.MODEL_A_TRADE_ESTIMATED

        slippage = run_config.slippage_btc
        spread = Decimal("0.0")
        calibrated = False

        if cost_scenario is not None:
            slippage = cost_scenario.slippage_btc
            spread = cost_scenario.spread_btc
            calibrated = cost_scenario.calibrated

        return cls(
            model_type=m_type,
            execution_model_id=run_config.execution_model_id,
            slippage_btc=slippage,
            spread_btc=spread,
            calibrated=calibrated,
            cost_scenario=cost_scenario,
        )


@dataclass(frozen=True)
class MultiLegFillDecision:
    """Outcome of attempting to fill a multi-leg order bundle under all-or-none semantics."""
    position_id: str
    filled: bool
    fills: Tuple[Fill, ...] = ()
    leg_decisions: Tuple[FillDecision, ...] = ()
    rejection_reason: Optional[str] = None
    all_or_none: bool = True

    def __post_init__(self) -> None:
        if not self.position_id:
            raise ValidationError("position_id cannot be empty")
        if self.filled:
            if len(self.fills) == 0:
                raise ValidationError("fills cannot be empty when filled is True")
            if self.rejection_reason is not None:
                raise ValidationError("rejection_reason must be None when filled is True")
        else:
            if len(self.fills) > 0:
                raise ValidationError("fills must be empty when filled is False (all-or-none)")
