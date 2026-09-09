import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from ingestion.deribit_underlying import (
    DEFAULT_DATA_QUALITY_LABEL,
    DEFAULT_SERIES_TYPE,
    HOUR_MS,
    MAX_HOURS_PER_REQUEST,
    DeribitUnderlyingError,
    HourlyUnderlyingBar,
    UnderlyingRequest,
    fetch_underlying_bars,
    plan_default_underlying_windows,
    plan_underlying_window_requests,
    parse_tradingview_response,
    write_underlying_bars_json,
    merge_and_dedup_bars,
    find_missing_underlying_intervals,
    compute_underlying_coverage,
    fetch_underlying_series,
    UnderlyingCoverageReport,
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


class DeribitUnderlyingTests(unittest.TestCase):
    def test_request_validation_valid(self):
        req1 = UnderlyingRequest(start_timestamp=1_700_000_000_000, end_timestamp=1_700_003_600_000)
        req1.validate()

        req2 = UnderlyingRequest(
            start_timestamp=1_700_000_000_000,
            end_timestamp=1_700_003_600_000,
            resolution="1h",
        )
        req2.validate()

    def test_request_validation_invalid_fields(self):
        with self.assertRaisesRegex(ValueError, "first version supports only BTC-PERPETUAL"):
            UnderlyingRequest(0, 1000, instrument_name="").validate()

        with self.assertRaisesRegex(ValueError, "first version supports only BTC-PERPETUAL"):
            UnderlyingRequest(0, 1000, index_name="").validate()

        with self.assertRaisesRegex(ValueError, "series_type must be perpetual_kline_proxy"):
            UnderlyingRequest(0, 1000, series_type="direct_index_ohlc").validate()

        with self.assertRaisesRegex(ValueError, "timestamps must be non-negative"):
            UnderlyingRequest(-1, 1000).validate()

        with self.assertRaisesRegex(ValueError, "start_timestamp must be before end_timestamp"):
            UnderlyingRequest(1000, 1000).validate()

        with self.assertRaisesRegex(ValueError, "start_timestamp must be before end_timestamp"):
            UnderlyingRequest(2000, 1000).validate()

        with self.assertRaisesRegex(ValueError, "first version supports only 1 hour resolution"):
            UnderlyingRequest(0, 3600000, resolution="1D").validate()

        with self.assertRaisesRegex(ValueError, "first version supports only 1 hour resolution"):
            UnderlyingRequest(0, 3600000, resolution="5m").validate()

        with self.assertRaisesRegex(ValueError, "exceeds maximum allowed limit"):
            UnderlyingRequest(0, 10_000 * 3600 * 1000, max_hours=100).validate()

    def test_parses_valid_tradingview_response(self):
        req = UnderlyingRequest(
            start_timestamp=1_700_000_000_000,
            end_timestamp=1_700_007_200_000,
            instrument_name="BTC-PERPETUAL",
            index_name="btc_usd",
        )
        sample = {
            "status": "ok",
            "ticks": [1_700_000_000_000, 1_700_003_600_000],
            "open": [60000.0, 60500.0],
            "high": [60800.0, 61000.0],
            "low": [59900.0, 60200.0],
            "close": [60500.0, 60900.0],
            "volume": [120.5, 95.0],
            "cost": [7200000.0, 5800000.0],
        }

        bars = parse_tradingview_response(sample, req)
        self.assertEqual(len(bars), 2)

        bar0 = bars[0]
        self.assertEqual(bar0.timestamp_ms, 1_700_000_000_000)
        self.assertEqual(bar0.open, 60000.0)
        self.assertEqual(bar0.high, 60800.0)
        self.assertEqual(bar0.low, 59900.0)
        self.assertEqual(bar0.close, 60500.0)
        self.assertEqual(bar0.volume, 120.5)
        self.assertEqual(bar0.cost, 7200000.0)
        self.assertEqual(bar0.instrument_name, "BTC-PERPETUAL")
        self.assertEqual(bar0.index_name, "btc_usd")
        self.assertEqual(bar0.series_type, DEFAULT_SERIES_TYPE)
        self.assertEqual(bar0.resolution, "1h")
        self.assertEqual(bar0.data_quality_label, DEFAULT_DATA_QUALITY_LABEL)
        self.assertEqual(bar0.timestamp_utc, datetime.fromtimestamp(1_700_000_000, tz=timezone.utc))
        self.assertEqual(bar0.bucket_end_utc, datetime.fromtimestamp(1_700_003_600, tz=timezone.utc))

    def test_rejects_misleading_instrument_index_metadata(self):
        with self.assertRaisesRegex(ValueError, "BTC-PERPETUAL"):
            UnderlyingRequest(
                start_timestamp=1_700_000_000_000,
                end_timestamp=1_700_003_600_000,
                instrument_name="ETH-PERPETUAL",
                index_name="btc_usd",
            ).validate()

        with self.assertRaisesRegex(ValueError, "BTC-PERPETUAL"):
            UnderlyingRequest(
                start_timestamp=1_700_000_000_000,
                end_timestamp=1_700_003_600_000,
                instrument_name="BTC-PERPETUAL",
                index_name="eth_usd",
            ).validate()

    def test_plans_360_day_window_into_bounded_contiguous_chunks(self):
        end = datetime(2026, 9, 9, 0, 0, tzinfo=timezone.utc)
        chunks = plan_underlying_window_requests(days=360, end_time=end)

        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].end_timestamp - chunks[0].start_timestamp, MAX_HOURS_PER_REQUEST * HOUR_MS)
        self.assertEqual(chunks[1].end_timestamp - chunks[1].start_timestamp, 3640 * HOUR_MS)
        self.assertEqual(chunks[0].end_timestamp, chunks[1].start_timestamp)
        self.assertEqual(chunks[1].end_timestamp, int(end.timestamp() * 1000))
        self.assertTrue(all((chunk.end_timestamp - chunk.start_timestamp) <= MAX_HOURS_PER_REQUEST * HOUR_MS for chunk in chunks))

    def test_default_30_to_120_day_windows_stay_single_bounded_requests(self):
        end = datetime(2026, 9, 9, 0, 0, tzinfo=timezone.utc)
        plans = plan_default_underlying_windows(end_time=end)

        for label in ("30d", "60d", "90d", "120d"):
            self.assertEqual(len(plans[label]), 1)
            chunks = plans[label]
            hours = int(label.removesuffix("d")) * 24
            self.assertEqual(chunks[0].end_timestamp - chunks[0].start_timestamp, hours * HOUR_MS)

        self.assertEqual(len(plans["360d"]), 2)

    def test_handles_no_data_or_empty_response(self):
        req = UnderlyingRequest(0, 3600000)
        self.assertEqual(parse_tradingview_response({"status": "no_data"}, req), ())
        self.assertEqual(
            parse_tradingview_response(
                {
                    "status": "ok",
                    "ticks": [],
                    "open": [],
                    "high": [],
                    "low": [],
                    "close": [],
                    "volume": [],
                    "cost": [],
                },
                req,
            ),
            (),
        )

    def test_rejects_missing_required_fields(self):
        req = UnderlyingRequest(0, 3600000)
        with self.assertRaisesRegex(DeribitUnderlyingError, "missing required OHLC"):
            parse_tradingview_response({"status": "ok", "ticks": [1000]}, req)

    def test_rejects_mismatched_array_lengths(self):
        req = UnderlyingRequest(0, 3600000)
        payload = {
            "status": "ok",
            "ticks": [1000, 2000],
            "open": [10.0],
            "high": [12.0, 15.0],
            "low": [9.0, 10.0],
            "close": [11.0, 14.0],
        }
        with self.assertRaisesRegex(DeribitUnderlyingError, "mismatched array lengths"):
            parse_tradingview_response(payload, req)

    def test_rejects_corrupted_non_numeric_rows(self):
        req = UnderlyingRequest(0, 3600000)
        payload = {
            "status": "ok",
            "ticks": [1000],
            "open": ["not_a_number"],
            "high": [12.0],
            "low": [9.0],
            "close": [11.0],
        }
        with self.assertRaisesRegex(DeribitUnderlyingError, "non-numeric value"):
            parse_tradingview_response(payload, req)

    def test_rejects_corrupted_negative_or_zero_prices(self):
        req = UnderlyingRequest(0, 3600000)
        payload = {
            "status": "ok",
            "ticks": [1000],
            "open": [0.0],
            "high": [12.0],
            "low": [9.0],
            "close": [11.0],
        }
        with self.assertRaisesRegex(DeribitUnderlyingError, "prices must be strictly positive"):
            parse_tradingview_response(payload, req)

    def test_rejects_corrupted_nan_prices(self):
        req = UnderlyingRequest(0, 3600000)
        payload = {
            "status": "ok",
            "ticks": [1000],
            "open": [float("nan")],
            "high": [12.0],
            "low": [9.0],
            "close": [11.0],
        }
        with self.assertRaisesRegex(DeribitUnderlyingError, "values must be finite"):
            parse_tradingview_response(payload, req)

    def test_rejects_corrupted_ohlc_boundary_violation(self):
        req = UnderlyingRequest(0, 3600000)
        # high < low
        payload1 = {
            "status": "ok",
            "ticks": [1000],
            "open": [10.0],
            "high": [8.0],
            "low": [9.0],
            "close": [9.5],
        }
        with self.assertRaisesRegex(DeribitUnderlyingError, "high .* cannot be less than low"):
            parse_tradingview_response(payload1, req)

        # open > high
        payload2 = {
            "status": "ok",
            "ticks": [1000],
            "open": [15.0],
            "high": [12.0],
            "low": [9.0],
            "close": [10.0],
        }
        with self.assertRaisesRegex(DeribitUnderlyingError, "high/low boundaries violated"):
            parse_tradingview_response(payload2, req)

    @patch("ingestion.deribit_underlying.urlopen")
    def test_fetch_underlying_bars_successful_mock(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            {
                "jsonrpc": "2.0",
                "result": {
                    "status": "ok",
                    "ticks": [1_700_000_000_000],
                    "open": [65000.0],
                    "high": [65500.0],
                    "low": [64800.0],
                    "close": [65200.0],
                    "volume": [42.0],
                    "cost": [2700000.0],
                },
            }
        )
        req = UnderlyingRequest(1_700_000_000_000, 1_700_003_600_000)
        bars = fetch_underlying_bars(req)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, 65200.0)

    @patch("ingestion.deribit_underlying.urlopen")
    def test_fetch_underlying_bars_api_error(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            {
                "jsonrpc": "2.0",
                "error": {"code": -32602, "message": "Invalid params: instrument not found"},
            }
        )
        req = UnderlyingRequest(1_700_000_000_000, 1_700_003_600_000)
        with self.assertRaisesRegex(DeribitUnderlyingError, "deribit api error"):
            fetch_underlying_bars(req)

    @patch("ingestion.deribit_underlying.urlopen")
    def test_fetch_underlying_bars_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = URLError("connection refused")
        req = UnderlyingRequest(1_700_000_000_000, 1_700_003_600_000)
        with self.assertRaisesRegex(DeribitUnderlyingError, "request failed"):
            fetch_underlying_bars(req)

    def test_write_underlying_bars_json(self):
        req = UnderlyingRequest(1_700_000_000_000, 1_700_003_600_000)
        sample = {
            "status": "ok",
            "ticks": [1_700_000_000_000],
            "open": [60000.0],
            "high": [60800.0],
            "low": [59900.0],
            "close": [60500.0],
            "volume": [120.5],
            "cost": [7200000.0],
        }
        bars = parse_tradingview_response(sample, req)

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = Path(tmp_dir) / "underlying.json"
            write_underlying_bars_json(out_file, bars)

            self.assertTrue(out_file.exists())
            payload = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["bar_count"], 1)
            self.assertEqual(payload["bars"][0]["open"], 60000.0)
            self.assertIn("timestamp_utc", payload["bars"][0])

    def test_merge_and_dedup_bars_sorted_and_deduplicated(self):
        req = UnderlyingRequest(1_700_000_000_000, 1_700_014_400_000)
        sample1 = {
            "status": "ok",
            "ticks": [1_700_000_000_000, 1_700_003_600_000],
            "open": [60000.0, 60500.0],
            "high": [60800.0, 61000.0],
            "low": [59900.0, 60200.0],
            "close": [60500.0, 60900.0],
            "volume": [10.0, 20.0],
            "cost": [600000.0, 1200000.0],
        }
        sample2 = {
            "status": "ok",
            # Overlapping tick at 1_700_003_600_000, plus new tick at 1_700_007_200_000
            "ticks": [1_700_003_600_000, 1_700_007_200_000],
            "open": [60500.0, 60900.0],
            "high": [61000.0, 61500.0],
            "low": [60200.0, 60800.0],
            "close": [60900.0, 61200.0],
            "volume": [20.0, 30.0],
            "cost": [1200000.0, 1800000.0],
        }
        chunk1 = parse_tradingview_response(sample1, req)
        chunk2 = parse_tradingview_response(sample2, req)

        # Merge chunks (with overlap)
        merged = merge_and_dedup_bars([chunk1, chunk2])
        self.assertEqual(len(merged), 3)
        self.assertEqual([b.timestamp_ms for b in merged], [1_700_000_000_000, 1_700_003_600_000, 1_700_007_200_000])

    def test_merge_and_dedup_bars_conflicting_data_raises_error(self):
        req = UnderlyingRequest(1_700_000_000_000, 1_700_007_200_000)
        sample1 = {
            "status": "ok",
            "ticks": [1_700_000_000_000],
            "open": [60000.0],
            "high": [60800.0],
            "low": [59900.0],
            "close": [60500.0],
        }
        sample2 = {
            "status": "ok",
            "ticks": [1_700_000_000_000],
            "open": [60000.0],
            "high": [62500.0],
            "low": [59900.0],
            "close": [62000.0],  # Conflicting close!
        }
        chunk1 = parse_tradingview_response(sample1, req)
        chunk2 = parse_tradingview_response(sample2, req)

        with self.assertRaisesRegex(DeribitUnderlyingError, "conflicting underlying bar data"):
            merge_and_dedup_bars([chunk1, chunk2])

    def test_merge_and_dedup_bars_conflicting_identity_raises_error(self):
        req = UnderlyingRequest(1_700_000_000_000, 1_700_003_600_000)
        sample = {
            "status": "ok",
            "ticks": [1_700_000_000_000],
            "open": [60000.0],
            "high": [60800.0],
            "low": [59900.0],
            "close": [60500.0],
        }
        btc_bar = parse_tradingview_response(sample, req)[0]
        mismatched = HourlyUnderlyingBar(
            timestamp_utc=btc_bar.timestamp_utc,
            bucket_start_utc=btc_bar.bucket_start_utc,
            bucket_end_utc=btc_bar.bucket_end_utc,
            timestamp_ms=btc_bar.timestamp_ms,
            open=btc_bar.open,
            high=btc_bar.high,
            low=btc_bar.low,
            close=btc_bar.close,
            volume=btc_bar.volume,
            cost=btc_bar.cost,
            instrument_name="ETH-PERPETUAL",
            index_name=btc_bar.index_name,
            series_type=btc_bar.series_type,
        )

        with self.assertRaisesRegex(DeribitUnderlyingError, "conflicting underlying bar data"):
            merge_and_dedup_bars([[btc_bar], [mismatched]])

    def test_find_missing_underlying_intervals(self):
        req = UnderlyingRequest(1_700_000_000_000, 1_700_014_400_000)
        # 4 hours total: 0, 3600, 7200, 10800. Provide 0 and 7200 -> missing 3600 and 10800.
        sample = {
            "status": "ok",
            "ticks": [1_700_000_000_000, 1_700_007_200_000],
            "open": [60000.0, 61000.0],
            "high": [60500.0, 61500.0],
            "low": [59500.0, 60500.0],
            "close": [60200.0, 61200.0],
        }
        bars = parse_tradingview_response(sample, req)

        missing = find_missing_underlying_intervals(
            bars,
            start_timestamp=1_700_000_000_000,
            end_timestamp=1_700_014_400_000,
            step_ms=HOUR_MS,
        )
        expected_missing = (
            (1_700_003_600_000, 1_700_007_200_000),
            (1_700_010_800_000, 1_700_014_400_000),
        )
        self.assertEqual(missing, expected_missing)

    def test_compute_underlying_coverage(self):
        req = UnderlyingRequest(1_700_000_000_000, 1_700_014_400_000)
        # 4 hours expected, provide 3 bars (0, 3600, 7200) -> 75% coverage
        sample = {
            "status": "ok",
            "ticks": [1_700_000_000_000, 1_700_003_600_000, 1_700_007_200_000],
            "open": [60000.0, 60500.0, 61000.0],
            "high": [60500.0, 61000.0, 61500.0],
            "low": [59500.0, 60000.0, 60500.0],
            "close": [60200.0, 60800.0, 61200.0],
        }
        bars = parse_tradingview_response(sample, req)

        report = compute_underlying_coverage(
            bars,
            start_timestamp=1_700_000_000_000,
            end_timestamp=1_700_014_400_000,
            step_ms=HOUR_MS,
        )
        self.assertEqual(report.expected_intervals, 4)
        self.assertEqual(report.observed_intervals, 3)
        self.assertEqual(report.missing_intervals_count, 1)
        self.assertEqual(report.coverage_ratio, 0.75)
        self.assertEqual(report.missing_intervals, ((1_700_010_800_000, 1_700_014_400_000),))

    @patch("ingestion.deribit_underlying.urlopen")
    def test_fetch_underlying_series_multi_chunk_mock(self, mock_urlopen):
        # Two chunk responses:
        resp1 = FakeResponse({
            "result": {
                "status": "ok",
                "ticks": [1_700_000_000_000, 1_700_003_600_000],
                "open": [50000.0, 50100.0],
                "high": [50200.0, 50300.0],
                "low": [49900.0, 50000.0],
                "close": [50100.0, 50200.0],
            }
        })
        resp2 = FakeResponse({
            "result": {
                "status": "ok",
                "ticks": [1_700_003_600_000, 1_700_007_200_000],
                "open": [50100.0, 50200.0],
                "high": [50300.0, 50400.0],
                "low": [50000.0, 50100.0],
                "close": [50200.0, 50300.0],
            }
        })
        mock_urlopen.side_effect = [resp1, resp2]

        bars = fetch_underlying_series(
            start_timestamp=1_700_000_000_000,
            end_timestamp=1_700_007_200_000,
            max_hours=1,  # Forces 2 chunks of 1h
        )
        self.assertEqual(len(bars), 3)
        self.assertEqual(
            [b.timestamp_ms for b in bars],
            [1_700_000_000_000, 1_700_003_600_000, 1_700_007_200_000],
        )

    def test_proxy_quality_label_preserved(self):
        self.assertEqual(DEFAULT_DATA_QUALITY_LABEL, "official-tradingview-perpetual-kline-proxy")
        self.assertEqual(DEFAULT_SERIES_TYPE, "perpetual_kline_proxy")
