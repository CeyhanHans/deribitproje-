"""
Settlement processing service for Deribit BTC inverse options.
Implements SettlerProtocol conforming to schema 1.0.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from core.contracts import (
    Currency,
    EntryType,
    LedgerEntry,
    OptionType,
    PortfolioState,
    Position,
    PositionStatus,
    ReasonCode,
    SettlementObservation,
    SettlerProtocol,
    Side,
)
from ingestion.settlement import (
    CONTRACT_SIZE_BTC,
    calculate_inverse_option_payoff_per_contract,
    is_daily_option,
)
from portfolio.models import MarginModelConfig


def compute_delivery_fee(
    quantity: Decimal,
    payoff_btc: Decimal,
    instrument_name: str,
    contract_size: Decimal = CONTRACT_SIZE_BTC,
) -> Decimal:
    """Compute delivery fee in BTC per Deribit rules.
    
    Daily options are strictly exempt (0.0 fee).
    Other expiries: 0.015% (0.00015 BTC) capped at 12.5% of delivery value.
    """
    if is_daily_option(instrument_name):
        return Decimal("0.0")

    uncapped_fee = quantity * contract_size * Decimal("0.00015")
    cap = quantity * contract_size * payoff_btc * Decimal("0.125")
    return min(uncapped_fee, cap)


def settle(
    state: PortfolioState,
    settlement: SettlementObservation,
    margin_config: Optional[MarginModelConfig] = None,
) -> PortfolioState:
    """Apply official delivery settlement to matching open positions.
    
    Conforms to SettlerProtocol:
    settle(state: PortfolioState, settlement: SettlementObservation) -> PortfolioState
    
    Invariants:
    - Settlement is idempotent (applied strictly once per instrument expiry).
    - Missing exit data on other positions does NOT delete or alter them.
    - Inverse option payoff: Call = max(S - K, 0) / S; Put = max(K - S, 0) / S.
    - Reserves locked for short options are released upon settlement.
    - Double-entry / net conservation: all cash changes are recorded in the ledger.
    """
    event_id_prefix = f"settle_{settlement.instrument_name}_{settlement.expiry_ms}_"
    
    # 1. Idempotency check: has this instrument already been settled?
    for entry in state.ledger:
        if entry.event_id.startswith(event_id_prefix):
            return state

    current_cash = state.cash_balance_btc
    current_reserved = state.reserved_balance_btc
    new_ledger: list[LedgerEntry] = list(state.ledger)
    stress_ratio = margin_config.stress_reserve_ratio if margin_config else Decimal("0.5")

    matching_positions: list[Position] = []
    unaffected_positions: list[Position] = []

    for pos in state.open_positions:
        has_matching_leg = any(
            leg.instrument_name == settlement.instrument_name and leg.expiry_ms == settlement.expiry_ms
            for leg in pos.legs
        )
        if has_matching_leg:
            matching_positions.append(pos)
        else:
            unaffected_positions.append(pos)

    # If no open positions match, return state unmodified
    if not matching_positions:
        return state

    new_closed_positions: list[Position] = list(state.closed_positions)

    for pos in matching_positions:
        # Calculate settlement cashflow for matching legs
        total_settlement_cashflow = Decimal("0.0")
        total_delivery_fees = Decimal("0.0")
        reserved_to_release = Decimal("0.0")

        for idx, leg in enumerate(pos.legs):
            if leg.instrument_name != settlement.instrument_name:
                continue

            open_f = pos.open_fills[idx] if idx < len(pos.open_fills) else None
            qty = pos.leg_quantities[idx]
            contract_size = leg.contract_size

            # Payoff in BTC per contract
            payoff_per_contract = calculate_inverse_option_payoff_per_contract(
                option_type=leg.option_type,
                strike_usd=leg.strike_usd,
                settlement_price_usd=settlement.settlement_price_usd,
            )
            total_payoff = payoff_per_contract * qty * contract_size
            deliv_fee = compute_delivery_fee(
                quantity=qty,
                payoff_btc=payoff_per_contract,
                instrument_name=leg.instrument_name,
                contract_size=contract_size,
            )

            # Long position receives payoff
            if open_f is not None and open_f.side == Side.BUY:
                if total_payoff > Decimal("0.0"):
                    bal_after_payoff = current_cash + total_payoff
                    new_ledger.append(
                        LedgerEntry(
                            event_id=f"{event_id_prefix}{pos.position_id}_payoff",
                            position_id=pos.position_id,
                            timestamp_ms=settlement.expiry_ms,
                            currency=Currency.BTC,
                            amount=total_payoff,
                            entry_type=EntryType.SETTLEMENT,
                            balance_after=bal_after_payoff,
                            description=f"Settlement payoff credit for {leg.instrument_name}",
                        )
                    )
                    current_cash = bal_after_payoff

                if deliv_fee > Decimal("0.0"):
                    bal_after_fee = current_cash - deliv_fee
                    new_ledger.append(
                        LedgerEntry(
                            event_id=f"{event_id_prefix}{pos.position_id}_fee",
                            position_id=pos.position_id,
                            timestamp_ms=settlement.expiry_ms,
                            currency=Currency.BTC,
                            amount=-deliv_fee,
                            entry_type=EntryType.FEE,
                            balance_after=bal_after_fee,
                            description=f"Delivery fee debit for {leg.instrument_name}",
                        )
                    )
                    current_cash = bal_after_fee

                total_settlement_cashflow += total_payoff
                total_delivery_fees += deliv_fee

            # Short position pays payoff
            else:
                if total_payoff > Decimal("0.0"):
                    bal_after_payoff = current_cash - total_payoff
                    new_ledger.append(
                        LedgerEntry(
                            event_id=f"{event_id_prefix}{pos.position_id}_payoff",
                            position_id=pos.position_id,
                            timestamp_ms=settlement.expiry_ms,
                            currency=Currency.BTC,
                            amount=-total_payoff,
                            entry_type=EntryType.SETTLEMENT,
                            balance_after=bal_after_payoff,
                            description=f"Settlement payoff debit for short {leg.instrument_name}",
                        )
                    )
                    current_cash = bal_after_payoff

                if deliv_fee > Decimal("0.0"):
                    bal_after_fee = current_cash - deliv_fee
                    new_ledger.append(
                        LedgerEntry(
                            event_id=f"{event_id_prefix}{pos.position_id}_fee",
                            position_id=pos.position_id,
                            timestamp_ms=settlement.expiry_ms,
                            currency=Currency.BTC,
                            amount=-deliv_fee,
                            entry_type=EntryType.FEE,
                            balance_after=bal_after_fee,
                            description=f"Delivery fee debit for short {leg.instrument_name}",
                        )
                    )
                    current_cash = bal_after_fee

                total_settlement_cashflow -= total_payoff
                total_delivery_fees += deliv_fee

                # Calculate reserve to release
                rel = qty * contract_size * stress_ratio
                reserved_to_release += rel

        if reserved_to_release > Decimal("0.0"):
            current_reserved = max(Decimal("0.0"), current_reserved - reserved_to_release)
            new_ledger.append(
                LedgerEntry(
                    event_id=f"{event_id_prefix}{pos.position_id}_reserve_release",
                    position_id=pos.position_id,
                    timestamp_ms=settlement.expiry_ms,
                    currency=Currency.BTC,
                    amount=Decimal("0.0"),
                    entry_type=EntryType.RESERVE_ADJUSTMENT,
                    balance_after=current_cash,
                    description=f"Released {reserved_to_release} BTC stress reserve upon settlement",
                )
            )

        # Calculate position realized PnL
        open_cashflow = Decimal("0.0")
        open_fees = Decimal("0.0")
        for of in pos.open_fills:
            open_fees += of.fee_btc
            if of.side == Side.BUY:
                open_cashflow -= (of.price_btc * of.quantity)
            else:
                open_cashflow += (of.price_btc * of.quantity)

        # Total position realized PnL in BTC:
        realized_pnl_btc = (open_cashflow + total_settlement_cashflow) - (open_fees + total_delivery_fees)
        realized_pnl_usd = realized_pnl_btc * settlement.settlement_price_usd

        settled_pos = Position(
            position_id=pos.position_id,
            strategy_name=pos.strategy_name,
            status=PositionStatus.SETTLED,
            legs=pos.legs,
            leg_quantities=pos.leg_quantities,
            opened_at_ms=pos.opened_at_ms,
            closed_at_ms=settlement.expiry_ms,
            open_fills=pos.open_fills,
            close_fills=pos.close_fills,
            realized_pnl_btc=realized_pnl_btc,
            realized_pnl_usd=realized_pnl_usd,
            exit_reason=ReasonCode.CONTRACT_EXPIRY,
        )
        new_closed_positions.append(settled_pos)

    return PortfolioState(
        timestamp_ms=settlement.expiry_ms,
        cash_balance_btc=current_cash,
        reserved_balance_btc=current_reserved,
        open_positions=tuple(unaffected_positions),
        closed_positions=tuple(new_closed_positions),
        ledger=tuple(new_ledger),
    )
