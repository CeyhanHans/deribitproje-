"""Time-safe selection of historical BTC inverse option straddles.

The catalog is metadata only: it answers which contracts were eligible at a
given instant T. It deliberately does not claim that either leg traded; the
strict trade-bar matcher remains responsible for that check.

Invariants:
- Eligibility rule: creation_timestamp <= as_of_time_ms < expiration_timestamp.
- Expiry at T (zero DTE boundary) is strictly excluded (T < expiry).
- Strict metadata consistency (name, strike, option_type, currencies, timestamps).
- Duplicate identical records are deduplicated; conflicting records raise ContractCatalogError.
- No silent defaults for tick_size, contract_size, or min_trade_amount.
- Expiry lookup is narrowed via bisect on sorted expiry timestamps.
- If the preferred expiry lacks common strikes, fallback to the next candidate expiry.
"""

from __future__ import annotations

import bisect
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

DAY_MS = 86_400_000

MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_expiry_date(date_str: str) -> tuple[int, int, int]:
    """Parse Deribit date string (e.g. '27DEC24', '15JUL16') into (year, month, day).

    Independent of system locale.
    """
    s = date_str.strip().upper()
    m = re.match(r"^(\d{1,2})([A-Z]{3})(\d{2}|\d{4})$", s)
    if not m:
        raise ContractCatalogError(f"invalid expiration date string format: '{date_str}'")
    day_str, mon_str, yr_str = m.groups()
    if mon_str not in MONTH_MAP:
        raise ContractCatalogError(f"invalid month in date string: '{mon_str}'")
    month = MONTH_MAP[mon_str]
    day = int(day_str)
    if day < 1 or day > 31:
        raise ContractCatalogError(f"invalid day in date string: {day}")
    if len(yr_str) == 2:
        year = 2000 + int(yr_str)
    else:
        year = int(yr_str)
    return year, month, day


def _validate_date_consistency(
    instrument_name: str, expiration_timestamp: int, expiration_date_str: str
) -> None:
    """Enforce that instrument_name, expiration_date_str, and expiration_timestamp match."""
    parts = instrument_name.split("-")
    if len(parts) != 4 or parts[0] != "BTC":
        raise ContractCatalogError(f"unsupported BTC inverse option name: {instrument_name}")

    expected_date_str = parts[1].upper()
    if expiration_date_str.upper() != expected_date_str:
        raise ContractCatalogError(
            f"expiration_date_str '{expiration_date_str}' does not match date in '{instrument_name}' ({expected_date_str})"
        )

    exp_year, exp_mon, exp_day = _parse_expiry_date(expected_date_str)

    # Convert timestamp to UTC date
    try:
        exp_dt = datetime.fromtimestamp(expiration_timestamp / 1000.0, tz=timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise ContractCatalogError(
            f"invalid expiration_timestamp {expiration_timestamp} for '{instrument_name}'"
        ) from exc

    if (exp_dt.year, exp_dt.month, exp_dt.day) != (exp_year, exp_mon, exp_day):
        raise ContractCatalogError(
            f"expiration_timestamp {expiration_timestamp} ({exp_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}) "
            f"calendar date does not match instrument date '{expected_date_str}' "
            f"({exp_year:04d}-{exp_mon:02d}-{exp_day:02d})"
        )


class ContractCatalogError(ValueError):
    """Raised when historical instrument metadata is malformed or inconsistent."""


@dataclass(frozen=True)
class ContractSpec:
    instrument_name: str
    underlying: str
    strike: float
    option_type: str
    expiration_timestamp: int
    expiration_date_str: str
    creation_timestamp: int
    is_active: bool
    tick_size: float
    contract_size: float
    min_trade_amount: float

    def __post_init__(self) -> None:
        if not self.instrument_name or not isinstance(self.instrument_name, str):
            raise ContractCatalogError("instrument_name must be a non-empty string")
        _validate_date_consistency(
            self.instrument_name, self.expiration_timestamp, self.expiration_date_str
        )
        if isinstance(self.creation_timestamp, bool) or not isinstance(self.creation_timestamp, int):
            raise ContractCatalogError("creation_timestamp must be an integer")
        if isinstance(self.expiration_timestamp, bool) or not isinstance(self.expiration_timestamp, int):
            raise ContractCatalogError("expiration_timestamp must be an integer")
        if self.creation_timestamp >= self.expiration_timestamp:
            raise ContractCatalogError(
                f"creation_timestamp ({self.creation_timestamp}) must precede "
                f"expiration_timestamp ({self.expiration_timestamp}) for '{self.instrument_name}'"
            )
        if (
            isinstance(self.strike, bool)
            or not isinstance(self.strike, (int, float))
            or self.strike <= 0
            or math.isnan(self.strike)
            or math.isinf(self.strike)
        ):
            raise ContractCatalogError(f"strike must be strictly positive number, got {self.strike}")
        if self.option_type not in ("call", "put"):
            raise ContractCatalogError(f"option_type must be 'call' or 'put', got {self.option_type!r}")
        if (
            isinstance(self.tick_size, bool)
            or not isinstance(self.tick_size, (int, float))
            or self.tick_size <= 0
            or math.isnan(self.tick_size)
            or math.isinf(self.tick_size)
        ):
            raise ContractCatalogError(f"tick_size must be strictly positive number, got {self.tick_size}")
        if (
            isinstance(self.contract_size, bool)
            or not isinstance(self.contract_size, (int, float))
            or self.contract_size <= 0
            or math.isnan(self.contract_size)
            or math.isinf(self.contract_size)
        ):
            raise ContractCatalogError(f"contract_size must be strictly positive number, got {self.contract_size}")
        if (
            isinstance(self.min_trade_amount, bool)
            or not isinstance(self.min_trade_amount, (int, float))
            or self.min_trade_amount <= 0
            or math.isnan(self.min_trade_amount)
            or math.isinf(self.min_trade_amount)
        ):
            raise ContractCatalogError(
                f"min_trade_amount must be strictly positive number, got {self.min_trade_amount}"
            )


@dataclass(frozen=True)
class CatalogQuery:
    as_of_time_ms: int
    underlying_price: float
    min_dte_days: float
    max_dte_days: float
    target_dte_days: float | None = None
    tie_break: str = "lower"

    def validate(self) -> None:
        if isinstance(self.as_of_time_ms, bool) or not isinstance(self.as_of_time_ms, int) or self.as_of_time_ms < 0:
            raise ContractCatalogError("as_of_time_ms must be a non-negative integer")
        if (
            isinstance(self.underlying_price, bool)
            or not isinstance(self.underlying_price, (int, float))
            or self.underlying_price <= 0
        ):
            raise ContractCatalogError("underlying_price must be positive")
        if math.isnan(self.underlying_price) or math.isinf(self.underlying_price):
            raise ContractCatalogError("underlying_price cannot be NaN or Infinity")

        for name, val in [("min_dte_days", self.min_dte_days), ("max_dte_days", self.max_dte_days)]:
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ContractCatalogError(f"{name} must be a number")
            if math.isnan(val) or math.isinf(val):
                raise ContractCatalogError(f"{name} cannot be NaN or Infinity")
            if val < 0:
                raise ContractCatalogError(f"{name} must be non-negative")

        if self.min_dte_days > self.max_dte_days:
            raise ContractCatalogError("min_dte_days must be <= max_dte_days")

        if self.target_dte_days is not None:
            if isinstance(self.target_dte_days, bool) or not isinstance(self.target_dte_days, (int, float)):
                raise ContractCatalogError("target_dte_days must be a number")
            if math.isnan(self.target_dte_days) or math.isinf(self.target_dte_days):
                raise ContractCatalogError("target_dte_days cannot be NaN or Infinity")
            if self.target_dte_days < 0:
                raise ContractCatalogError("target_dte_days must be non-negative")

        if self.tie_break not in {"lower", "higher"}:
            raise ContractCatalogError("tie_break must be 'lower' or 'higher'")


@dataclass(frozen=True)
class SelectedStraddlePair:
    query_time_ms: int
    underlying_price: float
    strike: float
    expiration_timestamp: int
    expiration_date_str: str
    dte_days: float
    strike_distance: float
    call_spec: ContractSpec
    put_spec: ContractSpec


@dataclass(frozen=True)
class SelectionResult:
    """Detailed result of a catalog query with diagnostic metadata."""
    pair: Optional[SelectedStraddlePair]
    reason_code: str  # 'selected', 'no_candidate_expiries', 'no_common_strikes'
    message: str
    candidates_evaluated: int  # Number of candidate expiry buckets evaluated
    contracts_scanned: int = 0  # Total contracts inspected in candidate expiry buckets
    evaluation_time_ms: float = 0.0


class HistoricalContractCatalog:
    """In-memory historical contract catalog indexed by expiration timestamp."""

    def __init__(self, specs: Sequence[ContractSpec]) -> None:
        self._specs = _dedup_and_validate_specs(specs)
        grouped: dict[int, list[ContractSpec]] = {}
        for spec in self._specs:
            grouped.setdefault(spec.expiration_timestamp, []).append(spec)
        self._by_expiry = MappingProxyType(
            {expiry: tuple(grouped[expiry]) for expiry in sorted(grouped)}
        )
        self._sorted_expiries: list[int] = sorted(self._by_expiry.keys())

    @classmethod
    def load_from_json(cls, json_path: Path) -> "HistoricalContractCatalog":
        raw = json.loads(json_path.read_text(encoding="utf-8"))
        rows = _extract_rows(raw)
        return cls(tuple(_parse_contract(row) for row in rows))

    def get_active_universe(self, as_of_time_ms: int) -> tuple[ContractSpec, ...]:
        """Return all contracts active at as_of_time_ms.

        Rule: creation_timestamp <= as_of_time_ms < expiration_timestamp.
        """
        if as_of_time_ms < 0:
            raise ContractCatalogError("as_of_time_ms must be non-negative")
        # Bisect to only scan expiries strictly > as_of_time_ms
        idx = bisect.bisect_right(self._sorted_expiries, as_of_time_ms)
        candidate_expiries = self._sorted_expiries[idx:]
        return tuple(
            spec
            for expiry in candidate_expiries
            for spec in self._by_expiry[expiry]
            if spec.creation_timestamp <= as_of_time_ms < spec.expiration_timestamp
        )

    def find_candidate_expiries(
        self, as_of_time_ms: int, min_dte: float, max_dte: float
    ) -> tuple[int, ...]:
        """Find expiries where min_dte <= DTE <= max_dte and expiry > as_of_time_ms.

        Uses bisect on sorted expiry timestamps.
        """
        query = CatalogQuery(as_of_time_ms, 1.0, min_dte, max_dte)
        query.validate()

        min_expiry = max(as_of_time_ms + 1, as_of_time_ms + int(min_dte * DAY_MS))
        max_expiry = as_of_time_ms + int(max_dte * DAY_MS)

        if min_expiry > max_expiry:
            return ()

        left_idx = bisect.bisect_left(self._sorted_expiries, min_expiry)
        right_idx = bisect.bisect_right(self._sorted_expiries, max_expiry)

        candidates = []
        for expiry in self._sorted_expiries[left_idx:right_idx]:
            dte = (expiry - as_of_time_ms) / DAY_MS
            if min_dte <= dte <= max_dte and expiry > as_of_time_ms:
                specs = self._by_expiry[expiry]
                if any(spec.creation_timestamp <= as_of_time_ms < spec.expiration_timestamp for spec in specs):
                    candidates.append(expiry)
        return tuple(candidates)

    def select_straddle(self, query: CatalogQuery) -> SelectionResult:
        """Select the best matching Call and Put straddle pair with fallback across expiries."""
        t0 = time.perf_counter()
        query.validate()

        candidate_expiries = self.find_candidate_expiries(
            query.as_of_time_ms, query.min_dte_days, query.max_dte_days
        )
        if not candidate_expiries:
            t1 = time.perf_counter()
            return SelectionResult(
                pair=None,
                reason_code="no_candidate_expiries",
                message=f"No expiries match DTE range [{query.min_dte_days}, {query.max_dte_days}] at {query.as_of_time_ms}",
                candidates_evaluated=0,
                evaluation_time_ms=(t1 - t0) * 1000.0,
            )

        # Sort expiries by distance to target DTE (or earliest expiry if target is None)
        sorted_expiries = sorted(
            candidate_expiries,
            key=lambda expiry: (
                abs((expiry - query.as_of_time_ms) / DAY_MS - query.target_dte_days)
                if query.target_dte_days is not None
                else (expiry - query.as_of_time_ms) / DAY_MS,
                expiry,
            ),
        )

        candidates_evaluated = 0
        contracts_scanned = 0
        for expiry in sorted_expiries:
            candidates_evaluated += 1
            expiry_specs = self._by_expiry[expiry]
            contracts_scanned += len(expiry_specs)
            active = [
                spec
                for spec in expiry_specs
                if spec.creation_timestamp <= query.as_of_time_ms < spec.expiration_timestamp
            ]
            calls = {spec.strike: spec for spec in active if spec.option_type == "call"}
            puts = {spec.strike: spec for spec in active if spec.option_type == "put"}
            common_strikes = sorted(set(calls) & set(puts))

            if not common_strikes:
                # Fallback: try next suitable expiry in sorted order
                continue

            # Pick closest common strike with deterministic tie-breaking
            if query.tie_break == "lower":
                strike = min(common_strikes, key=lambda val: (abs(val - query.underlying_price), val))
            else:
                strike = min(common_strikes, key=lambda val: (abs(val - query.underlying_price), -val))

            call_spec, put_spec = calls[strike], puts[strike]
            pair = SelectedStraddlePair(
                query_time_ms=query.as_of_time_ms,
                underlying_price=query.underlying_price,
                strike=strike,
                expiration_timestamp=expiry,
                expiration_date_str=call_spec.expiration_date_str,
                dte_days=(expiry - query.as_of_time_ms) / DAY_MS,
                strike_distance=abs(strike - query.underlying_price),
                call_spec=call_spec,
                put_spec=put_spec,
            )
            t1 = time.perf_counter()
            return SelectionResult(
                pair=pair,
                reason_code="selected",
                message=f"Successfully selected straddle at strike {strike} for expiry {expiry}",
                candidates_evaluated=candidates_evaluated,
                contracts_scanned=contracts_scanned,
                evaluation_time_ms=(t1 - t0) * 1000.0,
            )

        t1 = time.perf_counter()
        return SelectionResult(
            pair=None,
            reason_code="no_common_strikes",
            message=f"Evaluated {candidates_evaluated} candidate expiries, but none had common Call/Put strikes",
            candidates_evaluated=candidates_evaluated,
            contracts_scanned=contracts_scanned,
            evaluation_time_ms=(t1 - t0) * 1000.0,
        )

    def select_straddle_pair(self, query: CatalogQuery) -> SelectedStraddlePair | None:
        """Compatibility wrapper returning SelectedStraddlePair or None."""
        return self.select_straddle(query).pair


def _extract_rows(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict) and isinstance(raw.get("result"), list):
        rows = raw["result"]
    elif isinstance(raw, dict) and isinstance(raw.get("instruments"), list):
        rows = raw["instruments"]
    else:
        raise ContractCatalogError("catalog JSON must be a list or contain result/instruments")
    if not all(isinstance(row, dict) for row in rows):
        raise ContractCatalogError("catalog rows must be objects")
    return rows


def _parse_contract(row: Mapping[str, Any]) -> ContractSpec:
    name = _required_str(row, "instrument_name")
    parts = name.split("-")
    if len(parts) != 4 or parts[0] != "BTC" or parts[3] not in {"C", "P"}:
        raise ContractCatalogError(f"unsupported BTC inverse option name: {name}")

    expected_type = "call" if parts[3] == "C" else "put"
    if "option_type" in row:
        row_type = str(row["option_type"]).lower()
        if row_type != expected_type:
            raise ContractCatalogError(
                f"option_type '{row_type}' does not match instrument name suffix in '{name}'"
            )

    name_strike = _parse_positive_float(parts[2], f"strike in {name}")
    if "strike" in row and row["strike"] is not None:
        row_strike = _parse_positive_float(row["strike"], f"strike for {name}")
        if abs(row_strike - name_strike) > 1e-4:
            raise ContractCatalogError(
                f"strike {row_strike} does not match strike in instrument name '{name}' ({name_strike})"
            )

    if "expiration_date_str" in row and row["expiration_date_str"] is not None:
        row_date = str(row["expiration_date_str"])
        if row_date != parts[1]:
            raise ContractCatalogError(
                f"expiration_date_str '{row_date}' does not match date in '{name}' ({parts[1]})"
            )

    # Base and settlement currency consistency
    base_curr = row.get("base_currency") or row.get("underlying")
    if base_curr is not None and str(base_curr).upper() != "BTC":
        raise ContractCatalogError(f"base_currency must be 'BTC' for BTC inverse options, got '{base_curr}'")

    settle_curr = row.get("settlement_currency")
    if settle_curr is not None and str(settle_curr).upper() != "BTC":
        raise ContractCatalogError(f"settlement_currency must be 'BTC', got '{settle_curr}'")

    quote_curr = row.get("quote_currency")
    if quote_curr is not None and str(quote_curr).upper() not in {"BTC", "USD"}:
        raise ContractCatalogError(f"quote_currency must be 'BTC' or 'USD', got '{quote_curr}'")

    counter_curr = row.get("counter_currency")
    if counter_curr is not None and str(counter_curr).upper() not in {"USD", "BTC"}:
        raise ContractCatalogError(f"counter_currency must be 'USD' or 'BTC', got '{counter_curr}'")

    created = _required_int(row, "creation_timestamp")
    expiry = _required_int(row, "expiration_timestamp")
    if created >= expiry:
        raise ContractCatalogError(
            f"creation_timestamp ({created}) must precede expiration_timestamp ({expiry}) for '{name}'"
        )

    # Required numeric fields without silent defaults
    if "tick_size" not in row or row["tick_size"] is None:
        raise ContractCatalogError(f"missing required field 'tick_size' for '{name}'")
    tick_size = _parse_positive_float(row["tick_size"], f"tick_size for {name}")

    if "contract_size" not in row or row["contract_size"] is None:
        raise ContractCatalogError(f"missing required field 'contract_size' for '{name}'")
    contract_size = _parse_positive_float(row["contract_size"], f"contract_size for {name}")

    if "min_trade_amount" not in row or row["min_trade_amount"] is None:
        raise ContractCatalogError(f"missing required field 'min_trade_amount' for '{name}'")
    min_trade_amount = _parse_positive_float(row["min_trade_amount"], f"min_trade_amount for {name}")

    return ContractSpec(
        instrument_name=name,
        underlying=str(base_curr) if base_curr else parts[0],
        strike=name_strike,
        option_type=expected_type,
        expiration_timestamp=expiry,
        expiration_date_str=parts[1],
        creation_timestamp=created,
        is_active=bool(row.get("is_active", False)),
        tick_size=tick_size,
        contract_size=contract_size,
        min_trade_amount=min_trade_amount,
    )


def _dedup_and_validate_specs(specs: Sequence[ContractSpec]) -> tuple[ContractSpec, ...]:
    unique: dict[str, ContractSpec] = {}
    for spec in specs:
        name = spec.instrument_name
        if name in unique:
            existing = unique[name]
            if existing != spec:
                raise ContractCatalogError(
                    f"conflicting metadata for instrument '{name}': {existing} vs {spec}"
                )
            # Identical duplicate: dedup
            continue
        unique[name] = spec
    return tuple(unique.values())


def _parse_positive_float(val: Any, field_name: str) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float, str)):
        raise ContractCatalogError(f"{field_name} must be a number")
    try:
        f = float(val)
    except (ValueError, TypeError) as exc:
        raise ContractCatalogError(f"invalid {field_name}: {val!r}") from exc
    if math.isnan(f) or math.isinf(f):
        raise ContractCatalogError(f"{field_name} cannot be NaN or Infinity")
    if f <= 0:
        raise ContractCatalogError(f"{field_name} must be strictly positive")
    return f


def _required_str(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ContractCatalogError(f"missing or empty required string '{key}'")
    return value.strip()


def _required_int(row: Mapping[str, Any], key: str) -> int:
    val = row.get(key)
    if val is None:
        raise ContractCatalogError(f"missing required integer '{key}'")
    if isinstance(val, bool) or not isinstance(val, (int, str)):
        raise ContractCatalogError(f"{key} must be an integer, got {type(val).__name__}")
    try:
        i = int(val)
    except (ValueError, TypeError) as exc:
        raise ContractCatalogError(f"invalid integer for {key}: {val!r}") from exc
    if i < 0:
        raise ContractCatalogError(f"{key} must be non-negative")
    return i
