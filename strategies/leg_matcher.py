"""Strict hourly multi-leg matching for historical option trade bars."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from ingestion.historical_windows import HOUR_MS, HourlyTradeBar

QUALITY_LABEL = "strict_trade_matched_no_stale_fill"


class LegMatcherError(ValueError):
    """Raised when leg bars cannot be safely matched."""


@dataclass(frozen=True)
class LegCoverage:
    observed_hours: int
    missing_hours: int
    instrument_name: str | None = None


@dataclass(frozen=True)
class MatchedHourlyRow:
    bucket_start: int
    bucket_end: int
    bars_by_leg: Mapping[str, HourlyTradeBar]
    data_quality_label: str = QUALITY_LABEL


@dataclass(frozen=True)
class LegMatchMetrics:
    eligible_hours: int
    matched_hours: int
    skipped_hours: int
    per_leg_coverage: Mapping[str, LegCoverage]
    liquidity_coverage_ratio: float
    data_quality_label: str = QUALITY_LABEL


@dataclass(frozen=True)
class LegMatchResult:
    rows: tuple[MatchedHourlyRow, ...]
    metrics: LegMatchMetrics


def match_hourly_trade_bars(
    bars_by_leg: Mapping[str, Sequence[HourlyTradeBar]],
    *,
    window_start: int,
    window_end: int,
    expected_instruments: Mapping[str, str] | None = None,
) -> LegMatchResult:
    """Inner-join hourly bars across 2+ legs without carrying stale prices."""
    _validate_window(window_start, window_end)
    leg_names = tuple(bars_by_leg.keys())
    if len(leg_names) < 2:
        raise LegMatcherError("at least two legs are required")
    if len(set(leg_names)) != len(leg_names):
        raise LegMatcherError("leg names must be unique")

    expected_instruments = expected_instruments or {}
    unknown_expected = set(expected_instruments) - set(leg_names)
    if unknown_expected:
        raise LegMatcherError(f"expected instrument for unknown leg: {sorted(unknown_expected)[0]}")

    indexed_by_leg: dict[str, dict[int, HourlyTradeBar]] = {}
    coverage: dict[str, LegCoverage] = {}
    eligible_hours = (window_end - window_start) // HOUR_MS

    for leg_name in leg_names:
        indexed = _index_leg_bars(
            leg_name,
            bars_by_leg[leg_name],
            window_start=window_start,
            window_end=window_end,
            expected_instrument=expected_instruments.get(leg_name),
        )
        indexed_by_leg[leg_name] = indexed
        instrument_name = _single_instrument_name(indexed.values())
        coverage[leg_name] = LegCoverage(
            observed_hours=len(indexed),
            missing_hours=eligible_hours - len(indexed),
            instrument_name=instrument_name or expected_instruments.get(leg_name),
        )

    rows: list[MatchedHourlyRow] = []
    for bucket_start in range(window_start, window_end, HOUR_MS):
        selected: dict[str, HourlyTradeBar] = {}
        for leg_name in leg_names:
            bar = indexed_by_leg[leg_name].get(bucket_start)
            if bar is None:
                selected = {}
                break
            selected[leg_name] = bar
        if selected:
            rows.append(
                MatchedHourlyRow(
                    bucket_start=bucket_start,
                    bucket_end=bucket_start + HOUR_MS,
                    bars_by_leg=MappingProxyType(dict(selected)),
                )
            )

    matched_hours = len(rows)
    metrics = LegMatchMetrics(
        eligible_hours=eligible_hours,
        matched_hours=matched_hours,
        skipped_hours=eligible_hours - matched_hours,
        per_leg_coverage=MappingProxyType(dict(coverage)),
        liquidity_coverage_ratio=matched_hours / eligible_hours if eligible_hours else 0.0,
    )
    return LegMatchResult(rows=tuple(rows), metrics=metrics)


def _index_leg_bars(
    leg_name: str,
    bars: Sequence[HourlyTradeBar],
    *,
    window_start: int,
    window_end: int,
    expected_instrument: str | None,
) -> dict[int, HourlyTradeBar]:
    indexed: dict[int, HourlyTradeBar] = {}
    instruments: set[str] = set()

    for bar in bars:
        _validate_bar_alignment(leg_name, bar)
        if bar.bucket_start < window_start or bar.bucket_start >= window_end:
            continue
        if bar.bucket_start in indexed:
            raise LegMatcherError(f"duplicate bar for leg {leg_name} at {bar.bucket_start}")
        if expected_instrument and bar.instrument_name != expected_instrument:
            raise LegMatcherError(f"instrument mismatch for leg {leg_name}")
        indexed[bar.bucket_start] = bar
        instruments.add(bar.instrument_name)

    if len(instruments) > 1:
        raise LegMatcherError(f"multiple instruments for leg {leg_name}")
    return indexed


def _validate_window(window_start: int, window_end: int) -> None:
    if window_start < 0 or window_end < 0:
        raise LegMatcherError("window timestamps must be non-negative")
    if window_start >= window_end:
        raise LegMatcherError("window_start must be before window_end")
    if window_start % HOUR_MS != 0 or window_end % HOUR_MS != 0:
        raise LegMatcherError("window boundaries must be exact hourly buckets")


def _validate_bar_alignment(leg_name: str, bar: HourlyTradeBar) -> None:
    if bar.bucket_start % HOUR_MS != 0:
        raise LegMatcherError(f"misaligned bucket_start for leg {leg_name}")
    if bar.bucket_end != bar.bucket_start + HOUR_MS:
        raise LegMatcherError(f"misaligned bucket_end for leg {leg_name}")


def _single_instrument_name(bars: Sequence[HourlyTradeBar]) -> str | None:
    instruments = {bar.instrument_name for bar in bars}
    if not instruments:
        return None
    return next(iter(instruments))
