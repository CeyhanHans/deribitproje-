"""Time-safe selection of historical BTC inverse option straddles.

The catalogue is metadata only: it answers which contracts were eligible at a
given instant.  It deliberately does not claim that either leg traded; the
strict trade-bar matcher remains responsible for that check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

DAY_MS = 86_400_000


class ContractCatalogError(ValueError):
    """Raised when historical instrument metadata is malformed."""


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


@dataclass(frozen=True)
class CatalogQuery:
    as_of_time_ms: int
    underlying_price: float
    min_dte_days: float
    max_dte_days: float
    target_dte_days: float | None = None
    tie_break: str = "lower"

    def validate(self) -> None:
        if self.as_of_time_ms < 0:
            raise ContractCatalogError("as_of_time_ms must be non-negative")
        if self.underlying_price <= 0:
            raise ContractCatalogError("underlying_price must be positive")
        if self.min_dte_days < 0 or self.max_dte_days < 0:
            raise ContractCatalogError("DTE bounds must be non-negative")
        if self.min_dte_days > self.max_dte_days:
            raise ContractCatalogError("min_dte_days must be <= max_dte_days")
        if self.target_dte_days is not None and self.target_dte_days < 0:
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


class HistoricalContractCatalog:
    def __init__(self, specs: tuple[ContractSpec, ...]) -> None:
        self._specs = specs
        grouped: dict[int, list[ContractSpec]] = {}
        for spec in specs:
            grouped.setdefault(spec.expiration_timestamp, []).append(spec)
        self._by_expiry = MappingProxyType(
            {expiry: tuple(grouped[expiry]) for expiry in sorted(grouped)}
        )

    @classmethod
    def load_from_json(cls, json_path: Path) -> "HistoricalContractCatalog":
        raw = json.loads(json_path.read_text(encoding="utf-8"))
        rows = _extract_rows(raw)
        return cls(tuple(_parse_contract(row) for row in rows))

    def get_active_universe(self, as_of_time_ms: int) -> tuple[ContractSpec, ...]:
        if as_of_time_ms < 0:
            raise ContractCatalogError("as_of_time_ms must be non-negative")
        return tuple(
            spec
            for expiry, specs in self._by_expiry.items()
            if expiry > as_of_time_ms
            for spec in specs
            if spec.creation_timestamp <= as_of_time_ms
        )

    def find_candidate_expiries(
        self, as_of_time_ms: int, min_dte: float, max_dte: float
    ) -> tuple[int, ...]:
        query = CatalogQuery(as_of_time_ms, 1.0, min_dte, max_dte)
        query.validate()
        return tuple(
            expiry
            for expiry, specs in self._by_expiry.items()
            if min_dte <= (expiry - as_of_time_ms) / DAY_MS <= max_dte
            and any(spec.creation_timestamp <= as_of_time_ms for spec in specs)
        )

    def select_straddle_pair(self, query: CatalogQuery) -> SelectedStraddlePair | None:
        query.validate()
        expiries = self.find_candidate_expiries(
            query.as_of_time_ms, query.min_dte_days, query.max_dte_days
        )
        if not expiries:
            return None
        selected_expiry = min(
            expiries,
            key=lambda expiry: (
                abs((expiry - query.as_of_time_ms) / DAY_MS - query.target_dte_days)
                if query.target_dte_days is not None
                else (expiry - query.as_of_time_ms) / DAY_MS,
                expiry,
            ),
        )
        active = [
            spec
            for spec in self._by_expiry[selected_expiry]
            if spec.creation_timestamp <= query.as_of_time_ms < spec.expiration_timestamp
        ]
        calls = {spec.strike: spec for spec in active if spec.option_type == "call"}
        puts = {spec.strike: spec for spec in active if spec.option_type == "put"}
        common_strikes = sorted(set(calls) & set(puts))
        if not common_strikes:
            return None
        if query.tie_break == "lower":
            strike = min(common_strikes, key=lambda value: (abs(value - query.underlying_price), value))
        else:
            strike = min(common_strikes, key=lambda value: (abs(value - query.underlying_price), -value))
        call_spec, put_spec = calls[strike], puts[strike]
        return SelectedStraddlePair(
            query_time_ms=query.as_of_time_ms,
            underlying_price=query.underlying_price,
            strike=strike,
            expiration_timestamp=selected_expiry,
            expiration_date_str=call_spec.expiration_date_str,
            dte_days=(selected_expiry - query.as_of_time_ms) / DAY_MS,
            strike_distance=abs(strike - query.underlying_price),
            call_spec=call_spec,
            put_spec=put_spec,
        )


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
    option_type = str(row.get("option_type", "call" if parts[3] == "C" else "put")).lower()
    if option_type not in {"call", "put"}:
        raise ContractCatalogError(f"invalid option_type for {name}")
    return ContractSpec(
        instrument_name=name,
        underlying=_required_str(row, "base_currency", default=parts[0]),
        strike=float(row.get("strike", parts[2])),
        option_type=option_type,
        expiration_timestamp=_required_int(row, "expiration_timestamp"),
        expiration_date_str=parts[1],
        creation_timestamp=_required_int(row, "creation_timestamp"),
        is_active=bool(row.get("is_active", False)),
        tick_size=float(row.get("tick_size", 0.0)),
        contract_size=float(row.get("contract_size", 1.0)),
        min_trade_amount=float(row.get("min_trade_amount", 0.0)),
    )


def _required_str(row: Mapping[str, Any], key: str, default: str | None = None) -> str:
    value = row.get(key, default)
    if not isinstance(value, str) or not value:
        raise ContractCatalogError(f"missing {key}")
    return value


def _required_int(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    if value is None:
        raise ContractCatalogError(f"missing {key}")
    return int(value)
