import json
import tempfile
import unittest
from pathlib import Path

from ingestion.contract_catalog import CatalogQuery, ContractCatalogError, HistoricalContractCatalog


DAY = 86_400_000
NOW = 1_700_000_000_000
EXPIRY_10D = NOW + 10 * DAY
EXPIRY_12D = NOW + 12 * DAY


def row(name, expiry=EXPIRY_10D, created=NOW - DAY, **extra):
    data = {
        "instrument_name": name,
        "base_currency": "BTC",
        "strike": float(name.split("-")[2]),
        "option_type": "call" if name.endswith("-C") else "put",
        "expiration_timestamp": expiry,
        "creation_timestamp": created,
        "is_active": False,
        "tick_size": 0.0005,
        "contract_size": 1.0,
        "min_trade_amount": 0.1,
    }
    data.update(extra)
    return data


def catalog(rows):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "catalog.json"
        path.write_text(json.dumps({"result": rows}), encoding="utf-8")
        return HistoricalContractCatalog.load_from_json(path)


class ContractCatalogTests(unittest.TestCase):
    def test_creation_timestamp_lookahead_rejection(self):
        c = catalog([row("BTC-27DEC24-100000-C", created=NOW + 1), row("BTC-27DEC24-100000-P", created=NOW + 1)])
        self.assertEqual(c.get_active_universe(NOW), ())
        self.assertIsNone(c.select_straddle_pair(CatalogQuery(NOW, 100000, 7, 14)))

    def test_expired_contract_rejection(self):
        c = catalog([row("BTC-27DEC24-100000-C", expiry=NOW), row("BTC-27DEC24-100000-P", expiry=NOW)])
        self.assertEqual(c.get_active_universe(NOW), ())

    def test_future_records_in_expired_catalog_are_filtered(self):
        c = catalog([row("BTC-27DEC24-100000-C", created=NOW + DAY), row("BTC-27DEC24-100000-P", created=NOW + DAY)])
        self.assertEqual(c.find_candidate_expiries(NOW, 7, 14), ())

    def test_strict_same_strike_and_expiry_enforcement(self):
        c = catalog([row("BTC-27DEC24-100000-C"), row("BTC-27DEC24-100000-P")])
        pair = c.select_straddle_pair(CatalogQuery(NOW, 100001, 7, 14))
        self.assertEqual(pair.call_spec.strike, pair.put_spec.strike)
        self.assertEqual(pair.call_spec.expiration_timestamp, pair.put_spec.expiration_timestamp)

    def test_missing_leg_returns_none(self):
        c = catalog([row("BTC-27DEC24-100000-C")])
        self.assertIsNone(c.select_straddle_pair(CatalogQuery(NOW, 100000, 7, 14)))

    def test_atm_selection_closest_common_strike(self):
        c = catalog([row("BTC-27DEC24-90000-C"), row("BTC-27DEC24-90000-P"), row("BTC-27DEC24-100000-C"), row("BTC-27DEC24-100000-P")])
        self.assertEqual(c.select_straddle_pair(CatalogQuery(NOW, 97000, 7, 14)).strike, 100000)

    def test_equidistant_tie_break_is_deterministic(self):
        c = catalog([row("BTC-27DEC24-90000-C"), row("BTC-27DEC24-90000-P"), row("BTC-27DEC24-100000-C"), row("BTC-27DEC24-100000-P")])
        self.assertEqual(c.select_straddle_pair(CatalogQuery(NOW, 95000, 7, 14)).strike, 90000)
        self.assertEqual(c.select_straddle_pair(CatalogQuery(NOW, 95000, 7, 14, tie_break="higher")).strike, 100000)

    def test_parametric_dte_and_target_expiry(self):
        c = catalog([row("BTC-27DEC24-100000-C"), row("BTC-27DEC24-100000-P"), row("BTC-03JAN25-100000-C", expiry=EXPIRY_12D), row("BTC-03JAN25-100000-P", expiry=EXPIRY_12D)])
        self.assertEqual(c.select_straddle_pair(CatalogQuery(NOW, 100000, 7, 14)).expiration_timestamp, EXPIRY_10D)
        self.assertEqual(c.select_straddle_pair(CatalogQuery(NOW, 100000, 7, 14, target_dte_days=12)).expiration_timestamp, EXPIRY_12D)

    def test_empty_catalog_and_invalid_query_are_graceful(self):
        self.assertIsNone(catalog([]).select_straddle_pair(CatalogQuery(NOW, 100000, 7, 14)))
        with self.assertRaisesRegex(ContractCatalogError, "tie_break"):
            catalog([]).select_straddle_pair(CatalogQuery(NOW, 100000, 7, 14, tie_break="random"))

    def test_result_is_repeatable(self):
        c = catalog([row("BTC-27DEC24-90000-C"), row("BTC-27DEC24-90000-P"), row("BTC-27DEC24-100000-C"), row("BTC-27DEC24-100000-P")])
        outcomes = {
            c.select_straddle_pair(CatalogQuery(NOW, 95000, 7, 14)).strike
            for _ in range(100)
        }
        self.assertEqual(outcomes, {90000})


if __name__ == "__main__":
    unittest.main()
