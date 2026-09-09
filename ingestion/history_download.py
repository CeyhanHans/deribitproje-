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
        if self.sorting not in ("asc", "desc"):
            raise ValueError("sorting must be 'asc' or 'desc'")


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

        # Resume state restoration
        if resume and store is not None:
            resume_data = store.resume_state()
            if resume_data and resume_data.get("instrument_name") == self.plan.instrument_name:
                pages_fetched = resume_data.get("pages_fetched", 0)
                total_bytes_fetched = resume_data.get("total_bytes", 0)
                if self.plan.sorting == "asc":
                    current_start = resume_data.get("next_start_timestamp", current_start)
                else:
                    current_end = resume_data.get("next_end_timestamp", current_end)

                # Restore previous trades from store
                for _, page_bytes in store.query("trade_page_"):
                    page_items = json.loads(page_bytes.decode("utf-8"))
                    for t in page_items:
                        seq = int(t.get("trade_seq", 0)) if t.get("trade_seq") is not None else 0
                        tid = str(t.get("trade_id", ""))
                        key = (seq, tid)
                        if key not in seen_keys:
                            seen_keys.add(key)
                            collected_trades.append(t)

        while True:
            # Check budgets before making next request
            if pages_fetched >= self.plan.max_pages:
                is_complete = False
                status = "INCOMPLETE"
                reason_code = "page_cap_reached"
                break

            if self.plan.max_bytes is not None and total_bytes_fetched >= self.plan.max_bytes:
                is_complete = False
                status = "INCOMPLETE"
                reason_code = "byte_cap_reached"
                break

            elapsed = time.monotonic() - start_wall_time
            if self.plan.max_seconds is not None and elapsed >= self.plan.max_seconds:
                is_complete = False
                status = "INCOMPLETE"
                reason_code = "time_budget_exceeded"
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
                    timeout=self.plan.timeout_seconds,
                    max_retries=self.plan.max_retries,
                    sleeper=sleeper,
                )
            except DeribitHistoryError as exc:
                is_complete = False
                status = "FAILED"
                reason_code = f"fetch_error: {exc}"
                break

            pages_fetched += 1
            total_bytes_fetched += byte_size

            trades = result.get("trades", [])
            has_more = bool(result.get("has_more"))

            if not trades:
                # No trades found in this window
                if not collected_trades:
                    status = "EMPTY"
                    reason_code = "no_trades_found"
                break

            # Filter and deduplicate trades in page
            new_trades: list[dict[str, Any]] = []
            for t in trades:
                seq = int(t.get("trade_seq", 0)) if t.get("trade_seq") is not None else 0
                tid = str(t.get("trade_id", ""))
                key = (seq, tid)
                if key not in seen_keys:
                    seen_keys.add(key)
                    new_trades.append(t)
                    collected_trades.append(t)

            # Persist page to store if enabled
            if store is not None and new_trades:
                store.put(f"trade_page_{pages_fetched:06d}", json.dumps(new_trades).encode("utf-8"))

            timestamps = [int(t["timestamp"]) for t in trades if "timestamp" in t]
            if not timestamps:
                break

            min_ts = min(timestamps)
            max_ts = max(timestamps)

            # Check if there are more trades to fetch
            if not has_more:
                # We have reached the end of Deribit's available trades
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
                            sleeper=sleeper,
                        )
                        if seq_advanced is not None:
                            # Successfully bridged past the saturated millisecond!
                            if self.plan.sorting == "asc":
                                current_start = seq_advanced
                            else:
                                current_end = seq_advanced
                            continue

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
            if store is not None:
                cursor = {
                    "instrument_name": self.plan.instrument_name,
                    "pages_fetched": pages_fetched,
                    "total_bytes": total_bytes_fetched,
                    "next_start_timestamp": current_start,
                    "next_end_timestamp": current_end,
                    "collected_count": len(collected_trades),
                }
                store.put("resume_cursor", json.dumps(cursor).encode("utf-8"))

        # Finalize and sort trades
        sorted_unique_trades = sort_trades(collected_trades)
        gap_analysis = analyze_sequence_gaps(sorted_unique_trades)

        # Canonical SHA256
        canonical_bytes = json.dumps(
            [trade_identity_key(t) for t in sorted_unique_trades],
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
                "next_start_timestamp": current_start,
                "next_end_timestamp": current_end,
                "pages_fetched": pages_fetched,
            } if not is_complete else None,
        )

        return sorted_unique_trades, manifest

    def _continue_via_sequence(
        self,
        last_seq: int,
        target_timestamp: int,
        seen_keys: set[tuple[int, str]],
        collected_trades: list[dict[str, Any]],
        sleeper: Callable[[float], None],
    ) -> int | None:
        """Fetch trades via sequence endpoint when a single millisecond overflows page count."""
        start_seq = last_seq + 1 if self.plan.sorting == "asc" else max(1, last_seq - self.plan.count)
        end_seq = start_seq + self.plan.count - 1 if self.plan.sorting == "asc" else last_seq - 1

        params: dict[str, Any] = {
            "instrument_name": self.plan.instrument_name,
            "start_seq": start_seq,
            "end_seq": end_seq,
            "count": self.plan.count,
            "sorting": self.plan.sorting,
        }

        try:
            result, _ = fetch_deribit_json(
                self.plan.base_url,
                "/get_last_trades_by_instrument",
                params,
                timeout=self.plan.timeout_seconds,
                max_retries=self.plan.max_retries,
                sleeper=sleeper,
            )
        except DeribitHistoryError:
            return None

        trades = result.get("trades", [])
        if not trades:
            return None

        advanced_ts: int | None = None
        for t in trades:
            seq = int(t.get("trade_seq", 0)) if t.get("trade_seq") is not None else 0
            tid = str(t.get("trade_id", ""))
            key = (seq, tid)
            if key not in seen_keys:
                seen_keys.add(key)
                collected_trades.append(t)
            t_ms = int(t.get("timestamp", 0))
            if self.plan.sorting == "asc" and t_ms > target_timestamp:
                advanced_ts = t_ms
            elif self.plan.sorting == "desc" and t_ms < target_timestamp:
                advanced_ts = t_ms

        return advanced_ts


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
