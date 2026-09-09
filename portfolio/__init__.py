"""
Portfolio package for Deribit BTC Inverse Options Backtest Catalog.
Provides double-entry cash ledger, position management, margin stress reserves,
settlement processing, and valuation accounting conforming to schema 1.0.
"""

from portfolio.ledger import (
    apply_fill,
    make_instrument,
)
from portfolio.models import (
    DuplicateFillError,
    InsufficientCapitalError,
    MarginModelConfig,
    PortfolioError,
    RiskBreach,
    SettlementError,
    ShortPositionRejectedError,
)
from portfolio.settlement_service import (
    compute_delivery_fee,
    settle,
)
from portfolio.valuation import (
    AccountingMetricsSummary,
    check_risk_breaches,
    compute_accounting_summary,
    evaluate_equity,
)

__all__ = [
    "apply_fill",
    "settle",
    "make_instrument",
    "evaluate_equity",
    "check_risk_breaches",
    "compute_accounting_summary",
    "compute_delivery_fee",
    "AccountingMetricsSummary",
    "MarginModelConfig",
    "RiskBreach",
    "PortfolioError",
    "InsufficientCapitalError",
    "ShortPositionRejectedError",
    "DuplicateFillError",
    "SettlementError",
]
