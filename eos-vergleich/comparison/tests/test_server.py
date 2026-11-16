import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import eos_genes, eos_intent, micro_intent  # noqa: E402


class MicroIntentTest(unittest.TestCase):
    def test_idle_automatic_inverter_control_is_not_called_hold(self):
        plan = {
            "batt_ac_charge_w": [0.0],
            "batt_dc_charge_w": [0.0],
            "batt_discharge_w": [0.0],
            "batt_charge_limit_w": [12480.0],
            "batt_discharge_limit_w": [12120.0],
            "charge_limited": [0.0],
            "discharge_limited": [0.0],
        }

        self.assertEqual(micro_intent(plan, 0, mode="auto"), "Automatik")

    def test_real_discharge_hold_is_named_as_a_discharge_block(self):
        plan = {
            "batt_ac_charge_w": [0.0],
            "batt_dc_charge_w": [0.0],
            "batt_discharge_w": [0.0],
            "batt_charge_limit_w": [12480.0],
            "batt_discharge_limit_w": [0.0],
            "charge_limited": [0.0],
            "discharge_limited": [1.0],
        }

        self.assertEqual(
            micro_intent(plan, 0, mode="hold"), "Entladen gesperrt"
        )


class EosIntentTimeMappingTest(unittest.TestCase):
    def test_labels_active_pv_charging_before_discharge_block(self):
        genetic = {
            "start_hour": 7,
            "ac_charge": [0.0] * 72,
            "dc_charge": [1.0] * 72,
            "discharge_allowed": [0] * 72,
        }

        self.assertEqual(
            eos_intent({}, genetic, 0, battery_kw=3.09), "PV-Laden"
        )

    def test_labels_active_discharge_before_discharge_permission(self):
        genetic = {
            "start_hour": 7,
            "ac_charge": [0.0] * 72,
            "dc_charge": [1.0] * 72,
            "discharge_allowed": [1] * 72,
        }

        self.assertEqual(
            eos_intent({}, genetic, 0, battery_kw=-1.25), "Entladen"
        )

    def test_uses_simulation_start_hour_instead_of_shifted_solution_columns(self):
        genetic = {
            "start_hour": 7,
            "ac_charge": [0.0] * 72,
            "dc_charge": [1.0] * 72,
            "discharge_allowed": [1] * 72,
        }
        # Row 38 was simulated with gene 45. The general solution currently
        # serializes local-time gene 47 into this row instead.
        genetic["discharge_allowed"][45] = 0
        shifted_row = {
            "genetic_ac_charge_factor": 0.0,
            "genetic_dc_charge_factor": 1.0,
            "genetic_discharge_allowed_factor": 1.0,
        }

        self.assertEqual(eos_genes(shifted_row, genetic, 38), (0.0, 1.0, 0.0))
        self.assertEqual(eos_intent(shifted_row, genetic, 38), "Entladen gesperrt")

    def test_distinguishes_self_consumption_from_pv_only_charging(self):
        genetic = {
            "start_hour": 7,
            "ac_charge": [0.0] * 72,
            "dc_charge": [1.0] * 72,
            "discharge_allowed": [1] * 72,
        }
        row = {}

        self.assertEqual(eos_intent(row, genetic, 0), "Entladen frei")

    def test_does_not_call_ac_command_without_grid_import_netzladen(self):
        genetic = {
            "start_hour": 7,
            "ac_charge": [0.0] * 72,
            "dc_charge": [1.0] * 72,
            "discharge_allowed": [0] * 72,
        }
        genetic["ac_charge"][11] = 0.5

        self.assertEqual(
            eos_intent({}, genetic, 4, grid_import_kw=0.0, grid_export_kw=7.255),
            "AC-Ladebefehl · ohne Bezug",
        )

    def test_names_simultaneous_eos_import_and_export_explicitly(self):
        genetic = {
            "start_hour": 7,
            "ac_charge": [0.0] * 72,
            "dc_charge": [1.0] * 72,
            "discharge_allowed": [0] * 72,
        }
        genetic["ac_charge"][11] = 1.0

        self.assertEqual(
            eos_intent({}, genetic, 4, grid_import_kw=0.160, grid_export_kw=1.231),
            "AC-Laden + Einspeisen",
        )

    def test_calls_effective_positive_net_ac_charge_netzladen(self):
        genetic = {
            "start_hour": 7,
            "ac_charge": [0.0] * 72,
            "dc_charge": [1.0] * 72,
            "discharge_allowed": [0] * 72,
        }
        genetic["ac_charge"][11] = 1.0

        self.assertEqual(
            eos_intent({}, genetic, 4, grid_import_kw=4.0, grid_export_kw=0.5),
            "Netzladen",
        )


if __name__ == "__main__":
    unittest.main()
