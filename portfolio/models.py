"""
Portfolio and ledger models, exceptions, and margin configurations.
Schema Version: 1.0
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from core.contracts import (
    validate_decimal,
    validate_not_bool,
    validate_timestamp_ms,
)


class PortfolioError(Exception):
    """Base exception for portfolio and accounting errors."""
    pass


class InsufficientCapitalError(PortfolioError):
    """Raised when available cash is insufficient for premium, fees, or margin reserves."""
    pass


class ShortPositionRejectedError(PortfolioError):
    """Raised when a short position is attempted without an explicit margin model."""
    pass


class DuplicateFillError(PortfolioError):
    """Raised when a fill has already been processed and cannot be re-applied."""
    pass


class SettlementError(PortfolioError):
    """Raised when settlement encounters invalid contract or payoff state."""
    pass


@dataclass(frozen=True)
class MarginModelConfig:
    """Margin and stress reserve configuration for short options.
    
    Invariants:
    - exchange_margin_equivalent is strictly False for V1 (documented conservative model,
      not an official Deribit exchange margin calculation).
    - allow_naked_short defaults to False.
    """
    model_name: str = "conservative_stress_reserve"
    stress_reserve_ratio: Decimal = Decimal("0.5")
    exchange_margin_equivalent: bool = False
    allow_naked_short: bool = False
    notes: str = "V1 conservative stress reserve model (not exchange margin equivalent)"

    def __post_init__(self) -> None:
        if not self.model_name:
            raise ValueError("model_name cannot be empty")
        validate_decimal(self.stress_reserve_ratio, "stress_reserve_ratio", non_negative=True)
        validate_not_bool(self.stress_reserve_ratio, "stress_reserve_ratio")
        if not isinstance(self.exchange_margin_equivalent, bool):
            raise ValueError("exchange_margin_equivalent must be a boolean")
        if not isinstance(self.allow_naked_short, bool):
            raise ValueError("allow_naked_short must be a boolean")


@dataclass(frozen=True)
class RiskBreach:
    """Documented record of a margin or stress reserve risk breach.
    
    Strict rule: If liabilities exceed allocated stress reserves, report
    a risk breach. Do NOT invent a fake liquidation order.
    """
    timestamp_ms: int
    position_id: str
    instrument_name: str
    liability_btc: Decimal
    reserved_btc: Decimal
    underlying_price_usd: Decimal
    is_breached: bool = True
    message: str = ""

    def __post_init__(self) -> None:
        validate_timestamp_ms(self.timestamp_ms, "timestamp_ms")
        if not self.position_id:
            raise ValueError("position_id cannot be empty")
        if not self.instrument_name:
            raise ValueError("instrument_name cannot be empty")
        validate_decimal(self.liability_btc, "liability_btc", non_negative=True)
        validate_decimal(self.reserved_btc, "reserved_btc", non_negative=True)
        validate_decimal(self.underlying_price_usd, "underlying_price_usd", strictly_positive=True)
        if not isinstance(self.is_breached, bool):
            raise ValueError("is_breached must be a boolean")
