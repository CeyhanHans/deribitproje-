import json
import math
import random
import tempfile
import time
import tracemalloc
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ingestion.contract_catalog import (
    CatalogQuery,
    ContractCatalogError,
    ContractSpec,
    HistoricalContractCatalog,
    _parse_contract,
)

DAY = 86_400_000
# 2024-12-17 08:00:00 UTC
NOW = 1_734_422_400_000
EXPIRY_10D = 1_735_286_400_000  # 2024-12-27 08:00:00 UTC (NOW + 10 * DAY)
EXPIRY_17D = 1_735_891_200_000  # 2025-01-03 08:00:00 UTC (NOW + 17 * DAY)
EXPIRY_100D = 1_743_062_400_000  # 2025-03-27 08:00:00 UTC (NOW + 100 * DAY)


def row(name: str, expiry: int | None = None, created: int | None = None, **extra) -> dict:
    parts = name.split("-")
    date_str = parts[1]
    if expiry is None:
        if date_str == "27DEC24":
            expiry = EXPIRY_10D
        elif date_str == "03JAN25":
            expiry = EXPIRY_17D
        elif date_str == "27MAR25":
            expiry = EXPIRY_100D
        elif date_str == "17DEC24":
            expiry = NOW
        else:
            expiry = EXPIRY_10D

    if created is None:
        created = NOW - DAY

    data = {
        "instrument_name": name,
        "base_currency": "BTC",
        "settlement_currency": "BTC",
        "quote_currency": "USD",
        "strike": float(parts[2]),
        "option_type": "call" if parts[3] == "C" else "put",
        "expiration_timestamp": expiry,
        "creation_timestamp": created,
        "is_active": False,
        "tick_size": 0.0005,
        "contract_size": 1.0,
        "min_trade_amount": 0.1,
    }
    data.update(extra)
    return data


def catalog(rows: list) -> HistoricalContractCatalog:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "catalog.json"
        path.write_text(json.dumps({"result": rows}), encoding="utf-8")
        return HistoricalContractCatalog.load_from_json(path)


class ContractCatalogTests(unittest.TestCase):
    # =========================================================================
    # Existing Baseline Tests (Must continue passing without regression)
    # =========================================================================

    def test_creation_timestamp_lookahead_rejection(self):
        c = catalog([row("BTC-27DEC24-100000-C", created=NOW + 1), row("BTC-27DEC24-100000-P", created=NOW + 1)])
        self.assertEqual(c.get_active_universe(NOW), ())
        self.assertIsNone(c.select_straddle_pair(CatalogQuery(NOW, 100000, 7, 14)))

    def test_expired_contract_rejection(self):
        c = catalog([
            row("BTC-17DEC24-100000-C", expiry=NOW, created=NOW - DAY),
            row("BTC-17DEC24-100000-P", expiry=NOW, created=NOW - DAY),
        ])
        self.assertEqual(c.get_active_universe(NOW), ())

    def test_future_records_in_expired_catalog_are_filtered(self):
        c = catalog([row("BTC-27DEC24-100000-C", created=NOW + DAY), row("BTC-27DEC24-100000-P", created=NOW + DAY)])
        self.assertEqual(c.find_candidate_expiries(NOW, 7, 14), ())

    def test_strict_same_strike_and_expiry_enforcement(self):
        c = catalog([row("BTC-27DEC24-100000-C"), row("BTC-27DEC24-100000-P")])
        pair = c.select_straddle_pair(CatalogQuery(NOW, 100001, 7, 14))
        self.assertIsNotNone(pair)
        self.assertEqual(pair.call_spec.strike, pair.put_spec.strike)
        self.assertEqual(pair.call_spec.expiration_timestamp, pair.put_spec.expiration_timestamp)

    def test_missing_leg_returns_none(self):
        c = catalog([row("BTC-27DEC24-100000-C")])
        self.assertIsNone(c.select_straddle_pair(CatalogQuery(NOW, 100000, 7, 14)))

    def test_atm_selection_closest_common_strike(self):
        c = catalog([
            row("BTC-27DEC24-90000-C"), row("BTC-27DEC24-90000-P"),
            row("BTC-27DEC24-100000-C"), row("BTC-27DEC24-100000-P")
        ])
        pair = c.select_straddle_pair(CatalogQuery(NOW, 97000, 7, 14))
        self.assertIsNotNone(pair)
        self.assertEqual(pair.strike, 100000)

    def test_equidistant_tie_break_is_deterministic(self):
        c = catalog([
            row("BTC-27DEC24-90000-C"), row("BTC-27DEC24-90000-P"),
            row("BTC-27DEC24-100000-C"), row("BTC-27DEC24-100000-P")
        ])
        self.assertEqual(c.select_straddle_pair(CatalogQuery(NOW, 95000, 7, 14)).strike, 90000)
        self.assertEqual(c.select_straddle_pair(CatalogQuery(NOW, 95000, 7, 14, tie_break="higher")).strike, 100000)

    def test_parametric_dte_and_target_expiry(self):
        c = catalog([
            row("BTC-27DEC24-100000-C"), row("BTC-27DEC24-100000-P"),
            row("BTC-03JAN25-100000-C", expiry=EXPIRY_17D), row("BTC-03JAN25-100000-P", expiry=EXPIRY_17D)
        ])
        self.assertEqual(c.select_straddle_pair(CatalogQuery(NOW, 100000, 7, 20)).expiration_timestamp, EXPIRY_10D)
        self.assertEqual(c.select_straddle_pair(CatalogQuery(NOW, 100000, 7, 20, target_dte_days=17)).expiration_timestamp, EXPIRY_17D)

    def test_empty_catalog_and_invalid_query_are_graceful(self):
        self.assertIsNone(catalog([]).select_straddle_pair(CatalogQuery(NOW, 100000, 7, 14)))
        with self.assertRaisesRegex(ContractCatalogError, "tie_break"):
            catalog([]).select_straddle_pair(CatalogQuery(NOW, 100000, 7, 14, tie_break="random"))

    def test_result_is_repeatable(self):
        c = catalog([
            row("BTC-27DEC24-90000-C"), row("BTC-27DEC24-90000-P"),
            row("BTC-27DEC24-100000-C"), row("BTC-27DEC24-100000-P")
        ])
        outcomes = {
            c.select_straddle_pair(CatalogQuery(NOW, 95000, 7, 14)).strike
            for _ in range(100)
        }
        self.assertEqual(outcomes, {90000})

    # =========================================================================
    # Task 3 Acceptance Tests & QC Enhancements
    # =========================================================================

    def test_t_equal_creation_accepted_and_t_equal_expiry_rejected(self):
        """At T == creation, contract is active. At T == expiry (0 DTE boundary), contract is rejected."""
        created_time = NOW
        expiry_time = EXPIRY_10D

        c = catalog([
            row("BTC-27DEC24-90000-C", created=created_time, expiry=expiry_time),
            row("BTC-27DEC24-90000-P", created=created_time, expiry=expiry_time),
        ])

        # At T == created_time (exact listing moment): must be accepted
        active_at_creation = c.get_active_universe(created_time)
        self.assertEqual(len(active_at_creation), 2)
        pair_at_creation = c.select_straddle_pair(CatalogQuery(created_time, 90000, 0, 15))
        self.assertIsNotNone(pair_at_creation)

        # At T == expiry_time: contract has expired; must be rejected
        active_at_expiry = c.get_active_universe(expiry_time)
        self.assertEqual(len(active_at_expiry), 0)
        pair_at_expiry = c.select_straddle_pair(CatalogQuery(expiry_time, 90000, 0, 15))
        self.assertIsNone(pair_at_expiry)

    def test_future_expiry_created_in_past_accepted_even_if_is_active_false(self):
        """Historical contracts with is_active=False must be eligible as long as creation <= T < expiry."""
        c = catalog([
            row("BTC-27MAR25-90000-C", created=NOW - 5 * DAY, expiry=EXPIRY_100D, is_active=False),
            row("BTC-27MAR25-90000-P", created=NOW - 5 * DAY, expiry=EXPIRY_100D, is_active=False),
        ])
        pair = c.select_straddle_pair(CatalogQuery(NOW, 90000, 50, 150))
        self.assertIsNotNone(pair)
        self.assertEqual(pair.strike, 90000)

    def test_mismatched_metadata_rejected(self):
        """Mismatched option_type, strike, currency, or invalid timestamps must raise ContractCatalogError."""
        # 1. Option type mismatch (suffix -C but option_type 'put')
        with self.assertRaisesRegex(ContractCatalogError, "option_type"):
            catalog([row("BTC-27DEC24-90000-C", option_type="put")])

        # 2. Strike mismatch (name 90000 but strike 95000)
        with self.assertRaisesRegex(ContractCatalogError, "strike"):
            catalog([row("BTC-27DEC24-90000-C", strike=95000.0)])

        # 3. Currency mismatch (base_currency != BTC)
        with self.assertRaisesRegex(ContractCatalogError, "base_currency"):
            catalog([row("BTC-27DEC24-90000-C", base_currency="ETH")])

        # 4. Settlement currency mismatch
        with self.assertRaisesRegex(ContractCatalogError, "settlement_currency"):
            catalog([row("BTC-27DEC24-90000-C", settlement_currency="USDT")])

        # 5. creation_timestamp >= expiration_timestamp
        with self.assertRaisesRegex(ContractCatalogError, "creation_timestamp .* must precede"):
            catalog([row("BTC-27DEC24-90000-C", created=EXPIRY_10D + 1000, expiry=EXPIRY_10D)])

        # 6. Missing tick_size without silent default
        bad_row = row("BTC-27DEC24-90000-C")
        del bad_row["tick_size"]
        with self.assertRaisesRegex(ContractCatalogError, "tick_size"):
            catalog([bad_row])

        # 7. Expiration date string mismatch with instrument name
        with self.assertRaisesRegex(ContractCatalogError, "calendar date does not match"):
            catalog([row("BTC-27DEC24-90000-C", created=1000, expiry=2000)])

    def test_duplicate_deduplication_and_conflicting_record_rejection(self):
        """Exact identical duplicate records are deduped; conflicting records raise error."""
        r1 = row("BTC-27DEC24-90000-C")
        r2 = row("BTC-27DEC24-90000-C")  # Identical
        c = catalog([r1, r2, row("BTC-27DEC24-90000-P")])
        self.assertEqual(len(c.get_active_universe(NOW)), 2)

        # Conflicting metadata for same instrument name
        r_conflict = row("BTC-27DEC24-90000-C", tick_size=0.0010)
        with self.assertRaisesRegex(ContractCatalogError, "conflicting metadata"):
            catalog([r1, r_conflict])

    def test_shuffled_input_determinism(self):
        """Loading records in shuffled order produces byte-for-byte identical selection."""
        rows = [
            row("BTC-27DEC24-90000-C"), row("BTC-27DEC24-90000-P"),
            row("BTC-27DEC24-100000-C"), row("BTC-27DEC24-100000-P"),
            row("BTC-03JAN25-90000-C", expiry=EXPIRY_17D), row("BTC-03JAN25-90000-P", expiry=EXPIRY_17D),
        ]
        c1 = catalog(rows)
        query = CatalogQuery(NOW, 95000, 7, 20)
        pair1 = c1.select_straddle_pair(query)

        for seed in (42, 123, 999):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            c2 = catalog(shuffled)
            pair2 = c2.select_straddle_pair(query)
            self.assertEqual(pair1, pair2)

    def test_first_candidate_expiry_missing_leg_falls_back_to_second_expiry(self):
        """If expiry 1 only has Call and no Put, catalog must evaluate next candidate expiry."""
        rows = [
            row("BTC-27DEC24-90000-C", expiry=EXPIRY_10D),
            row("BTC-03JAN25-90000-C", expiry=EXPIRY_17D),
            row("BTC-03JAN25-90000-P", expiry=EXPIRY_17D),
        ]
        c = catalog(rows)

        detailed_res = c.select_straddle(CatalogQuery(NOW, 90000, 7, 20, target_dte_days=10))
        self.assertIsNotNone(detailed_res.pair)
        self.assertEqual(detailed_res.pair.expiration_timestamp, EXPIRY_17D)
        self.assertEqual(detailed_res.pair.strike, 90000)
        self.assertEqual(detailed_res.candidates_evaluated, 2)
        self.assertEqual(detailed_res.contracts_scanned, 3)
        self.assertEqual(detailed_res.reason_code, "selected")

    # =========================================================================
    # QC P1 & P2 Specific Tests
    # =========================================================================

    def test_real_deribit_pilot_sample_records_parsed_successfully(self):
        """Real Deribit records with quote_currency='BTC' and counter_currency='USD' must parse cleanly."""
        pilot_file = Path("reports/pilot/catalog_sample.json")
        if not pilot_file.exists():
            self.skipTest("pilot file not found")
        data = json.loads(pilot_file.read_text(encoding="utf-8"))
        samples = data.get("sample_records", [])
        self.assertGreater(len(samples), 0)

        parsed_specs = [_parse_contract(r) for r in samples]
        self.assertEqual(len(parsed_specs), len(samples))
        # Verify first sample BTC-15JUL16-600-C
        first = parsed_specs[0]
        self.assertEqual(first.instrument_name, "BTC-15JUL16-600-C")
        self.assertEqual(first.underlying, "BTC")
        self.assertEqual(first.strike, 600.0)
        self.assertEqual(first.option_type, "call")
        self.assertEqual(first.expiration_date_str, "15JUL16")
        self.assertEqual(first.contract_size, 1.0)
        self.assertEqual(first.tick_size, 0.0001)

    def test_direct_contract_spec_constructor_validates_dates_and_numerics(self):
        """Direct ContractSpec instantiation must run full validation."""
        # 1. Date mismatch
        with self.assertRaisesRegex(ContractCatalogError, "calendar date does not match"):
            ContractSpec(
                instrument_name="BTC-27DEC24-90000-C",
                underlying="BTC",
                strike=90000.0,
                option_type="call",
                expiration_timestamp=2000,
                expiration_date_str="27DEC24",
                creation_timestamp=1000,
                is_active=False,
                tick_size=0.0005,
                contract_size=1.0,
                min_trade_amount=0.1,
            )

        # 2. Negative strike
        with self.assertRaisesRegex(ContractCatalogError, "strike must be strictly positive"):
            ContractSpec(
                instrument_name="BTC-27DEC24-90000-C",
                underlying="BTC",
                strike=-500.0,
                option_type="call",
                expiration_timestamp=EXPIRY_10D,
                expiration_date_str="27DEC24",
                creation_timestamp=NOW,
                is_active=False,
                tick_size=0.0005,
                contract_size=1.0,
                min_trade_amount=0.1,
            )

        # 3. Invalid option_type
        with self.assertRaisesRegex(ContractCatalogError, "option_type must be 'call' or 'put'"):
            ContractSpec(
                instrument_name="BTC-27DEC24-90000-C",
                underlying="BTC",
                strike=90000.0,
                option_type="future",
                expiration_timestamp=EXPIRY_10D,
                expiration_date_str="27DEC24",
                creation_timestamp=NOW,
                is_active=False,
                tick_size=0.0005,
                contract_size=1.0,
                min_trade_amount=0.1,
            )

    def test_catalog_query_validation_rejects_nan_inf_and_booleans(self):
        """CatalogQuery must reject NaN, Infinity, negative values, and boolean types across all fields."""
        # NaN / Inf in DTE
        with self.assertRaisesRegex(ContractCatalogError, "cannot be NaN or Infinity"):
            CatalogQuery(NOW, 90000.0, float("nan"), 14.0).validate()
        with self.assertRaisesRegex(ContractCatalogError, "cannot be NaN or Infinity"):
            CatalogQuery(NOW, 90000.0, 7.0, float("inf")).validate()
        with self.assertRaisesRegex(ContractCatalogError, "cannot be NaN or Infinity"):
            CatalogQuery(NOW, 90000.0, 7.0, 14.0, target_dte_days=float("nan")).validate()

        # Booleans disguised as numbers
        with self.assertRaisesRegex(ContractCatalogError, "min_dte_days must be a number"):
            CatalogQuery(NOW, 90000.0, True, 14.0).validate()
        with self.assertRaisesRegex(ContractCatalogError, "max_dte_days must be a number"):
            CatalogQuery(NOW, 90000.0, 7.0, False).validate()
        with self.assertRaisesRegex(ContractCatalogError, "target_dte_days must be a number"):
            CatalogQuery(NOW, 90000.0, 7.0, 14.0, target_dte_days=True).validate()
        with self.assertRaisesRegex(ContractCatalogError, "as_of_time_ms must be a non-negative integer"):
            CatalogQuery(True, 90000.0, 7.0, 14.0).validate()
        with self.assertRaisesRegex(ContractCatalogError, "underlying_price must be positive"):
            CatalogQuery(NOW, True, 7.0, 14.0).validate()

    def test_benchmark_120k_synthetic_records_scale_and_candidates_measured(self):
        """Verify performance and memory efficiency on 120,000 synthetic option records.

        Explicitly tests bisect index efficiency, valid calendar dates, memory footprint,
        and candidates_evaluated / contracts_scanned metrics.
        """
        MONTH_NAMES = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
        base_dt = datetime(2024, 12, 27, 8, 0, 0, tzinfo=timezone.utc)

        # Track memory allocation during 120k record generation and catalog construction
        tracemalloc.start()
        specs = []
        for exp_idx in range(100):
            cur_dt = base_dt + timedelta(days=exp_idx)
            exp_ts = int(cur_dt.timestamp() * 1000)
            exp_str = f"{cur_dt.day:02d}{MONTH_NAMES[cur_dt.month - 1]}{str(cur_dt.year)[-2:]}"
            for strike_idx in range(600):
                strike = 50000.0 + strike_idx * 200.0
                specs.append(ContractSpec(
                    instrument_name=f"BTC-{exp_str}-{int(strike)}-C",
                    underlying="BTC",
                    strike=strike,
                    option_type="call",
                    expiration_timestamp=exp_ts,
                    expiration_date_str=exp_str,
                    creation_timestamp=NOW - 5 * DAY,
                    is_active=False,
                    tick_size=0.0005,
                    contract_size=1.0,
                    min_trade_amount=0.1,
                ))
                specs.append(ContractSpec(
                    instrument_name=f"BTC-{exp_str}-{int(strike)}-P",
                    underlying="BTC",
                    strike=strike,
                    option_type="put",
                    expiration_timestamp=exp_ts,
                    expiration_date_str=exp_str,
                    creation_timestamp=NOW - 5 * DAY,
                    is_active=False,
                    tick_size=0.0005,
                    contract_size=1.0,
                    min_trade_amount=0.1,
                ))

        self.assertEqual(len(specs), 60000 * 2)  # 120,000 specs

        t0 = time.perf_counter()
        c = HistoricalContractCatalog(specs)
        init_time = time.perf_counter() - t0

        current_mem, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Query straddle in the middle of the 120k catalog
        query = CatalogQuery(
            as_of_time_ms=NOW,
            underlying_price=78500.0,
            min_dte_days=15.0,
            max_dte_days=25.0,
            target_dte_days=20.0,
        )

        t_query_0 = time.perf_counter()
        result = c.select_straddle(query)
        t_query = time.perf_counter() - t_query_0

        self.assertIsNotNone(result.pair)
        self.assertEqual(result.pair.strike, 78400.0)  # Closest strike to 78,500 (lower tie-break)
        self.assertLess(t_query, 0.050, f"Query took {t_query*1000:.2f}ms (expected < 50ms)")
        # Assert candidates evaluated was 1 (target DTE match)
        self.assertEqual(result.candidates_evaluated, 1)
        # Assert contracts scanned was exactly 1200 (600 calls + 600 puts in target expiry bucket)
        self.assertEqual(result.contracts_scanned, 1200)
        # Verify peak memory is reasonable (< 300MB)
        self.assertLess(peak_mem / (1024 * 1024), 300.0)


if __name__ == "__main__":
    unittest.main()
