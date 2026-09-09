"""Acceptance and unit tests for Task 5: Storage engine, index, cache, and manifests.

Covers:
- Kesintili yazım (interrupted write / temp file cleanup)
- Bozuk hash (tampered archive detected by checksum verification)
- Aynı dosya tekrar import (idempotent re-import)
- Conflicting content (hash collision / conflict detection)
- Eşzamanlı okuyucu (concurrent multithreaded readers)
- Path traversal rejection
- Streaming büyük fixture (large fixture streaming with generator)
- Kaynak fiyatları değişmeden roundtrip (exact Decimal precision preservation)
- Manifest incomplete iken complete cache hit verme
- Store protocol compliance (put, get, query, resume_state)
- Export / import bundle roundtrip for offline replay
"""

import concurrent.futures
import gzip
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core.contracts import Store, TradeTick
from storage import (
    ArchiveManifest,
    ArchiveStatus,
    SQLiteCatalogIndex,
    StorageChecksumError,
    StorageConflictError,
    StorageSecurityError,
    TradeArchiveReader,
    TradeArchiveWriter,
    TradeStorageEngine,
    validate_safe_path,
)


class StorageV1Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name)
        self.engine = TradeStorageEngine(self.root_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    # --------------------------------------------------------------------------
    # 1. Kaynak Fiyatları Değişmeden Roundtrip (Decimal Precision)
    # --------------------------------------------------------------------------

    def test_price_and_quantity_exact_decimal_roundtrip(self):
        """Verify TradeTick price and quantity retain exact Decimal precision through storage."""
        inst = "BTC-27DEC24-100000-C"
        precise_price = Decimal("0.000012345678")
        precise_qty = Decimal("12345.67890123")

        ticks = [
            TradeTick(
                instrument_name=inst,
                trade_id="T_DEC_01",
                trade_seq=1,
                timestamp_ms=1700000000000,
                price_btc=precise_price,
                quantity_contracts=precise_qty,
            )
        ]

        manifest = self.engine.store_trades(
            instrument_name=inst,
            start_ms=1700000000000,
            end_ms=1700000001000,
            trades=ticks,
        )
        self.assertEqual(manifest.record_count, 1)

        loaded_ticks = list(self.engine.stream_trades(instrument_name=inst))
        self.assertEqual(len(loaded_ticks), 1)
        self.assertEqual(loaded_ticks[0].price_btc, precise_price)
        self.assertEqual(loaded_ticks[0].quantity_contracts, precise_qty)
        self.assertIsInstance(loaded_ticks[0].price_btc, Decimal)
        self.assertIsInstance(loaded_ticks[0].quantity_contracts, Decimal)

    # --------------------------------------------------------------------------
    # 2. Manifest Incomplete İken Complete Cache Hit Verme
    # --------------------------------------------------------------------------

    def test_incomplete_manifest_never_returns_complete_cache_hit(self):
        """CRITICAL: Incomplete manifest must NEVER be returned as a complete cache hit."""
        inst = "BTC-27DEC24-90000-P"
        trades = [
            {"instrument_name": inst, "trade_id": "T1", "trade_seq": 1, "timestamp": 1000, "price": 0.05, "amount": 1}
        ]

        # 1. Store as INCOMPLETE
        self.engine.store_trades(
            instrument_name=inst,
            start_ms=1000,
            end_ms=5000,
            trades=trades,
            status=ArchiveStatus.INCOMPLETE.value,
        )

        # Cache check must return None
        cache_hit = self.engine.find_cache_hit(inst, 1000, 5000)
        self.assertIsNone(cache_hit, "Incomplete archive must not produce a complete cache hit")

        # 2. Re-store as COMPLETE (e.g. full download finished)
        complete_trades = [
            {"instrument_name": inst, "trade_id": "T1", "trade_seq": 1, "timestamp": 1000, "price": 0.05, "amount": 1},
            {"instrument_name": inst, "trade_id": "T2", "trade_seq": 2, "timestamp": 4000, "price": 0.06, "amount": 2},
        ]
        self.engine.store_trades(
            instrument_name=inst,
            start_ms=1000,
            end_ms=5000,
            trades=complete_trades,
            status=ArchiveStatus.COMPLETE.value,
        )

        # Cache check now succeeds!
        cache_hit_2 = self.engine.find_cache_hit(inst, 1000, 5000)
        self.assertIsNotNone(cache_hit_2)
        self.assertEqual(cache_hit_2.status, ArchiveStatus.COMPLETE.value)
        self.assertEqual(cache_hit_2.record_count, 2)

    # --------------------------------------------------------------------------
    # 3. Bozuk Hash (Tampered Checksum Detection)
    # --------------------------------------------------------------------------

    def test_tampered_archive_raises_storage_checksum_error(self):
        """Tampering with an archive file is detected by checksum verification."""
        inst = "BTC-TEST"
        trades = [{"instrument_name": inst, "trade_id": "T1", "trade_seq": 1, "timestamp": 1000, "price": 0.05, "amount": 1}]
        manifest = self.engine.store_trades(inst, 1000, 2000, trades)

        # Corrupt the file on disk
        archive_file = self.root_path / manifest.archive_relpath
        data = bytearray(archive_file.read_bytes())
        data[-1] ^= 0xFF  # flip bits in the last byte
        archive_file.write_bytes(data)

        # Streaming should detect checksum mismatch and raise StorageChecksumError
        with self.assertRaises(StorageChecksumError):
            list(self.engine.stream_trades(inst))

    # --------------------------------------------------------------------------
    # 4. Kesintili Yazım (Interrupted Write)
    # --------------------------------------------------------------------------

    def test_interrupted_write_leaves_no_corrupt_index_entry(self):
        """Simulate an interrupted write leaving a temp file; index remains uncorrupted."""
        temp_file = self.engine.archives_dir / "_tmp_orphan_crash.jsonl.gz"
        temp_file.write_bytes(b"partial corrupted gzip bytes")

        # Ensure index still queries cleanly
        archives = self.engine.index.query_archives()
        self.assertEqual(len(archives), 0)

        # Storing new trades succeeds normally without interference
        inst = "BTC-TEST"
        self.engine.store_trades(inst, 1000, 2000, [{"instrument_name": inst, "trade_seq": 1, "timestamp": 1000}])
        self.assertEqual(len(self.engine.index.query_archives()), 1)

    # --------------------------------------------------------------------------
    # 5. Aynı Dosya Tekrar Import & Conflicting Content
    # --------------------------------------------------------------------------

    def test_idempotent_re_store_and_conflicting_content_rejection(self):
        """Identical re-store succeeds; conflicting content for same range raises StorageConflictError."""
        inst = "BTC-27DEC24-100000-C"
        trades_1 = [{"instrument_name": inst, "trade_id": "T1", "trade_seq": 1, "timestamp": 1000, "price": 0.05, "amount": 1}]
        trades_conflict = [{"instrument_name": inst, "trade_id": "T2", "trade_seq": 2, "timestamp": 1000, "price": 0.99, "amount": 5}]

        # 1. First store
        m1 = self.engine.store_trades(inst, 1000, 2000, trades_1)

        # 2. Idempotent re-store with same trades
        m2 = self.engine.store_trades(inst, 1000, 2000, trades_1)
        self.assertEqual(m1.sha256, m2.sha256)

        # 3. Conflicting store for same instrument & time range
        with self.assertRaisesRegex(StorageConflictError, "Conflict: archive"):
            self.engine.store_trades(inst, 1000, 2000, trades_conflict)

    # --------------------------------------------------------------------------
    # 6. Eşzamanlı Okuyucu (Concurrent Readers)
    # --------------------------------------------------------------------------

    def test_concurrent_multithreaded_readers(self):
        """Multiple concurrent threads stream trades without deadlock or error."""
        inst = "BTC-CONCURRENT"
        trades = [
            {"instrument_name": inst, "trade_id": f"T{i}", "trade_seq": i, "timestamp": 1000 + i * 10, "price": 0.05, "amount": 1.0}
            for i in range(100)
        ]
        self.engine.store_trades(inst, 1000, 3000, trades)

        def reader_task():
            ticks = list(self.engine.stream_trades(inst))
            return len(ticks)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(reader_task) for _ in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        self.assertEqual(len(results), 10)
        self.assertTrue(all(r == 100 for r in results))

    # --------------------------------------------------------------------------
    # 7. Path Traversal Koruması
    # --------------------------------------------------------------------------

    def test_path_traversal_attempts_are_rejected(self):
        """Attempts to access paths with '..' or escape the root directory raise StorageSecurityError."""
        with self.assertRaises(StorageSecurityError):
            validate_safe_path("../../etc/passwd", self.root_path)

        with self.assertRaises(StorageSecurityError):
            validate_safe_path("archives/../../../secret.txt", self.root_path)

    # --------------------------------------------------------------------------
    # 8. Streaming Büyük Fixture (Generator Efficiency)
    # --------------------------------------------------------------------------

    def test_large_fixture_streaming_and_window_filtering(self):
        """Stream 5,000 trades efficiently with timestamp filtering."""
        inst = "BTC-BIG-FIXTURE"
        trades = [
            {"instrument_name": inst, "trade_id": f"T_{i}", "trade_seq": i, "timestamp": 1000 + i, "price": 0.05, "amount": 1.0}
            for i in range(5000)
        ]
        self.engine.store_trades(inst, 1000, 6000, trades)

        # Stream subset [2000, 3000)
        subset_generator = self.engine.stream_trades(inst, start_ms=2000, end_ms=3000)
        subset = list(subset_generator)

        self.assertEqual(len(subset), 1000)
        self.assertEqual(subset[0].timestamp_ms, 2000)
        self.assertEqual(subset[-1].timestamp_ms, 2999)

    # --------------------------------------------------------------------------
    # 9. Store Protocol Implementation
    # --------------------------------------------------------------------------

    def test_store_protocol_put_get_query_resume_state(self):
        """Verify TradeStorageEngine fully satisfies the Store protocol."""
        self.assertIsInstance(self.engine, Store)

        # put and get
        self.engine.put("cursor_1", b"state_bytes_123")
        self.assertEqual(self.engine.get("cursor_1"), b"state_bytes_123")
        self.assertIsNone(self.engine.get("non_existent"))

        # query
        self.engine.put("state/a", b"1")
        self.engine.put("state/b", b"2")
        self.engine.put("other/c", b"3")
        queried = list(self.engine.query("state/"))
        self.assertEqual(len(queried), 2)
        self.assertEqual(queried[0][0], "state/a")

        # resume_state
        self.engine.put("resume_cursor", json.dumps({"page": 5, "last_seq": 500}).encode("utf-8"))
        resumed = self.engine.resume_state()
        self.assertEqual(resumed, {"page": 5, "last_seq": 500})

    # --------------------------------------------------------------------------
    # 10. Export / Import Bundle Roundtrip
    # --------------------------------------------------------------------------

    def test_export_and_import_bundle_offline_replay(self):
        """Export archives to offline bundle and import into a fresh engine instance."""
        inst = "BTC-OFFLINE"
        trades = [
            {"instrument_name": inst, "trade_id": "T1", "trade_seq": 1, "timestamp": 1000, "price": 0.05, "amount": 1.0},
            {"instrument_name": inst, "trade_id": "T2", "trade_seq": 2, "timestamp": 2000, "price": 0.06, "amount": 2.0},
        ]
        orig_manifest = self.engine.store_trades(inst, 1000, 3000, trades)

        with tempfile.TemporaryDirectory() as export_dir:
            bundle_manifest = self.engine.export_bundle(export_dir)
            self.assertTrue(Path(bundle_manifest).exists())

            # Create fresh second engine
            with tempfile.TemporaryDirectory() as second_root:
                engine2 = TradeStorageEngine(second_root)
                imported_manifests = engine2.import_bundle(export_dir)

                self.assertEqual(len(imported_manifests), 1)
                self.assertEqual(imported_manifests[0].sha256, orig_manifest.sha256)
                self.assertEqual(imported_manifests[0].record_count, 2)

                # Verify streaming from second engine produces identical TradeTicks
                ticks = list(engine2.stream_trades(inst))
                self.assertEqual(len(ticks), 2)
                self.assertEqual(ticks[0].price_btc, Decimal("0.05"))
                self.assertEqual(ticks[1].price_btc, Decimal("0.06"))

    def test_repeat_import_same_bundle_is_idempotent(self):
        """Importing the same offline bundle twice keeps archive count and checksum stable."""
        inst = "BTC-REIMPORT"
        trades = [
            {"instrument_name": inst, "trade_id": "T1", "trade_seq": 1, "timestamp": 1000, "price": 0.05, "amount": 1.0}
        ]
        orig_manifest = self.engine.store_trades(inst, 1000, 2000, trades)

        with tempfile.TemporaryDirectory() as export_dir:
            self.engine.export_bundle(export_dir)
            with tempfile.TemporaryDirectory() as second_root:
                engine2 = TradeStorageEngine(second_root)
                first = engine2.import_bundle(export_dir)
                second = engine2.import_bundle(export_dir)

                self.assertEqual(len(first), 1)
                self.assertEqual(len(second), 1)
                self.assertEqual(engine2.index.query_archives()[0].sha256, orig_manifest.sha256)
                self.assertEqual(len(engine2.index.query_archives()), 1)

    def test_missing_archive_invalidates_cache_and_stream(self):
        """Index rows without archive files are not silent cache hits or empty streams."""
        inst = "BTC-MISSING-ARCHIVE"
        trades = [
            {"instrument_name": inst, "trade_id": "T1", "trade_seq": 1, "timestamp": 1000, "price": 0.05, "amount": 1.0}
        ]
        manifest = self.engine.store_trades(inst, 1000, 2000, trades)
        archive_file = self.root_path / manifest.archive_relpath
        archive_file.rename(archive_file.with_suffix(".missing"))

        with self.assertRaises(FileNotFoundError):
            self.engine.find_cache_hit(inst, 1000, 2000)
        with self.assertRaises(FileNotFoundError):
            list(self.engine.stream_trades(inst))
        with tempfile.TemporaryDirectory() as export_dir:
            with self.assertRaises(FileNotFoundError):
                self.engine.export_bundle(export_dir)

    def test_complete_upgrade_refreshes_metadata_and_archive_id(self):
        """INCOMPLETE -> COMPLETE updates coverage/source metadata consistently."""
        inst = "BTC-UPGRADE"
        partial = [
            {"instrument_name": inst, "trade_id": "T1", "trade_seq": 1, "timestamp": 1000, "price": 0.05, "amount": 1.0}
        ]
        complete = [
            {"instrument_name": inst, "trade_id": "T1", "trade_seq": 1, "timestamp": 1000, "price": 0.05, "amount": 1.0},
            {"instrument_name": inst, "trade_id": "T2", "trade_seq": 2, "timestamp": 1500, "price": 0.06, "amount": 1.0},
        ]

        self.engine.store_trades(
            inst,
            1000,
            2000,
            partial,
            status=ArchiveStatus.INCOMPLETE.value,
            coverage_ratio="0.2",
            source_url="partial",
        )
        upgraded = self.engine.store_trades(
            inst,
            1000,
            2000,
            complete,
            status=ArchiveStatus.COMPLETE.value,
            coverage_ratio="1.0",
            source_url="finished",
        )
        hit = self.engine.find_cache_hit(inst, 1000, 2000)

        self.assertIsNotNone(hit)
        self.assertEqual(hit.coverage_ratio, "1.0")
        self.assertEqual(hit.source_url, "finished")
        self.assertEqual(hit.archive_id, upgraded.archive_id)

    def test_complete_upgrade_rejects_changed_existing_trade_economics(self):
        """A COMPLETE upgrade cannot rewrite price/size of an existing trade identity."""
        inst = "BTC-UPGRADE-CONFLICT"
        partial = [
            {"instrument_name": inst, "trade_id": "T1", "trade_seq": 1, "timestamp": 1000, "price": 0.05, "amount": 1.0}
        ]
        changed = [
            {"instrument_name": inst, "trade_id": "T1", "trade_seq": 1, "timestamp": 1000, "price": 0.99, "amount": 1.0}
        ]

        self.engine.store_trades(inst, 1000, 2000, partial, status=ArchiveStatus.INCOMPLETE.value)
        with self.assertRaisesRegex(StorageConflictError, "economic trade content"):
            self.engine.store_trades(inst, 1000, 2000, changed, status=ArchiveStatus.COMPLETE.value)

    def test_complete_upgrade_rejects_dropped_existing_trade_identity(self):
        """A COMPLETE upgrade cannot silently drop trades already stored in the partial archive."""
        inst = "BTC-UPGRADE-DROP"
        partial = [
            {"instrument_name": inst, "trade_id": "T1", "trade_seq": 1, "timestamp": 1000, "price": 0.05, "amount": 1.0}
        ]
        complete_missing_old = [
            {"instrument_name": inst, "trade_id": "T2", "trade_seq": 2, "timestamp": 1500, "price": 0.06, "amount": 1.0}
        ]

        self.engine.store_trades(inst, 1000, 2000, partial, status=ArchiveStatus.INCOMPLETE.value)
        with self.assertRaisesRegex(StorageConflictError, "drops an existing trade identity"):
            self.engine.store_trades(inst, 1000, 2000, complete_missing_old, status=ArchiveStatus.COMPLETE.value)


if __name__ == "__main__":
    unittest.main()
