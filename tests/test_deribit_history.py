import io
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from ingestion.deribit_history import (
    DEFAULT_BASE_URL,
    DEFAULT_COUNT,
    MAX_COUNT,
    DeribitHistoryError,
    TradeDownloadPlan,
    build_sequence_plan,
    build_time_plan,
    fetch_deribit_json,
    fetch_trade_pages,
    planned_requests,
    summarize_download,
)


class FakeResponse:
    def __init__(self, payload, byte_content: bytes | None = None):
        self.payload = payload
        self.byte_content = byte_content

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        if self.byte_content is not None:
            return self.byte_content
        return json.dumps(self.payload).encode("utf-8")


class DeribitHistoryTests(unittest.TestCase):
    def test_history_collector_uses_dedicated_history_host_by_default(self):
        self.assertEqual(DEFAULT_BASE_URL, "https://history.deribit.com/api/v2/public")
        self.assertNotIn("www.deribit.com", DEFAULT_BASE_URL)

    def test_sequence_plan_defaults_to_count_1000_and_is_bounded(self):
        plan = build_sequence_plan("BTC-27JUN25-100000-C", 1, 2500, max_pages=2)

        self.assertEqual(plan.count, DEFAULT_COUNT)
        self.assertEqual(plan.count, MAX_COUNT)
        self.assertEqual(
            planned_requests(plan),
            [
                {
                    "instrument_name": "BTC-27JUN25-100000-C",
                    "start_seq": 1,
                    "end_seq": 1000,
                    "count": 1000,
                },
                {
                    "instrument_name": "BTC-27JUN25-100000-C",
                    "start_seq": 1001,
                    "end_seq": 2000,
                    "count": 1000,
                },
            ],
        )

    def test_time_plan_emits_single_initial_window_for_dry_run(self):
        plan = build_time_plan("BTC-27JUN25-100000-C", 1_700_000_000_000, 1_700_000_060_000)

        self.assertEqual(
            planned_requests(plan),
            [
                {
                    "instrument_name": "BTC-27JUN25-100000-C",
                    "start_timestamp": 1_700_000_000_000,
                    "end_timestamp": 1_700_000_060_000,
                    "count": 1000,
                }
            ],
        )

    def test_sequence_fetch_summarizes_exact_coverage(self):
        plan = build_sequence_plan("BTC-TEST", 1, 3, count=3)
        payload = {
            "result": {
                "trades": [
                    {"instrument_name": "BTC-TEST", "trade_seq": 3},
                    {"instrument_name": "BTC-TEST", "trade_seq": 1},
                    {"instrument_name": "BTC-TEST", "trade_seq": 2},
                ],
                "has_more": False,
            }
        }

        with patch("ingestion.deribit_history.urlopen", return_value=FakeResponse(payload)) as mocked:
            pages = fetch_trade_pages(plan)

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].trade_count, 3)
        self.assertIn("count=3", mocked.call_args.args[0].full_url)
        self.assertTrue(summarize_download(plan, pages)["sequence_range_complete"])

    def test_time_fetch_pages_toward_older_trades(self):
        plan = build_time_plan("BTC-TEST", 1000, 5000, count=2, max_pages=2)
        payloads = [
            FakeResponse(
                {
                    "result": {
                        "trades": [
                            {"instrument_name": "BTC-TEST", "trade_seq": 3, "timestamp": 5000},
                            {"instrument_name": "BTC-TEST", "trade_seq": 2, "timestamp": 3000},
                        ],
                        "has_more": True,
                    }
                }
            ),
            FakeResponse(
                {
                    "result": {
                        "trades": [
                            {"instrument_name": "BTC-TEST", "trade_seq": 1, "timestamp": 1000},
                        ],
                        "has_more": False,
                    }
                }
            ),
        ]

        with patch("ingestion.deribit_history.urlopen", side_effect=payloads) as mocked:
            pages = fetch_trade_pages(plan)

        self.assertEqual(len(pages), 2)
        self.assertIn("end_timestamp=2999", mocked.call_args_list[1].args[0].full_url)
        self.assertFalse(summarize_download(plan, pages)["stopped_with_more_available"])

    def test_api_error_is_reported(self):
        plan = build_sequence_plan("BTC-BAD", 1, 1)
        payload = {"error": {"code": 10000, "message": "bad request"}}

        with patch("ingestion.deribit_history.urlopen", return_value=FakeResponse(payload)):
            with self.assertRaises(DeribitHistoryError):
                fetch_trade_pages(plan)

    def test_requires_exactly_one_range_mode(self):
        with self.assertRaises(ValueError):
            build_sequence_plan("BTC-TEST", 3, 1)

    def test_rejects_count_above_official_limit(self):
        with self.assertRaisesRegex(ValueError, "count must be <= 1000"):
            build_sequence_plan("BTC-TEST", 1, 1001, count=1001)

    # --------------------------------------------------------------------------
    # Additional Resiliency, Timeout, 429, and Budget Tests
    # --------------------------------------------------------------------------

    def test_http_429_retry_after_respected(self):
        """HTTP 429 with Retry-After header backs off and succeeds on retry."""
        err_429 = HTTPError(
            url="http://test",
            code=429,
            msg="Too Many Requests",
            hdrs={"Retry-After": "3.5"},
            fp=None,
        )
        success = FakeResponse({"result": {"trades": [{"trade_seq": 1}], "has_more": False}})
        sleep_calls = []

        try:
            with patch("ingestion.deribit_history.urlopen", side_effect=[err_429, success]):
                result, byte_size = fetch_deribit_json(
                    DEFAULT_BASE_URL,
                    "/get_last_trades_by_instrument",
                    {"instrument_name": "BTC-TEST"},
                    max_retries=3,
                    sleeper=lambda s: sleep_calls.append(s),
                )
        finally:
            err_429.close()

        self.assertEqual(len(sleep_calls), 1)
        self.assertEqual(sleep_calls[0], 3.5)
        self.assertEqual(len(result["trades"]), 1)

    def test_retry_exhaustion_raises_deribit_history_error(self):
        """When retries are exhausted after max_retries attempts, raises DeribitHistoryError."""
        err = URLError("Temporary connection failure")
        sleep_calls = []

        with patch("ingestion.deribit_history.urlopen", side_effect=err):
            with self.assertRaisesRegex(DeribitHistoryError, "after 3 retries"):
                fetch_deribit_json(
                    DEFAULT_BASE_URL,
                    "/get_last_trades_by_instrument",
                    {"instrument_name": "BTC-TEST"},
                    max_retries=3,
                    sleeper=lambda s: sleep_calls.append(s),
                )

        self.assertEqual(len(sleep_calls), 2)  # Slept before attempt 2 and attempt 3

    def test_server_error_500_retries_with_exponential_backoff(self):
        """HTTP 500 server error retries with exponential backoff and succeeds."""
        err_500_1 = HTTPError(url="http://test", code=500, msg="Internal Server Error", hdrs={}, fp=None)
        err_500_2 = HTTPError(url="http://test", code=500, msg="Internal Server Error", hdrs={}, fp=None)
        success = FakeResponse({"result": {"trades": [{"trade_seq": 42}], "has_more": False}})
        sleep_calls = []

        try:
            with patch("ingestion.deribit_history.urlopen", side_effect=[err_500_1, err_500_2, success]):
                result, _ = fetch_deribit_json(
                    DEFAULT_BASE_URL,
                    "/get_last_trades_by_instrument",
                    {"instrument_name": "BTC-TEST"},
                    max_retries=5,
                    backoff_factor=1.0,
                    sleeper=lambda s: sleep_calls.append(s),
                )
        finally:
            err_500_1.close()
            err_500_2.close()

        self.assertEqual(len(sleep_calls), 2)
        self.assertEqual(sleep_calls[0], 1.0)  # 1.0 * (2^0)
        self.assertEqual(sleep_calls[1], 2.0)  # 1.0 * (2^1)
        self.assertEqual(result["trades"][0]["trade_seq"], 42)

    def test_http_400_is_non_retryable(self):
        """HTTP 400 Bad Request immediately fails without wasting retries."""
        err_400 = HTTPError(
            url="http://test",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=None,
        )
        sleep_calls = []

        try:
            with patch("ingestion.deribit_history.urlopen", side_effect=err_400):
                with self.assertRaisesRegex(DeribitHistoryError, "HTTP 400"):
                    fetch_deribit_json(
                        DEFAULT_BASE_URL,
                        "/get_last_trades_by_instrument",
                        {"instrument_name": "BTC-TEST"},
                        max_retries=5,
                        sleeper=lambda s: sleep_calls.append(s),
                    )
        finally:
            err_400.close()

        self.assertEqual(len(sleep_calls), 0)  # Zero retries attempted

    def test_budget_cap_max_bytes(self):
        """fetch_trade_pages respects max_bytes limit."""
        plan = build_sequence_plan("BTC-TEST", 1, 100, count=10, max_pages=10, max_bytes=50)
        # Each page response has byte size ~ 80 bytes
        payload = {"result": {"trades": [{"trade_seq": i} for i in range(10)], "has_more": True}}

        with patch("ingestion.deribit_history.urlopen", return_value=FakeResponse(payload)):
            pages = fetch_trade_pages(plan)

        # After page 1, total_bytes >= 50, so page 2 is not fetched!
        self.assertEqual(len(pages), 1)

    def test_budget_cap_max_seconds(self):
        """fetch_trade_pages respects max_seconds limit."""
        plan = build_sequence_plan("BTC-TEST", 1, 100, count=10, max_pages=10, max_seconds=0.001)
        payload = {"result": {"trades": [{"trade_seq": 1}], "has_more": True}}

        def slow_urlopen(*args, **kwargs):
            import time
            time.sleep(0.005)
            return FakeResponse(payload)

        with patch("ingestion.deribit_history.urlopen", side_effect=slow_urlopen):
            pages = fetch_trade_pages(plan)

        # Due to max_seconds=0.001, only 1 page fetched before time budget expires
        self.assertEqual(len(pages), 1)


if __name__ == "__main__":
    unittest.main()
