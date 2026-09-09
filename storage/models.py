"""Storage models, manifest specifications, and exceptions for Deribit option backtest catalog.

Schema Version: 1.0
Conforms to immutable core contracts specification.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA_VERSION: str = "1.0"


class StorageError(Exception):
    """Base exception for storage errors."""


class StorageChecksumError(StorageError):
    """Raised when stored or imported data fails SHA256 checksum verification."""


class StorageConflictError(StorageError):
    """Raised when an operation attempts to overwrite data with conflicting content."""


class StorageSecurityError(StorageError):
    """Raised when path traversal or unauthorized directory escape is detected."""


class ArchiveStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    EMPTY = "EMPTY"


@dataclass(frozen=True)
class ArchiveManifest:
    """Immutable manifest for a stored trade archive."""
    archive_id: str
    instrument_name: str
    start_ms: int
    end_ms: int
    source_url: str
    source_params: Dict[str, Any]
    capture_utc_ms: int
    record_count: int
    sha256: str
    raw_sha256: str
    archive_relpath: str
    schema_version: str = SCHEMA_VERSION
    status: str = ArchiveStatus.COMPLETE.value
    coverage_ratio: str = "1.0"
    resume_cursor: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if not self.archive_id:
            raise ValueError("archive_id cannot be empty")
        if not self.instrument_name:
            raise ValueError("instrument_name cannot be empty")
        if self.start_ms < 0 or self.end_ms < 0:
            raise ValueError("start_ms and end_ms must be non-negative")
        if self.start_ms > self.end_ms:
            raise ValueError(f"start_ms ({self.start_ms}) cannot exceed end_ms ({self.end_ms})")
        if not self.sha256 or len(self.sha256) != 64:
            raise ValueError("sha256 must be a 64-character hex string")
        if not self.raw_sha256 or len(self.raw_sha256) != 64:
            raise ValueError("raw_sha256 must be a 64-character hex string")
        if self.status not in [e.value for e in ArchiveStatus]:
            raise ValueError(f"Invalid status: {self.status}")

    def is_complete(self) -> bool:
        return self.status == ArchiveStatus.COMPLETE.value

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ArchiveManifest:
        return cls(
            archive_id=str(d["archive_id"]),
            instrument_name=str(d["instrument_name"]),
            start_ms=int(d["start_ms"]),
            end_ms=int(d["end_ms"]),
            source_url=str(d.get("source_url", "")),
            source_params=dict(d.get("source_params", {})),
            capture_utc_ms=int(d["capture_utc_ms"]),
            record_count=int(d["record_count"]),
            sha256=str(d["sha256"]),
            raw_sha256=str(d["raw_sha256"]),
            archive_relpath=str(d["archive_relpath"]),
            schema_version=str(d.get("schema_version", SCHEMA_VERSION)),
            status=str(d.get("status", ArchiveStatus.COMPLETE.value)),
            coverage_ratio=str(d.get("coverage_ratio", "1.0")),
            resume_cursor=d.get("resume_cursor"),
        )
