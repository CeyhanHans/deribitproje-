"""Single bounded Deribit public hourly BTC-PERPETUAL bar collector.

Uses Deribit public/get_tradingview_chart_data for the BTC-PERPETUAL instrument
as a btc_usd index proxy. This endpoint returns instrument klines, not direct
index OHLC. First version supports strictly 1-hour resolution and normalizes all
timestamps and OHLC fields to UTC.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import math
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://www.deribit.com/api/v2/public"
DEFAULT_INSTRUMENT = "BTC-PERPETUAL"
DEFAULT_INDEX_NAME = "btc_usd"
DEFAULT_RESOLUTION = "60"
DEFAULT_SERIES_TYPE = "perpetual_kline_proxy"
HOUR_MS = 60 * 60 * 1000
MAX_HOURS_PER_REQUEST = 5000
DEFAULT_DATA_QUALITY_LABEL = "official-tradingview-perpetual-kline-proxy"
SUPPORTED_PAIRINGS = {DEFAULT_INSTRUMENT: DEFAULT_INDEX_NAME}


class DeribitUnderlyingError(RuntimeError):
    """Raised when Deribit underlying/index kline data cannot be fetched or parsed."""


@dataclass(frozen=True)
class UnderlyingRequest:
    start_timestamp: int
    end_timestamp: int
    instrument_name: str = DEFAULT_INSTRUMENT
    index_name: str = DEFAULT_INDEX_NAME
    series_type: str = DEFAULT_SERIES_TYPE
    resolution: str = DEFAULT_RESOLUTION
    base_url: str = DEFAULT_BASE_URL
    max_hours: int = MAX_HOURS_PER_REQUEST

    def validate_pairing_only(self) -> None:
        expected_index = SUPPORTED_PAIRINGS.get(self.instrument_name)
        if expected_index is None or self.index_name != expected_index:
            raise ValueError(
                "first version supports only BTC-PERPETUAL instrument klines paired with btc_usd index metadata"
            )
        if self.series_type != DEFAULT_SERIES_TYPE:
            raise ValueError("series_type must be perpetual_kline_proxy")

    def validate(self) -> None:
        self.validate_pairing_only()
        if self.start_timestamp < 0 or self.end_timestamp < 0:
            raise ValueError("timestamps must be non-negative")
        if self.start_timestamp >= self.end_timestamp:
            raise ValueError("start_timestamp must be before end_timestamp")

        if self.resolution not in {"60", "1h"}:
            raise ValueError("first version supports only 1 hour resolution ('60' or '1h')")

        hours_requested = (self.end_timestamp - self.start_timestamp) / HOUR_MS
        if hours_requested > self.max_hours:
            raise ValueError(
                f"requested range ({hours_requested:.1f} hours) exceeds maximum allowed limit ({self.max_hours} hours)"
            )


@dataclass(frozen=True)
class HourlyUnderlyingBar:
    timestamp_utc: datetime
    bucket_start_utc: datetime
    bucket_end_utc: datetime
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    cost: float
    instrument_name: str
    index_name: str
    series_type: str
    resolution: str = "1h"
    data_quality_label: str = DEFAULT_DATA_QUALITY_LABEL


def fetch_underlying_bars(request: UnderlyingRequest) -> tuple[HourlyUnderlyingBar, ...]:
    """Fetch bounded hourly underlying bars from Deribit public API."""
    request.validate()
    resolution_param = "60" if request.resolution == "1h" else request.resolution
    params = {
        "instrument_name": request.instrument_name,
        "start_timestamp": request.start_timestamp,
        "end_timestamp": request.end_timestamp,
        "resolution": resolution_param,
    }
    result = _fetch_result(request.base_url, "/get_tradingview_chart_data", params)
    return parse_tradingview_response(result, request)


def parse_tradingview_response(
    data: Mapping[str, Any],
    request: UnderlyingRequest,
) -> tuple[HourlyUnderlyingBar, ...]:
    """Parse and validate Deribit TradingView chart data response payload."""
    status = data.get("status")
    if status == "no_data":
        return ()

    ticks = data.get("ticks")
    opens = data.get("open")
    highs = data.get("high")
    lows = data.get("low")
    closes = data.get("close")
    volumes = data.get("volume", [])
    costs = data.get("cost", [])

    if ticks is None or opens is None or highs is None or lows is None or closes is None:
        raise DeribitUnderlyingError("missing required OHLC or ticks fields in response")

    if not (
        isinstance(ticks, list)
        and isinstance(opens, list)
        and isinstance(highs, list)
        and isinstance(lows, list)
        and isinstance(closes, list)
    ):
        raise DeribitUnderlyingError("OHLC and ticks fields must be lists")

    n = len(ticks)
    if not (len(opens) == n and len(highs) == n and len(lows) == n and len(closes) == n):
        raise DeribitUnderlyingError(
            f"mismatched array lengths: ticks={n}, open={len(opens)}, high={len(highs)}, low={len(lows)}, close={len(closes)}"
        )

    if n == 0:
        return ()

    bars: list[HourlyUnderlyingBar] = []
    for i in range(n):
        vol = volumes[i] if i < len(volumes) else 0.0
        cost = costs[i] if i < len(costs) else 0.0

        bar = _validate_and_build_bar(
            tick_ms=ticks[i],
            open_val=opens[i],
            high_val=highs[i],
            low_val=lows[i],
            close_val=closes[i],
            volume_val=vol,
            cost_val=cost,
            instrument_name=request.instrument_name,
            index_name=request.index_name,
            series_type=request.series_type,
        )
        bars.append(bar)

    return tuple(bars)


def _validate_and_build_bar(
    tick_ms: Any,
    open_val: Any,
    high_val: Any,
    low_val: Any,
    close_val: Any,
    volume_val: Any,
    cost_val: Any,
    instrument_name: str,
    index_name: str,
    series_type: str,
) -> HourlyUnderlyingBar:
    try:
        ts = int(tick_ms)
        o = float(open_val)
        h = float(high_val)
        l = float(low_val)
        c = float(close_val)
        vol = float(volume_val)
        cost = float(cost_val)
    except (ValueError, TypeError) as exc:
        raise DeribitUnderlyingError(f"corrupted bar row: non-numeric value: {exc}") from exc

    if ts <= 0:
        raise DeribitUnderlyingError(f"corrupted bar row: invalid timestamp {ts}")
    if not all(math.isfinite(v) for v in (o, h, l, c, vol, cost)):
        raise DeribitUnderlyingError("corrupted bar row: values must be finite")
    if o <= 0 or h <= 0 or l <= 0 or c <= 0:
        raise DeribitUnderlyingError("corrupted bar row: OHLC prices must be strictly positive")
    if h < l:
        raise DeribitUnderlyingError(f"corrupted bar row: high ({h}) cannot be less than low ({l})")
    if h < o or h < c or l > o or l > c:
        raise DeribitUnderlyingError(
            f"corrupted bar row: high/low boundaries violated (O={o}, H={h}, L={l}, C={c})"
        )
    if vol < 0 or cost < 0:
        raise DeribitUnderlyingError("corrupted bar row: volume and cost must be non-negative")

    bucket_start_utc = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    bucket_end_utc = bucket_start_utc + timedelta(hours=1)

    return HourlyUnderlyingBar(
        timestamp_utc=bucket_start_utc,
        bucket_start_utc=bucket_start_utc,
        bucket_end_utc=bucket_end_utc,
        timestamp_ms=ts,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=vol,
        cost=cost,
        instrument_name=instrument_name,
        index_name=index_name,
        series_type=series_type,
    )


def plan_underlying_requests(
    *,
    start_timestamp: int,
    end_timestamp: int,
    instrument_name: str = DEFAULT_INSTRUMENT,
    index_name: str = DEFAULT_INDEX_NAME,
    series_type: str = DEFAULT_SERIES_TYPE,
    resolution: str = DEFAULT_RESOLUTION,
    base_url: str = DEFAULT_BASE_URL,
    max_hours: int = MAX_HOURS_PER_REQUEST,
) -> tuple[UnderlyingRequest, ...]:
    """Plan contiguous bounded requests without fetching data."""
    template = UnderlyingRequest(
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        instrument_name=instrument_name,
        index_name=index_name,
        series_type=series_type,
        resolution=resolution,
        base_url=base_url,
        max_hours=max_hours,
    )
    if max_hours <= 0:
        raise ValueError("max_hours must be positive")
    if start_timestamp < 0 or end_timestamp < 0:
        raise ValueError("timestamps must be non-negative")
    if start_timestamp >= end_timestamp:
        raise ValueError("start_timestamp must be before end_timestamp")
    template.validate_pairing_only()

    chunk_ms = max_hours * HOUR_MS
    chunks: list[UnderlyingRequest] = []
    cursor = start_timestamp
    while cursor < end_timestamp:
        chunk_end = min(cursor + chunk_ms, end_timestamp)
        chunk = UnderlyingRequest(
            start_timestamp=cursor,
            end_timestamp=chunk_end,
            instrument_name=instrument_name,
            index_name=index_name,
            series_type=series_type,
            resolution=resolution,
            base_url=base_url,
            max_hours=max_hours,
        )
        chunk.validate()
        chunks.append(chunk)
        cursor = chunk_end
    return tuple(chunks)


def plan_underlying_window_requests(
    *,
    days: int,
    end_time: datetime,
    instrument_name: str = DEFAULT_INSTRUMENT,
    index_name: str = DEFAULT_INDEX_NAME,
    series_type: str = DEFAULT_SERIES_TYPE,
    resolution: str = DEFAULT_RESOLUTION,
    base_url: str = DEFAULT_BASE_URL,
    max_hours: int = MAX_HOURS_PER_REQUEST,
) -> tuple[UnderlyingRequest, ...]:
    """Plan bounded requests for an explicit day lookback window."""
    if days <= 0:
        raise ValueError("days must be positive")
    if end_time.tzinfo is None:
        raise ValueError("end_time must be timezone-aware")
    end_utc = end_time.astimezone(timezone.utc)
    start_utc = end_utc - timedelta(days=days)
    return plan_underlying_requests(
        start_timestamp=int(start_utc.timestamp() * 1000),
        end_timestamp=int(end_utc.timestamp() * 1000),
        instrument_name=instrument_name,
        index_name=index_name,
        series_type=series_type,
        resolution=resolution,
        base_url=base_url,
        max_hours=max_hours,
    )


def plan_default_underlying_windows(
    *,
    end_time: datetime,
    days: Sequence[int] = (30, 60, 90, 120, 360),
    max_hours: int = MAX_HOURS_PER_REQUEST,
) -> dict[str, tuple[UnderlyingRequest, ...]]:
    """Plan the standard backtest windows without fetching data."""
    return {
        f"{day_count}d": plan_underlying_window_requests(
            days=day_count,
            end_time=end_time,
            max_hours=max_hours,
        )
        for day_count in days
    }


def _fetch_result(base_url: str, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{endpoint}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "DeribitBacktestCatalog/1.0"})
    try:
        with urlopen(req, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise DeribitUnderlyingError(f"request failed: {exc}") from exc

    if not isinstance(payload, dict):
        raise DeribitUnderlyingError("malformed response: expected json object")
    if "error" in payload:
        error = payload["error"]
        msg = error.get("message") if isinstance(error, dict) else str(error)
        raise DeribitUnderlyingError(f"deribit api error: {msg}")
    if "result" not in payload:
        raise DeribitUnderlyingError("missing result field in response")
    return payload["result"]


def write_underlying_bars_json(path: Path, bars: Sequence[HourlyUnderlyingBar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bar_count": len(bars),
        "bars": [
            {
                **asdict(bar),
                "timestamp_utc": bar.timestamp_utc.isoformat(),
                "bucket_start_utc": bar.bucket_start_utc.isoformat(),
                "bucket_end_utc": bar.bucket_end_utc.isoformat(),
            }
            for bar in bars
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass(frozen=True)
class UnderlyingCoverageReport:
    start_timestamp: int
    end_timestamp: int
    step_ms: int
    expected_intervals: int
    observed_intervals: int
    missing_intervals_count: int
    coverage_ratio: float
    missing_intervals: tuple[tuple[int, int], ...]


def merge_and_dedup_bars(
    bars_or_chunks: Sequence[HourlyUnderlyingBar] | Sequence[Sequence[HourlyUnderlyingBar]] | Iterable[Sequence[HourlyUnderlyingBar]],
) -> tuple[HourlyUnderlyingBar, ...]:
    """Merge multiple underlying bar sequences or a single sequence, deduplicating by timestamp_ms.

    Validates data consistency: raises DeribitUnderlyingError if conflicting OHLC
    data exists for the same timestamp.
    Returns bars sorted ascending by timestamp_ms.
    """
    flat_bars: list[HourlyUnderlyingBar] = []
    for item in bars_or_chunks:
        if isinstance(item, HourlyUnderlyingBar):
            flat_bars.append(item)
        elif isinstance(item, (list, tuple)):
            for sub_item in item:
                if not isinstance(sub_item, HourlyUnderlyingBar):
                    raise DeribitUnderlyingError(f"expected HourlyUnderlyingBar, got {type(sub_item).__name__}")
                flat_bars.append(sub_item)
        else:
            raise DeribitUnderlyingError(f"expected HourlyUnderlyingBar or sequence, got {type(item).__name__}")

    by_ts: dict[int, HourlyUnderlyingBar] = {}
    for bar in flat_bars:
        ts = bar.timestamp_ms
        if ts in by_ts:
            existing = by_ts[ts]
            if (
                existing.instrument_name != bar.instrument_name
                or existing.index_name != bar.index_name
                or existing.series_type != bar.series_type
                or not all(
                    math.isfinite(v)
                    for v in (
                        existing.open,
                        existing.high,
                        existing.low,
                        existing.close,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                    )
                )
                or
                abs(existing.open - bar.open) > 1e-4
                or abs(existing.high - bar.high) > 1e-4
                or abs(existing.low - bar.low) > 1e-4
                or abs(existing.close - bar.close) > 1e-4
            ):
                raise DeribitUnderlyingError(
                    f"conflicting underlying bar data at timestamp {ts}: "
                    f"existing=(O={existing.open}, H={existing.high}, L={existing.low}, C={existing.close}) vs "
                    f"new=(O={bar.open}, H={bar.high}, L={bar.low}, C={bar.close})"
                )
            continue
        by_ts[ts] = bar

    return tuple(by_ts[ts] for ts in sorted(by_ts.keys()))


def find_missing_underlying_intervals(
    bars: Sequence[HourlyUnderlyingBar],
    start_timestamp: int,
    end_timestamp: int,
    step_ms: int = HOUR_MS,
) -> tuple[tuple[int, int], ...]:
    """Find contiguous expected intervals missing from the observed underlying bars.

    Returns tuple of (interval_start_ms, interval_end_ms).
    """
    if start_timestamp < 0 or end_timestamp < 0:
        raise ValueError("timestamps must be non-negative")
    if start_timestamp >= end_timestamp:
        raise ValueError("start_timestamp must be before end_timestamp")
    if step_ms <= 0:
        raise ValueError("step_ms must be positive")

    present_ts = {b.timestamp_ms for b in bars}
    missing: list[tuple[int, int]] = []
    cursor = start_timestamp
    while cursor < end_timestamp:
        interval_end = min(cursor + step_ms, end_timestamp)
        if cursor not in present_ts:
            missing.append((cursor, interval_end))
        cursor += step_ms

    return tuple(missing)


def compute_underlying_coverage(
    bars: Sequence[HourlyUnderlyingBar],
    start_timestamp: int,
    end_timestamp: int,
    step_ms: int = HOUR_MS,
) -> UnderlyingCoverageReport:
    """Compute coverage ratio and missing intervals report for an underlying series."""
    missing = find_missing_underlying_intervals(
        bars, start_timestamp, end_timestamp, step_ms=step_ms
    )
    expected = (end_timestamp - start_timestamp + step_ms - 1) // step_ms
    missing_count = len(missing)
    observed = max(0, expected - missing_count)
    ratio = round(float(observed) / float(expected), 6) if expected > 0 else 1.0

    return UnderlyingCoverageReport(
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        step_ms=step_ms,
        expected_intervals=expected,
        observed_intervals=observed,
        missing_intervals_count=missing_count,
        coverage_ratio=ratio,
        missing_intervals=missing,
    )


def fetch_underlying_series(
    start_timestamp: int,
    end_timestamp: int,
    *,
    instrument_name: str = DEFAULT_INSTRUMENT,
    index_name: str = DEFAULT_INDEX_NAME,
    series_type: str = DEFAULT_SERIES_TYPE,
    resolution: str = DEFAULT_RESOLUTION,
    base_url: str = DEFAULT_BASE_URL,
    max_hours: int = MAX_HOURS_PER_REQUEST,
) -> tuple[HourlyUnderlyingBar, ...]:
    """Fetch multi-chunk underlying bars, automatically merging and deduplicating them."""
    requests = plan_underlying_requests(
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        instrument_name=instrument_name,
        index_name=index_name,
        series_type=series_type,
        resolution=resolution,
        base_url=base_url,
        max_hours=max_hours,
    )
    chunk_results: list[tuple[HourlyUnderlyingBar, ...]] = []
    for req in requests:
        bars = fetch_underlying_bars(req)
        chunk_results.append(bars)

    return merge_and_dedup_bars(chunk_results)
