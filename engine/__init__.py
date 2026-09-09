"""
Engine package for Deribit BTC Inverse Options Backtest Catalog.
Provides chronological multi-trade event loop conforming to schema 1.0.
"""

from engine.data_bundle import DataBundle
from engine.event_engine import (
    EventEngine,
    run,
)
from engine.metrics import compute_run_metrics

__all__ = [
    "run",
    "EventEngine",
    "DataBundle",
    "compute_run_metrics",
]
