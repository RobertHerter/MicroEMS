import importlib.util
import json
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "e3dc_to_eos.py"
SPEC = importlib.util.spec_from_file_location("e3dc_to_eos", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class OptimizationRefreshTest(unittest.TestCase):
    def test_waits_for_new_solution_and_always_restores_disabled_mode(self):
        calls = []
        solutions = iter([
            {"generated_at": "old"},
            {"generated_at": "old"},
            {"generated_at": "new"},
        ])

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            if method == "GET":
                return next(solutions)
            return {}

        generated = MODULE.run_optimization(
            "http://eos",
            timeout=10,
            poll_interval=0,
            request_fn=request,
            sleep_fn=lambda _: None,
        )

        self.assertEqual(generated, "new")
        self.assertIn(("PUT", "/v1/config/ems/mode", "OPTIMIZATION"), calls)
        self.assertEqual(calls[-3:], [
            ("PUT", "/v1/config/ems/mode", "DISABLED"),
            ("PUT", "/v1/config/ems/interval", 900.0),
            ("PUT", "/v1/config/file", None),
        ])


class ForecastArchiveTest(unittest.TestCase):
    def test_archives_compact_prediction_vintage_idempotently(self):
        solution = {
            "generated_at": "2026-08-06T00:03:00+02:00",
            "prediction": {"data": {
                "2026-08-05T22:00:00Z": {
                    "pvforecast_ac_energy_wh": 1500.0,
                    "loadforecast_energy_wh": 700.0,
                    "elec_price_amt_kwh": 0.12,
                },
                "2026-08-05T23:00:00Z": {
                    "pvforecast_ac_energy_wh": 0.0,
                    "loadforecast_energy_wh": 650.0,
                    "elec_price_amt_kwh": 0.10,
                },
            }},
            "solution": {"data": {
                "2026-08-05T22:00:00Z": {
                    "grid_consumption_energy_wh": 100.0,
                    "grid_feedin_energy_wh": 300.0,
                    "battery1_soc_factor": 0.55,
                },
                "2026-08-05T23:00:00Z": {
                    "grid_consumption_energy_wh": 400.0,
                    "grid_feedin_energy_wh": 0.0,
                    "battery1_soc_factor": 0.50,
                },
            }},
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forecast.sqlite"
            first = MODULE.archive_solution_forecast(
                database, "eos_native", solution
            )
            second = MODULE.archive_solution_forecast(
                database, "eos_native", solution
            )

            connection = sqlite3.connect(database)
            rows = connection.execute(
                "SELECT system_id, issue_ts, payload FROM forecast_snapshots"
            ).fetchall()
            connection.close()

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:2], (
            "eos_native", "2026-08-05T22:03:00+00:00"
        ))
        payload = json.loads(zlib.decompress(rows[0][2]))
        self.assertEqual(payload["timestamps"], [
            "2026-08-05T22:00:00+00:00", "2026-08-05T23:00:00+00:00"
        ])
        self.assertEqual(payload["pv_wh"], [1500.0, 0.0])
        self.assertEqual(payload["load_wh"], [700.0, 650.0])
        self.assertEqual(payload["grid_net_wh"], [-200.0, 400.0])
        self.assertEqual(payload["grid_import_wh"], [100.0, 400.0])
        self.assertEqual(payload["grid_export_wh"], [300.0, 0.0])
        self.assertEqual(payload["soc_pct"], [55.0, 50.0])

    def test_timeout_still_restores_disabled_mode(self):
        calls = []
        ticks = iter([0.0, 0.0, 20.0])

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            if method == "GET":
                return {"generated_at": "old"}
            return {}

        with self.assertRaises(TimeoutError):
            MODULE.run_optimization(
                "http://eos",
                timeout=10,
                poll_interval=0,
                request_fn=request,
                sleep_fn=lambda _: None,
                monotonic_fn=lambda: next(ticks),
            )

        self.assertEqual(calls[-3:], [
            ("PUT", "/v1/config/ems/mode", "DISABLED"),
            ("PUT", "/v1/config/ems/interval", 900.0),
            ("PUT", "/v1/config/file", None),
        ])


class EnsureIdleTest(unittest.TestCase):
    def _request(self, state, calls):
        def request(method, path, payload=None):
            calls.append((method, path, payload))
            if method == "GET" and path == "/v1/config/ems":
                return {"mode": state["mode"], "interval": state["interval"]}
            if method == "GET" and path == "/v1/config/pvforecast/provider":
                return state["provider"]
            return {}
        return request

    def test_idle_instance_is_left_untouched(self):
        calls = []
        state = {"mode": "DISABLED", "interval": 900.0, "provider": "PVForecastImport"}
        repaired = MODULE.ensure_idle(
            "http://eos", request_fn=self._request(state, calls),
            pv_provider="PVForecastImport")
        self.assertEqual(repaired, [])
        self.assertFalse([c for c in calls if c[0] == "PUT"])

    def test_leftover_optimization_mode_and_provider_are_repaired_and_saved(self):
        calls = []
        state = {"mode": "OPTIMIZATION", "interval": 60.0,
                 "provider": "PVForecastAkkudoktor"}
        repaired = MODULE.ensure_idle(
            "http://eos", request_fn=self._request(state, calls),
            pv_provider="PVForecastImport")
        self.assertEqual(len(repaired), 3)
        puts = [c for c in calls if c[0] == "PUT"]
        self.assertEqual(puts, [
            ("PUT", "/v1/config/ems/mode", "DISABLED"),
            ("PUT", "/v1/config/ems/interval", 900.0),
            ("PUT", "/v1/config/pvforecast/provider", "PVForecastImport"),
            ("PUT", "/v1/config/file", None),
        ])

    def test_native_instance_keeps_its_own_provider(self):
        calls = []
        state = {"mode": "OPTIMIZATION", "interval": 60.0,
                 "provider": "PVForecastForecastSolar"}
        MODULE.ensure_idle("http://eos", request_fn=self._request(state, calls))
        self.assertNotIn(
            "/v1/config/pvforecast/provider", [c[1] for c in calls])


if __name__ == "__main__":
    unittest.main()
