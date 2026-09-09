"""SQLite transactional catalog index for historical archives and key-value state.

Features:
- WAL mode for concurrent readers and writer safety.
- Transactional ACID integrity with automatic connection closing on Windows.
- Hash conflict detection (rejects overwriting data with conflicting checksum).
- Strict cache hit rule: incomplete manifests never return complete cache hits.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple

from storage.models import (
    ArchiveManifest,
    ArchiveStatus,
    StorageConflictError,
)


class SQLiteCatalogIndex:
    """SQLite-backed metadata catalog and KV store for historical archives."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS archives (
                    archive_id TEXT PRIMARY KEY,
                    instrument_name TEXT NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    source_url TEXT,
                    source_params TEXT,
                    capture_utc_ms INTEGER NOT NULL,
                    schema_version TEXT NOT NULL,
                    record_count INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    coverage_ratio TEXT NOT NULL,
                    archive_relpath TEXT NOT NULL,
                    resume_cursor TEXT,
                    created_at_ms INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_archives_inst_time
                ON archives (instrument_name, start_ms, end_ms);

                CREATE INDEX IF NOT EXISTS idx_archives_sha256
                ON archives (sha256);

                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value BLOB NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                """
            )

    def register_archive(self, manifest: ArchiveManifest) -> None:
        """Register an archive manifest, enforcing hash consistency."""
        now_ms = int(time.time() * 1000)
        with self._connection() as conn:
            # Check for existing entry with same instrument and time range
            cur = conn.execute(
                """
                SELECT archive_id, sha256, status
                FROM archives
                WHERE instrument_name = ? AND start_ms = ? AND end_ms = ?
                """,
                (manifest.instrument_name, manifest.start_ms, manifest.end_ms),
            )
            existing = cur.fetchone()
            if existing:
                is_completion_upgrade = (
                    existing["status"] == ArchiveStatus.INCOMPLETE.value
                    and manifest.status == ArchiveStatus.COMPLETE.value
                )
                if existing["sha256"] != manifest.sha256 and not is_completion_upgrade:
                    raise StorageConflictError(
                        f"Conflict: archive for {manifest.instrument_name} [{manifest.start_ms}, {manifest.end_ms}) "
                        f"already exists with different SHA256 (existing {existing['sha256'][:12]}, incoming {manifest.sha256[:12]})"
                    )
                # Update archive metadata
                conn.execute(
                    """
                    UPDATE archives
                    SET archive_id = ?,
                        source_url = ?,
                        source_params = ?,
                        capture_utc_ms = MAX(capture_utc_ms, ?),
                        status = ?,
                        sha256 = ?,
                        raw_sha256 = ?,
                        record_count = ?,
                        coverage_ratio = ?,
                        resume_cursor = ?,
                        archive_relpath = ?
                    WHERE archive_id = ?
                    """,
                    (
                        manifest.archive_id,
                        manifest.source_url,
                        json.dumps(manifest.source_params),
                        manifest.capture_utc_ms,
                        manifest.status,
                        manifest.sha256,
                        manifest.raw_sha256,
                        manifest.record_count,
                        manifest.coverage_ratio,
                        json.dumps(manifest.resume_cursor) if manifest.resume_cursor else None,
                        manifest.archive_relpath,
                        existing["archive_id"],
                    ),
                )
                return

            # Insert new archive
            conn.execute(
                """
                INSERT INTO archives (
                    archive_id, instrument_name, start_ms, end_ms,
                    source_url, source_params, capture_utc_ms, schema_version,
                    record_count, sha256, raw_sha256, status,
                    coverage_ratio, archive_relpath, resume_cursor, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.archive_id,
                    manifest.instrument_name,
                    manifest.start_ms,
                    manifest.end_ms,
                    manifest.source_url,
                    json.dumps(manifest.source_params),
                    manifest.capture_utc_ms,
                    manifest.schema_version,
                    manifest.record_count,
                    manifest.sha256,
                    manifest.raw_sha256,
                    manifest.status,
                    manifest.coverage_ratio,
                    manifest.archive_relpath,
                    json.dumps(manifest.resume_cursor) if manifest.resume_cursor else None,
                    now_ms,
                ),
            )

    def find_complete_cache_hit(
        self, instrument_name: str, start_ms: int, end_ms: int
    ) -> Optional[ArchiveManifest]:
        """Find an existing archive that completely covers the requested window.

        CRITICAL RULE:
        Manifests marked as INCOMPLETE will NEVER be returned as a complete cache hit.
        """
        with self._connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM archives
                WHERE instrument_name = ?
                  AND start_ms <= ?
                  AND end_ms >= ?
                  AND status = ?
                ORDER BY (end_ms - start_ms) DESC, created_at_ms DESC
                LIMIT 1
                """,
                (instrument_name, start_ms, end_ms, ArchiveStatus.COMPLETE.value),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_manifest(row)

    def query_archives(
        self,
        instrument_name: Optional[str] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> list[ArchiveManifest]:
        """Query archives matching criteria."""
        query = "SELECT * FROM archives WHERE 1=1"
        params: list[Any] = []

        if instrument_name is not None:
            query += " AND instrument_name = ?"
            params.append(instrument_name)
        if start_ms is not None:
            query += " AND end_ms > ?"
            params.append(start_ms)
        if end_ms is not None:
            query += " AND start_ms < ?"
            params.append(end_ms)

        query += " ORDER BY start_ms ASC, end_ms ASC"

        with self._connection() as conn:
            cur = conn.execute(query, params)
            return [self._row_to_manifest(row) for row in cur.fetchall()]

    def get_manifest_by_id(self, archive_id: str) -> Optional[ArchiveManifest]:
        with self._connection() as conn:
            cur = conn.execute("SELECT * FROM archives WHERE archive_id = ?", (archive_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_manifest(row)

    def find_exact_archive(
        self,
        instrument_name: str,
        start_ms: int,
        end_ms: int,
    ) -> Optional[ArchiveManifest]:
        with self._connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM archives
                WHERE instrument_name = ? AND start_ms = ? AND end_ms = ?
                LIMIT 1
                """,
                (instrument_name, start_ms, end_ms),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_manifest(row)

    def put_kv(self, key: str, value: bytes) -> None:
        now_ms = int(time.time() * 1000)
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO kv_store (key, value, updated_at_ms)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at_ms = excluded.updated_at_ms
                """,
                (key, value, now_ms),
            )

    def get_kv(self, key: str) -> Optional[bytes]:
        with self._connection() as conn:
            cur = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
            row = cur.fetchone()
            if not row:
                return None
            return bytes(row["value"])

    def query_kv(self, prefix: str) -> Iterator[Tuple[str, bytes]]:
        with self._connection() as conn:
            cur = conn.execute(
                "SELECT key, value FROM kv_store WHERE key LIKE ? ORDER BY key ASC",
                (f"{prefix}%",),
            )
            rows = cur.fetchall()
        for row in rows:
            yield (str(row["key"]), bytes(row["value"]))

    @staticmethod
    def _row_to_manifest(row: sqlite3.Row) -> ArchiveManifest:
        cursor_data = json.loads(row["resume_cursor"]) if row["resume_cursor"] else None
        source_params = json.loads(row["source_params"]) if row["source_params"] else {}
        return ArchiveManifest(
            archive_id=row["archive_id"],
            instrument_name=row["instrument_name"],
            start_ms=row["start_ms"],
            end_ms=row["end_ms"],
            source_url=row["source_url"] or "",
            source_params=source_params,
            capture_utc_ms=row["capture_utc_ms"],
            schema_version=row["schema_version"],
            record_count=row["record_count"],
            sha256=row["sha256"],
            raw_sha256=row["raw_sha256"],
            status=row["status"],
            coverage_ratio=row["coverage_ratio"],
            archive_relpath=row["archive_relpath"],
            resume_cursor=cursor_data,
        )
