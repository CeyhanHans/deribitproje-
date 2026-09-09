"""Small, bounded Deribit History API trade downloader.

The adapter is intentionally narrow: one instrument, one sequence or time range,
and an explicit page cap. It uses only Deribit's public history endpoint and does
not require API keys.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://history.deribit.com/api/v2/public"
DEFAULT_COUNT = 1000
MAX_COUNT = 1000
DEFAULT_MAX_PAGES = 1


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

    def validate(self) -> None:
        if not self.instrument_name:
            raise ValueError("instrument_name is required")
        if self.count < 1:
            raise ValueError("count must be >= 1")
        if self.count > MAX_COUNT:
            raise ValueError(f"count must be <= {MAX_COUNT}")
        if self.max_pages < 1:
            raise ValueError("max_pages must be >= 1")
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


def build_sequence_plan(
    instrument_name: str,
    start_seq: int,
    end_seq: int,
    *,
    count: int = DEFAULT_COUNT,
    max_pages: int = DEFAULT_MAX_PAGES,
    base_url: str = DEFAULT_BASE_URL,
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
            requests.append(
                {
                    "instrument_name": plan.instrument_name,
                    "start_seq": start_seq,
                    "end_seq": end_seq,
                    "count": plan.count,
                }
            )
            start_seq = end_seq + 1
        return requests

    assert plan.start_timestamp is not None
    assert plan.end_timestamp is not None
    return [
        {
            "instrument_name": plan.instrument_name,
            "start_timestamp": plan.start_timestamp,
            "end_timestamp": plan.end_timestamp,
            "count": plan.count,
        }
    ]


def fetch_trade_pages(plan: TradeDownloadPlan) -> list[TradePage]:
    """Fetch pages for the bounded plan.

    Sequence mode advances with fixed `count`-sized sequence windows. Time mode
    pages older trades by moving `end_timestamp` to the oldest returned
    timestamp minus one millisecond; for dense same-millisecond trades, prefer
    sequence mode for exact coverage checks.
    """
    plan.validate()
    pages: list[TradePage] = []

    if plan.mode == "sequence":
        for page_no, params in enumerate(planned_requests(plan), start=1):
            result = _fetch_result(plan.base_url, plan.endpoint, params)
            trades = result.get("trades", [])
            if not isinstance(trades, list):
                raise DeribitHistoryError("Deribit response result.trades is not a list")
            pages.append(_page_from_result(page_no, params, result, trades))
        return pages

    assert plan.start_timestamp is not None
    assert plan.end_timestamp is not None
    next_end = plan.end_timestamp
    for page_no in range(1, plan.max_pages + 1):
        params = {
            "instrument_name": plan.instrument_name,
            "start_timestamp": plan.start_timestamp,
            "end_timestamp": next_end,
            "count": plan.count,
        }
        result = _fetch_result(plan.base_url, plan.endpoint, params)
        trades = result.get("trades", [])
        if not isinstance(trades, list):
            raise DeribitHistoryError("Deribit response result.trades is not a list")
        pages.append(_page_from_result(page_no, params, result, trades))
        if not result.get("has_more") or not trades:
            break
        timestamps = [int(row["timestamp"]) for row in trades if "timestamp" in row]
        if not timestamps:
            break
        next_end = min(timestamps) - 1
        if next_end < plan.start_timestamp:
            break
    return pages


def summarize_download(plan: TradeDownloadPlan, pages: list[TradePage]) -> dict[str, Any]:
    all_trades = [trade for page in pages for trade in page.trades]
    summary: dict[str, Any] = {
        "instrument_name": plan.instrument_name,
        "mode": plan.mode,
        "page_count": len(pages),
        "trade_count": len(all_trades),
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


def _fetch_result(
    base_url: str,
    endpoint: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "deribit-backtest-catalog/0.1"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise DeribitHistoryError(f"HTTP {exc.code} from Deribit history API") from exc
    except URLError as exc:
        raise DeribitHistoryError(f"Could not reach Deribit history API: {exc}") from exc

    if "error" in payload:
        error = payload["error"]
        raise DeribitHistoryError(
            f"Deribit API error {error.get('code')}: {error.get('message')}"
        )

    result = payload.get("result")
    if not isinstance(result, dict):
        raise DeribitHistoryError("Deribit response missing result object")
    return result


def _page_from_result(
    page_no: int,
    params: dict[str, Any],
    result: dict[str, Any],
    trades: list[dict[str, Any]],
) -> TradePage:
    return TradePage(
        page_no=page_no,
        request=params,
        trade_count=len(trades),
        has_more=bool(result.get("has_more")),
        trades=trades,
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
        )
    return build_time_plan(
        args.instrument_name,
        args.start_timestamp,
        args.end_timestamp,
        count=args.count,
        max_pages=args.max_pages,
        base_url=args.base_url,
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
