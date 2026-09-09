"""Strict multi-leg matching and calendar coverage for historical option trade bars."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence

from core.contracts import CoverageReport
from ingestion.historical_windows import DAY_MS, HOUR_MS, HourlyTradeBar

QUALITY_LABEL = "strict_trade_matched_no_stale_fill"


class LegMatcherError(ValueError):
    """Raised when leg bars cannot be safely matched."""


class GapReason(str, Enum):
    """Reason code for missing or unmatchable interval in a leg series."""
    NO_TRADE = "observed_no_trade"
    NOT_LISTED = "not_listed"
    INCOMPLETE = "incomplete_download"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class LegCoverage:
    observed_hours: int
    missing_hours: int
    instrument_name: str | None = None
    eligible_hours: int = 0
    coverage_ratio: float = 0.0
    missing_reasons: Mapping[str, int] = MappingProxyType({})


@dataclass(frozen=True)
class MatchedHourlyRow:
    bucket_start: int
    bucket_end: int
    bars_by_leg: Mapping[str, HourlyTradeBar]
    data_quality_label: str = QUALITY_LABEL


@dataclass(frozen=True)
class CalendarBucket:
    """Full calendar timeline bucket, tracking both matched and missing legs."""
    bucket_start: int
    bucket_end: int
    is_matched: bool
    bars_by_leg: Mapping[str, HourlyTradeBar]
    missing_legs: tuple[str, ...]
    missing_reasons_by_leg: Mapping[str, str]


@dataclass(frozen=True)
class MissingIntervalEvent:
    """Diagnostic event for missing bar(s) in an eligible calendar interval."""
    bucket_start: int
    bucket_end: int
    missing_legs: tuple[str, ...]
    reasons_by_leg: Mapping[str, str]
    description: str


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
    calendar_grid: tuple[CalendarBucket, ...] = ()
    missing_events: tuple[MissingIntervalEvent, ...] = ()


def match_hourly_trade_bars(
    bars_by_leg: Mapping[str, Sequence[HourlyTradeBar]],
    *,
    window_start: int,
    window_end: int,
    expected_instruments: Mapping[str, str] | None = None,
    min_legs: int = 1,
    resolution_ms: int = HOUR_MS,
) -> LegMatchResult:
    """Match bars across 1..N legs without carrying stale prices, preserving full calendar grid.

    Supports:
    - Single leg (min_legs=1), 2 legs, and N legs (e.g. 4-leg Iron Condor).
    - Hourly (1h) and Daily (1d) resolutions.
    - Full calendar grid and missing events so open positions are not dropped during gaps.
    """
    _validate_window(window_start, window_end, resolution_ms=resolution_ms)
    leg_names = tuple(bars_by_leg.keys())
    if len(leg_names) < min_legs:
        raise LegMatcherError(f"at least {min_legs} leg(s) required, got {len(leg_names)}")
    if len(set(leg_names)) != len(leg_names):
        raise LegMatcherError("leg names must be unique")

    expected_instruments = expected_instruments or {}
    unknown_expected = set(expected_instruments) - set(leg_names)
    if unknown_expected:
        raise LegMatcherError(f"expected instrument for unknown leg: {sorted(unknown_expected)[0]}")

    indexed_by_leg: dict[str, dict[int, HourlyTradeBar]] = {}
    coverage: dict[str, LegCoverage] = {}
    eligible_intervals = (window_end - window_start) // resolution_ms

    for leg_name in leg_names:
        indexed = _index_leg_bars(
            leg_name,
            bars_by_leg[leg_name],
            window_start=window_start,
            window_end=window_end,
            expected_instrument=expected_instruments.get(leg_name),
            resolution_ms=resolution_ms,
        )
        indexed_by_leg[leg_name] = indexed
        instrument_name = _single_instrument_name(indexed.values()) or expected_instruments.get(leg_name)
        obs_count = len(indexed)
        miss_count = eligible_intervals - obs_count
        ratio = round(obs_count / eligible_intervals, 6) if eligible_intervals else 0.0
        missing_reasons = {GapReason.NO_TRADE.value: miss_count} if miss_count > 0 else {}

        coverage[leg_name] = LegCoverage(
            observed_hours=obs_count,
            missing_hours=miss_count,
            instrument_name=instrument_name,
            eligible_hours=eligible_intervals,
            coverage_ratio=ratio,
            missing_reasons=MappingProxyType(missing_reasons),
        )

    rows: list[MatchedHourlyRow] = []
    calendar_grid: list[CalendarBucket] = []
    missing_events: list[MissingIntervalEvent] = []

    for bucket_start in range(window_start, window_end, resolution_ms):
        bucket_end = bucket_start + resolution_ms
        selected: dict[str, HourlyTradeBar] = {}
        missing_legs: list[str] = []
        reasons_by_leg: dict[str, str] = {}

        for leg_name in leg_names:
            bar = indexed_by_leg[leg_name].get(bucket_start)
            if bar is None:
                missing_legs.append(leg_name)
                reasons_by_leg[leg_name] = GapReason.NO_TRADE.value
            else:
                selected[leg_name] = bar

        if not missing_legs:
            # All legs traded in this bucket
            row = MatchedHourlyRow(
                bucket_start=bucket_start,
                bucket_end=bucket_end,
                bars_by_leg=MappingProxyType(dict(selected)),
            )
            rows.append(row)
            calendar_grid.append(
                CalendarBucket(
                    bucket_start=bucket_start,
                    bucket_end=bucket_end,
                    is_matched=True,
                    bars_by_leg=MappingProxyType(dict(selected)),
                    missing_legs=(),
                    missing_reasons_by_leg=MappingProxyType({}),
                )
            )
        else:
            # One or more legs missing: record calendar bucket and missing event
            evt = MissingIntervalEvent(
                bucket_start=bucket_start,
                bucket_end=bucket_end,
                missing_legs=tuple(missing_legs),
                reasons_by_leg=MappingProxyType(reasons_by_leg),
                description=f"Missing trade(s) for leg(s): {', '.join(missing_legs)}",
            )
            missing_events.append(evt)
            calendar_grid.append(
                CalendarBucket(
                    bucket_start=bucket_start,
                    bucket_end=bucket_end,
                    is_matched=False,
                    bars_by_leg=MappingProxyType(dict(selected)),
                    missing_legs=tuple(missing_legs),
                    missing_reasons_by_leg=MappingProxyType(reasons_by_leg),
                )
            )

    matched_hours = len(rows)
    metrics = LegMatchMetrics(
        eligible_hours=eligible_intervals,
        matched_hours=matched_hours,
        skipped_hours=eligible_intervals - matched_hours,
        per_leg_coverage=MappingProxyType(dict(coverage)),
        liquidity_coverage_ratio=matched_hours / eligible_intervals if eligible_intervals else 0.0,
    )
    return LegMatchResult(
        rows=tuple(rows),
        metrics=metrics,
        calendar_grid=tuple(calendar_grid),
        missing_events=tuple(missing_events),
    )


def match_two_plus_legs(
    bars_by_leg: Mapping[str, Sequence[HourlyTradeBar]],
    *,
    window_start: int,
    window_end: int,
    expected_instruments: Mapping[str, str] | None = None,
) -> LegMatchResult:
    """Legacy compatibility function strictly requiring 2+ legs."""
    if len(bars_by_leg) < 2:
        raise LegMatcherError("at least two legs are required")
    return match_hourly_trade_bars(
        bars_by_leg,
        window_start=window_start,
        window_end=window_end,
        expected_instruments=expected_instruments,
        min_legs=2,
    )


def has_simultaneous_trades(row: MatchedHourlyRow) -> bool:
    """Check if all legs in a matched row traded at the exact same timestamp.

    Historical trade bars preserve actual trade execution times (first_trade_at,
    last_trade_at). Different legs typically trade at different times within
    the hour; they must not be mislabeled as simultaneous bid/ask quotes.
    """
    trade_times = {
        bar.last_trade_at if bar.last_trade_at is not None else bar.bucket_start
        for bar in row.bars_by_leg.values()
    }
    return len(trade_times) == 1


def to_contract_coverage_reports(
    result: LegMatchResult,
    window_start: int,
    window_end: int,
) -> tuple[CoverageReport, ...]:
    """Convert per-leg coverage into immutable core.contracts.CoverageReport objects."""
    reports: list[CoverageReport] = []
    for leg_name, cov in result.metrics.per_leg_coverage.items():
        instrument = cov.instrument_name or leg_name
        reports.append(
            CoverageReport(
                instrument_name=instrument,
                start_ms=window_start,
                end_ms=window_end,
                expected_intervals=cov.eligible_hours,
                observed_intervals=cov.observed_hours,
                missing_intervals=cov.missing_hours,
                coverage_ratio=Decimal(str(cov.coverage_ratio)),
                quality_breakdown=tuple((k, v) for k, v in cov.missing_reasons.items()),
                status="complete" if cov.missing_hours == 0 else "incomplete",
            )
        )
    return tuple(reports)


def _index_leg_bars(
    leg_name: str,
    bars: Sequence[HourlyTradeBar],
    *,
    window_start: int,
    window_end: int,
    expected_instrument: str | None,
    resolution_ms: int = HOUR_MS,
) -> dict[int, HourlyTradeBar]:
    indexed: dict[int, HourlyTradeBar] = {}
    instruments: set[str] = set()

    for bar in bars:
        _validate_bar_alignment(leg_name, bar, resolution_ms=resolution_ms)
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


def _validate_window(window_start: int, window_end: int, resolution_ms: int = HOUR_MS) -> None:
    if window_start < 0 or window_end < 0:
        raise LegMatcherError("window timestamps must be non-negative")
    if window_start >= window_end:
        raise LegMatcherError("window_start must be before window_end")
    if window_start % resolution_ms != 0 or window_end % resolution_ms != 0:
        raise LegMatcherError("window boundaries must be exact bucket intervals")


def _validate_bar_alignment(leg_name: str, bar: HourlyTradeBar, resolution_ms: int = HOUR_MS) -> None:
    if bar.bucket_start % resolution_ms != 0:
        raise LegMatcherError(f"misaligned bucket_start for leg {leg_name}")
    if bar.bucket_end != bar.bucket_start + resolution_ms:
        raise LegMatcherError(f"misaligned bucket_end for leg {leg_name}")


def _single_instrument_name(bars: Sequence[HourlyTradeBar]) -> str | None:
    instruments = {bar.instrument_name for bar in bars}
    if not instruments:
        return None
    return next(iter(instruments))

