"""Comprehensive acceptance tests for lossless and resumable historical trade download.

Covers all Task 4 requirements:
  - Tek ms'de count üstü trade (Same millisecond count overflow & sequence continuation)
  - Reverse/unordered rows (Deterministic sorting by timestamp, trade_seq, trade_id)
  - Tekrar sayfa / Overlap deduplication
  - Kesilen bağlantı (Interrupted connection & recovery)
  - Retry exhaustion (Clean failure reporting)
  - Page-cap, max_bytes, max_seconds boundaries (INCOMPLETE manifests)
  - Boundary inclusive fixtures
  - Resume + dedup identical to uninterrupted reference
  - Sequence gap analysis without biased loss labeling
  - Store protocol implementations (InMemoryStore & JsonFileStore)
  - HistoricalTradeProvider & TradeTick conversion
  - Smoke check cap (azami 20 request)
"""

import json
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from core.contracts import HistoricalTradeProvider, Store, TradeTick
from ingestion.history_download import (
    CompletenessManifest,
    DeribitHistoricalTradeProvider,
    HistoryDownloadPlan,
    InMemoryStore,
    JsonFileStore,
    LosslessHistoryDownloader,
    analyze_sequence_gaps,
    deduplicate_trades,
    run_live_smoke_check,
    sort_trades,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class HistoryDownloadTests(unittest.TestCase):
    # --------------------------------------------------------------------------
    # 1. Single Millisecond Count Overflow & Sequence Continuation
    # --------------------------------------------------------------------------

    def test_single_millisecond_count_overflow_with_sequence_continuation(self):
        """When more trades than count occur in the same millisecond, sequence continuation bridges across."""
        # 8 trades at ts 1,000,000. Count=5.
        # Time page 1 returns 5 trades (seq 1..5) at ts 1,000,000 with has_more=True.
        time_page_1 = {
            "result": {
                "trades": [
                    {"instrument_name": "BTC-TEST", "trade_seq": i, "trade_id": f"T{i}", "timestamp": 1000000, "price": 0.05, "amount": 1.0}
                    for i in range(1, 6)
                ],
                "has_more": True,
            }
        }
        # Sequence continuation returns remaining trades (seq 6..8) at ts 1,000,000 + next trade at ts 1,001,000
        seq_page = {
            "result": {
                "trades": [
                    {"instrument_name": "BTC-TEST", "trade_seq": 6, "trade_id": "T6", "timestamp": 1000000, "price": 0.05, "amount": 1.0},
                    {"instrument_name": "BTC-TEST", "trade_seq": 7, "trade_id": "T7", "timestamp": 1000000, "price": 0.05, "amount": 1.0},
                    {"instrument_name": "BTC-TEST", "trade_seq": 8, "trade_id": "T8", "timestamp": 1000000, "price": 0.05, "amount": 1.0},
                    {"instrument_name": "BTC-TEST", "trade_seq": 9, "trade_id": "T9", "timestamp": 1001000, "price": 0.05, "amount": 1.0},
                ],
                "has_more": False,
            }
        }
        # Subsequent time page from 1,001,000 returns no more trades
        time_page_2 = {
            "result": {
                "trades": [
                    {"instrument_name": "BTC-TEST", "trade_seq": 9, "trade_id": "T9", "timestamp": 1001000, "price": 0.05, "amount": 1.0},
                ],
                "has_more": False,
            }
        }

        plan = HistoryDownloadPlan(
            instrument_name="BTC-TEST",
            start_timestamp=1000000,
            end_timestamp=1002000,
            count=5,
            enable_sequence_continuation=True,
        )
        downloader = LosslessHistoryDownloader(plan)

        with patch("ingestion.deribit_history.urlopen", side_effect=[
            FakeResponse(time_page_1),
            FakeResponse(seq_page),
            FakeResponse(time_page_2),
        ]):
            trades, manifest = downloader.download()

        self.assertTrue(manifest.is_complete)
        self.assertEqual(manifest.status, "COMPLETE")
        self.assertEqual(len(trades), 9)
        self.assertEqual([t["trade_seq"] for t in trades], list(range(1, 10)))

    def test_single_millisecond_overflow_without_sequence_continuation_marks_incomplete(self):
        """When sequence continuation is disabled and same-ms overflows, manifest marks INCOMPLETE."""
        time_page = {
            "result": {
                "trades": [
                    {"instrument_name": "BTC-TEST", "trade_seq": i, "trade_id": f"T{i}", "timestamp": 1000000, "price": 0.05, "amount": 1.0}
                    for i in range(1, 6)
                ],
                "has_more": True,
            }
        }

        plan = HistoryDownloadPlan(
            instrument_name="BTC-TEST",
            start_timestamp=1000000,
            end_timestamp=1002000,
            count=5,
            enable_sequence_continuation=False,
        )
        downloader = LosslessHistoryDownloader(plan)

        with patch("ingestion.deribit_history.urlopen", return_value=FakeResponse(time_page)):
            trades, manifest = downloader.download()

        self.assertFalse(manifest.is_complete)
        self.assertEqual(manifest.status, "INCOMPLETE")
        self.assertEqual(manifest.reason_code, "same_millisecond_overflow")
        self.assertEqual(len(trades), 5)

    # --------------------------------------------------------------------------
    # 2. Reverse and Unordered Rows
    # --------------------------------------------------------------------------

    def test_reverse_and_unordered_rows_are_stably_sorted(self):
        """Incoming trades in arbitrary or reversed order are sorted deterministically."""
        scrambled = [
            {"instrument_name": "BTC-TEST", "trade_seq": 5, "trade_id": "E", "timestamp": 3000},
            {"instrument_name": "BTC-TEST", "trade_seq": 1, "trade_id": "A", "timestamp": 1000},
            {"instrument_name": "BTC-TEST", "trade_seq": 4, "trade_id": "D", "timestamp": 2500},
            {"instrument_name": "BTC-TEST", "trade_seq": 2, "trade_id": "B", "timestamp": 1000},
            {"instrument_name": "BTC-TEST", "trade_seq": 3, "trade_id": "C", "timestamp": 2000},
        ]
        sorted_res = sort_trades(scrambled)
        expected_seqs = [1, 2, 3, 4, 5]
        self.assertEqual([t["trade_seq"] for t in sorted_res], expected_seqs)

    # --------------------------------------------------------------------------
    # 3. Repeated Page / Overlap Deduplication
    # --------------------------------------------------------------------------

    def test_repeated_page_boundary_inclusive_and_overlap_deduplication(self):
        """Boundary inclusive time queries repeat trades on the boundary; dedup removes them perfectly."""
        # Page 1 ends at ts 2000 with trades 1, 2, 3
        page_1 = {
            "result": {
                "trades": [
                    {"instrument_name": "BTC-TEST", "trade_seq": 1, "trade_id": "T1", "timestamp": 1000},
                    {"instrument_name": "BTC-TEST", "trade_seq": 2, "trade_id": "T2", "timestamp": 2000},
                    {"instrument_name": "BTC-TEST", "trade_seq": 3, "trade_id": "T3", "timestamp": 2000},
                ],
                "has_more": True,
            }
        }
        # Page 2 starts at ts 2000 (inclusive) and returns trades 2, 3, 4
        page_2 = {
            "result": {
                "trades": [
                    {"instrument_name": "BTC-TEST", "trade_seq": 2, "trade_id": "T2", "timestamp": 2000},
                    {"instrument_name": "BTC-TEST", "trade_seq": 3, "trade_id": "T3", "timestamp": 2000},
                    {"instrument_name": "BTC-TEST", "trade_seq": 4, "trade_id": "T4", "timestamp": 3000},
                ],
                "has_more": False,
            }
        }

        plan = HistoryDownloadPlan("BTC-TEST", 1000, 4000, count=3)
        downloader = LosslessHistoryDownloader(plan)

        with patch("ingestion.deribit_history.urlopen", side_effect=[FakeResponse(page_1), FakeResponse(page_2)]):
            trades, manifest = downloader.download()

        self.assertTrue(manifest.is_complete)
        self.assertEqual(len(trades), 4)
        self.assertEqual([t["trade_seq"] for t in trades], [1, 2, 3, 4])
        self.assertEqual(manifest.unique_trades_count, 4)

    # --------------------------------------------------------------------------
    # 4. Broken Connection & Recovery
    # --------------------------------------------------------------------------

    def test_broken_connection_retries_and_recovers(self):
        """Interrupted connection retries up to max_retries and recovers seamlessly."""
        success_payload = {
            "result": {
                "trades": [{"instrument_name": "BTC-TEST", "trade_seq": 1, "trade_id": "T1", "timestamp": 1000}],
                "has_more": False,
            }
        }
        err_1 = URLError("Connection reset by peer")
        err_2 = TimeoutError("Read timed out")
        success = FakeResponse(success_payload)

        plan = HistoryDownloadPlan("BTC-TEST", 1000, 2000, max_retries=5)
        downloader = LosslessHistoryDownloader(plan)

        with patch("ingestion.deribit_history.urlopen", side_effect=[err_1, err_2, success]):
            trades, manifest = downloader.download(sleeper=lambda s: None)

        self.assertTrue(manifest.is_complete)
        self.assertEqual(manifest.status, "COMPLETE")
        self.assertEqual(len(trades), 1)

    # --------------------------------------------------------------------------
    # 5. Retry Exhaustion
    # --------------------------------------------------------------------------

    def test_retry_exhaustion_marks_failure_in_manifest(self):
        """When retries are exhausted, manifest documents FAILED status and error reason."""
        plan = HistoryDownloadPlan("BTC-TEST", 1000, 2000, max_retries=3)
        downloader = LosslessHistoryDownloader(plan)

        with patch("ingestion.deribit_history.urlopen", side_effect=URLError("Network down")):
            trades, manifest = downloader.download(sleeper=lambda s: None)

        self.assertFalse(manifest.is_complete)
        self.assertEqual(manifest.status, "FAILED")
        self.assertIn("fetch_error", manifest.reason_code)

    # --------------------------------------------------------------------------
    # 6. Budget Limits (Page-Cap, Max-Bytes, Max-Seconds)
    # --------------------------------------------------------------------------

    def test_page_cap_budget_marks_incomplete(self):
        """Downloader stops when max_pages is reached, marking INCOMPLETE."""
        page = {
            "result": {
                "trades": [{"instrument_name": "BTC-TEST", "trade_seq": 1, "trade_id": "T1", "timestamp": 1000}],
                "has_more": True,
            }
        }
        plan = HistoryDownloadPlan("BTC-TEST", 1000, 5000, max_pages=2)
        downloader = LosslessHistoryDownloader(plan)

        with patch("ingestion.deribit_history.urlopen", return_value=FakeResponse(page)):
            trades, manifest = downloader.download()

        self.assertFalse(manifest.is_complete)
        self.assertEqual(manifest.status, "INCOMPLETE")
        self.assertEqual(manifest.reason_code, "page_cap_reached")
        self.assertEqual(manifest.pages_fetched, 2)

    def test_max_bytes_budget_marks_incomplete(self):
        """Downloader stops when max_bytes limit is reached, marking INCOMPLETE."""
        page = {
            "result": {
                "trades": [{"instrument_name": "BTC-TEST", "trade_seq": 1, "trade_id": "T1", "timestamp": 1000}],
                "has_more": True,
            }
        }
        # Each FakeResponse serialized is ~120 bytes; max_bytes=80 will stop after 1 page
        plan = HistoryDownloadPlan("BTC-TEST", 1000, 5000, max_pages=10, max_bytes=80)
        downloader = LosslessHistoryDownloader(plan)

        with patch("ingestion.deribit_history.urlopen", return_value=FakeResponse(page)):
            trades, manifest = downloader.download()

        self.assertFalse(manifest.is_complete)
        self.assertEqual(manifest.status, "INCOMPLETE")
        self.assertEqual(manifest.reason_code, "byte_cap_reached")
        self.assertEqual(manifest.pages_fetched, 1)

    def test_max_seconds_budget_marks_incomplete(self):
        """Downloader stops when wall-clock duration exceeds max_seconds."""
        page = {
            "result": {
                "trades": [{"instrument_name": "BTC-TEST", "trade_seq": 1, "trade_id": "T1", "timestamp": 1000}],
                "has_more": True,
            }
        }
        plan = HistoryDownloadPlan("BTC-TEST", 1000, 5000, max_pages=10, max_seconds=0.001)
        downloader = LosslessHistoryDownloader(plan)

        def slow_urlopen(*args, **kwargs):
            time.sleep(0.005)
            return FakeResponse(page)

        with patch("ingestion.deribit_history.urlopen", side_effect=slow_urlopen):
            trades, manifest = downloader.download()

        self.assertFalse(manifest.is_complete)
        self.assertEqual(manifest.status, "INCOMPLETE")
        self.assertEqual(manifest.reason_code, "time_budget_exceeded")

    # --------------------------------------------------------------------------
    # 7. Boundary Inclusive Fixtures
    # --------------------------------------------------------------------------

    def test_half_open_boundary_fixtures(self):
        """Trades on exact start_timestamp are captured; exact end_timestamp is excluded."""
        page = {
            "result": {
                "trades": [
                    {"instrument_name": "BTC-TEST", "trade_seq": 1, "trade_id": "T1", "timestamp": 1000},
                    {"instrument_name": "BTC-TEST", "trade_seq": 2, "trade_id": "T2", "timestamp": 5000},
                ],
                "has_more": False,
            }
        }
        plan = HistoryDownloadPlan("BTC-TEST", 1000, 5000)
        downloader = LosslessHistoryDownloader(plan)

        with patch("ingestion.deribit_history.urlopen", return_value=FakeResponse(page)):
            trades, manifest = downloader.download()

        self.assertEqual(trades[0]["timestamp"], 1000)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[-1]["timestamp"], 1000)
        self.assertEqual(manifest.actual_start_ms, 1000)
        self.assertEqual(manifest.actual_end_ms, 1000)

    def test_economic_hash_changes_when_price_changes(self):
        """trades_sha256 includes economic fields, not only trade identity."""
        page_low = {
            "result": {
                "trades": [
                    {
                        "instrument_name": "BTC-TEST",
                        "trade_seq": 1,
                        "trade_id": "T1",
                        "timestamp": 1000,
                        "price": "0.01",
                        "amount": "1.0",
                    }
                ],
                "has_more": False,
            }
        }
        page_high = {
            "result": {
                "trades": [
                    {
                        "instrument_name": "BTC-TEST",
                        "trade_seq": 1,
                        "trade_id": "T1",
                        "timestamp": 1000,
                        "price": "0.99",
                        "amount": "1.0",
                    }
                ],
                "has_more": False,
            }
        }
        plan = HistoryDownloadPlan("BTC-TEST", 1000, 2000)

        with patch("ingestion.deribit_history.urlopen", return_value=FakeResponse(page_low)):
            _, manifest_low = LosslessHistoryDownloader(plan).download()
        with patch("ingestion.deribit_history.urlopen", return_value=FakeResponse(page_high)):
            _, manifest_high = LosslessHistoryDownloader(plan).download()

        self.assertNotEqual(manifest_low.trades_sha256, manifest_high.trades_sha256)

    def test_empty_page_with_has_more_is_incomplete(self):
        """Contradictory empty page with has_more=True is not a complete empty range."""
        page = {"result": {"trades": [], "has_more": True}}
        plan = HistoryDownloadPlan("BTC-TEST", 1000, 2000)

        with patch("ingestion.deribit_history.urlopen", return_value=FakeResponse(page)):
            trades, manifest = LosslessHistoryDownloader(plan).download()

        self.assertEqual(trades, [])
        self.assertFalse(manifest.is_complete)
        self.assertEqual(manifest.status, "INCOMPLETE")
        self.assertEqual(manifest.reason_code, "malformed_empty_page_with_has_more")

    def test_resume_cursor_must_match_plan_identity(self):
        """Resume state from a different range/instrument is rejected instead of restored."""
        store = InMemoryStore()
        old_plan = HistoryDownloadPlan("BTC-TEST", 1000, 2000)
        old_cursor = {
            "schema_version": "1.0",
            "plan_key": old_plan.resume_plan_key(),
            "instrument_name": "BTC-TEST",
            "requested_start_ms": 1000,
            "requested_end_ms": 2000,
            "count": old_plan.count,
            "sorting": old_plan.sorting,
            "base_url": old_plan.base_url,
            "pages_fetched": 1,
            "total_bytes": 100,
            "next_start_timestamp": 1500,
            "next_end_timestamp": 2000,
        }
        store.put("resume_cursor", json.dumps(old_cursor).encode("utf-8"))
        new_plan = HistoryDownloadPlan("BTC-TEST", 4000, 5000)

        with self.assertRaisesRegex(ValueError, "resume cursor does not match"):
            LosslessHistoryDownloader(new_plan).download(store=store, resume=True)

    def test_sequence_continuation_shares_budget_and_filters_time_range(self):
        """Sequence continuation counts toward page/byte budgets and filters [start,end)."""
        time_page = {
            "result": {
                "trades": [
                    {
                        "instrument_name": "BTC-TEST",
                        "trade_seq": 1,
                        "trade_id": "T1",
                        "timestamp": 1000,
                        "price": "0.05",
                        "amount": "1.0",
                    }
                ],
                "has_more": True,
            }
        }
        seq_page = {
            "result": {
                "trades": [
                    {
                        "instrument_name": "BTC-TEST",
                        "trade_seq": 2,
                        "trade_id": "T2",
                        "timestamp": 3000,
                        "price": "0.06",
                        "amount": "1.0",
                    }
                ],
                "has_more": False,
            }
        }
        plan = HistoryDownloadPlan("BTC-TEST", 1000, 2000, count=1, max_pages=2)

        with patch("ingestion.deribit_history.urlopen", side_effect=[FakeResponse(time_page), FakeResponse(seq_page)]):
            trades, manifest = LosslessHistoryDownloader(plan).download()

        self.assertTrue(manifest.is_complete)
        self.assertEqual([t["timestamp"] for t in trades], [1000])
        self.assertEqual(manifest.pages_fetched, 2)
        self.assertGreater(manifest.total_bytes_fetched, 0)

    def test_sequence_continuation_persists_in_range_records(self):
        """Sequence continuation pages are persisted and can be resumed from the same plan namespace."""
        time_page = {
            "result": {
                "trades": [
                    {"instrument_name": "BTC-TEST", "trade_seq": 1, "trade_id": "T1", "timestamp": 1000}
                ],
                "has_more": True,
            }
        }
        seq_page = {
            "result": {
                "trades": [
                    {"instrument_name": "BTC-TEST", "trade_seq": 2, "trade_id": "T2", "timestamp": 1000},
                    {"instrument_name": "BTC-TEST", "trade_seq": 3, "trade_id": "T3", "timestamp": 1500},
                ],
                "has_more": False,
            }
        }
        plan = HistoryDownloadPlan("BTC-TEST", 1000, 2000, count=1, max_pages=3)
        store = InMemoryStore()

        with patch("ingestion.deribit_history.urlopen", side_effect=[FakeResponse(time_page), FakeResponse(seq_page)]):
            trades, manifest = LosslessHistoryDownloader(plan).download(store=store)

        self.assertTrue(manifest.is_complete)
        self.assertEqual([t["trade_seq"] for t in trades], [1, 2, 3])
        stored_pages = list(store.query(plan.page_key_prefix()))
        self.assertEqual(len(stored_pages), 2)

    def test_sequence_continuation_does_not_trust_single_has_more_false_page(self):
        """A same-ms sequence page with has_more=False is not proof that overflow is exhausted."""
        time_page = {
            "result": {
                "trades": [
                    {"instrument_name": "BTC-TEST", "trade_seq": 1, "trade_id": "T1", "timestamp": 1000}
                ],
                "has_more": True,
            }
        }
        seq_page_same_ms = {
            "result": {
                "trades": [
                    {"instrument_name": "BTC-TEST", "trade_seq": 2, "trade_id": "T2", "timestamp": 1000}
                ],
                "has_more": False,
            }
        }
        seq_page_advance = {
            "result": {
                "trades": [
                    {"instrument_name": "BTC-TEST", "trade_seq": 3, "trade_id": "T3", "timestamp": 1500}
                ],
                "has_more": False,
            }
        }
        plan = HistoryDownloadPlan("BTC-TEST", 1000, 2000, count=1, max_pages=4)

        with patch(
            "ingestion.deribit_history.urlopen",
            side_effect=[FakeResponse(time_page), FakeResponse(seq_page_same_ms), FakeResponse(seq_page_advance)],
        ):
            trades, manifest = LosslessHistoryDownloader(plan).download()

        self.assertTrue(manifest.is_complete)
        self.assertEqual([t["trade_seq"] for t in trades], [1, 2, 3])
        self.assertEqual(manifest.pages_fetched, 3)

    def test_max_retries_hard_cap(self):
        with self.assertRaisesRegex(ValueError, "max_retries must be <= 5"):
            HistoryDownloadPlan("BTC-TEST", 1000, 2000, max_retries=6).validate()

    # --------------------------------------------------------------------------
    # 8. Resume + Dedup Equals Uninterrupted Reference
    # --------------------------------------------------------------------------

    def test_resume_plus_dedup_produces_identical_result_to_uninterrupted_reference(self):
        """Interrupted run resumed from Store produces identical result to uninterrupted run."""
        pages = [
            {
                "result": {
                    "trades": [
                        {"instrument_name": "BTC-TEST", "trade_seq": 1, "trade_id": "T1", "timestamp": 1000},
                        {"instrument_name": "BTC-TEST", "trade_seq": 2, "trade_id": "T2", "timestamp": 2000},
                    ],
                    "has_more": True,
                }
            },
            {
                "result": {
                    "trades": [
                        {"instrument_name": "BTC-TEST", "trade_seq": 2, "trade_id": "T2", "timestamp": 2000},
                        {"instrument_name": "BTC-TEST", "trade_seq": 3, "trade_id": "T3", "timestamp": 3000},
                    ],
                    "has_more": True,
                }
            },
            {
                "result": {
                    "trades": [
                        {"instrument_name": "BTC-TEST", "trade_seq": 3, "trade_id": "T3", "timestamp": 3000},
                        {"instrument_name": "BTC-TEST", "trade_seq": 4, "trade_id": "T4", "timestamp": 4000},
                    ],
                    "has_more": False,
                }
            },
        ]

        # 1. Uninterrupted run
        plan_ref = HistoryDownloadPlan("BTC-TEST", 1000, 4000, count=2, max_pages=10)
        ref_downloader = LosslessHistoryDownloader(plan_ref)
        with patch("ingestion.deribit_history.urlopen", side_effect=[
            FakeResponse(pages[0]),
            FakeResponse(pages[1]),
            FakeResponse(pages[2]),
        ]):
            ref_trades, ref_manifest = ref_downloader.download()

        # 2. Resumed run (batch 1 fetches page 1, batch 2 resumes to fetch pages 2 and 3)
        store = InMemoryStore()
        plan_part1 = HistoryDownloadPlan("BTC-TEST", 1000, 4000, count=2, max_pages=1)
        part1_downloader = LosslessHistoryDownloader(plan_part1)
        with patch("ingestion.deribit_history.urlopen", side_effect=[FakeResponse(pages[0])]):
            part1_trades, part1_manifest = part1_downloader.download(store=store)

        self.assertFalse(part1_manifest.is_complete)
        self.assertIsNotNone(store.get("resume_cursor"))

        plan_part2 = HistoryDownloadPlan("BTC-TEST", 1000, 4000, count=2, max_pages=10)
        part2_downloader = LosslessHistoryDownloader(plan_part2)
        with patch("ingestion.deribit_history.urlopen", side_effect=[
            FakeResponse(pages[1]),
            FakeResponse(pages[2]),
        ]):
            resumed_trades, resumed_manifest = part2_downloader.download(store=store, resume=True)

        self.assertTrue(resumed_manifest.is_complete)
        self.assertEqual(len(ref_trades), len(resumed_trades))
        self.assertEqual([t["trade_seq"] for t in ref_trades], [t["trade_seq"] for t in resumed_trades])
        self.assertEqual(ref_manifest.trades_sha256, resumed_manifest.trades_sha256)

    # --------------------------------------------------------------------------
    # 9. Sequence Gap Analysis
    # --------------------------------------------------------------------------

    def test_sequence_gap_analysis_tracks_diagnostics_without_false_accusation(self):
        """Sequence gap analysis identifies missing numbers without blindly declaring data loss."""
        trades = [
            {"trade_seq": 10},
            {"trade_seq": 11},
            {"trade_seq": 15},
            {"trade_seq": 16},
            {"trade_seq": 20},
        ]
        res = analyze_sequence_gaps(trades)
        self.assertEqual(res["min_seq"], 10)
        self.assertEqual(res["max_seq"], 20)
        self.assertEqual(res["unique_seq_count"], 5)
        self.assertEqual(res["gap_count"], 6)
        self.assertEqual(res["gaps"], [(12, 14), (17, 19)])
        self.assertFalse(res["is_contiguous"])
        self.assertIn("gaps_detected", res["interpretation"])
        self.assertIn("exchange-level counter progression", res["interpretation"])

    # --------------------------------------------------------------------------
    # 10. Store Protocol Implementations
    # --------------------------------------------------------------------------

    def test_store_implementations_in_memory_and_json_file(self):
        """Both InMemoryStore and JsonFileStore satisfy the Store protocol."""
        stores = [InMemoryStore()]
        with tempfile.TemporaryDirectory() as tmp:
            stores.append(JsonFileStore(tmp))
            for s in stores:
                self.assertIsInstance(s, Store)
                s.put("test_key", b"hello world")
                self.assertEqual(s.get("test_key"), b"hello world")
                self.assertIsNone(s.get("non_existent"))

                # Query
                s.put("item_1", b"val1")
                s.put("item_2", b"val2")
                queried = list(s.query("item_"))
                self.assertEqual(len(queried), 2)

                # Resume state
                s.put("resume_cursor", json.dumps({"cursor": 123}).encode("utf-8"))
                self.assertEqual(s.resume_state(), {"cursor": 123})

    # --------------------------------------------------------------------------
    # 11. HistoricalTradeProvider & TradeTick Conversion
    # --------------------------------------------------------------------------

    def test_historical_trade_provider_protocol_and_tradetick_conversion(self):
        """DeribitHistoricalTradeProvider converts raw trades to immutable TradeTick contracts."""
        provider = DeribitHistoricalTradeProvider()
        self.assertIsInstance(provider, HistoricalTradeProvider)

        raw_trades = [
            {
                "instrument_name": "BTC-27DEC24-100000-C",
                "trade_id": "T101",
                "trade_seq": 101,
                "timestamp": 1700000000000,
                "price": 0.045,
                "amount": 2.5,
            }
        ]
        ticks = provider.to_trade_ticks(raw_trades)
        self.assertEqual(len(ticks), 1)
        tick = ticks[0]
        self.assertIsInstance(tick, TradeTick)
        self.assertEqual(tick.instrument_name, "BTC-27DEC24-100000-C")
        self.assertEqual(tick.price_btc, Decimal("0.045"))
        self.assertEqual(tick.quantity_contracts, Decimal("2.5"))

    # --------------------------------------------------------------------------
    # 12. Smoke Check Cap (Azami 20 Requests)
    # --------------------------------------------------------------------------

    def test_smoke_check_capped_at_max_20_requests(self):
        """run_live_smoke_check caps requests at max 20 even when called with higher value."""
        empty_page = {"result": {"trades": [], "has_more": False}}
        with patch("ingestion.deribit_history.urlopen", return_value=FakeResponse(empty_page)):
            res = run_live_smoke_check("BTC-TEST", 1000, 2000, max_requests=100)

        self.assertEqual(res["capped_max_requests"], 20)


if __name__ == "__main__":
    unittest.main()
