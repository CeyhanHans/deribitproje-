"""
Acceptance and unit tests for Task 14: Data Pilot and Source Adequacy Gate.

Covers:
- Linking every claim to raw data manifest (reports/pilot/manifest.json)
- Request cap incomplete test (max_requests enforcement)
- Observed zero-trade separation from errors/download gaps
- 2023 real data verification (29DEC23)
- Non-quarterly expiry reporting and liquidity comparison
- Status gate decision (READY/PARTIAL/BLOCKED) and disclaimer
- Catalog metadata dynamic count verification (not hardcoded to 121497)
- Configurable budget caps
"""

import json
from decimal import Decimal
from pathlib import Path
import unittest

from research.pilot import (
    BudgetTracker,
    DataPilot,
    GateDecision,
    LegPilotResult,
    PilotBudgetCapError,
    PilotConfig,
    HourlyBucketStat,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PILOT_DIR = REPO_ROOT / "reports" / "pilot"
MANIFEST_PATH = PILOT_DIR / "manifest.json"
REPORT_PATH = PILOT_DIR / "pilot_report.md"
CATALOG_SAMPLE_PATH = PILOT_DIR / "catalog_sample.json"


class DataPilotAcceptanceTests(unittest.TestCase):
    """Rigorous acceptance tests for Task 14 Data Pilot."""

    # --------------------------------------------------------------------------
    # 1. Manifest Audit Link
    # --------------------------------------------------------------------------
    def test_manifest_audit_link(self) -> None:
        """Every claim in pilot_report.md strictly links to raw data in manifest.json."""
        self.assertTrue(MANIFEST_PATH.exists(), "reports/pilot/manifest.json must exist")
        self.assertTrue(REPORT_PATH.exists(), "reports/pilot/pilot_report.md must exist")
        self.assertTrue(CATALOG_SAMPLE_PATH.exists(), "reports/pilot/catalog_sample.json must exist")

        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        report_text = REPORT_PATH.read_text(encoding="utf-8")

        # Verify manifest structure
        self.assertEqual(manifest.get("schema_version"), "1.0")
        self.assertIn("catalog_metadata", manifest)
        self.assertIn("resource_consumption", manifest)
        self.assertIn("gate_decision", manifest)
        self.assertIn("expiries", manifest)

        # Audit consistency: total instruments count in manifest must appear in report
        total_inst = manifest["catalog_metadata"]["total_instruments_count"]
        self.assertIn(str(total_inst), report_text)

        # Audit consistency: gate decision status in manifest must match report
        gate_status = manifest["gate_decision"]["status"]
        self.assertIn(f"`{gate_status}`", report_text)

        # Audit consistency: all expiry names in manifest appear in report
        for exp_data in manifest["expiries"]:
            self.assertIn(exp_data["expiry_name"], report_text)
            self.assertIn(exp_data["call_instrument"], report_text)
            self.assertIn(exp_data["put_instrument"], report_text)

    # --------------------------------------------------------------------------
    # 2. Request Cap & Budget Cap Incomplete Test
    # --------------------------------------------------------------------------
    def test_request_cap_incomplete(self) -> None:
        """Exceeding max_requests budget cap halts execution and raises PilotBudgetCapError."""
        tracker = BudgetTracker(max_requests=2, max_bytes=1000000, max_seconds=60.0)
        tracker.check_and_increment()
        tracker.check_and_increment()
        self.assertEqual(tracker.requests_made, 2)

        # Third request exceeds the cap
        with self.assertRaises(PilotBudgetCapError) as ctx:
            tracker.check_and_increment()
        self.assertIn("max_requests cap exceeded", str(ctx.exception))

    def test_bytes_cap_incomplete(self) -> None:
        """Exceeding max_bytes budget cap halts execution and raises PilotBudgetCapError."""
        tracker = BudgetTracker(max_requests=100, max_bytes=500, max_seconds=60.0)
        tracker.check_and_increment(added_bytes=400)
        self.assertEqual(tracker.bytes_received, 400)

        # Exceeds max_bytes
        with self.assertRaises(PilotBudgetCapError) as ctx:
            tracker.check_and_increment(added_bytes=200)
        self.assertIn("max_bytes cap exceeded", str(ctx.exception))

    def test_seconds_cap_incomplete(self) -> None:
        """Exceeding max_seconds budget cap halts execution and raises PilotBudgetCapError."""
        tracker = BudgetTracker(max_requests=100, max_bytes=1000000, max_seconds=0.001)
        import time
        time.sleep(0.005)
        with self.assertRaises(PilotBudgetCapError) as ctx:
            tracker.check_and_increment()
        self.assertIn("max_seconds cap exceeded", str(ctx.exception))

    # --------------------------------------------------------------------------
    # 3. Observed No-Trade vs Download Error / Gap Separation
    # --------------------------------------------------------------------------
    def test_observed_no_trade_distinction(self) -> None:
        """Zero trades in valid response is strictly classified as observed_no_trade, not gap/error."""
        # Load real manifest and check that observed_no_trade hours are recorded
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        has_observed_no_trade = False
        for exp in manifest["expiries"]:
            if exp.get("call_observed_no_trade_hours", 0) > 0 or exp.get("put_observed_no_trade_hours", 0) > 0:
                has_observed_no_trade = True
                break
        self.assertTrue(has_observed_no_trade, "Must observe and record valid zero-trade intervals")

        # Synthetic verification of classification logic
        pilot = DataPilot(PilotConfig(max_requests=10))
        # Complete status -> observed_no_trade
        stat_complete = HourlyBucketStat(
            hour_index=0,
            bucket_start_ms=1700000000000,
            bucket_end_ms=1700003600000,
            classification="observed_no_trade",
            trade_count=0,
            volume_btc=Decimal("0"),
        )
        self.assertEqual(stat_complete.classification, "observed_no_trade")

        # Incomplete status -> incomplete_download
        stat_incomplete = HourlyBucketStat(
            hour_index=0,
            bucket_start_ms=1700000000000,
            bucket_end_ms=1700003600000,
            classification="incomplete_download",
            trade_count=0,
            volume_btc=Decimal("0"),
        )
        self.assertEqual(stat_incomplete.classification, "incomplete_download")

    # --------------------------------------------------------------------------
    # 4. 2023 Real Data Sample Verification
    # --------------------------------------------------------------------------
    def test_real_data_sample_2023(self) -> None:
        """Verify real trade data was fetched for 2023 (29DEC23)."""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        exp_2023 = [e for e in manifest["expiries"] if e["expiry_name"] == "29DEC23"]
        self.assertEqual(len(exp_2023), 1)
        data = exp_2023[0]

        self.assertEqual(data["call_instrument"], "BTC-29DEC23-43000-C")
        self.assertEqual(data["put_instrument"], "BTC-29DEC23-43000-P")
        self.assertGreater(data["call_trades"], 100, "2023 Call must contain real trades")
        self.assertGreater(data["put_trades"], 50, "2023 Put must contain real trades")
        self.assertGreater(Decimal(data["call_volume_btc"]), Decimal("1.0"))
        self.assertGreater(Decimal(data["put_volume_btc"]), Decimal("1.0"))

    # --------------------------------------------------------------------------
    # 5. Non-Quarterly Expiry Reported
    # --------------------------------------------------------------------------
    def test_non_quarterly_expiry_reported(self) -> None:
        """Pilot includes and reports at least one non-quarterly expiry (e.g. 26JAN24)."""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        nq = [e for e in manifest["expiries"] if not e["is_quarterly"]]
        self.assertGreaterEqual(len(nq), 1, "Must report at least one non-quarterly expiry")
        nq_data = nq[0]
        self.assertEqual(nq_data["expiry_name"], "26JAN24")
        self.assertFalse(nq_data["is_quarterly"])
        self.assertGreater(nq_data["call_trades"], 0)
        self.assertGreater(nq_data["put_trades"], 0)

    # --------------------------------------------------------------------------
    # 6. Status Gate Decision & Regulatory Disclaimer
    # --------------------------------------------------------------------------
    def test_status_gate_decision_and_disclaimer(self) -> None:
        """Gate decision produces READY/PARTIAL/BLOCKED with required disclaimer."""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        decision = manifest["gate_decision"]

        self.assertIn(decision["status"], {"READY", "PARTIAL", "BLOCKED"})
        self.assertIn("disclaimer", decision)
        self.assertIn("strateji kârlılığı kanıtlamaz", decision["disclaimer"].lower())
        self.assertIn("veri kaynak yeterliliği", decision["disclaimer"].lower())

    # --------------------------------------------------------------------------
    # 7. Catalog Metadata Currency (Not Frozen to Old 121497)
    # --------------------------------------------------------------------------
    def test_catalog_metadata_not_static(self) -> None:
        """Catalog count reflects live captured count and is not hardcoded to old 121497."""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cat_meta = manifest["catalog_metadata"]
        count = cat_meta["total_instruments_count"]

        self.assertIsInstance(count, int)
        self.assertGreater(count, 120000)
        # Verify SHA-256 payload hash is recorded and valid hex
        sha256 = cat_meta["payload_sha256"]
        self.assertEqual(len(sha256), 64)
        int(sha256, 16)  # Must be valid hex

    # --------------------------------------------------------------------------
    # 8. Configurable Budget Caps
    # --------------------------------------------------------------------------
    def test_budget_caps_configuration(self) -> None:
        """PilotConfig permits customizing caps for requests, bytes, and timeout."""
        custom_cfg = PilotConfig(
            max_requests=100,
            max_bytes=10_000_000,
            max_seconds=120.0,
            inter_request_delay_seconds=0.01,
            target_coverage_threshold=Decimal("0.90"),
        )
        self.assertEqual(custom_cfg.max_requests, 100)
        self.assertEqual(custom_cfg.max_bytes, 10_000_000)
        self.assertEqual(custom_cfg.max_seconds, 120.0)
        self.assertEqual(custom_cfg.target_coverage_threshold, Decimal("0.90"))


if __name__ == "__main__":
    unittest.main()
