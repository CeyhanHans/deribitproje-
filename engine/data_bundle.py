"""
DataBundle structure holding historical market data inputs for backtest simulation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Optional, Sequence, Tuple

from core.contracts import (
    Instrument,
    PriceObservation,
    SettlementObservation,
)


@dataclass(frozen=True)
class DataBundle:
    """Immutable market data bundle provided to the event engine."""
    instruments: Tuple[Instrument, ...] = ()
    observations: Tuple[PriceObservation, ...] = ()
    settlements: Tuple[SettlementObservation, ...] = ()
    underlying_prices_usd: Mapping[int, Decimal] = field(default_factory=dict)

    def get_underlying_price_at(self, timestamp_ms: int) -> Optional[Decimal]:
        """Get underlying BTC index price at or before timestamp_ms."""
        if timestamp_ms in self.underlying_prices_usd:
            return self.underlying_prices_usd[timestamp_ms]

        # Look in observations
        latest_price: Optional[Decimal] = None
        latest_time = -1
        for obs in self.observations:
            if obs.available_at_ms <= timestamp_ms and obs.available_at_ms > latest_time:
                if obs.underlying_price_usd is not None:
                    latest_price = obs.underlying_price_usd
                    latest_time = obs.available_at_ms
                elif obs.index_price_usd is not None:
                    latest_price = obs.index_price_usd
                    latest_time = obs.available_at_ms

        return latest_price
