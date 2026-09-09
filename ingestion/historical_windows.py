"""Historical window helpers for BTC inverse option backtests.

This module keeps the first backtest scope explicit:
- independent 30/60/90/120/360 day windows
- 1 hour evaluation bars
- public Deribit historical trades as the free data source

The hourly bars are trade-price based. They are not historical bid/ask quotes.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.contracts import DataQuality, PriceBasis, PriceObservation
from ingestion.deribit_history import (
    DEFAULT_COUNT,
    DEFAULT_MAX_PAGES,
    TradeDownloadPlan,
    build_time_plan,
    fetch_trade_pages,
    summarize_download,
)

DEFAULT_WINDOWS_DAYS = (30, 60, 90, 120, 360)
DEFAULT_RESOLUTION = "1h"
HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * 60 * 60 * 1000
SUPPORTED_RESOLUTIONS = {"1h", "1d", "60", "1D"}


class HistoricalWindowError(ValueError):
    """Raised when a historical window or trade row is unusable."""


@dataclass(frozen=True)
class HistoricalWindow:
    label: str
    days: int
    start_timestamp: int
    end_timestamp: int
    resolution: str = DEFAULT_RESOLUTION

    def validate(self) -> None:
        if self.days <= 0:
            raise HistoricalWindowError("days must be positive")
        if self.start_timestamp < 0 or self.end_timestamp < 0:
            raise HistoricalWindowError("timestamps must be non-negative")
        if self.start_timestamp >= self.end_timestamp:
            raise HistoricalWindowError("start_timestamp must be before end_timestamp")
        if self.resolution not in SUPPORTED_RESOLUTIONS:
            raise HistoricalWindowError(
                f"unsupported resolution {self.resolution!r}: first version supports only 1h and 1d resolution"
            )


@dataclass(frozen=True)
class HourlyTradeBar:
    instrument_name: str
    bucket_start: int
    bucket_end: int
    open: float
    high: float
    low: float
    close: float
    trade_count: int
    volume_contracts: float
    source: str = "deribit.public.historical_trades"
    price_basis: str = "trade_price_not_historical_bid_ask"
    first_trade_at: int | None = None
    last_trade_at: int | None = None
    available_at: int | None = None
    resolution: str = "1h"


TradeBar = HourlyTradeBar  # General alias for 1h or 1d trade bars


def utc_ms(value: datetime) -> int:
    """Return Unix milliseconds for an aware UTC datetime."""
    if value.tzinfo is None:
        raise HistoricalWindowError("datetime must be timezone-aware")
    return int(value.astimezone(UTC).timestamp() * 1000)


def build_default_windows(
    *,
    end_time: datetime | None = None,
    days: Sequence[int] = DEFAULT_WINDOWS_DAYS,
) -> tuple[HistoricalWindow, ...]:
    """Build independent lookback windows ending at one UTC timestamp."""
    resolved_end = end_time or datetime.now(UTC)
    end_timestamp = utc_ms(resolved_end)
    windows: list[HistoricalWindow] = []
    for day_count in days:
        if day_count <= 0:
            raise HistoricalWindowError("all day windows must be positive")
        start_timestamp = utc_ms(resolved_end - timedelta(days=day_count))
        window = HistoricalWindow(
            label=f"{day_count}d",
            days=day_count,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        window.validate()
        windows.append(window)
    return tuple(windows)


def build_trade_plan_for_window(
    instrument_name: str,
    window: HistoricalWindow,
    *,
    count: int = DEFAULT_COUNT,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> TradeDownloadPlan:
    """Create a bounded Deribit History API plan for one instrument/window."""
    window.validate()
    return build_time_plan(
        instrument_name,
        window.start_timestamp,
        window.end_timestamp,
        count=count,
        max_pages=max_pages,
    )


def clean_and_dedup_trades(
    trades: Iterable[Mapping[str, Any]],
    *,
    instrument_name: str | None = None,
) -> list[dict[str, Any]]:
    """Validate, deduplicate by trade_id, and stably sort trades by (timestamp, trade_seq)."""
    cleaned: list[dict[str, Any]] = []
    seen_trade_ids: set[str] = set()

    for trade in trades:
        if instrument_name is not None:
            row_instrument = str(trade.get("instrument_name", instrument_name))
            if row_instrument != instrument_name:
                continue

        timestamp = _required_int(trade, "timestamp")
        price = _required_float(trade, "price")
        amount = _required_float(trade, "amount")

        # Validate numeric bounds & reject NaN/Inf
        if math.isnan(price) or math.isinf(price) or math.isnan(amount) or math.isinf(amount):
            raise HistoricalWindowError("trade price and amount cannot be NaN or Infinity")
        if price <= 0:
            raise HistoricalWindowError(f"trade price must be strictly positive, got {price}")
        if amount <= 0:
            raise HistoricalWindowError(f"trade amount must be strictly positive (zero amount rejected), got {amount}")

        # Deduplicate by trade_id / id
        tid = str(trade.get("trade_id") or trade.get("id") or "")
        if tid:
            if tid in seen_trade_ids:
                continue  # Duplicate trade; skip to prevent double counting volume
            seen_trade_ids.add(tid)

        trade_seq = int(trade.get("trade_seq", 0))
        cleaned.append({
            "timestamp": timestamp,
            "trade_seq": trade_seq,
            "price": price,
            "amount": amount,
            "trade_id": tid,
            "instrument_name": str(trade.get("instrument_name", instrument_name or "")),
        })

    # Sort strictly by timestamp ascending, then trade_seq ascending
    cleaned.sort(key=lambda t: (t["timestamp"], t["trade_seq"]))
    return cleaned


def aggregate_trade_bars(
    trades: Iterable[Mapping[str, Any]],
    *,
    instrument_name: str,
    resolution: str = "1h",
) -> tuple[HourlyTradeBar, ...]:
    """Aggregate Deribit trade rows into OHLCV bars (supports 1h and 1d).

    Guarantees:
    1. Stable sorting by (timestamp, trade_seq) for deterministic first/last trade times.
    2. Deduplication by trade_id so volume is accurate.
    3. OHLC close is only available at bucket_end (available_at = bucket_end).
    4. First and last trade timestamps are recorded per bar.
    """
    cleaned_trades = clean_and_dedup_trades(trades, instrument_name=instrument_name)
    resolution_ms = DAY_MS if resolution in ("1d", "1D", "24h") else HOUR_MS
    res_str = "1d" if resolution in ("1d", "1D", "24h") else "1h"

    buckets: dict[int, list[dict[str, Any]]] = {}
    for trade in cleaned_trades:
        ts = trade["timestamp"]
        bucket_start = ts - (ts % resolution_ms)
        buckets.setdefault(bucket_start, []).append(trade)

    bars: list[HourlyTradeBar] = []
    for bucket_start in sorted(buckets):
        rows = buckets[bucket_start]
        # Already sorted by (timestamp, trade_seq) from clean_and_dedup_trades
        prices = [r["price"] for r in rows]
        volumes = [r["amount"] for r in rows]
        bucket_end = bucket_start + resolution_ms

        bars.append(
            HourlyTradeBar(
                instrument_name=instrument_name,
                bucket_start=bucket_start,
                bucket_end=bucket_end,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                trade_count=len(rows),
                volume_contracts=sum(volumes),
                source="deribit.public.historical_trades",
                price_basis="trade_price_not_historical_bid_ask",
                first_trade_at=rows[0]["timestamp"],
                last_trade_at=rows[-1]["timestamp"],
                available_at=bucket_end,  # OHLC close is only known at bucket_end
                resolution=res_str,
            )
        )
    return tuple(bars)


def aggregate_hourly_trade_bars(
    trades: Iterable[Mapping[str, Any]],
    *,
    instrument_name: str,
) -> tuple[HourlyTradeBar, ...]:
    """Aggregate Deribit trade rows into hourly OHLCV bars (backward-compatible)."""
    return aggregate_trade_bars(trades, instrument_name=instrument_name, resolution="1h")


def trade_bar_to_price_observation(
    bar: HourlyTradeBar,
    data_quality: DataQuality = DataQuality.OBSERVED_TRADE,
) -> PriceObservation:
    """Convert an HourlyTradeBar to an authoritative core.contracts.PriceObservation."""
    return PriceObservation(
        instrument_name=bar.instrument_name,
        observed_at_ms=bar.bucket_end,
        available_at_ms=bar.available_at or bar.bucket_end,
        price_basis=PriceBasis.TRADE,
        trade_price_btc=Decimal(str(bar.close)),
        trade_size=Decimal(str(bar.volume_contracts)),
        interval_start_ms=bar.bucket_start,
        interval_end_ms=bar.bucket_end,
        quality=data_quality,
        source_ref=bar.source,
    )


def fetch_hourly_trade_bars(
    instrument_name: str,
    window: HistoricalWindow,
    *,
    count: int = DEFAULT_COUNT,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    """Fetch bounded historical trades and return hourly bars plus summary."""
    plan = build_trade_plan_for_window(
        instrument_name,
        window,
        count=count,
        max_pages=max_pages,
    )
    pages = fetch_trade_pages(plan)
    trades = [trade for page in pages for trade in page.trades]
    bars = aggregate_hourly_trade_bars(trades, instrument_name=instrument_name)
    return {
        "window": asdict(window),
        "download_summary": summarize_download(plan, pages),
        "bar_count": len(bars),
        "bars": [asdict(bar) for bar in bars],
    }


def write_window_bars_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _required_int(row: Mapping[str, Any], field_name: str) -> int:
    value = row.get(field_name)
    if value is None:
        raise HistoricalWindowError(f"trade row missing {field_name}")
    return int(value)


def _required_float(row: Mapping[str, Any], field_name: str) -> float:
    value = row.get(field_name)
    if value is None:
        raise HistoricalWindowError(f"trade row missing {field_name}")
    return float(value)


def _parse_utc_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise SystemExit("--end-time must include timezone, for example 2026-09-09T00:00:00Z")
    return parsed.astimezone(UTC)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build bounded 1h historical trade bars for BTC inverse options."
    )
    parser.add_argument("instrument_name")
    parser.add_argument("--days", type=int, default=30, choices=DEFAULT_WINDOWS_DAYS)
    parser.add_argument("--end-time", help="UTC ISO timestamp, for example 2026-09-09T00:00:00Z")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    end_time = _parse_utc_datetime(args.end_time) if args.end_time else datetime.now(UTC)
    window = next(item for item in build_default_windows(end_time=end_time) if item.days == args.days)
    plan = build_trade_plan_for_window(
        args.instrument_name,
        window,
        count=args.count,
        max_pages=args.max_pages,
    )

    if args.dry_run:
        payload = {"window": asdict(window), "plan": asdict(plan)}
    else:
        payload = fetch_hourly_trade_bars(
            args.instrument_name,
            window,
            count=args.count,
            max_pages=args.max_pages,
        )

    if args.output:
        write_window_bars_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
