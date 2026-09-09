"""Storage package for Deribit options backtest catalog.

Provides:
- SQLite transactional index.
- Atomic Gzip JSONL raw archives.
- Checksum verification & conflict protection.
- Streaming TradeTick access with exact Decimal precision.
- Store protocol compliance.
"""

from storage.archive import (
    TradeArchiveReader,
    TradeArchiveWriter,
    validate_safe_path,
)
from storage.engine import TradeStorageEngine
from storage.models import (
    ArchiveManifest,
    ArchiveStatus,
    StorageChecksumError,
    StorageConflictError,
    StorageError,
    StorageSecurityError,
)
from storage.sqlite_index import SQLiteCatalogIndex

__all__ = [
    "ArchiveManifest",
    "ArchiveStatus",
    "StorageError",
    "StorageChecksumError",
    "StorageConflictError",
    "StorageSecurityError",
    "TradeArchiveReader",
    "TradeArchiveWriter",
    "SQLiteCatalogIndex",
    "TradeStorageEngine",
    "validate_safe_path",
]
