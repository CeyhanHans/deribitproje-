"""Gzip-compressed JSONL raw archive manager with atomic writes and checksum verification.

Ensures:
- Decimal precision preservation without float conversion loss.
- Atomic temp-file creation and rename (crash-safe).
- Path traversal rejection.
- Streaming generator reads with timestamp / instrument filters.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence, Union

from core.contracts import TradeTick
from storage.models import (
    StorageChecksumError,
    StorageSecurityError,
)


def validate_safe_path(relpath: Union[str, Path], base_dir: Path) -> Path:
    """Ensure relpath stays safely within base_dir, preventing path traversal attacks."""
    base_resolved = base_dir.resolve()
    relpath_str = str(relpath)
    if ".." in relpath_str.split("/") or ".." in relpath_str.split("\\"):
        raise StorageSecurityError(f"Path traversal detected: {relpath!r} contains '..'")
    
    target = (base_dir / relpath).resolve()
    if not target.is_relative_to(base_resolved):
        raise StorageSecurityError(f"Path traversal detected: {relpath!r} escapes base directory {base_dir}")
    return target


def trade_to_dict(trade: Union[dict, TradeTick]) -> Dict[str, Any]:
    """Convert trade dict or TradeTick to JSON-serializable dictionary with string decimals."""
    if isinstance(trade, TradeTick):
        return {
            "instrument_name": trade.instrument_name,
            "trade_id": trade.trade_id,
            "trade_seq": trade.trade_seq,
            "timestamp": trade.timestamp_ms,
            "price": str(trade.price_btc),
            "amount": str(trade.quantity_contracts),
            "source_ref": trade.source_ref,
        }
    
    # Dict trade: ensure numeric values are properly preserved
    d = dict(trade)
    if "price" in d and isinstance(d["price"], (Decimal, float, int)):
        d["price"] = str(d["price"])
    if "amount" in d and isinstance(d["amount"], (Decimal, float, int)):
        d["amount"] = str(d["amount"])
    if "timestamp_ms" in d and "timestamp" not in d:
        d["timestamp"] = d["timestamp_ms"]
    return d


def dict_to_trade_tick(d: Dict[str, Any]) -> TradeTick:
    """Reconstruct immutable TradeTick with Decimal amounts from stored JSON dict."""
    price_str = str(d.get("price", "0"))
    amount_str = str(d.get("amount", d.get("quantity_contracts", "0")))
    ts = int(d.get("timestamp", d.get("timestamp_ms", 0)))
    seq = int(d.get("trade_seq", 0)) if d.get("trade_seq") is not None else 0
    trade_id = str(d.get("trade_id", ""))
    inst = str(d.get("instrument_name", ""))
    source_ref = str(d.get("source_ref", f"archive_{trade_id}"))

    return TradeTick(
        instrument_name=inst,
        trade_id=trade_id,
        trade_seq=seq,
        timestamp_ms=ts,
        price_btc=Decimal(price_str),
        quantity_contracts=Decimal(amount_str),
        source_ref=source_ref,
    )


class TradeArchiveWriter:
    """Writes trade streams to gzip-compressed JSONL with atomic temp-file rename."""

    def __init__(self, archives_dir: Path) -> None:
        self.archives_dir = archives_dir.resolve()
        self.archives_dir.mkdir(parents=True, exist_ok=True)

    def write_archive(
        self,
        instrument_name: str,
        start_ms: int,
        end_ms: int,
        trades: Sequence[Union[dict, TradeTick]],
    ) -> tuple[Path, str, str, int]:
        """Write trades to .jsonl.gz atomically.

        Returns:
            (destination_path, raw_sha256, compressed_sha256, record_count)
        """
        temp_name = f"_tmp_{uuid.uuid4().hex}.jsonl.gz"
        temp_path = self.archives_dir / temp_name

        raw_hasher = hashlib.sha256()
        record_count = 0

        # 1. Write to temp file with fixed mtime for byte-for-byte deterministic hashing
        with open(temp_path, "wb") as f_out:
            with gzip.GzipFile(filename="", mode="wb", fileobj=f_out, compresslevel=6, mtime=0.0) as gz_out:
                for t in trades:
                    record = trade_to_dict(t)
                    line_bytes = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
                    raw_hasher.update(line_bytes)
                    gz_out.write(line_bytes)
                    record_count += 1

        # 2. Compute compressed sha256 of the temp file
        compressed_hasher = hashlib.sha256()
        with open(temp_path, "rb") as f:
            while chunk := f.read(65536):
                compressed_hasher.update(chunk)

        raw_sha256 = raw_hasher.hexdigest()
        compressed_sha256 = compressed_hasher.hexdigest()

        # 3. Final atomic destination
        safe_inst = instrument_name.replace("/", "_").replace("\\", "_")
        dest_filename = f"{safe_inst}_{start_ms}_{end_ms}_{compressed_sha256[:12]}.jsonl.gz"
        dest_path = self.archives_dir / dest_filename

        # If dest already exists with same checksum, replace safely
        if dest_path.exists():
            temp_path.unlink()
        else:
            temp_path.replace(dest_path)

        return dest_path, raw_sha256, compressed_sha256, record_count


class TradeArchiveReader:
    """Streams trade records from gzip-compressed JSONL files with checksum verification."""

    @staticmethod
    def verify_checksum(archive_path: Path, expected_sha256: str) -> None:
        """Verify the SHA256 checksum of an archive file."""
        if not archive_path.exists():
            raise FileNotFoundError(f"Archive file not found: {archive_path}")
        hasher = hashlib.sha256()
        with open(archive_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        actual = hasher.hexdigest()
        if actual != expected_sha256:
            raise StorageChecksumError(
                f"Checksum mismatch for {archive_path.name}: expected {expected_sha256}, got {actual}"
            )

    @classmethod
    def stream_ticks(
        cls,
        archive_path: Path,
        *,
        expected_sha256: Optional[str] = None,
        instrument_name: Optional[str] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Iterator[TradeTick]:
        """Stream TradeTick instances from .jsonl.gz with streaming filter."""
        if expected_sha256 is not None:
            cls.verify_checksum(archive_path, expected_sha256)

        with gzip.open(archive_path, "rt", encoding="utf-8") as gz_in:
            for line in gz_in:
                if not line.strip():
                    continue
                record = json.loads(line)
                ts = int(record.get("timestamp", record.get("timestamp_ms", 0)))
                inst = str(record.get("instrument_name", ""))

                if instrument_name is not None and inst != instrument_name:
                    continue
                if start_ms is not None and ts < start_ms:
                    continue
                if end_ms is not None and ts >= end_ms:
                    continue

                yield dict_to_trade_tick(record)
