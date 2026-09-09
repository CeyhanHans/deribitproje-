"""Single Deribit public order-book snapshot collector.

The adapter intentionally performs one bounded public/get_order_book request for
one explicit instrument. It does not poll, subscribe, or require API keys.
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

DEFAULT_BASE_URL = "https://www.deribit.com/api/v2/public"


class DeribitSnapshotError(RuntimeError):
    """Raised when Deribit cannot return a usable public snapshot."""


@dataclass(frozen=True)
class SnapshotRequest:
    instrument_name: str
    base_url: str = DEFAULT_BASE_URL

    def validate(self) -> None:
        if not self.instrument_name:
            raise ValueError("instrument_name is required")
        if not self.instrument_name.startswith("BTC-"):
            raise ValueError("instrument_name must be a BTC inverse option instrument")
        parts = self.instrument_name.split("-")
        if len(parts) != 4 or parts[-1] not in {"C", "P"}:
            raise ValueError("instrument_name must use Deribit option format BTC-<expiry>-<strike>-<C|P>")


@dataclass(frozen=True)
class OptionSnapshot:
    source: str
    instrument_name: str
    timestamp: int | None
    best_bid_price: float | None
    best_ask_price: float | None
    mark_price: float | None
    bid_iv: float | None
    ask_iv: float | None
    mark_iv: float | None
    greeks: dict[str, float | None]
    open_interest: float | None


def fetch_option_snapshot(request: SnapshotRequest) -> OptionSnapshot:
    request.validate()
    result = _fetch_result(
        request.base_url,
        "/get_order_book",
        {"instrument_name": request.instrument_name},
    )
    return normalize_order_book_snapshot(result)


def normalize_order_book_snapshot(result: dict[str, Any]) -> OptionSnapshot:
    greeks = result.get("greeks", {})
    if greeks is None:
        greeks = {}
    if not isinstance(greeks, dict):
        raise DeribitSnapshotError("Deribit response result.greeks is not an object")

    instrument_name = result.get("instrument_name")
    if not isinstance(instrument_name, str) or not instrument_name:
        raise DeribitSnapshotError("Deribit response missing instrument_name")

    return OptionSnapshot(
        source="deribit.public.get_order_book",
        instrument_name=instrument_name,
        timestamp=_optional_int(result.get("timestamp")),
        best_bid_price=_optional_float(result.get("best_bid_price")),
        best_ask_price=_optional_float(result.get("best_ask_price")),
        mark_price=_optional_float(result.get("mark_price")),
        bid_iv=_optional_float(result.get("bid_iv")),
        ask_iv=_optional_float(result.get("ask_iv")),
        mark_iv=_optional_float(result.get("mark_iv")),
        greeks={
            "delta": _optional_float(greeks.get("delta")),
            "gamma": _optional_float(greeks.get("gamma")),
            "vega": _optional_float(greeks.get("vega")),
            "theta": _optional_float(greeks.get("theta")),
            "rho": _optional_float(greeks.get("rho")),
        },
        open_interest=_optional_float(result.get("open_interest")),
    )


def write_snapshot_json(path: Path, snapshot: OptionSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(snapshot), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
        raise DeribitSnapshotError(f"HTTP {exc.code} from Deribit public API") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        detail = f": {reason}" if reason else ""
        raise DeribitSnapshotError(f"Could not reach Deribit public API{detail}") from exc
    except TimeoutError as exc:
        raise DeribitSnapshotError("Timed out reaching Deribit public API") from exc

    if "error" in payload:
        error = payload["error"]
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else "unknown error"
        raise DeribitSnapshotError(f"Deribit API error {code}: {message}")

    result = payload.get("result")
    if not isinstance(result, dict):
        raise DeribitSnapshotError("Deribit response missing result object")
    return result


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch one Deribit public BTC option order-book snapshot."
    )
    parser.add_argument("instrument_name")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    request = SnapshotRequest(args.instrument_name, base_url=args.base_url)
    snapshot = fetch_option_snapshot(request)
    payload = asdict(snapshot)
    if args.output:
        write_snapshot_json(args.output, snapshot)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
