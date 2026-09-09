"""Integrated storage engine for Deribit trade archives, index, and cache.

Conforms to core.contracts.Store protocol.
Implements:
- Atomic writes & rename.
- SQLite indexing & transaction safety.
- Path traversal prevention.
- Checksum verification on read/import.
- Decimal precision preservation for TradeTicks.
- Export / import bundles for offline replay.
- Incomplete manifest cache-hit exclusion.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from core.contracts import Store, TradeTick
from storage.archive import (
    TradeArchiveReader,
    TradeArchiveWriter,
    validate_safe_path,
)
from storage.models import (
    ArchiveManifest,
    ArchiveStatus,
    StorageChecksumError,
    StorageConflictError,
    StorageSecurityError,
)
from storage.sqlite_index import SQLiteCatalogIndex


class TradeStorageEngine:
    """Unified storage engine providing raw gzip archives, SQLite catalog, and Store protocol."""

    def __init__(self, root_dir: Union[str, Path]) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

        self.archives_dir = self.root_dir / "archives"
        self.archives_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.root_dir / "catalog_index.db"
        self.index = SQLiteCatalogIndex(self.db_path)
        self.writer = TradeArchiveWriter(self.archives_dir)
        self.reader = TradeArchiveReader()

    # --------------------------------------------------------------------------
    # Store Protocol Implementation
    # --------------------------------------------------------------------------

    def put(self, key: str, value: bytes) -> None:
        self.index.put_kv(key, value)

    def get(self, key: str) -> Optional[bytes]:
        return self.index.get_kv(key)

    def query(self, prefix: str) -> Iterator[Tuple[str, bytes]]:
        return self.index.query_kv(prefix)

    def resume_state(self) -> Dict[str, Any]:
        raw = self.get("resume_cursor")
        if raw:
            return json.loads(raw.decode("utf-8"))
        return {}

    # --------------------------------------------------------------------------
    # Trade Archive Storage & Caching
    # --------------------------------------------------------------------------

    def store_trades(
        self,
        instrument_name: str,
        start_ms: int,
        end_ms: int,
        trades: Sequence[Union[dict, TradeTick]],
        *,
        source_url: str = "",
        source_params: Optional[Dict[str, Any]] = None,
        status: str = ArchiveStatus.COMPLETE.value,
        coverage_ratio: str = "1.0",
        resume_cursor: Optional[Dict[str, Any]] = None,
    ) -> ArchiveManifest:
        """Atomically persist trades to a gzip-compressed JSONL archive and index it."""
        dest_path, raw_sha, comp_sha, count = self.writer.write_archive(
            instrument_name=instrument_name,
            start_ms=start_ms,
            end_ms=end_ms,
            trades=trades,
        )
        relpath = dest_path.relative_to(self.root_dir).as_posix()
        archive_id = f"arch_{instrument_name}_{start_ms}_{end_ms}_{comp_sha[:8]}"

        manifest = ArchiveManifest(
            archive_id=archive_id,
            instrument_name=instrument_name,
            start_ms=start_ms,
            end_ms=end_ms,
            source_url=source_url,
            source_params=source_params or {},
            capture_utc_ms=int(time.time() * 1000),
            record_count=count,
            sha256=comp_sha,
            raw_sha256=raw_sha,
            archive_relpath=relpath,
            status=status,
            coverage_ratio=coverage_ratio,
            resume_cursor=resume_cursor,
        )

        self.index.register_archive(manifest)
        return manifest

    def find_cache_hit(
        self, instrument_name: str, start_ms: int, end_ms: int
    ) -> Optional[ArchiveManifest]:
        """Check if an archive already exists that completely covers the requested window.

        RULE: INCOMPLETE manifests NEVER qualify as a complete cache hit.
        """
        return self.index.find_complete_cache_hit(instrument_name, start_ms, end_ms)

    def stream_trades(
        self,
        instrument_name: Optional[str] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Iterator[TradeTick]:
        """Stream TradeTicks matching criteria across stored archives without memory bloat."""
        matching_archives = self.index.query_archives(instrument_name, start_ms, end_ms)
        seen_trade_keys: set[tuple[int, str]] = set()

        for arch in matching_archives:
            archive_file = self.root_dir / arch.archive_relpath
            if not archive_file.exists():
                continue

            for tick in self.reader.stream_ticks(
                archive_file,
                expected_sha256=arch.sha256,
                instrument_name=instrument_name,
                start_ms=start_ms,
                end_ms=end_ms,
            ):
                key = (tick.trade_seq, tick.trade_id)
                if key in seen_trade_keys:
                    continue
                seen_trade_keys.add(key)
                yield tick

    # --------------------------------------------------------------------------
    # Import / Export Bundles for Offline Replay
    # --------------------------------------------------------------------------

    def export_bundle(
        self,
        target_dir: Union[str, Path],
        *,
        instrument_name: Optional[str] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Path:
        """Export matching archives and manifest to a target directory for offline replay."""
        out_dir = Path(target_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_archives_dir = out_dir / "archives"
        out_archives_dir.mkdir(parents=True, exist_ok=True)

        archives = self.index.query_archives(instrument_name, start_ms, end_ms)
        manifests_data = []

        for arch in archives:
            src_file = validate_safe_path(arch.archive_relpath, self.root_dir)
            if not src_file.exists():
                continue

            # Verify checksum before exporting
            self.reader.verify_checksum(src_file, arch.sha256)

            dest_filename = Path(arch.archive_relpath).name
            dest_file = validate_safe_path(dest_filename, out_archives_dir)
            shutil.copy2(src_file, dest_file)

            d = arch.to_dict()
            d["archive_relpath"] = f"archives/{dest_filename}"
            manifests_data.append(d)

        manifest_file = out_dir / "manifest.json"
        manifest_payload = {
            "exported_at_utc_ms": int(time.time() * 1000),
            "archive_count": len(manifests_data),
            "archives": manifests_data,
        }
        manifest_file.write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")
        return manifest_file

    def import_bundle(self, source_dir: Union[str, Path]) -> list[ArchiveManifest]:
        """Import archives from an offline bundle with security and checksum checks.

        Enforces:
        - Path traversal rejection.
        - Checksum verification of every imported archive.
        - Hash conflict rejection if destination exists with different checksum.
        """
        src_dir = Path(source_dir).resolve()
        manifest_file = src_dir / "manifest.json"
        if not manifest_file.exists():
            raise FileNotFoundError(f"Bundle manifest.json not found in {src_dir}")

        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
        archive_dicts = payload.get("archives", [])
        imported: list[ArchiveManifest] = []

        for d in archive_dicts:
            manifest = ArchiveManifest.from_dict(d)

            # Security: validate safe path
            src_archive = validate_safe_path(manifest.archive_relpath, src_dir)
            if not src_archive.exists():
                raise FileNotFoundError(f"Archive file {src_archive} referenced in bundle does not exist")

            # Checksum verification
            self.reader.verify_checksum(src_archive, manifest.sha256)

            dest_filename = Path(manifest.archive_relpath).name
            dest_archive = validate_safe_path(dest_filename, self.archives_dir)

            if dest_archive.exists():
                # Check for hash conflict
                hasher = hashlib.sha256()
                with open(dest_archive, "rb") as f:
                    while chunk := f.read(65536):
                        hasher.update(chunk)
                existing_sha = hasher.hexdigest()
                if existing_sha != manifest.sha256:
                    raise StorageConflictError(
                        f"Existing file {dest_archive.name} has conflicting SHA256 ({existing_sha}) "
                        f"versus incoming archive ({manifest.sha256})"
                    )
            else:
                shutil.copy2(src_archive, dest_archive)

            # Register in SQLite catalog
            new_relpath = dest_archive.relative_to(self.root_dir).as_posix()
            updated_manifest = ArchiveManifest(
                archive_id=manifest.archive_id,
                instrument_name=manifest.instrument_name,
                start_ms=manifest.start_ms,
                end_ms=manifest.end_ms,
                source_url=manifest.source_url,
                source_params=manifest.source_params,
                capture_utc_ms=manifest.capture_utc_ms,
                record_count=manifest.record_count,
                sha256=manifest.sha256,
                raw_sha256=manifest.raw_sha256,
                archive_relpath=new_relpath,
                schema_version=manifest.schema_version,
                status=manifest.status,
                coverage_ratio=manifest.coverage_ratio,
                resume_cursor=manifest.resume_cursor,
            )
            self.index.register_archive(updated_manifest)
            imported.append(updated_manifest)

        return imported
