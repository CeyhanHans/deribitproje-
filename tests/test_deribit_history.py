import json
import unittest
from unittest.mock import patch

from ingestion.deribit_history import (
    DEFAULT_BASE_URL,
    DEFAULT_COUNT,
    MAX_COUNT,
    DeribitHistoryError,
    build_sequence_plan,
    build_time_plan,
    fetch_trade_pages,
    planned_requests,
    summarize_download,
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


if __name__ == "__main__":
    unittest.main()
