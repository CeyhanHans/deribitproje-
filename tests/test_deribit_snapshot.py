import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from ingestion.deribit_snapshot import (
    DeribitSnapshotError,
    SnapshotRequest,
    fetch_option_snapshot,
    normalize_order_book_snapshot,
    write_snapshot_json,
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


class DeribitSnapshotTests(unittest.TestCase):
    def test_requires_btc_option_instrument_name(self):
        with self.assertRaisesRegex(ValueError, "instrument_name is required"):
            SnapshotRequest("").validate()
        with self.assertRaisesRegex(ValueError, "BTC inverse option"):
            SnapshotRequest("ETH-27JUN25-3000-C").validate()
        with self.assertRaisesRegex(ValueError, "option format"):
            SnapshotRequest("BTC-PERPETUAL").validate()

    def test_normalizes_order_book_option_fields(self):
        snapshot = normalize_order_book_snapshot(
            {
                "instrument_name": "BTC-27JUN25-100000-C",
                "timestamp": 1_735_000_000_000,
                "best_bid_price": "0.012",
                "best_ask_price": 0.013,
                "mark_price": 0.0125,
                "bid_iv": 56.1,
                "ask_iv": 58.2,
                "mark_iv": 57.0,
                "greeks": {
                    "delta": 0.41,
                    "gamma": 0.0002,
                    "vega": 95.5,
                    "theta": -21.2,
                    "rho": 3.1,
                },
                "open_interest": "123.45",
            }
        )

        self.assertEqual(snapshot.instrument_name, "BTC-27JUN25-100000-C")
        self.assertEqual(snapshot.source, "deribit.public.get_order_book")
        self.assertEqual(snapshot.timestamp, 1_735_000_000_000)
        self.assertEqual(snapshot.best_bid_price, 0.012)
        self.assertEqual(snapshot.best_ask_price, 0.013)
        self.assertEqual(snapshot.mark_price, 0.0125)
        self.assertEqual(snapshot.bid_iv, 56.1)
        self.assertEqual(snapshot.ask_iv, 58.2)
        self.assertEqual(snapshot.mark_iv, 57.0)
        self.assertEqual(snapshot.greeks["delta"], 0.41)
        self.assertEqual(snapshot.open_interest, 123.45)

    def test_fetches_single_public_order_book_snapshot(self):
        payload = {
            "result": {
                "instrument_name": "BTC-27JUN25-100000-C",
                "timestamp": 1_735_000_000_000,
                "best_bid_price": 0.012,
                "best_ask_price": 0.013,
                "mark_price": 0.0125,
                "bid_iv": 56.1,
                "ask_iv": 58.2,
                "mark_iv": 57.0,
                "greeks": {"delta": 0.41},
                "open_interest": 123.45,
            }
        }

        with patch("ingestion.deribit_snapshot.urlopen", return_value=FakeResponse(payload)) as mocked:
            snapshot = fetch_option_snapshot(SnapshotRequest("BTC-27JUN25-100000-C"))

        self.assertEqual(snapshot.instrument_name, "BTC-27JUN25-100000-C")
        self.assertIn("/public/get_order_book?", mocked.call_args.args[0].full_url)
        self.assertIn("instrument_name=BTC-27JUN25-100000-C", mocked.call_args.args[0].full_url)
        self.assertEqual(mocked.call_count, 1)

    def test_api_and_network_errors_are_safe(self):
        api_payload = {"error": {"code": 10028, "message": "bad instrument"}}
        with patch("ingestion.deribit_snapshot.urlopen", return_value=FakeResponse(api_payload)):
            with self.assertRaisesRegex(DeribitSnapshotError, "Deribit API error 10028"):
                fetch_option_snapshot(SnapshotRequest("BTC-27JUN25-100000-C"))

        with patch("ingestion.deribit_snapshot.urlopen", side_effect=URLError("temporary failure")):
            with self.assertRaisesRegex(DeribitSnapshotError, "Could not reach Deribit public API"):
                fetch_option_snapshot(SnapshotRequest("BTC-27JUN25-100000-C"))

    def test_writes_snapshot_json_record(self):
        snapshot = normalize_order_book_snapshot(
            {
                "instrument_name": "BTC-27JUN25-100000-C",
                "timestamp": 1,
                "greeks": {},
            }
        )

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "snapshot.json"
            write_snapshot_json(output, snapshot)
            record = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(record["instrument_name"], "BTC-27JUN25-100000-C")
        self.assertEqual(record["timestamp"], 1)
        self.assertIn("best_bid_price", record)


if __name__ == "__main__":
    unittest.main()
