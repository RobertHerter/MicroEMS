"""Tests für das Ersparnis-Tracking (Ist vs. Ohne-EMS-Baseline)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ems.savings import SavingsTracker
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
FREQ = "15min"
NOW = pd.Timestamp("2026-05-01 12:00", tz=TZ)


class FakeRepo:
    """Repo-Stub: konstante Signale, kein InfluxDB nötig."""

    def __init__(self, pv=0.0, load=1000.0, grid=1000.0, price=30.0, soc_pct=50.0):
        self.vals = {"pv_generation": pv, "house_consumption": load,
                     "grid_power": grid, "electricity_price": price}
        self.soc_pct = soc_pct
        self.written = None

    def signal_available(self, name):
        return True

    def read_scalar_latest(self, name, start, end):
        return self.soc_pct

    def read_slots(self, name, start, end, fill=True):
        idx = pd.date_range(start, end, freq=FREQ, tz=TZ, inclusive="left")
        return pd.Series(float(self.vals[name]), index=idx)

    def write_frame(self, key, df):
        self.written = (key, df)


def _tracker(tmp_path):
    cfg = make_config()
    cfg.savings.state_path = str(tmp_path / "savings_state.json")
    return cfg, SavingsTracker(cfg)


def test_init_creates_state_and_returns_zero(tmp_path):
    cfg, tracker = _tracker(tmp_path)
    repo = FakeRepo()
    assert tracker.update(repo, NOW) == 0.0
    assert (tmp_path / "savings_state.json").exists()


def test_savings_math(tmp_path):
    """Nachts, Akku halb voll: Baseline entlädt (0 ct), Ist bezog aus dem Netz
    (weil das EMS z.B. den Akku für die Morgenspitze schont) -> 'Ersparnis'
    ist negativ und exakt die Ist-Kosten."""
    cfg, tracker = _tracker(tmp_path)
    repo = FakeRepo(pv=0.0, load=1000.0, grid=1000.0, price=30.0, soc_pct=50.0)
    tracker.update(repo, NOW)                       # Initialisierung
    later = NOW + pd.Timedelta(hours=1)             # 4 Slots à 15 min
    cum = tracker.update(repo, later)
    # Ist: 1000 W * 30 ct/kWh * 0.25 h = 7.5 ct je Slot, 4 Slots = 30 ct.
    # Baseline: Akku (50 %) deckt die Last -> 0 ct. Differenz = -0.30 EUR.
    assert abs(cum - (-0.30)) < 1e-6
    key, df = repo.written
    assert key == "savings" and len(df) == 4
    assert abs(float(df["cum_saved_eur"].iloc[-1]) - (-0.30)) < 1e-6
    # Baseline-SoC ist gesunken (Akku hat 4 Slots die Last gedeckt)
    assert float(df["baseline_soc_percent"].iloc[-1]) < 50.0


def test_export_valued_with_feedin(tmp_path):
    """PV-Überschuss, Akku (Baseline) voll: Baseline speist ein (Erlös).
    Ist-Einspeisung (grid < 0) wird ebenso mit der Vergütung bewertet."""
    cfg, tracker = _tracker(tmp_path)
    repo = FakeRepo(pv=3000.0, load=1000.0, grid=-2000.0, price=30.0,
                    soc_pct=100.0)
    tracker.update(repo, NOW)
    cum = tracker.update(repo, NOW + pd.Timedelta(hours=1))
    # Baseline: Akku voll -> 2000 W Export * 8 ct = 4 ct/Slot Erlös.
    # Ist: ebenfalls 2000 W Export -> identisch, Ersparnis 0.
    assert abs(cum) < 1e-6


def test_data_gaps_are_skipped(tmp_path):
    cfg, tracker = _tracker(tmp_path)

    class GappyRepo(FakeRepo):
        def read_slots(self, name, start, end, fill=True):
            s = super().read_slots(name, start, end, fill)
            if name == "grid_power":
                s.iloc[:2] = np.nan     # 2 Slots ohne Messwert
            return s

    repo = GappyRepo()
    tracker.update(repo, NOW)
    cum = tracker.update(repo, NOW + pd.Timedelta(hours=1))
    key, df = repo.written
    assert len(df) == 2                 # nur vollständige Slots verrechnet
    assert abs(cum - (-0.15)) < 1e-6    # 2 Slots à 7.5 ct
