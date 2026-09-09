"""Lossless, resumable historical trade download engine for Deribit options.

Official API Reference & Semantics:
  Date Verified: 2026-09-09
  Source: Deribit API v2 Official Documentation (https://docs.deribit.com/)
  Endpoints:
    - /public/get_last_trades_by_instrument_and_time:
        Parameters: instrument_name (str), start_timestamp (int ms), end_timestamp (int ms),
                    count (int 1..1000), sorting ("asc" | "desc" | "default").
        Response: {"jsonrpc": "2.0", "result": {"trades": [...], "has_more": bool}}
    - /public/get_last_trades_by_instrument:
        Parameters: instrument_name (str), start_seq (int), end_seq (int),
                    count (int 1..1000), sorting ("asc" | "desc" | "default").
        Response: {"jsonrpc": "2.0", "result": {"trades": [...], "has_more": bool}}
  Host Architecture:
    - Dedicated History Host: https://history.deribit.com/api/v2/public (Default)
    - Production Live Host: https://www.deribit.com/api/v2/public
  Rate Limits & Error Handling:
    - HTTP 429: Too Many Requests; respect Retry-After header.
    - JSON-RPC -32602: Invalid params (non-retryable client error).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from core.contracts import (
    DownloadResult,
    HistoricalTradeProvider,
    Store,
    TradeTick,
    ValidationError,
)
from ingestion.deribit_history import (
    DEFAULT_BACKOFF_FACTOR,
    DEFAULT_BASE_URL,
    DEFAULT_COUNT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_COUNT,
    DeribitHistoryError,
    fetch_deribit_json,
)

SCHEMA_VERSION = "1.0"
MAX_SMOKE_REQUESTS = 20


# ==============================================================================
# 1. Trade Sorting & Deduplication
# ==============================================================================

def trade_identity_key(trade: dict[str, Any]) -> tuple[int, int, str]:
    """Extract a canonical identity key (timestamp, trade_seq, trade_id) for sorting and dedup."""
    ts = int(trade.get("timestamp", 0))
    seq = int(trade.get("trade_seq", 0)) if trade.get("trade_seq") is not None else 0
    trade_id = str(trade.get("trade_id", ""))
    return (ts, seq, trade_id)


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return str(Decimal(str(value)))
    if isinstance(value, dict):
        return {str(k): _canonical_json_value(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical_json_value(v) for v in value]
    return value


def canonical_trade_payload(trade: dict[str, Any]) -> dict[str, Any]:
    """Return a stable payload that includes identity, economic fields, and units."""
    return {
        "instrument_name": str(trade.get("instrument_name", "")),
        "trade_id": str(trade.get("trade_id", "")),
        "trade_seq": int(trade.get("trade_seq", 0)) if trade.get("trade_seq") is not None else 0,
        "timestamp_ms": int(trade.get("timestamp", trade.get("timestamp_ms", 0))),
        "price_btc": _canonical_json_value(trade.get("price")),
        "quantity_contracts": _canonical_json_value(trade.get("amount", trade.get("quantity_contracts"))),
        "direction": str(trade.get("direction", "")),
    }


def sort_trades(trades: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministically sort trades by (timestamp, trade_seq, trade_id)."""
    return sorted(trades, key=trade_identity_key)


def deduplicate_trades(trades: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate trades while preserving deterministic sort order.

    Deduplication matches on trade_seq (if positive) and trade_id.
    """
    seen: set[tuple[int, str]] = set()
    unique: list[dict[str, Any]] = []

    for t in sort_trades(trades):
        seq = int(t.get("trade_seq", 0)) if t.get("trade_seq") is not None else 0
        tid = str(t.get("trade_id", ""))
        key = (seq, tid)
        if key == (0, ""):
            # Fallback to timestamp if neither seq nor id is present
            ts_key = (int(t.get("timestamp", 0)), str(t))
            if ts_key in seen:
                continue
            seen.add(ts_key)  # type: ignore[arg-type]
            unique.append(t)
        else:
            if key in seen:
                continue
            seen.add(key)
            unique.append(t)

    return unique


def _trade_in_requested_range(trade: dict[str, Any], start_ms: int, end_ms: int) -> bool:
    try:
        ts = int(trade.get("timestamp", trade.get("timestamp_ms")))
    except (TypeError, ValueError):
        return False
    return start_ms <= ts < end_ms


# ==============================================================================
# 2. Sequence Gap Analysis
# ==============================================================================

def analyze_sequence_gaps(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Analyze sequence numbers without jumping to unwarranted conclusions.

    Rationale:
    In Deribit's architecture, trade_seq numbers are monotonic across matching engines.
    An option instrument may show sequence gaps because trades on other instruments
    or non-trade matching events incremented the sequence counter.
    Therefore, an observed sequence gap DOES NOT necessarily prove missing data
    unless corroborated by an incomplete range response.
    """
    if not trades:
        return {
            "total_trades": 0,
            "min_seq": None,
            "max_seq": None,
            "unique_seq_count": 0,
            "gap_count": 0,
            "gaps": [],
            "is_contiguous": True,
            "interpretation": "empty_trades",
        }

    seqs = sorted({int(t["trade_seq"]) for t in trades if "trade_seq" in t and t["trade_seq"] is not None})
    if not seqs:
        return {
            "total_trades": len(trades),
            "min_seq": None,
            "max_seq": None,
            "unique_seq_count": 0,
            "gap_count": 0,
            "gaps": [],
            "is_contiguous": True,
            "interpretation": "no_sequence_numbers_in_trades",
        }

    min_seq = seqs[0]
    max_seq = seqs[-1]
    expected_count = max_seq - min_seq + 1
    unique_count = len(seqs)
    gap_count = expected_count - unique_count

    gaps: list[tuple[int, int]] = []
    if gap_count > 0:
        for i in range(len(seqs) - 1):
            curr_s = seqs[i]
            next_s = seqs[i + 1]
            if next_s > curr_s + 1:
                gaps.append((curr_s + 1, next_s - 1))

    return {
        "total_trades": len(trades),
        "min_seq": min_seq,
        "max_seq": max_seq,
        "unique_seq_count": unique_count,
        "gap_count": gap_count,
        "gaps": gaps,
        "is_contiguous": gap_count == 0,
        "interpretation": (
            "strictly_contiguous" if gap_count == 0 else
            f"gaps_detected: {gap_count} non-consecutive sequence numbers observed. Note: On Deribit, sequence gaps may arise from exchange-level counter progression across instruments and do not automatically indicate download failure."
        ),
    }


# ==============================================================================
# 3. Storage Protocol Implementations
# ==============================================================================

class InMemoryStore:
    """In-memory implementation of the Store protocol for caching and testing."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def put(self, key: str, value: bytes) -> None:
        self._store[key] = value

    def get(self, key: str) -> Optional[bytes]:
        return self._store.get(key)

    def query(self, prefix: str) -> Iterator[Tuple[str, bytes]]:
        for k in sorted(self._store.keys()):
            if k.startswith(prefix):
                yield (k, self._store[k])

    def resume_state(self) -> Dict[str, Any]:
        raw = self.get("resume_cursor")
        if raw:
            return json.loads(raw.decode("utf-8"))
        return {}


class JsonFileStore:
    """Disk-backed implementation of the Store protocol using JSON/binary files."""

    def __init__(self, root_dir: Path | str) -> None:
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        safe_key = key.replace("/", "_").replace("\\", "_")
        return self.root / safe_key

    def put(self, key: str, value: bytes) -> None:
        p = self._path_for(key)
        p.write_bytes(value)

    def get(self, key: str) -> Optional[bytes]:
        p = self._path_for(key)
        if p.exists() and p.is_file():
            return p.read_bytes()
        return None

    def query(self, prefix: str) -> Iterator[Tuple[str, bytes]]:
        safe_prefix = prefix.replace("/", "_").replace("\\", "_")
        for item in sorted(self.root.glob(f"{safe_prefix}*")):
            if item.is_file():
                yield (item.name, item.read_bytes())

    def resume_state(self) -> Dict[str, Any]:
        raw = self.get("resume_cursor")
        if raw:
            return json.loads(raw.decode("utf-8"))
        return {}


# ==============================================================================
# 4. Download Plan & Completeness Manifest
# ==============================================================================

@dataclass(frozen=True)
class HistoryDownloadPlan:
    """Immutable plan specifying what historical trade data to download."""
    instrument_name: str
    start_timestamp: int
    end_timestamp: int
    count: int = DEFAULT_COUNT
    max_pages: int = 100
    max_bytes: int | None = None
    max_seconds: float | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    base_url: str = DEFAULT_BASE_URL
    sorting: str = "asc"
    enable_sequence_continuation: bool = True

    def validate(self) -> None:
        if not self.instrument_name:
            raise ValueError("instrument_name cannot be empty")
        if self.start_timestamp < 0 or self.end_timestamp < 0:
            raise ValueError("timestamps must be non-negative")
        if self.start_timestamp > self.end_timestamp:
            raise ValueError(f"start_timestamp ({self.start_timestamp}) cannot exceed end_timestamp ({self.end_timestamp})")
        if self.count < 1 or self.count > MAX_COUNT:
            raise ValueError(f"count must be between 1 and {MAX_COUNT}")
        if self.max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 1:
            raise ValueError("max_retries must be >= 1")
        if self.max_retries > DEFAULT_MAX_RETRIES:
            raise ValueError(f"max_retries must be <= {DEFAULT_MAX_RETRIES}")
        if self.max_bytes is not None and self.max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        if self.max_seconds is not None and self.max_seconds <= 0:
            raise ValueError("max_seconds must be positive")
        if self.sorting not in ("asc", "desc"):
            raise ValueError("sorting must be 'asc' or 'desc'")

    def resume_plan_key(self) -> str:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "instrument_name": self.instrument_name,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "count": self.count,
            "sorting": self.sorting,
            "base_url": self.base_url,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def page_key_prefix(self) -> str:
        return f"trade_page_{self.resume_plan_key()}_"


@dataclass(frozen=True)
class CompletenessManifest:
    """Manifest documenting the actual execution, coverage completeness, and audit trail."""
    schema_version: str
    instrument_name: str
    requested_start_ms: int
    requested_end_ms: int
    actual_start_ms: int | None
    actual_end_ms: int | None
    is_complete: bool
    status: str  # "COMPLETE", "INCOMPLETE", "EMPTY", "FAILED"
    reason_code: str
    total_trades_received: int
    unique_trades_count: int
    pages_fetched: int
    total_bytes_fetched: int
    elapsed_seconds: float
    sequence_gaps_analysis: dict[str, Any]
    trades_sha256: str
    resume_cursor: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ==============================================================================
# 5. Lossless Historical Downloader Engine
# ==============================================================================

class LosslessHistoryDownloader:
    """Resilient, boundary-loss-free historical trade downloader for Deribit options."""

    def __init__(self, plan: HistoryDownloadPlan) -> None:
        plan.validate()
        self.plan = plan

    def download(
        self,
        *,
        store: Store | None = None,
        resume: bool = False,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> tuple[list[dict[str, Any]], CompletenessManifest]:
        """Execute lossless trade download with overlap deduplication and sequence continuation."""
        start_wall_time = time.monotonic()
        collected_trades: list[dict[str, Any]] = []
        seen_keys: set[tuple[int, str]] = set()

        current_start = self.plan.start_timestamp
        current_end = self.plan.end_timestamp
        pages_fetched = 0
        total_bytes_fetched = 0
        is_complete = True
        status = "COMPLETE"
        reason_code = "full_range_covered"
        plan_key = self.plan.resume_plan_key()
        page_prefix = self.plan.page_key_prefix()

        def remaining_timeout() -> float:
            timeout = self.plan.timeout_seconds
            if self.plan.max_seconds is None:
                return timeout
            elapsed = time.monotonic() - start_wall_time
            remaining = self.plan.max_seconds - elapsed
            return max(0.001, min(timeout, remaining))

        def deadline_monotonic() -> float | None:
            if self.plan.max_seconds is None:
                return None
            return start_wall_time + self.plan.max_seconds

        def remaining_bytes() -> int | None:
            if self.plan.max_bytes is None:
                return None
            return max(0, self.plan.max_bytes - total_bytes_fetched)

        def budget_failure() -> str | None:
            if pages_fetched >= self.plan.max_pages:
                return "page_cap_reached"
            if self.plan.max_bytes is not None and total_bytes_fetched >= self.plan.max_bytes:
                return "byte_cap_reached"
            elapsed = time.monotonic() - start_wall_time
            if self.plan.max_seconds is not None and elapsed >= self.plan.max_seconds:
                return "time_budget_exceeded"
            return None

        def add_filtered_trades(raw_trades: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
            new_items: list[dict[str, Any]] = []
            for t in raw_trades:
                if "timestamp" not in t and "timestamp_ms" not in t:
                    raise DeribitHistoryError("Deribit response trade missing timestamp")
                if not _trade_in_requested_range(t, self.plan.start_timestamp, self.plan.end_timestamp):
                    continue
                seq = int(t.get("trade_seq", 0)) if t.get("trade_seq") is not None else 0
                tid = str(t.get("trade_id", ""))
                key = (seq, tid)
                if key not in seen_keys:
                    seen_keys.add(key)
                    new_items.append(t)
                    collected_trades.append(t)
            return new_items

        def persist_page(new_items: Sequence[dict[str, Any]]) -> None:
            if store is not None and new_items:
                key = f"{page_prefix}{pages_fetched:06d}"
                payload = json.dumps(new_items, default=str, separators=(",", ":")).encode("utf-8")
                store.put(key, payload)

        def persist_cursor() -> None:
            if store is None:
                return
            cursor = {
                "schema_version": SCHEMA_VERSION,
                "plan_key": plan_key,
                "instrument_name": self.plan.instrument_name,
                "requested_start_ms": self.plan.start_timestamp,
                "requested_end_ms": self.plan.end_timestamp,
                "count": self.plan.count,
                "sorting": self.plan.sorting,
                "base_url": self.plan.base_url,
                "pages_fetched": pages_fetched,
                "total_bytes": total_bytes_fetched,
                "next_start_timestamp": current_start,
                "next_end_timestamp": current_end,
                "collected_count": len(collected_trades),
            }
            encoded = json.dumps(cursor, separators=(",", ":")).encode("utf-8")
            store.put(f"resume_cursor_{plan_key}", encoded)
            store.put("resume_cursor", encoded)

        # Resume state restoration
        if resume and store is not None:
            plan_cursor = store.get(f"resume_cursor_{plan_key}")
            resume_data = json.loads(plan_cursor.decode("utf-8")) if plan_cursor else store.resume_state()
            if resume_data:
                if resume_data.get("plan_key") != plan_key:
                    raise ValueError("resume cursor does not match the requested download plan")
                pages_fetched = resume_data.get("pages_fetched", 0)
                total_bytes_fetched = resume_data.get("total_bytes", 0)
                if self.plan.sorting == "asc":
                    current_start = resume_data.get("next_start_timestamp", current_start)
                else:
                    current_end = resume_data.get("next_end_timestamp", current_end)

                # Restore previous trades from store
                for _, page_bytes in store.query(page_prefix):
                    page_items = json.loads(page_bytes.decode("utf-8"))
                    add_filtered_trades(page_items)

        while True:
            # Check budgets before making next request
            failure = budget_failure()
            if failure is not None:
                is_complete = False
                status = "INCOMPLETE"
                reason_code = failure
                break

            # Build request params
            params: dict[str, Any] = {
                "instrument_name": self.plan.instrument_name,
                "start_timestamp": current_start,
                "end_timestamp": current_end,
                "count": self.plan.count,
                "sorting": self.plan.sorting,
            }

            try:
                result, byte_size = fetch_deribit_json(
                    self.plan.base_url,
                    "/get_last_trades_by_instrument_and_time",
                    params,
                    timeout=remaining_timeout(),
                    max_retries=self.plan.max_retries,
                    deadline_monotonic=deadline_monotonic(),
                    max_response_bytes=remaining_bytes(),
                    sleeper=sleeper,
                )
            except DeribitHistoryError as exc:
                is_complete = False
                message = str(exc)
                if "max_response_bytes" in message:
                    status = "INCOMPLETE"
                    reason_code = "byte_cap_reached"
                elif "deadline exhausted" in message:
                    status = "INCOMPLETE"
                    reason_code = "time_budget_exceeded"
                else:
                    status = "FAILED"
                    reason_code = f"fetch_error: {exc}"
                break

            pages_fetched += 1
            total_bytes_fetched += byte_size

            trades = result.get("trades", [])
            has_more = bool(result.get("has_more"))
            if not isinstance(trades, list):
                is_complete = False
                status = "FAILED"
                reason_code = "malformed_response_trades_not_list"
                break

            if not trades:
                # No trades found in this window
                if has_more:
                    is_complete = False
                    status = "INCOMPLETE"
                    reason_code = "malformed_empty_page_with_has_more"
                    persist_cursor()
                    break
                if not collected_trades:
                    status = "EMPTY"
                    reason_code = "no_trades_found"
                break

            # Filter and deduplicate trades in page
            try:
                new_trades = add_filtered_trades(trades)
            except DeribitHistoryError as exc:
                is_complete = False
                status = "FAILED"
                reason_code = f"malformed_response: {exc}"
                break

            # Persist page to store if enabled
            persist_page(new_trades)
            persist_cursor()

            timestamps = [int(t["timestamp"]) for t in trades if "timestamp" in t]
            if not timestamps:
                is_complete = False
                status = "FAILED"
                reason_code = "malformed_response_missing_timestamps"
                break

            min_ts = min(timestamps)
            max_ts = max(timestamps)

            # Check if there are more trades to fetch
            if not has_more:
                # We have reached the end of Deribit's available trades
                break
            if self.plan.sorting == "asc" and max_ts >= self.plan.end_timestamp:
                break
            if self.plan.sorting == "desc" and min_ts < self.plan.start_timestamp:
                break

            # Check for Same-Millisecond Overflow condition:
            # All trades returned in this full page have the EXACT same millisecond AND hit the page count limit.
            if len(trades) >= self.plan.count and min_ts == max_ts:
                if self.plan.enable_sequence_continuation:
                    # Attempt sequence continuation across this saturated millisecond
                    seqs = [int(t["trade_seq"]) for t in trades if "trade_seq" in t and t["trade_seq"] is not None]
                    if seqs:
                        last_seq = max(seqs) if self.plan.sorting == "asc" else min(seqs)
                        seq_advanced = self._continue_via_sequence(
                            last_seq=last_seq,
                            target_timestamp=max_ts,
                            seen_keys=seen_keys,
                            collected_trades=collected_trades,
                            store=store,
                            page_prefix=page_prefix,
                            start_wall_time=start_wall_time,
                            pages_fetched_ref=lambda: pages_fetched,
                            total_bytes_fetched_ref=lambda: total_bytes_fetched,
                            plan_key=plan_key,
                            current_start_ref=lambda: current_start,
                            current_end_ref=lambda: current_end,
                            sleeper=sleeper,
                        )
                        seq_advanced, seq_pages, seq_bytes, seq_reason = seq_advanced
                        pages_fetched += seq_pages
                        total_bytes_fetched += seq_bytes
                        persist_cursor()
                        if seq_advanced is not None:
                            # Successfully bridged past the saturated millisecond!
                            if self.plan.sorting == "asc":
                                current_start = seq_advanced
                            else:
                                current_end = seq_advanced
                            if current_start >= self.plan.end_timestamp:
                                break
                            if seq_reason is not None:
                                is_complete = False
                                status = "INCOMPLETE"
                                reason_code = seq_reason
                                break
                            continue
                        if seq_reason is not None:
                            is_complete = False
                            status = "INCOMPLETE"
                            reason_code = seq_reason
                            break

                # If sequence continuation cannot advance or is disabled:
                # We cannot guarantee complete coverage for this millisecond!
                is_complete = False
                status = "INCOMPLETE"
                reason_code = "same_millisecond_overflow"
                break

            # Boundary Inclusive Pagination:
            # If all trades at max_ts fit into the page (len(trades) < count and min_ts == max_ts),
            # we can advance past max_ts without skipping.
            if min_ts == max_ts and len(trades) < self.plan.count:
                if self.plan.sorting == "asc":
                    current_start = max_ts + 1
                    if current_start > self.plan.end_timestamp:
                        break
                else:
                    current_end = min_ts - 1
                    if current_end < self.plan.start_timestamp:
                        break
            else:
                # Overlap the boundary timestamp to prevent losing trades in the same millisecond!
                if self.plan.sorting == "asc":
                    current_start = max_ts  # Inclusive boundary forward
                    if current_start > self.plan.end_timestamp:
                        break
                else:
                    current_end = min_ts  # Inclusive boundary backward
                    if current_end < self.plan.start_timestamp:
                        break

            # Persist resume cursor
            persist_cursor()

        # Finalize and sort trades
        sorted_unique_trades = sort_trades(collected_trades)
        gap_analysis = analyze_sequence_gaps(sorted_unique_trades)

        # Canonical SHA256
        canonical_bytes = json.dumps(
            [canonical_trade_payload(t) for t in sorted_unique_trades],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        trades_sha256 = hashlib.sha256(canonical_bytes).hexdigest()

        actual_start = sorted_unique_trades[0]["timestamp"] if sorted_unique_trades else None
        actual_end = sorted_unique_trades[-1]["timestamp"] if sorted_unique_trades else None

        elapsed_seconds = time.monotonic() - start_wall_time

        manifest = CompletenessManifest(
            schema_version=SCHEMA_VERSION,
            instrument_name=self.plan.instrument_name,
            requested_start_ms=self.plan.start_timestamp,
            requested_end_ms=self.plan.end_timestamp,
            actual_start_ms=actual_start,
            actual_end_ms=actual_end,
            is_complete=is_complete,
            status=status,
            reason_code=reason_code,
            total_trades_received=len(collected_trades),
            unique_trades_count=len(sorted_unique_trades),
            pages_fetched=pages_fetched,
            total_bytes_fetched=total_bytes_fetched,
            elapsed_seconds=elapsed_seconds,
            sequence_gaps_analysis=gap_analysis,
            trades_sha256=trades_sha256,
            resume_cursor={
                "schema_version": SCHEMA_VERSION,
                "plan_key": plan_key,
                "next_start_timestamp": current_start,
                "next_end_timestamp": current_end,
                "pages_fetched": pages_fetched,
                "total_bytes": total_bytes_fetched,
            } if not is_complete else None,
        )

        return sorted_unique_trades, manifest

    def _continue_via_sequence(
        self,
        last_seq: int,
        target_timestamp: int,
        seen_keys: set[tuple[int, str]],
        collected_trades: list[dict[str, Any]],
        store: Store | None,
        page_prefix: str,
        start_wall_time: float,
        pages_fetched_ref: Callable[[], int],
        total_bytes_fetched_ref: Callable[[], int],
        plan_key: str,
        current_start_ref: Callable[[], int],
        current_end_ref: Callable[[], int],
        sleeper: Callable[[float], None],
    ) -> tuple[int | None, int, int, str | None]:
        """Fetch trades via sequence endpoint when a single millisecond overflows page count."""
        pages = 0
        bytes_seen = 0
        cursor_seq = last_seq

        while True:
            if pages_fetched_ref() + pages >= self.plan.max_pages:
                return None, pages, bytes_seen, "page_cap_reached"
            if self.plan.max_bytes is not None and total_bytes_fetched_ref() + bytes_seen >= self.plan.max_bytes:
                return None, pages, bytes_seen, "byte_cap_reached"
            if self.plan.max_seconds is not None and (time.monotonic() - start_wall_time) >= self.plan.max_seconds:
                return None, pages, bytes_seen, "time_budget_exceeded"

            start_seq = cursor_seq + 1 if self.plan.sorting == "asc" else max(1, cursor_seq - self.plan.count)
            end_seq = start_seq + self.plan.count - 1 if self.plan.sorting == "asc" else cursor_seq - 1
            params: dict[str, Any] = {
                "instrument_name": self.plan.instrument_name,
                "start_seq": start_seq,
                "end_seq": end_seq,
                "count": self.plan.count,
                "sorting": self.plan.sorting,
            }

            timeout = self.plan.timeout_seconds
            if self.plan.max_seconds is not None:
                timeout = max(0.001, min(timeout, self.plan.max_seconds - (time.monotonic() - start_wall_time)))

            try:
                result, byte_size = fetch_deribit_json(
                    self.plan.base_url,
                    "/get_last_trades_by_instrument",
                    params,
                    timeout=timeout,
                    max_retries=self.plan.max_retries,
                    deadline_monotonic=(
                        start_wall_time + self.plan.max_seconds
                        if self.plan.max_seconds is not None else None
                    ),
                    max_response_bytes=(
                        max(0, self.plan.max_bytes - (total_bytes_fetched_ref() + bytes_seen))
                        if self.plan.max_bytes is not None else None
                    ),
                    sleeper=sleeper,
                )
            except DeribitHistoryError:
                return None, pages, bytes_seen, "sequence_fetch_error"

            pages += 1
            bytes_seen += byte_size
            trades = result.get("trades", [])
            if not isinstance(trades, list) or not trades:
                return None, pages, bytes_seen, "same_millisecond_overflow"

            new_trades: list[dict[str, Any]] = []
            advanced_ts: int | None = None
            max_seen_seq = cursor_seq
            min_seen_seq = cursor_seq
            for t in trades:
                if "timestamp" not in t and "timestamp_ms" not in t:
                    return None, pages, bytes_seen, "malformed_response_missing_timestamps"
                seq = int(t.get("trade_seq", 0)) if t.get("trade_seq") is not None else 0
                tid = str(t.get("trade_id", ""))
                key = (seq, tid)
                t_ms = int(t.get("timestamp", t.get("timestamp_ms", 0)))
                max_seen_seq = max(max_seen_seq, seq)
                min_seen_seq = min(min_seen_seq, seq)
                if self.plan.start_timestamp <= t_ms < self.plan.end_timestamp and key not in seen_keys:
                    seen_keys.add(key)
                    new_trades.append(t)
                    collected_trades.append(t)
                if self.plan.sorting == "asc" and t_ms > target_timestamp:
                    advanced_ts = t_ms if advanced_ts is None else min(advanced_ts, t_ms)
                elif self.plan.sorting == "desc" and t_ms < target_timestamp:
                    advanced_ts = t_ms if advanced_ts is None else max(advanced_ts, t_ms)

            if store is not None and new_trades:
                key = f"{page_prefix}{pages_fetched_ref() + pages:06d}"
                payload = json.dumps(new_trades, default=str, separators=(",", ":")).encode("utf-8")
                store.put(key, payload)
            if store is not None:
                cursor = {
                    "schema_version": SCHEMA_VERSION,
                    "plan_key": plan_key,
                    "instrument_name": self.plan.instrument_name,
                    "requested_start_ms": self.plan.start_timestamp,
                    "requested_end_ms": self.plan.end_timestamp,
                    "count": self.plan.count,
                    "sorting": self.plan.sorting,
                    "base_url": self.plan.base_url,
                    "pages_fetched": pages_fetched_ref() + pages,
                    "total_bytes": total_bytes_fetched_ref() + bytes_seen,
                    "next_start_timestamp": current_start_ref(),
                    "next_end_timestamp": current_end_ref(),
                    "collected_count": len(collected_trades),
                    "sequence_cursor": cursor_seq,
                }
                encoded = json.dumps(cursor, separators=(",", ":")).encode("utf-8")
                store.put(f"resume_cursor_{plan_key}", encoded)
                store.put("resume_cursor", encoded)

            if advanced_ts is not None:
                return advanced_ts, pages, bytes_seen, None

            next_cursor = max_seen_seq if self.plan.sorting == "asc" else min_seen_seq
            if next_cursor == cursor_seq:
                return None, pages, bytes_seen, "same_millisecond_overflow"
            cursor_seq = next_cursor


# ==============================================================================
# 6. HistoricalTradeProvider Implementation
# ==============================================================================

class DeribitHistoricalTradeProvider:
    """Historical trade provider conforming to core.contracts.HistoricalTradeProvider."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL) -> None:
        self.base_url = base_url

    def fetch(self, plan: Any) -> DownloadResult:
        """Fetch historical trades for the given plan and return DownloadResult."""
        if not isinstance(plan, HistoryDownloadPlan):
            raise ValidationError("plan must be an instance of HistoryDownloadPlan")

        downloader = LosslessHistoryDownloader(plan)
        trades, manifest = downloader.download()

        return DownloadResult(
            success=manifest.is_complete,
            ticks_count=manifest.unique_trades_count,
            start_ms=self.convert_to_ms(manifest.actual_start_ms or plan.start_timestamp),
            end_ms=self.convert_to_ms(manifest.actual_end_ms or plan.end_timestamp),
            output_path=None,
            error_message=None if manifest.is_complete else manifest.reason_code,
        )

    @staticmethod
    def convert_to_ms(val: int) -> int:
        return int(val)

    @staticmethod
    def to_trade_ticks(trades: Sequence[dict[str, Any]]) -> list[TradeTick]:
        """Convert raw Deribit trades into immutable TradeTick contracts with Decimals."""
        ticks: list[TradeTick] = []
        for t in sort_trades(trades):
            ticks.append(
                TradeTick(
                    instrument_name=str(t.get("instrument_name", "")),
                    trade_id=str(t.get("trade_id", "")),
                    trade_seq=int(t.get("trade_seq", 0)),
                    timestamp_ms=int(t.get("timestamp", 0)),
                    price_btc=Decimal(str(t.get("price", "0"))),
                    quantity_contracts=Decimal(str(t.get("amount", "0"))),
                    source_ref=f"deribit_history_{t.get('trade_id', '')}",
                )
            )
        return ticks


# ==============================================================================
# 7. Smoke Check Runner (Max 20 Requests)
# ==============================================================================

def run_live_smoke_check(
    instrument_name: str,
    start_timestamp: int,
    end_timestamp: int,
    *,
    max_requests: int = MAX_SMOKE_REQUESTS,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any]:
    """Execute a live smoke check strictly capped at max 20 requests."""
    capped_requests = min(max_requests, MAX_SMOKE_REQUESTS)
    plan = HistoryDownloadPlan(
        instrument_name=instrument_name,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        count=min(DEFAULT_COUNT, 100),
        max_pages=capped_requests,
        base_url=base_url,
    )
    downloader = LosslessHistoryDownloader(plan)
    trades, manifest = downloader.download()
    return {
        "manifest": manifest.to_dict(),
        "trade_sample_count": len(trades),
        "requests_executed": manifest.pages_fetched,
        "capped_max_requests": capped_requests,
    }
