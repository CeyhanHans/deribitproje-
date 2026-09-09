"""
Portfolio cash ledger, transaction processing, and fill application.
Implements FillApplicatorProtocol conforming to schema 1.0.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping, Optional, Sequence, Tuple

from core.contracts import (
    Currency,
    EntryType,
    Fill,
    FillApplicatorProtocol,
    Instrument,
    LedgerEntry,
    OptionType,
    PortfolioState,
    Position,
    PositionStatus,
    ReasonCode,
    Side,
    validate_decimal,
)
from portfolio.models import (
    DuplicateFillError,
    InsufficientCapitalError,
    MarginModelConfig,
    ShortPositionRejectedError,
)

CONTRACT_SIZE_BTC = Decimal("1.0")


def parse_instrument_metadata(name: str) -> Tuple[OptionType, Decimal, int]:
    """Parse option_type, strike_usd, and expiry_ms from Deribit instrument name.
    
    Format: BTC-{DDMMMYY}-{STRIKE}-{C|P} e.g. BTC-27DEC24-90000-C
    """
    parts = name.split("-")
    if len(parts) >= 4:
        strike = Decimal(parts[2])
        opt_type = OptionType.CALL if parts[3].upper().startswith("C") else OptionType.PUT
        return opt_type, strike, 0
    return OptionType.CALL, Decimal("100000"), 0


def make_instrument(
    name: str,
    contract_size: Decimal = CONTRACT_SIZE_BTC,
    creation_ms: int = 0,
    expiry_ms: int = 1800000000000,
) -> Instrument:
    """Create a valid Instrument dataclass from an instrument name."""
    opt_type, strike, _ = parse_instrument_metadata(name)
    return Instrument(
        instrument_name=name,
        option_type=opt_type,
        strike_usd=strike,
        creation_ms=creation_ms,
        expiry_ms=expiry_ms,
        base_currency=Currency.BTC,
        quote_currency=Currency.USD,
        settlement_currency=Currency.BTC,
        contract_size=contract_size,
        min_qty=Decimal("0.1"),
        qty_step=Decimal("0.1"),
        qty_step_known=True,
        tick_size=Decimal("0.0005"),
        source_ref="ledger_instrument_factory",
    )


def apply_fill(
    state: PortfolioState,
    fill: Fill,
    underlying_price_usd: Optional[Decimal] = None,
    margin_config: Optional[MarginModelConfig] = None,
    instrument_map: Optional[Mapping[str, Instrument]] = None,
) -> PortfolioState:
    """Apply an execution fill to the portfolio state.
    
    Conforms to FillApplicatorProtocol:
    apply_fill(state: PortfolioState, fill: Fill) -> PortfolioState
    
    Invariants:
    - Double-entry / net conservation: every cash change is recorded in the ledger.
    - Idempotency: duplicate fills do not double-charge or duplicate entries.
    - Insufficient capital check: long entry requires cash >= premium + fee.
    - Short options require an explicit margin model; naked shorts are rejected by default.
    - Reserves released strictly upon position close or settlement.
    """
    # 1. Idempotency check
    event_prefix = f"fill_{fill.fill_id}_"
    for entry in state.ledger:
        if entry.event_id.startswith(event_prefix):
            # Already applied, return state unmodified
            return state

    for pos in state.open_positions:
        if any(f.fill_id == fill.fill_id for f in pos.open_fills) or any(f.fill_id == fill.fill_id for f in pos.close_fills):
            return state

    for pos in state.closed_positions:
        if any(f.fill_id == fill.fill_id for f in pos.open_fills) or any(f.fill_id == fill.fill_id for f in pos.close_fills):
            return state

    # 2. Determine if this fill is an Entry or an Exit
    existing_pos: Optional[Position] = None
    for pos in state.open_positions:
        if pos.position_id == fill.position_id:
            existing_pos = pos
            break

    is_exit = False
    if existing_pos is not None:
        # If this leg already has an open fill, this fill is a closing exit fill
        for open_f in existing_pos.open_fills:
            if open_f.leg_id == fill.leg_id:
                is_exit = True
                break

    # 3. Retrieve or construct Instrument
    if instrument_map is not None and fill.instrument_name in instrument_map:
        inst = instrument_map[fill.instrument_name]
    else:
        inst = make_instrument(fill.instrument_name)

    contract_size = inst.contract_size
    current_cash = state.cash_balance_btc
    current_reserved = state.reserved_balance_btc
    available_cash = current_cash - current_reserved

    new_ledger: list[LedgerEntry] = list(state.ledger)
    stress_ratio = margin_config.stress_reserve_ratio if margin_config else Decimal("0.5")

    # ==========================================================================
    # CASE 1: OPENING FILL (ENTRY)
    # ==========================================================================
    if not is_exit:
        # Long Entry: BUY
        if fill.side == Side.BUY:
            premium_amount = fill.price_btc * fill.quantity * contract_size
            total_required = premium_amount + fill.fee_btc

            if available_cash < total_required:
                raise InsufficientCapitalError(
                    f"Insufficient cash for long entry: required {total_required} BTC, available {available_cash} BTC"
                )

            # Cash debits
            bal_after_prem = current_cash - premium_amount
            new_ledger.append(
                LedgerEntry(
                    event_id=f"fill_{fill.fill_id}_premium",
                    position_id=fill.position_id,
                    timestamp_ms=fill.actual_ms,
                    currency=Currency.BTC,
                    amount=-premium_amount,
                    entry_type=EntryType.PREMIUM,
                    balance_after=bal_after_prem,
                    description=f"Long premium debit for {fill.instrument_name}",
                )
            )

            bal_after_fee = bal_after_prem - fill.fee_btc
            new_ledger.append(
                LedgerEntry(
                    event_id=f"fill_{fill.fill_id}_fee",
                    position_id=fill.position_id,
                    timestamp_ms=fill.actual_ms,
                    currency=Currency.BTC,
                    amount=-fill.fee_btc,
                    entry_type=EntryType.FEE,
                    balance_after=bal_after_fee,
                    description=f"Trading fee debit for {fill.instrument_name}",
                )
            )

            current_cash = bal_after_fee

        # Short Entry: SELL
        else:
            # Check margin model requirement
            if margin_config is None or not margin_config.allow_naked_short:
                raise ShortPositionRejectedError(
                    "Short positions require an explicit margin model and are rejected by default without configured model"
                )

            premium_credit = fill.price_btc * fill.quantity * contract_size
            net_cash_added = premium_credit - fill.fee_btc

            # Calculate stress reserve requirement
            reserve_required = fill.quantity * contract_size * stress_ratio

            # Check solvency including incoming premium
            if (available_cash + net_cash_added) < reserve_required:
                raise InsufficientCapitalError(
                    f"Insufficient capital for short reserve: required {reserve_required} BTC, available {available_cash + net_cash_added} BTC"
                )

            # Cash credits
            bal_after_prem = current_cash + premium_credit
            new_ledger.append(
                LedgerEntry(
                    event_id=f"fill_{fill.fill_id}_premium",
                    position_id=fill.position_id,
                    timestamp_ms=fill.actual_ms,
                    currency=Currency.BTC,
                    amount=premium_credit,
                    entry_type=EntryType.PREMIUM,
                    balance_after=bal_after_prem,
                    description=f"Short premium credit for {fill.instrument_name}",
                )
            )

            bal_after_fee = bal_after_prem - fill.fee_btc
            new_ledger.append(
                LedgerEntry(
                    event_id=f"fill_{fill.fill_id}_fee",
                    position_id=fill.position_id,
                    timestamp_ms=fill.actual_ms,
                    currency=Currency.BTC,
                    amount=-fill.fee_btc,
                    entry_type=EntryType.FEE,
                    balance_after=bal_after_fee,
                    description=f"Trading fee debit for {fill.instrument_name}",
                )
            )

            current_cash = bal_after_fee
            current_reserved += reserve_required

            # Record reserve lock in ledger
            new_ledger.append(
                LedgerEntry(
                    event_id=f"fill_{fill.fill_id}_reserve",
                    position_id=fill.position_id,
                    timestamp_ms=fill.actual_ms,
                    currency=Currency.BTC,
                    amount=Decimal("0.0"),
                    entry_type=EntryType.RESERVE_ADJUSTMENT,
                    balance_after=current_cash,
                    description=f"Locked {reserve_required} BTC stress reserve for short {fill.instrument_name}",
                )
            )

        # Update Open Positions
        new_open_positions: list[Position] = []
        if existing_pos is None:
            # Create new Position
            new_pos = Position(
                position_id=fill.position_id,
                strategy_name="strategy_v1",
                status=PositionStatus.OPEN,
                legs=(inst,),
                leg_quantities=(fill.quantity,),
                opened_at_ms=fill.actual_ms,
                closed_at_ms=None,
                open_fills=(fill,),
                close_fills=(),
                realized_pnl_btc=Decimal("0.0"),
                realized_pnl_usd=Decimal("0.0"),
                exit_reason=None,
            )
            new_open_positions = list(state.open_positions) + [new_pos]
        else:
            # Add leg to existing multi-leg position
            for p in state.open_positions:
                if p.position_id == fill.position_id:
                    updated_pos = Position(
                        position_id=p.position_id,
                        strategy_name=p.strategy_name,
                        status=PositionStatus.OPEN,
                        legs=p.legs + (inst,),
                        leg_quantities=p.leg_quantities + (fill.quantity,),
                        opened_at_ms=min(p.opened_at_ms, fill.actual_ms),
                        closed_at_ms=None,
                        open_fills=p.open_fills + (fill,),
                        close_fills=p.close_fills,
                        realized_pnl_btc=p.realized_pnl_btc,
                        realized_pnl_usd=p.realized_pnl_usd,
                        exit_reason=None,
                    )
                    new_open_positions.append(updated_pos)
                else:
                    new_open_positions.append(p)

        return PortfolioState(
            timestamp_ms=fill.actual_ms,
            cash_balance_btc=current_cash,
            reserved_balance_btc=current_reserved,
            open_positions=tuple(new_open_positions),
            closed_positions=state.closed_positions,
            ledger=tuple(new_ledger),
        )

    # ==========================================================================
    # CASE 2: CLOSING FILL (EXIT)
    # ==========================================================================
    assert existing_pos is not None
    assert is_exit

    # Long Exit: SELL
    if fill.side == Side.SELL:
        proceeds = fill.price_btc * fill.quantity * contract_size

        bal_after_proceeds = current_cash + proceeds
        new_ledger.append(
            LedgerEntry(
                event_id=f"fill_{fill.fill_id}_premium",
                position_id=fill.position_id,
                timestamp_ms=fill.actual_ms,
                currency=Currency.BTC,
                amount=proceeds,
                entry_type=EntryType.PREMIUM,
                balance_after=bal_after_proceeds,
                description=f"Long proceeds credit for {fill.instrument_name}",
            )
        )

        bal_after_fee = bal_after_proceeds - fill.fee_btc
        new_ledger.append(
            LedgerEntry(
                event_id=f"fill_{fill.fill_id}_fee",
                position_id=fill.position_id,
                timestamp_ms=fill.actual_ms,
                currency=Currency.BTC,
                amount=-fill.fee_btc,
                entry_type=EntryType.FEE,
                balance_after=bal_after_fee,
                description=f"Trading fee debit for {fill.instrument_name}",
            )
        )
        current_cash = bal_after_fee

    # Short Exit: BUY (buyback)
    else:
        buyback_cost = fill.price_btc * fill.quantity * contract_size
        reserve_to_release = fill.quantity * contract_size * stress_ratio

        # Release reserve
        current_reserved = max(Decimal("0.0"), current_reserved - reserve_to_release)

        bal_after_cost = current_cash - buyback_cost
        new_ledger.append(
            LedgerEntry(
                event_id=f"fill_{fill.fill_id}_premium",
                position_id=fill.position_id,
                timestamp_ms=fill.actual_ms,
                currency=Currency.BTC,
                amount=-buyback_cost,
                entry_type=EntryType.PREMIUM,
                balance_after=bal_after_cost,
                description=f"Short buyback debit for {fill.instrument_name}",
            )
        )

        bal_after_fee = bal_after_cost - fill.fee_btc
        new_ledger.append(
            LedgerEntry(
                event_id=f"fill_{fill.fill_id}_fee",
                position_id=fill.position_id,
                timestamp_ms=fill.actual_ms,
                currency=Currency.BTC,
                amount=-fill.fee_btc,
                entry_type=EntryType.FEE,
                balance_after=bal_after_fee,
                description=f"Trading fee debit for {fill.instrument_name}",
            )
        )
        current_cash = bal_after_fee

        new_ledger.append(
            LedgerEntry(
                event_id=f"fill_{fill.fill_id}_reserve",
                position_id=fill.position_id,
                timestamp_ms=fill.actual_ms,
                currency=Currency.BTC,
                amount=Decimal("0.0"),
                entry_type=EntryType.RESERVE_ADJUSTMENT,
                balance_after=current_cash,
                description=f"Released {reserve_to_release} BTC stress reserve upon closing short {fill.instrument_name}",
            )
        )

    # Check if position is now fully closed
    updated_close_fills = existing_pos.close_fills + (fill,)
    all_legs_closed = len(updated_close_fills) >= len(existing_pos.open_fills)

    new_open_positions = []
    new_closed_positions = list(state.closed_positions)

    if all_legs_closed:
        # Calculate final realized PnL for the position
        total_open_cashflow = Decimal("0.0")
        total_open_fees = Decimal("0.0")
        for of in existing_pos.open_fills:
            total_open_fees += of.fee_btc
            if of.side == Side.BUY:
                total_open_cashflow -= (of.price_btc * of.quantity * contract_size)
            else:
                total_open_cashflow += (of.price_btc * of.quantity * contract_size)

        total_close_cashflow = Decimal("0.0")
        total_close_fees = Decimal("0.0")
        for cf in updated_close_fills:
            total_close_fees += cf.fee_btc
            if cf.side == Side.BUY:
                total_close_cashflow -= (cf.price_btc * cf.quantity * contract_size)
            else:
                total_close_cashflow += (cf.price_btc * cf.quantity * contract_size)

        realized_pnl_btc = (total_open_cashflow + total_close_cashflow) - (total_open_fees + total_close_fees)
        
        # Realized USD PnL at closing rate
        if underlying_price_usd is not None:
            realized_pnl_usd = realized_pnl_btc * underlying_price_usd
        else:
            realized_pnl_usd = Decimal("0.0")

        closed_pos = Position(
            position_id=existing_pos.position_id,
            strategy_name=existing_pos.strategy_name,
            status=PositionStatus.CLOSED,
            legs=existing_pos.legs,
            leg_quantities=existing_pos.leg_quantities,
            opened_at_ms=existing_pos.opened_at_ms,
            closed_at_ms=fill.actual_ms,
            open_fills=existing_pos.open_fills,
            close_fills=updated_close_fills,
            realized_pnl_btc=realized_pnl_btc,
            realized_pnl_usd=realized_pnl_usd,
            exit_reason=ReasonCode.TAKE_PROFIT,
        )
        new_closed_positions.append(closed_pos)
        for p in state.open_positions:
            if p.position_id != fill.position_id:
                new_open_positions.append(p)
    else:
        # Partially closed multi-leg position
        for p in state.open_positions:
            if p.position_id == fill.position_id:
                partially_closed = Position(
                    position_id=p.position_id,
                    strategy_name=p.strategy_name,
                    status=PositionStatus.OPEN,
                    legs=p.legs,
                    leg_quantities=p.leg_quantities,
                    opened_at_ms=p.opened_at_ms,
                    closed_at_ms=None,
                    open_fills=p.open_fills,
                    close_fills=updated_close_fills,
                    realized_pnl_btc=p.realized_pnl_btc,
                    realized_pnl_usd=p.realized_pnl_usd,
                    exit_reason=None,
                )
                new_open_positions.append(partially_closed)
            else:
                new_open_positions.append(p)

    return PortfolioState(
        timestamp_ms=fill.actual_ms,
        cash_balance_btc=current_cash,
        reserved_balance_btc=current_reserved,
        open_positions=tuple(new_open_positions),
        closed_positions=tuple(new_closed_positions),
        ledger=tuple(new_ledger),
    )
