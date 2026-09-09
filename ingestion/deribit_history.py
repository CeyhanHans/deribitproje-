"""Bounded and resilient Deribit History API trade downloader.

Official Reference:
  Deribit API v2 Public History Endpoints
  Documentation: https://docs.deribit.com/
  Verified: 2026-09-09
  Endpoints:
    - /public/get_last_trades_by_instrument
    - /public/get_last_trades_by_instrument_and_time
  Default Host: https://history.deribit.com/api/v2/public
  Count Limit: 1..1000 per request
  HTTP 429: Rate limit backoff with Retry-After header
  Error format: {"jsonrpc": "2.0", "error": {"code": int, "message": str}}
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://history.deribit.com/api/v2/public"
DEFAULT_COUNT = 1000
MAX_COUNT = 1000
DEFAULT_MAX_PAGES = 1
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_FACTOR = 0.5


class DeribitHistoryError(RuntimeError):
    """Raised when the Deribit history endpoint cannot return a usable result."""


@dataclass(frozen=True)
class TradeDownloadPlan:
    instrument_name: str
    mode: str
    endpoint: str
    count: int = DEFAULT_COUNT
    max_pages: int = DEFAULT_MAX_PAGES
    base_url: str = DEFAULT_BASE_URL
    start_seq: int | None = None
    end_seq: int | None = None
    start_timestamp: int | None = None
    end_timestamp: int | None = None
    sorting: str = "default"
    overlap_boundary: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    max_bytes: int | None = None
    max_seconds: float | None = None

    def validate(self) -> None:
        if not self.instrument_name:
            raise ValueError("instrument_name is required")
        if self.count < 1:
            raise ValueError("count must be >= 1")
        if self.count > MAX_COUNT:
            raise ValueError(f"count must be <= {MAX_COUNT}")
        if self.max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if self.max_retries < 1:
            raise ValueError("max_retries must be >= 1")
        if self.max_bytes is not None and self.max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        if self.max_seconds is not None and self.max_seconds <= 0:
            raise ValueError("max_seconds must be > 0")

        if self.mode == "sequence":
            if self.start_seq is None or self.end_seq is None:
                raise ValueError("sequence mode requires start_seq and end_seq")
            if self.start_seq < 1 or self.end_seq < 1:
                raise ValueError("sequence bounds must be >= 1")
            if self.start_seq > self.end_seq:
                raise ValueError("start_seq must be <= end_seq")
        elif self.mode == "time":
            if self.start_timestamp is None or self.end_timestamp is None:
                raise ValueError("time mode requires start_timestamp and end_timestamp")
            if self.start_timestamp < 0 or self.end_timestamp < 0:
                raise ValueError("timestamp bounds must be >= 0")
            if self.start_timestamp > self.end_timestamp:
                raise ValueError("start_timestamp must be <= end_timestamp")
        else:
            raise ValueError("mode must be 'sequence' or 'time'")


@dataclass(frozen=True)
class TradePage:
    page_no: int
    request: dict[str, Any]
    trade_count: int
    has_more: bool
    trades: list[dict[str, Any]]
    byte_size: int = 0


def build_sequence_plan(
    instrument_name: str,
    start_seq: int,
    end_seq: int,
    *,
    count: int = DEFAULT_COUNT,
    max_pages: int = DEFAULT_MAX_PAGES,
    base_url: str = DEFAULT_BASE_URL,
    sorting: str = "default",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_bytes: int | None = None,
    max_seconds: float | None = None,
) -> TradeDownloadPlan:
    plan = TradeDownloadPlan(
        instrument_name=instrument_name,
        mode="sequence",
        endpoint="/get_last_trades_by_instrument",
        count=count,
        max_pages=max_pages,
        base_url=base_url,
        start_seq=start_seq,
        end_seq=end_seq,
        sorting=sorting,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        max_bytes=max_bytes,
        max_seconds=max_seconds,
    )
    plan.validate()
    return plan


def build_time_plan(
    instrument_name: str,
    start_timestamp: int,
    end_timestamp: int,
    *,
    count: int = DEFAULT_COUNT,
    max_pages: int = DEFAULT_MAX_PAGES,
    base_url: str = DEFAULT_BASE_URL,
    sorting: str = "default",
    overlap_boundary: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_bytes: int | None = None,
    max_seconds: float | None = None,
) -> TradeDownloadPlan:
    plan = TradeDownloadPlan(
        instrument_name=instrument_name,
        mode="time",
        endpoint="/get_last_trades_by_instrument_and_time",
        count=count,
        max_pages=max_pages,
        base_url=base_url,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        sorting=sorting,
        overlap_boundary=overlap_boundary,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        max_bytes=max_bytes,
        max_seconds=max_seconds,
    )
    plan.validate()
    return plan


def planned_requests(plan: TradeDownloadPlan) -> list[dict[str, Any]]:
    """Return bounded request params without contacting Deribit."""
    plan.validate()
    if plan.mode == "sequence":
        assert plan.start_seq is not None
        assert plan.end_seq is not None
        requests = []
        start_seq = plan.start_seq
        while start_seq <= plan.end_seq and len(requests) < plan.max_pages:
            end_seq = min(start_seq + plan.count - 1, plan.end_seq)
            req: dict[str, Any] = {
                "instrument_name": plan.instrument_name,
                "start_seq": start_seq,
                "end_seq": end_seq,
                "count": plan.count,
            }
            if plan.sorting != "default":
                req["sorting"] = plan.sorting
            requests.append(req)
            start_seq = end_seq + 1
        return requests

    assert plan.start_timestamp is not None
    assert plan.end_timestamp is not None
    req_time: dict[str, Any] = {
        "instrument_name": plan.instrument_name,
        "start_timestamp": plan.start_timestamp,
        "end_timestamp": plan.end_timestamp,
        "count": plan.count,
    }
    if plan.sorting != "default":
        req_time["sorting"] = plan.sorting
    return [req_time]


def fetch_deribit_json(
    base_url: str,
    endpoint: str,
    params: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], int]:
    """Fetch JSON from Deribit public API with timeout, retries, 429 Retry-After, and backoff.

    Official Reference: Deribit API v2 public specification (verified 2026-09-09).
    """
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "deribit-backtest-catalog/0.1"})

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                raw_bytes = response.read()
                byte_size = len(raw_bytes)
                payload = json.loads(raw_bytes.decode("utf-8"))

            if "error" in payload:
                error = payload["error"]
                # JSON-RPC error: client/logical error, non-retryable
                raise DeribitHistoryError(
                    f"Deribit API error {error.get('code')}: {error.get('message')}"
                )

            result = payload.get("result")
            if not isinstance(result, dict):
                raise DeribitHistoryError("Deribit response missing result object")

            return result, byte_size

        except HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                retry_after_str = None
                if getattr(exc, "headers", None):
                    retry_after_str = exc.headers.get("Retry-After")
                try:
                    retry_seconds = float(retry_after_str) if retry_after_str else backoff_factor * (2 ** (attempt - 1))
                except (ValueError, TypeError):
                    retry_seconds = backoff_factor * (2 ** (attempt - 1))
                if attempt < max_retries:
                    sleeper(retry_seconds)
                    continue
                raise DeribitHistoryError(f"HTTP 429 Rate Limit exceeded after {max_retries} retries") from exc
            elif exc.code in (500, 502, 503, 504):
                if attempt < max_retries:
                    sleeper(backoff_factor * (2 ** (attempt - 1)))
                    continue
                raise DeribitHistoryError(f"HTTP {exc.code} server error after {max_retries} retries") from exc
            else:
                # Client error (e.g. 400, 404): non-retryable
                raise DeribitHistoryError(f"HTTP {exc.code} from Deribit history API") from exc

        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = exc
            if attempt < max_retries:
                sleeper(backoff_factor * (2 ** (attempt - 1)))
                continue
            raise DeribitHistoryError(f"Could not reach Deribit history API after {max_retries} retries: {exc}") from exc

    raise DeribitHistoryError(f"Request failed after {max_retries} retries: {last_error}")


def _fetch_result(
    base_url: str,
    endpoint: str,
    params: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    result, _ = fetch_deribit_json(
        base_url,
        endpoint,
        params,
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        sleeper=sleeper,
    )
    return result


def fetch_trade_pages(
    plan: TradeDownloadPlan,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[TradePage]:
    """Fetch pages for the bounded plan.

    Supports timeout, retry with backoff, 429 rate limit backoff, max_pages,
    max_bytes, and max_seconds constraints.
    """
    plan.validate()
    pages: list[TradePage] = []
    total_bytes = 0
    start_wall_time = time.monotonic()

    def check_budgets() -> bool:
        if plan.max_bytes is not None and total_bytes >= plan.max_bytes:
            return False
        if plan.max_seconds is not None and (time.monotonic() - start_wall_time) >= plan.max_seconds:
            return False
        return True

    if plan.mode == "sequence":
        for page_no, params in enumerate(planned_requests(plan), start=1):
            if not check_budgets():
                break
            result, byte_size = fetch_deribit_json(
                plan.base_url,
                plan.endpoint,
                params,
                timeout=plan.timeout_seconds,
                max_retries=plan.max_retries,
                sleeper=sleeper,
            )
            total_bytes += byte_size
            trades = result.get("trades", [])
            if not isinstance(trades, list):
                raise DeribitHistoryError("Deribit response result.trades is not a list")
            pages.append(_page_from_result(page_no, params, result, trades, byte_size=byte_size))
            if not result.get("has_more") or not trades:
                break
        return pages

    assert plan.start_timestamp is not None
    assert plan.end_timestamp is not None
    next_end = plan.end_timestamp

    for page_no in range(1, plan.max_pages + 1):
        if not check_budgets():
            break
        params: dict[str, Any] = {
            "instrument_name": plan.instrument_name,
            "start_timestamp": plan.start_timestamp,
            "end_timestamp": next_end,
            "count": plan.count,
        }
        if plan.sorting != "default":
            params["sorting"] = plan.sorting

        result, byte_size = fetch_deribit_json(
            plan.base_url,
            plan.endpoint,
            params,
            timeout=plan.timeout_seconds,
            max_retries=plan.max_retries,
            sleeper=sleeper,
        )
        total_bytes += byte_size
        trades = result.get("trades", [])
        if not isinstance(trades, list):
            raise DeribitHistoryError("Deribit response result.trades is not a list")
        pages.append(_page_from_result(page_no, params, result, trades, byte_size=byte_size))
        if not result.get("has_more") or not trades:
            break
        timestamps = [int(row["timestamp"]) for row in trades if "timestamp" in row]
        if not timestamps:
            break

        min_ts = min(timestamps)
        if plan.overlap_boundary:
            next_end = min_ts  # Boundary inclusive
        else:
            next_end = min_ts - 1  # Legacy exclusive

        if next_end < plan.start_timestamp:
            break
    return pages


def summarize_download(plan: TradeDownloadPlan, pages: list[TradePage]) -> dict[str, Any]:
    all_trades = [trade for page in pages for trade in page.trades]
    total_bytes = sum(page.byte_size for page in pages)
    summary: dict[str, Any] = {
        "instrument_name": plan.instrument_name,
        "mode": plan.mode,
        "page_count": len(pages),
        "trade_count": len(all_trades),
        "total_bytes": total_bytes,
        "any_has_more": any(page.has_more for page in pages),
        "last_has_more": pages[-1].has_more if pages else False,
        "page_cap_reached": len(pages) >= plan.max_pages,
        "stopped_with_more_available": bool(pages)
        and pages[-1].has_more
        and len(pages) >= plan.max_pages,
    }

    seqs = sorted(
        {int(trade["trade_seq"]) for trade in all_trades if "trade_seq" in trade}
    )
    if seqs:
        summary["min_trade_seq"] = seqs[0]
        summary["max_trade_seq"] = seqs[-1]

    if plan.mode == "sequence":
        assert plan.start_seq is not None
        assert plan.end_seq is not None
        expected = set(range(plan.start_seq, plan.end_seq + 1))
        seen = set(seqs)
        summary["requested_sequence_count"] = len(expected)
        summary["in_range_unique_sequence_count"] = len(expected & seen)
        summary["sequence_range_complete"] = expected.issubset(seen)

    return summary


def write_json(path: Path, plan: TradeDownloadPlan, pages: list[TradePage]) -> None:
    payload = {
        "plan": asdict(plan),
        "summary": summarize_download(plan, pages),
        "pages": [asdict(page) for page in pages],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _page_from_result(
    page_no: int,
    params: dict[str, Any],
    result: dict[str, Any],
    trades: list[dict[str, Any]],
    byte_size: int = 0,
) -> TradePage:
    return TradePage(
        page_no=page_no,
        request=params,
        trade_count=len(trades),
        has_more=bool(result.get("has_more")),
        trades=trades,
        byte_size=byte_size,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch a bounded Deribit History API trade JSON sample."
    )
    parser.add_argument("instrument_name")
    parser.add_argument("--start-seq", type=int)
    parser.add_argument("--end-seq", type=int)
    parser.add_argument("--start-timestamp", type=int)
    parser.add_argument("--end-timestamp", type=int)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--sorting", default="default", choices=["default", "asc", "desc"])
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--max-bytes", type=int)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request plan without contacting Deribit.",
    )
    return parser.parse_args(argv)


def _plan_from_args(args: argparse.Namespace) -> TradeDownloadPlan:
    has_sequence = args.start_seq is not None or args.end_seq is not None
    has_time = args.start_timestamp is not None or args.end_timestamp is not None
    if has_sequence == has_time:
        raise SystemExit("Choose exactly one range mode: sequence or time.")
    if has_sequence:
        return build_sequence_plan(
            args.instrument_name,
            args.start_seq,
            args.end_seq,
            count=args.count,
            max_pages=args.max_pages,
            base_url=args.base_url,
            sorting=args.sorting,
            timeout_seconds=args.timeout,
            max_retries=args.max_retries,
            max_bytes=args.max_bytes,
            max_seconds=args.max_seconds,
        )
    return build_time_plan(
        args.instrument_name,
        args.start_timestamp,
        args.end_timestamp,
        count=args.count,
        max_pages=args.max_pages,
        base_url=args.base_url,
        sorting=args.sorting,
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
        max_bytes=args.max_bytes,
        max_seconds=args.max_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    plan = _plan_from_args(args)
    if args.dry_run:
        print(json.dumps({"plan": asdict(plan), "requests": planned_requests(plan)}, indent=2))
        return 0

    pages = fetch_trade_pages(plan)
    payload = {
        "plan": asdict(plan),
        "summary": summarize_download(plan, pages),
        "pages": [asdict(page) for page in pages],
    }
    if args.output:
        write_json(args.output, plan, pages)
        print(json.dumps(payload["summary"], indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
