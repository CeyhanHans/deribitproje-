"""
Metrics computation module for backtest results conforming to schema 1.0.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Sequence

from core.contracts import (
    EquityPoint,
    Metrics,
    Position,
    PositionStatus,
)


def compute_run_metrics(
    trades: Sequence[Position],
    equity_curve: Sequence[EquityPoint],
) -> Metrics:
    """Compute standard backtest performance and risk metrics.
    
    Invariants:
    - total_trades equals winning + losing + break_even.
    - Closed/settled positions count as trades; open positions at end do not count.
    - Trade count represents roundtrips (completed positions), not individual legs.
    """
    completed_trades = [
        t for t in trades
        if t.status in (PositionStatus.CLOSED, PositionStatus.SETTLED)
    ]
    total_trades = len(completed_trades)

    if total_trades == 0:
        return Metrics(
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            break_even_trades=0,
            win_rate=Decimal("0.0"),
            net_pnl_btc=Decimal("0.0"),
            net_pnl_usd=Decimal("0.0"),
            max_drawdown_btc=Decimal("0.0"),
            max_drawdown_pct=Decimal("0.0"),
            profit_factor=None,
            sharpe_ratio=None,
            coverage_summary=(("trades_evaluated", "0"),),
        )

    winning = 0
    losing = 0
    break_even = 0
    net_pnl_btc = Decimal("0.0")
    net_pnl_usd = Decimal("0.0")
    gross_gain_btc = Decimal("0.0")
    gross_loss_btc = Decimal("0.0")

    for t in completed_trades:
        net_pnl_btc += t.realized_pnl_btc
        net_pnl_usd += t.realized_pnl_usd

        if t.realized_pnl_btc > Decimal("0.0"):
            winning += 1
            gross_gain_btc += t.realized_pnl_btc
        elif t.realized_pnl_btc < Decimal("0.0"):
            losing += 1
            gross_loss_btc += abs(t.realized_pnl_btc)
        else:
            break_even += 1

    win_rate = Decimal(winning) / Decimal(total_trades)

    # Compute Max Drawdown from equity curve
    max_dd_btc = Decimal("0.0")
    max_dd_pct = Decimal("0.0")

    if equity_curve:
        peak_btc = equity_curve[0].total_equity_btc
        for eq in equity_curve:
            if eq.total_equity_btc > peak_btc:
                peak_btc = eq.total_equity_btc
            dd_btc = max(Decimal("0.0"), peak_btc - eq.total_equity_btc)
            if dd_btc > max_dd_btc:
                max_dd_btc = dd_btc

            if peak_btc > Decimal("0.0"):
                dd_pct = dd_btc / peak_btc
                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct

    profit_factor: Optional[Decimal] = None
    if gross_loss_btc > Decimal("0.0"):
        profit_factor = gross_gain_btc / gross_loss_btc
    elif gross_gain_btc > Decimal("0.0"):
        profit_factor = Decimal("99.99")

    return Metrics(
        total_trades=total_trades,
        winning_trades=winning,
        losing_trades=losing,
        break_even_trades=break_even,
        win_rate=win_rate,
        net_pnl_btc=net_pnl_btc,
        net_pnl_usd=net_pnl_usd,
        max_drawdown_btc=max_dd_btc,
        max_drawdown_pct=max_dd_pct,
        profit_factor=profit_factor,
        sharpe_ratio=None,
        coverage_summary=(("completed_trades", str(total_trades)),),
    )
