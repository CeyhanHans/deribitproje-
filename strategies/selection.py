"""Multi-leg contract selection engine for Deribit BTC inverse options backtest catalog.

Provides general multi-leg contract selection (select_legs) complying with:
- LegSelectorProtocol locked signature: select_legs(strategy, asof_view) -> SelectionDecision
- Anti-lookahead invariant: creation_ms <= T < expiry_ms and available_at <= T
- Strict tie-break policies ('lower' / 'higher') with canonical permutation invariance
- Support for 1..N legs: single-leg, straddle, strangle, spreads, iron condors
- Strike selectors: exact_usd, atm, moneyness_percent, nearest_premium_usd, same_strike_as, strike_offset_usd
- Expiry selectors: exact_date, days_to_expiry, nearest_days_to_expiry, same_expiry_as
- Delta capability rule: delta selection rejected when reliable observation is missing
- Open position immutability: once opened, position instruments remain fixed until closed/expired
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence, Tuple

from core.contracts import (
    Currency,
    Instrument,
    OptionType,
    ReasonCode,
    SelectionDecision,
    TieBreakPolicy,
)
from strategies.strategy_schema import (
    EntryRule,
    ExitRule,
    ExpirySelectorType,
    ResultMetric,
    Selector,
    Side,
    StrategyDefinition,
    StrategyLeg,
    StrikeSelectorType,
)

DAY_MS = 86_400_000


class SelectionError(ValueError):
    """Raised when selection input is malformed or invalid."""


@dataclass(frozen=True)
class AsOfView:
    """Market and catalog state as of timestamp T for contract selection."""
    decision_at_ms: int
    underlying_price_usd: Decimal
    instruments: Sequence[Any]
    observations_by_instrument: Mapping[str, Any] = field(default_factory=dict)
    tie_break: str = "lower"
    open_position: Any = None

    def __post_init__(self) -> None:
        if isinstance(self.decision_at_ms, bool) or not isinstance(self.decision_at_ms, int) or self.decision_at_ms <= 0:
            raise SelectionError(f"decision_at_ms must be a positive integer, got {self.decision_at_ms!r}")
        if not isinstance(self.underlying_price_usd, Decimal) or self.underlying_price_usd <= Decimal("0"):
            raise SelectionError(f"underlying_price_usd must be a strictly positive Decimal, got {self.underlying_price_usd!r}")


def _normalize_instrument(item: Any) -> Instrument:
    """Normalize an Instrument or ContractSpec into a core.contracts.Instrument."""
    if isinstance(item, Instrument):
        return item
    if hasattr(item, "instrument_name") and hasattr(item, "strike") and hasattr(item, "expiration_timestamp"):
        # ContractSpec from contract_catalog
        opt_type = OptionType.CALL if str(item.option_type).lower() in ("call", "c") else OptionType.PUT
        return Instrument(
            instrument_name=item.instrument_name,
            option_type=opt_type,
            strike_usd=Decimal(str(item.strike)),
            creation_ms=int(item.creation_timestamp),
            expiry_ms=int(item.expiration_timestamp),
            base_currency=Currency.BTC,
            quote_currency=Currency.USD,
            settlement_currency=Currency.BTC,
            contract_size=Decimal(str(getattr(item, "contract_size", 1.0))),
            min_qty=Decimal(str(getattr(item, "min_trade_amount", 0.1))),
            tick_size=Decimal(str(getattr(item, "tick_size", 0.0005))),
            source_ref="catalog_spec",
        )
    raise SelectionError(f"unsupported instrument type: {type(item).__name__}")


def select_legs(
    strategy: Any,
    asof_view: Any,
    open_position: Any = None,
) -> SelectionDecision:
    """Select option instruments for a strategy complying with LegSelectorProtocol.

    Parameters:
        strategy: StrategyDefinition, RunConfigDefinition, RunConfig, or strategy-like object.
        asof_view: AsOfView or mapping/object with decision_at_ms, underlying_price_usd, instruments.
        open_position: Optional active position. When provided and open, existing instruments remain
                       fixed and are not mutated/re-selected on hourly price drift.

    Returns:
        SelectionDecision with selected instruments and detailed audit trace.
    """
    # 1. Parse strategy
    if hasattr(strategy, "strategy") and getattr(strategy, "strategy") is not None:
        strat_def = strategy.strategy
    else:
        strat_def = strategy

    strategy_name = str(getattr(strat_def, "name", getattr(strategy, "strategy_name", "unnamed_strategy"))).strip()
    legs: Sequence[StrategyLeg] = getattr(strat_def, "legs", ())
    if not legs:
        raise SelectionError(f"strategy {strategy_name!r} has no legs defined")

    # 2. Parse asof_view
    if isinstance(asof_view, AsOfView):
        t_ms = asof_view.decision_at_ms
        spot_usd = asof_view.underlying_price_usd
        raw_instruments = asof_view.instruments
        observations = asof_view.observations_by_instrument
        tie_break = str(asof_view.tie_break).lower()
        pos = open_position if open_position is not None else asof_view.open_position
    elif isinstance(asof_view, Mapping):
        t_ms = int(asof_view.get("decision_at_ms", asof_view.get("as_of_time_ms", asof_view.get("t", 0))))
        spot_usd = Decimal(str(asof_view.get("underlying_price_usd", asof_view.get("underlying_price", "0"))))
        raw_instruments = asof_view.get("instruments", asof_view.get("available_instruments", ()))
        observations = asof_view.get("observations_by_instrument", asof_view.get("observations", {}))
        tie_break = str(asof_view.get("tie_break", "lower")).lower()
        pos = open_position if open_position is not None else asof_view.get("open_position")
    else:
        t_ms = int(getattr(asof_view, "decision_at_ms", getattr(asof_view, "as_of_time_ms", 0)))
        spot_usd = Decimal(str(getattr(asof_view, "underlying_price_usd", getattr(asof_view, "underlying_price", "0"))))
        raw_instruments = getattr(asof_view, "instruments", getattr(asof_view, "available_instruments", ()))
        observations = getattr(asof_view, "observations_by_instrument", getattr(asof_view, "observations", {}))
        tie_break = str(getattr(asof_view, "tie_break", "lower")).lower()
        pos = open_position if open_position is not None else getattr(asof_view, "open_position", None)

    if t_ms <= 0:
        raise SelectionError(f"invalid decision_at_ms: {t_ms}")
    if spot_usd <= Decimal("0"):
        raise SelectionError(f"invalid underlying_price_usd: {spot_usd}")

    # 3. Position immutability check:
    # If a position is already open and its contracts are not expired, keep existing instruments!
    if pos is not None:
        held = getattr(pos, "instruments", getattr(pos, "selected_instruments", None))
        status = getattr(pos, "status", None)
        status_val = status.value if hasattr(status, "value") else str(status or "")
        is_open = status_val.lower() in ("open", "pending", "")
        if held and is_open:
            held_tuple = tuple(_normalize_instrument(i) for i in held)
            # Ensure none of the held instruments expired at T
            if all(t_ms < inst.expiry_ms for inst in held_tuple):
                common_strike = held_tuple[0].strike_usd if len({i.strike_usd for i in held_tuple}) == 1 else None
                common_expiry = held_tuple[0].expiry_ms if len({i.expiry_ms for i in held_tuple}) == 1 else None
                return SelectionDecision(
                    decision_at_ms=t_ms,
                    strategy_name=strategy_name,
                    selected_instruments=held_tuple,
                    underlying_price_usd=spot_usd,
                    target_expiry_ms=common_expiry,
                    common_strike_usd=common_strike,
                    reason_code=ReasonCode.ENTRY_SIGNAL,
                    metadata=(
                        ("position_status", "held_open"),
                        ("immutability_preserved", "true"),
                        ("instruments_count", str(len(held_tuple))),
                    ),
                )

    # 4. Anti-lookahead and eligibility filter:
    # creation_ms <= T < expiry_ms and available_at <= T
    eligible: list[Instrument] = []
    future_count = 0
    expired_count = 0

    for raw_inst in raw_instruments:
        inst = _normalize_instrument(raw_inst)
        # Check future listing
        if inst.creation_ms > t_ms:
            future_count += 1
            continue
        # Check expired (zero DTE boundary: T < expiry)
        if t_ms >= inst.expiry_ms:
            expired_count += 1
            continue
        # Check observation available_at if available
        obs = observations.get(inst.instrument_name)
        if obs is not None and hasattr(obs, "available_at_ms"):
            if obs.available_at_ms > t_ms:
                continue
        eligible.append(inst)

    if not eligible:
        return SelectionDecision(
            decision_at_ms=t_ms,
            strategy_name=strategy_name,
            selected_instruments=(),
            underlying_price_usd=spot_usd,
            reason_code=ReasonCode.NO_CANDIDATE_FOUND,
            metadata=(
                ("failure_reason", "no_eligible_instruments_in_catalog"),
                ("future_instruments_excluded", str(future_count)),
                ("expired_instruments_excluded", str(expired_count)),
                ("total_input_instruments", str(len(raw_instruments))),
            ),
        )

    # 5. Group eligible instruments by expiry
    by_expiry: dict[int, list[Instrument]] = {}
    for inst in eligible:
        by_expiry.setdefault(inst.expiry_ms, []).append(inst)

    # 6. Determine candidate expiries sorted by preference
    # Look at the master legs (legs not using same_expiry_as) to identify target expiry
    master_legs = [leg for leg in legs if leg.same_expiry_as is None]
    if not master_legs:
        master_legs = [legs[0]]

    primary_leg = master_legs[0]
    target_expiry_ms = _target_expiry_for_leg(primary_leg, t_ms)

    # Sort available candidate expiries by proximity to target expiry with deterministic tie-break
    sorted_expiries = sorted(
        by_expiry.keys(),
        key=lambda exp: (
            abs(exp - target_expiry_ms),
            exp if tie_break == "lower" else -exp,
        ),
    )

    # 7. Evaluate candidate expiries (with fallback if an expiry lacks candidates for all legs)
    rejection_reasons: list[str] = []
    candidates_evaluated = 0

    for candidate_expiry in sorted_expiries:
        expiry_pool = by_expiry[candidate_expiry]
        selected_by_leg: dict[str, Instrument] = {}
        expiry_failed = False
        fail_reason = ""

        # Process legs in order
        for leg in legs:
            candidates_evaluated += len(expiry_pool)

            # Determine leg expiry pool
            if leg.same_expiry_as is not None:
                if leg.same_expiry_as not in selected_by_leg:
                    expiry_failed = True
                    fail_reason = f"leg {leg.name!r} depends on unselected leg {leg.same_expiry_as!r}"
                    break
                # Uses the same expiry as the referenced leg
                leg_expiry_ms = selected_by_leg[leg.same_expiry_as].expiry_ms
                pool_for_leg = by_expiry.get(leg_expiry_ms, [])
            else:
                pool_for_leg = expiry_pool

            # Filter by option_type (CALL vs PUT)
            req_type = OptionType.CALL if leg.option_type.lower() in ("call", "c") else OptionType.PUT
            type_pool = [i for i in pool_for_leg if i.option_type == req_type]
            if not type_pool:
                expiry_failed = True
                fail_reason = f"no {req_type.value} instruments for expiry {candidate_expiry}"
                break

            # Resolve target strike for leg
            target_strike_usd, strike_error = _resolve_target_strike(
                leg, spot_usd, selected_by_leg
            )
            if strike_error:
                expiry_failed = True
                fail_reason = strike_error
                break

            # Capability check for delta
            if leg.strike.selector_type == StrikeSelectorType.DELTA_TARGET.value:
                # Delta requires reliable observation at or before T
                target_delta = Decimal(str(leg.strike.value))
                best_inst = _select_by_delta(type_pool, target_delta, observations, t_ms, tie_break)
                if best_inst is None:
                    expiry_failed = True
                    fail_reason = f"missing_delta_observation: delta target {target_delta} requires reliable quote observation at or before T"
                    break
                selected_by_leg[leg.name] = best_inst
                continue

            # Select strike closest to target_strike_usd using deterministic tie-break
            best_inst = _select_best_strike(type_pool, target_strike_usd, tie_break)
            if best_inst is None:
                expiry_failed = True
                fail_reason = f"no candidate strike found for leg {leg.name!r}"
                break

            selected_by_leg[leg.name] = best_inst

        if not expiry_failed and len(selected_by_leg) == len(legs):
            # All legs successfully selected for this candidate expiry!
            ordered_selected = tuple(selected_by_leg[leg.name] for leg in legs)
            common_strike = (
                ordered_selected[0].strike_usd
                if len({i.strike_usd for i in ordered_selected}) == 1
                else None
            )
            common_expiry = (
                ordered_selected[0].expiry_ms
                if len({i.expiry_ms for i in ordered_selected}) == 1
                else None
            )

            metadata_list: list[tuple[str, str]] = [
                ("candidates_evaluated", str(candidates_evaluated)),
                ("legs_requested", str(len(legs))),
                ("legs_selected", str(len(ordered_selected))),
                ("target_expiry_ms", str(candidate_expiry)),
                ("underlying_price_usd", str(spot_usd)),
            ]
            for leg_idx, (leg, inst) in enumerate(zip(legs, ordered_selected)):
                dte_days = (inst.expiry_ms - t_ms) / DAY_MS
                metadata_list.append((f"leg_{leg.name}_instrument", inst.instrument_name))
                metadata_list.append((f"leg_{leg.name}_type", inst.option_type.value))
                metadata_list.append((f"leg_{leg.name}_strike", str(inst.strike_usd)))
                metadata_list.append((f"leg_{leg.name}_dte_days", f"{dte_days:.2f}"))

            return SelectionDecision(
                decision_at_ms=t_ms,
                strategy_name=strategy_name,
                selected_instruments=ordered_selected,
                underlying_price_usd=spot_usd,
                target_expiry_ms=common_expiry,
                common_strike_usd=common_strike,
                reason_code=ReasonCode.ENTRY_SIGNAL,
                metadata=tuple(metadata_list),
            )
        else:
            rejection_reasons.append(f"expiry {candidate_expiry}: {fail_reason}")

    # If all candidate expiries failed to satisfy all legs (e.g. "çift yok" or "missing delta")
    delta_missing = any("missing_delta_observation" in r for r in rejection_reasons)
    reason_code = ReasonCode.DATA_MISSING if delta_missing else ReasonCode.NO_CANDIDATE_FOUND

    return SelectionDecision(
        decision_at_ms=t_ms,
        strategy_name=strategy_name,
        selected_instruments=(),
        underlying_price_usd=spot_usd,
        reason_code=reason_code,
        metadata=(
            ("failure_reason", "no_matching_candidate_set_for_all_legs"),
            ("rejection_trace", "; ".join(rejection_reasons[:5])),
            ("candidates_evaluated", str(candidates_evaluated)),
            ("total_expiries_tried", str(len(sorted_expiries))),
        ),
    )


def _target_expiry_for_leg(leg: StrategyLeg, decision_at_ms: int) -> int:
    """Calculate target expiration timestamp for a leg in milliseconds."""
    sel = leg.expiry
    stype = sel.selector_type
    if stype == ExpirySelectorType.EXACT_DATE.value:
        from strategies.strategy_schema import parse_utc_timestamp_ms
        return parse_utc_timestamp_ms(sel.value, "exact_date")
    if stype in (ExpirySelectorType.DAYS_TO_EXPIRY.value, ExpirySelectorType.NEAREST_DAYS_TO_EXPIRY.value):
        days = float(sel.value)
        return decision_at_ms + int(days * DAY_MS)
    # Default fallback: 30 days
    return decision_at_ms + int(30 * DAY_MS)


def _resolve_target_strike(
    leg: StrategyLeg,
    spot_usd: Decimal,
    selected_by_leg: Mapping[str, Instrument],
) -> tuple[Decimal, str]:
    """Resolve target strike USD for a leg based on its strike selector and relations."""
    # Check same_strike_as relation
    if leg.same_strike_as is not None:
        if leg.same_strike_as not in selected_by_leg:
            return Decimal("0"), f"referenced leg {leg.same_strike_as!r} not yet selected"
        base_strike = selected_by_leg[leg.same_strike_as].strike_usd
        if leg.strike_offset_usd is not None:
            base_strike += Decimal(str(leg.strike_offset_usd))
        return base_strike, ""

    stype = leg.strike.selector_type
    val = leg.strike.value

    if stype == StrikeSelectorType.ATM.value:
        target = spot_usd
        if val not in (None, "", "atm"):
            target += Decimal(str(val))
        if leg.strike_offset_usd is not None:
            target += Decimal(str(leg.strike_offset_usd))
        return target, ""

    if stype == StrikeSelectorType.MONEYNESS_PERCENT.value:
        percent = Decimal(str(val))
        target = spot_usd * (percent / Decimal("100"))
        if leg.strike_offset_usd is not None:
            target += Decimal(str(leg.strike_offset_usd))
        return target, ""

    if stype in (StrikeSelectorType.EXACT_USD.value, StrikeSelectorType.NEAREST_PREMIUM_USD.value):
        target = Decimal(str(val))
        if leg.strike_offset_usd is not None:
            target += Decimal(str(leg.strike_offset_usd))
        return target, ""

    if stype == StrikeSelectorType.SAME_STRIKE_AS.value:
        ref_name = str(val)
        if ref_name not in selected_by_leg:
            return Decimal("0"), f"referenced leg {ref_name!r} not yet selected"
        target = selected_by_leg[ref_name].strike_usd
        if leg.strike_offset_usd is not None:
            target += Decimal(str(leg.strike_offset_usd))
        return target, ""

    if stype == StrikeSelectorType.DELTA_TARGET.value:
        return Decimal("0"), ""

    return Decimal("0"), f"unsupported strike selector type: {stype!r}"


def _select_best_strike(
    pool: Sequence[Instrument],
    target_strike_usd: Decimal,
    tie_break: str,
) -> Instrument | None:
    """Select candidate instrument closest to target strike with permutation-invariant tie-break.

    Sorting key components:
    1. Distance to target: abs(inst.strike_usd - target_strike_usd)
    2. Primary tie-break: inst.strike_usd (if 'lower') or -inst.strike_usd (if 'higher')
    3. Secondary tie-break: inst.expiry_ms
    4. Canonical tie-break: inst.instrument_name (ensures 100% input permutation determinism)
    """
    if not pool:
        return None

    def sort_key(inst: Instrument):
        dist = abs(inst.strike_usd - target_strike_usd)
        if tie_break == "higher":
            directional = -inst.strike_usd
        else:
            directional = inst.strike_usd
        return (dist, directional, inst.expiry_ms, inst.instrument_name)

    sorted_pool = sorted(pool, key=sort_key)
    return sorted_pool[0]


def _select_by_delta(
    pool: Sequence[Instrument],
    target_delta: Decimal,
    observations: Mapping[str, Any],
    t_ms: int,
    tie_break: str,
) -> Instrument | None:
    """Select candidate instrument closest to target delta when reliable observation exists at T."""
    candidates_with_delta: list[tuple[Instrument, Decimal]] = []

    for inst in pool:
        obs = observations.get(inst.instrument_name)
        if obs is None:
            continue
        # Ensure observation was available at or before T
        avail = getattr(obs, "available_at_ms", getattr(obs, "observed_at_ms", t_ms))
        if avail > t_ms:
            continue
        # Extract delta
        delta_val = getattr(obs, "delta", None)
        if delta_val is None:
            continue
        try:
            delta_dec = Decimal(str(delta_val))
            if delta_dec.is_nan() or delta_dec.is_infinite():
                continue
            candidates_with_delta.append((inst, delta_dec))
        except Exception:
            continue

    if not candidates_with_delta:
        return None

    def delta_sort_key(item: tuple[Instrument, Decimal]):
        inst, delta = item
        dist = abs(delta - target_delta)
        directional = inst.strike_usd if tie_break == "lower" else -inst.strike_usd
        return (dist, directional, inst.instrument_name)

    candidates_with_delta.sort(key=delta_sort_key)
    return candidates_with_delta[0][0]


# ==============================================================================
# Canonical Multi-Leg Strategy Fixtures
# ==============================================================================

def build_long_straddle_strategy(name: str = "long_straddle", target_dte: float = 30.0) -> StrategyDefinition:
    """Build canonical Long Straddle (ATM Call + ATM Put, same expiry)."""
    return StrategyDefinition(
        name=name,
        version="1.0",
        legs=(
            StrategyLeg(
                name="long_call",
                option_type="call",
                side=Side.LONG.value,
                quantity=1.0,
                strike=Selector(StrikeSelectorType.ATM.value, 0),
                expiry=Selector(ExpirySelectorType.NEAREST_DAYS_TO_EXPIRY.value, target_dte),
            ),
            StrategyLeg(
                name="long_put",
                option_type="put",
                side=Side.LONG.value,
                quantity=1.0,
                strike=Selector(StrikeSelectorType.ATM.value, 0),
                expiry=Selector(ExpirySelectorType.SAME_EXPIRY_AS.value, "long_call"),
                same_strike_as="long_call",
                same_expiry_as="long_call",
            ),
        ),
        entry_rules=(EntryRule("enter_when_chain_has_required_legs", {}),),
        exit_rules=(ExitRule("expiry_exit", "at_expiry"),),
        fee_spread_model="deribit_inverse_option_bid_ask",
        result_metrics=(
            ResultMetric("trade_count", "Total trades"),
            ResultMetric("net_pnl_usd", "Net USD PnL"),
            ResultMetric("max_drawdown_usd", "Max DD"),
            ResultMetric("win_rate", "Win rate"),
        ),
    )


def build_strangle_strategy(
    name: str = "strangle",
    call_moneyness: float = 105.0,
    put_moneyness: float = 95.0,
    target_dte: float = 30.0,
) -> StrategyDefinition:
    """Build canonical Strangle (OTM Call + OTM Put, same expiry)."""
    return StrategyDefinition(
        name=name,
        version="1.0",
        legs=(
            StrategyLeg(
                name="otm_call",
                option_type="call",
                side=Side.LONG.value,
                quantity=1.0,
                strike=Selector(StrikeSelectorType.MONEYNESS_PERCENT.value, call_moneyness),
                expiry=Selector(ExpirySelectorType.NEAREST_DAYS_TO_EXPIRY.value, target_dte),
            ),
            StrategyLeg(
                name="otm_put",
                option_type="put",
                side=Side.LONG.value,
                quantity=1.0,
                strike=Selector(StrikeSelectorType.MONEYNESS_PERCENT.value, put_moneyness),
                expiry=Selector(ExpirySelectorType.SAME_EXPIRY_AS.value, "otm_call"),
                same_expiry_as="otm_call",
            ),
        ),
        entry_rules=(EntryRule("enter_when_chain_has_required_legs", {}),),
        exit_rules=(ExitRule("expiry_exit", "at_expiry"),),
        fee_spread_model="deribit_inverse_option_bid_ask",
        result_metrics=(
            ResultMetric("trade_count", "Total trades"),
            ResultMetric("net_pnl_usd", "Net USD PnL"),
            ResultMetric("max_drawdown_usd", "Max DD"),
            ResultMetric("win_rate", "Win rate"),
        ),
    )


def build_call_debit_spread_strategy(
    name: str = "call_debit_spread",
    strike_offset_usd: float = 2000.0,
    target_dte: float = 30.0,
) -> StrategyDefinition:
    """Build Call Debit Spread (Long ATM Call + Short OTM Call, same expiry)."""
    return StrategyDefinition(
        name=name,
        version="1.0",
        legs=(
            StrategyLeg(
                name="long_call_atm",
                option_type="call",
                side=Side.LONG.value,
                quantity=1.0,
                strike=Selector(StrikeSelectorType.ATM.value, 0),
                expiry=Selector(ExpirySelectorType.NEAREST_DAYS_TO_EXPIRY.value, target_dte),
            ),
            StrategyLeg(
                name="short_call_otm",
                option_type="call",
                side=Side.SHORT.value,
                quantity=1.0,
                strike=Selector(StrikeSelectorType.SAME_STRIKE_AS.value, "long_call_atm"),
                expiry=Selector(ExpirySelectorType.SAME_EXPIRY_AS.value, "long_call_atm"),
                same_strike_as="long_call_atm",
                same_expiry_as="long_call_atm",
                strike_offset_usd=strike_offset_usd,
            ),
        ),
        entry_rules=(EntryRule("enter_when_chain_has_required_legs", {}),),
        exit_rules=(ExitRule("expiry_exit", "at_expiry"),),
        fee_spread_model="deribit_inverse_option_bid_ask",
        result_metrics=(
            ResultMetric("trade_count", "Total trades"),
            ResultMetric("net_pnl_usd", "Net USD PnL"),
            ResultMetric("max_drawdown_usd", "Max DD"),
            ResultMetric("win_rate", "Win rate"),
        ),
    )


def build_put_debit_spread_strategy(
    name: str = "put_debit_spread",
    strike_offset_usd: float = -2000.0,
    target_dte: float = 30.0,
) -> StrategyDefinition:
    """Build Put Debit Spread (Long ATM Put + Short OTM Put, same expiry)."""
    return StrategyDefinition(
        name=name,
        version="1.0",
        legs=(
            StrategyLeg(
                name="long_put_atm",
                option_type="put",
                side=Side.LONG.value,
                quantity=1.0,
                strike=Selector(StrikeSelectorType.ATM.value, 0),
                expiry=Selector(ExpirySelectorType.NEAREST_DAYS_TO_EXPIRY.value, target_dte),
            ),
            StrategyLeg(
                name="short_put_otm",
                option_type="put",
                side=Side.SHORT.value,
                quantity=1.0,
                strike=Selector(StrikeSelectorType.SAME_STRIKE_AS.value, "long_put_atm"),
                expiry=Selector(ExpirySelectorType.SAME_EXPIRY_AS.value, "long_put_atm"),
                same_strike_as="long_put_atm",
                same_expiry_as="long_put_atm",
                strike_offset_usd=strike_offset_usd,
            ),
        ),
        entry_rules=(EntryRule("enter_when_chain_has_required_legs", {}),),
        exit_rules=(ExitRule("expiry_exit", "at_expiry"),),
        fee_spread_model="deribit_inverse_option_bid_ask",
        result_metrics=(
            ResultMetric("trade_count", "Total trades"),
            ResultMetric("net_pnl_usd", "Net USD PnL"),
            ResultMetric("max_drawdown_usd", "Max DD"),
            ResultMetric("win_rate", "Win rate"),
        ),
    )


def build_iron_condor_strategy(
    name: str = "iron_condor",
    put_wing_moneyness: float = 90.0,
    put_short_moneyness: float = 95.0,
    call_short_moneyness: float = 105.0,
    call_wing_moneyness: float = 110.0,
    target_dte: float = 30.0,
) -> StrategyDefinition:
    """Build 4-leg Iron Condor (Long Put Wing, Short Put, Short Call, Long Call Wing, same expiry)."""
    return StrategyDefinition(
        name=name,
        version="1.0",
        legs=(
            StrategyLeg(
                name="long_put_wing",
                option_type="put",
                side=Side.LONG.value,
                quantity=1.0,
                strike=Selector(StrikeSelectorType.MONEYNESS_PERCENT.value, put_wing_moneyness),
                expiry=Selector(ExpirySelectorType.NEAREST_DAYS_TO_EXPIRY.value, target_dte),
            ),
            StrategyLeg(
                name="short_put",
                option_type="put",
                side=Side.SHORT.value,
                quantity=1.0,
                strike=Selector(StrikeSelectorType.MONEYNESS_PERCENT.value, put_short_moneyness),
                expiry=Selector(ExpirySelectorType.SAME_EXPIRY_AS.value, "long_put_wing"),
                same_expiry_as="long_put_wing",
            ),
            StrategyLeg(
                name="short_call",
                option_type="call",
                side=Side.SHORT.value,
                quantity=1.0,
                strike=Selector(StrikeSelectorType.MONEYNESS_PERCENT.value, call_short_moneyness),
                expiry=Selector(ExpirySelectorType.SAME_EXPIRY_AS.value, "long_put_wing"),
                same_expiry_as="long_put_wing",
            ),
            StrategyLeg(
                name="long_call_wing",
                option_type="call",
                side=Side.LONG.value,
                quantity=1.0,
                strike=Selector(StrikeSelectorType.MONEYNESS_PERCENT.value, call_wing_moneyness),
                expiry=Selector(ExpirySelectorType.SAME_EXPIRY_AS.value, "long_put_wing"),
                same_expiry_as="long_put_wing",
            ),
        ),
        entry_rules=(EntryRule("enter_when_chain_has_required_legs", {}),),
        exit_rules=(ExitRule("expiry_exit", "at_expiry"),),
        fee_spread_model="deribit_inverse_option_bid_ask",
        result_metrics=(
            ResultMetric("trade_count", "Total trades"),
            ResultMetric("net_pnl_usd", "Net USD PnL"),
            ResultMetric("max_drawdown_usd", "Max DD"),
            ResultMetric("win_rate", "Win rate"),
        ),
    )


def make_sample_catalog_fixture(
    decision_at_ms: int,
    strikes: Sequence[int | float | Decimal],
    expiries: Sequence[int],
    creation_offset_days: float = 30.0,
) -> list[Instrument]:
    """Generate a clean fixture catalog of Call and Put instruments across strikes and expiries."""
    catalog: list[Instrument] = []
    created_ms = decision_at_ms - int(creation_offset_days * DAY_MS)

    for expiry in expiries:
        for strike in strikes:
            strike_dec = Decimal(str(strike))
            strike_int = int(strike_dec)
            call_name = f"BTC-{expiry}-{strike_int}-C"
            put_name = f"BTC-{expiry}-{strike_int}-P"

            catalog.append(
                Instrument(
                    instrument_name=call_name,
                    option_type=OptionType.CALL,
                    strike_usd=strike_dec,
                    creation_ms=created_ms,
                    expiry_ms=expiry,
                    base_currency=Currency.BTC,
                    quote_currency=Currency.USD,
                    settlement_currency=Currency.BTC,
                    contract_size=Decimal("1.0"),
                    min_qty=Decimal("0.1"),
                    tick_size=Decimal("0.0005"),
                    source_ref="test_fixture",
                )
            )
            catalog.append(
                Instrument(
                    instrument_name=put_name,
                    option_type=OptionType.PUT,
                    strike_usd=strike_dec,
                    creation_ms=created_ms,
                    expiry_ms=expiry,
                    base_currency=Currency.BTC,
                    quote_currency=Currency.USD,
                    settlement_currency=Currency.BTC,
                    contract_size=Decimal("1.0"),
                    min_qty=Decimal("0.1"),
                    tick_size=Decimal("0.0005"),
                    source_ref="test_fixture",
                )
            )
    return catalog
