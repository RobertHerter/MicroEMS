import json
import sqlite3
import sys
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import build_forecast_quality  # noqa: E402


class DispatchQualityTest(unittest.TestCase):
    def test_compares_midnight_dispatch_against_grid_and_soc_actuals(self):
        timezone_name = "Europe/Berlin"
        tz = ZoneInfo(timezone_name)
        utc = ZoneInfo("UTC")
        day = datetime(2026, 8, 5, tzinfo=tz)
        quarters = [day + timedelta(minutes=15 * index) for index in range(96)]
        hours = [day + timedelta(hours=index) for index in range(24)]

        micro_payload = zlib.compress(json.dumps({
            "generated": (day + timedelta(seconds=10)).isoformat(),
            "inputs": {"index": [stamp.isoformat() for stamp in quarters]},
            "plan": {
                "grid_import_w": [1100.0] * 96,
                "grid_export_w": [0.0] * 96,
                # MicroEMS stores end-of-slot SoC; the evaluator must shift it.
                "house_soc_percent": [51.0] * 96,
            },
        }).encode())

        def eos_payload(grid_wh, soc_pct):
            return zlib.compress(json.dumps({
                "timestamps": [stamp.astimezone(utc).isoformat() for stamp in hours],
                "grid_import_wh": [grid_wh] * 24,
                "grid_export_wh": [0.0] * 24,
                "soc_pct": [soc_pct] * 24,
            }).encode())

        with tempfile.TemporaryDirectory() as directory:
            micro_db = Path(directory) / "micro.sqlite"
            eos_db = Path(directory) / "eos.sqlite"
            connection = sqlite3.connect(micro_db)
            connection.execute(
                "CREATE TABLE actuals (ts TEXT PRIMARY KEY, pv_w REAL, house_w REAL, "
                "grid_w REAL, battery_w REAL, soc REAL)"
            )
            connection.execute(
                "CREATE TABLE debug_snapshot "
                "(id INTEGER PRIMARY KEY, generated TEXT, status TEXT, payload BLOB)"
            )
            connection.executemany(
                "INSERT INTO actuals VALUES(?,?,?,?,?,?)",
                [(stamp.astimezone(utc).isoformat(), 0, 0, 1000.0, 0, 50.0)
                 for stamp in quarters],
            )
            connection.execute(
                "INSERT INTO debug_snapshot VALUES(?,?,?,?)",
                (1, (day + timedelta(seconds=10)).isoformat(), "Optimal", micro_payload),
            )
            connection.commit()
            connection.close()

            connection = sqlite3.connect(eos_db)
            connection.execute(
                "CREATE TABLE forecast_snapshots "
                "(system_id TEXT, issue_ts TEXT, payload BLOB, "
                "PRIMARY KEY(system_id, issue_ts))"
            )
            connection.executemany(
                "INSERT INTO forecast_snapshots VALUES(?,?,?)",
                [
                    ("eos_import", (day + timedelta(minutes=2)).isoformat(),
                     eos_payload(1200.0, 52.0)),
                    ("eos_native", (day + timedelta(minutes=3)).isoformat(),
                     eos_payload(1300.0, 53.0)),
                ],
            )
            connection.commit()
            connection.close()

            quality = build_forecast_quality(
                micro_db=micro_db,
                eos_archive_db=eos_db,
                timezone_name=timezone_name,
                now=datetime(2026, 8, 6, 12, tzinfo=tz),
                lookback_days=7,
                grid_reference_w=1000.0,
            )

        systems = {item["id"]: item for item in quality["systems"]}
        self.assertEqual(systems["microems"]["gridImportMaeKw"], 0.1)
        self.assertEqual(systems["microems"]["socMaePp"], 1.0)
        self.assertEqual(systems["microems"]["distanceScorePct"], 5.5)
        self.assertEqual(systems["eos_import"]["distanceScorePct"], 11.0)
        self.assertEqual(systems["eos_native"]["distanceScorePct"], 16.5)
        self.assertEqual(quality["winnerIds"], ["microems"])
        self.assertEqual(quality["commonDays"], 1)
        self.assertEqual(quality["days"][0]["actualImportKwh"], 24.0)
        self.assertEqual(quality["days"][0]["systems"]["microems"]["hours"], 23)

    def test_never_ranks_systems_across_different_days(self):
        timezone_name = "Europe/Berlin"
        tz = ZoneInfo(timezone_name)
        day = datetime(2026, 8, 5, tzinfo=tz)
        quarters = [day + timedelta(minutes=15 * index) for index in range(96)]
        payload = zlib.compress(json.dumps({
            "inputs": {"index": [stamp.isoformat() for stamp in quarters]},
            "plan": {
                "grid_import_w": [1000.0] * 96,
                "grid_export_w": [0.0] * 96,
                "house_soc_percent": [50.0] * 96,
            },
        }).encode())

        with tempfile.TemporaryDirectory() as directory:
            micro_db = Path(directory) / "micro.sqlite"
            connection = sqlite3.connect(micro_db)
            connection.execute(
                "CREATE TABLE actuals (ts TEXT PRIMARY KEY, pv_w REAL, house_w REAL, "
                "grid_w REAL, battery_w REAL, soc REAL)"
            )
            connection.execute(
                "CREATE TABLE debug_snapshot "
                "(id INTEGER PRIMARY KEY, generated TEXT, status TEXT, payload BLOB)"
            )
            connection.executemany(
                "INSERT INTO actuals VALUES(?,?,?,?,?,?)",
                [(stamp.astimezone(ZoneInfo('UTC')).isoformat(), 0, 0, 1000, 0, 50)
                 for stamp in quarters],
            )
            connection.execute(
                "INSERT INTO debug_snapshot VALUES(?,?,?,?)",
                (1, day.isoformat(), "Optimal", payload),
            )
            connection.commit()
            connection.close()

            quality = build_forecast_quality(
                micro_db=micro_db,
                eos_archive_db=Path(directory) / "missing.sqlite",
                timezone_name=timezone_name,
                now=datetime(2026, 8, 6, 12, tzinfo=tz),
                lookback_days=7,
            )

        self.assertEqual(quality["commonDays"], 0)
        self.assertEqual(quality["winnerIds"], [])
        self.assertFalse(quality["comparisonReady"])
        self.assertEqual(quality["systems"][0]["capturedDays"], 1)
        self.assertIsNone(quality["systems"][0]["distanceScorePct"])


if __name__ == "__main__":
    unittest.main()
