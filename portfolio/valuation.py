"""
Portfolio valuation, equity tracking, and margin risk breach monitoring.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Optional, Sequence, Tuple

from core.contracts import (
    EquityPoint,
    OptionType,
    PortfolioState,
    PositionStatus,
    Side,
    validate_decimal,
    validate_timestamp_ms,
)
from portfolio.models import MarginModelConfig, RiskBreach


@dataclass(frozen=True)
class AccountingMetricsSummary:
    """Explicitly separated accounting metrics per Task 12 specification:
    
    1. realized_trade_pnl_btc: Total realized PnL of closed/settled trades in BTC.
    2. realized_trade_pnl_usd: Realized trade PnL converted at the respective exit/settlement exchange rate.
    3. portfolio_usd_change_from_start: Change in total portfolio USD equity from initial capital.
    4. benchmark_buy_and_hold_btc: Benchmark holding initial capital in BTC.
    """
    realized_trade_pnl_btc: Decimal
    realized_trade_pnl_usd: Decimal
    portfolio_usd_change_from_start: Decimal
    benchmark_buy_and_hold_btc: Decimal
    benchmark_buy_and_hold_usd: Decimal
    total_equity_btc: Decimal
    total_equity_usd: Decimal


def evaluate_equity(
    state: PortfolioState,
    timestamp_ms: int,
    underlying_price_usd: Decimal,
    benchmark_initial_btc: Decimal,
    option_mark_prices: Optional[Mapping[str, Decimal]] = None,
    is_valuation_reliable: bool = True,
    quality_note: str = "",
) -> EquityPoint:
    """Calculate portfolio equity snapshot at a point in time."""
    validate_timestamp_ms(timestamp_ms, "timestamp_ms")
    u_price = validate_decimal(underlying_price_usd, "underlying_price_usd", strictly_positive=True)
    bench_btc = validate_decimal(benchmark_initial_btc, "benchmark_initial_btc", non_negative=True)

    # Calculate unrealized PnL across open positions
    unrealized_pnl_btc = Decimal("0.0")
    if option_mark_prices:
        for pos in state.open_positions:
            for idx, leg in enumerate(pos.legs):
                mark = option_mark_prices.get(leg.instrument_name)
                if mark is not None and idx < len(pos.open_fills):
                    of = pos.open_fills[idx]
                    qty = pos.leg_quantities[idx]
                    if of.side == Side.BUY:
                        unrealized_pnl_btc += (mark - of.price_btc) * qty
                    else:
                        unrealized_pnl_btc += (of.price_btc - mark) * qty

    # Total equity in BTC = cash_balance_btc + unrealized_pnl_btc
    total_equity_btc = state.cash_balance_btc + unrealized_pnl_btc
    total_equity_usd = total_equity_btc * u_price

    return EquityPoint(
        timestamp_ms=timestamp_ms,
        cash_balance_btc=state.cash_balance_btc,
        reserved_balance_btc=state.reserved_balance_btc,
        unrealized_pnl_btc=unrealized_pnl_btc,
        total_equity_btc=total_equity_btc,
        underlying_price_usd=u_price,
        total_equity_usd=total_equity_usd,
        benchmark_buy_and_hold_btc=bench_btc,
        is_valuation_reliable=is_valuation_reliable,
        quality_note=quality_note,
    )


def compute_accounting_summary(
    state: PortfolioState,
    initial_cash_btc: Decimal,
    initial_underlying_usd: Decimal,
    current_underlying_usd: Decimal,
    option_mark_prices: Optional[Mapping[str, Decimal]] = None,
) -> AccountingMetricsSummary:
    """Compute the 4 strictly separated performance metrics.
    
    Shows distinction between:
    - Trade USD PnL (converted at trade close exchange rate).
    - Portfolio USD change (includes underlying BTC price change on cash balances).
    """
    init_cash = validate_decimal(initial_cash_btc, "initial_cash_btc", strictly_positive=True)
    init_u = validate_decimal(initial_underlying_usd, "initial_underlying_usd", strictly_positive=True)
    curr_u = validate_decimal(current_underlying_usd, "current_underlying_usd", strictly_positive=True)

    # 1 & 2. Realized Trade PnL in BTC and USD
    realized_btc = sum((pos.realized_pnl_btc for pos in state.closed_positions), Decimal("0.0"))
    realized_usd = sum((pos.realized_pnl_usd for pos in state.closed_positions), Decimal("0.0"))

    # Current equity
    equity_pt = evaluate_equity(
        state=state,
        timestamp_ms=state.timestamp_ms,
        underlying_price_usd=curr_u,
        benchmark_initial_btc=init_cash,
        option_mark_prices=option_mark_prices,
    )

    # 3. Portfolio USD change from initial equity
    initial_equity_usd = init_cash * init_u
    portfolio_usd_change = equity_pt.total_equity_usd - initial_equity_usd

    # 4. Benchmark buy-and-hold
    benchmark_btc = init_cash
    benchmark_usd = init_cash * curr_u

    return AccountingMetricsSummary(
        realized_trade_pnl_btc=realized_btc,
        realized_trade_pnl_usd=realized_usd,
        portfolio_usd_change_from_start=portfolio_usd_change,
        benchmark_buy_and_hold_btc=benchmark_btc,
        benchmark_buy_and_hold_usd=benchmark_usd,
        total_equity_btc=equity_pt.total_equity_btc,
        total_equity_usd=equity_pt.total_equity_usd,
    )


def check_risk_breaches(
    state: PortfolioState,
    underlying_price_usd: Decimal,
    margin_config: MarginModelConfig,
    timestamp_ms: int,
) -> Tuple[RiskBreach, ...]:
    """Inspect open short positions for stress reserve breaches.
    
    Strict rule: If liabilities exceed allocated stress reserves, report
    a risk breach. Do NOT invent a fake liquidation order.
    """
    u_price = validate_decimal(underlying_price_usd, "underlying_price_usd", strictly_positive=True)
    breaches: list[RiskBreach] = []

    for pos in state.open_positions:
        for idx, leg in enumerate(pos.legs):
            of = pos.open_fills[idx] if idx < len(pos.open_fills) else None
            if of is None or of.side != Side.SELL:
                continue  # Only shorts have liability risk

            qty = pos.leg_quantities[idx]
            contract_size = leg.contract_size
            allocated_reserve = qty * contract_size * margin_config.stress_reserve_ratio

            # Calculate intrinsic liability at current stress price
            if leg.option_type == OptionType.CALL:
                intrinsic = max(u_price - leg.strike_usd, Decimal("0.0")) / u_price
            else:
                intrinsic = max(leg.strike_usd - u_price, Decimal("0.0")) / u_price

            liability_btc = intrinsic * qty * contract_size

            if liability_btc > allocated_reserve:
                breaches.append(
                    RiskBreach(
                        timestamp_ms=timestamp_ms,
                        position_id=pos.position_id,
                        instrument_name=leg.instrument_name,
                        liability_btc=liability_btc,
                        reserved_btc=allocated_reserve,
                        underlying_price_usd=u_price,
                        is_breached=True,
                        message=(
                            f"Short liability {liability_btc:.6f} BTC exceeds allocated "
                            f"stress reserve {allocated_reserve:.6f} BTC at S=${u_price} (no fake liquidation)"
                        ),
                    )
                )

    return tuple(breaches)
