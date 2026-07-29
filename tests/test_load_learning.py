"""Lastprofil verschiebbarer Lasten aus der Rueckmeldung anlernen
(ems/load_learning.py). Der Optimierer kann das Profil laengst fahren
(ControllableLoad.power_profile_w) - gelernt wurde es bisher nicht.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems.load_learning import (APPLY_MIN_RUNS, detect_runs, learn_profile,
                               maybe_apply)
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"
DT = 0.25
# Waschgang: Heizphase, Waschen, Schleudern - genau die Form, die eine
# konstante Leistung nicht abbilden kann.
CYCLE = [2000.0, 2000.0, 300.0, 200.0, 200.0, 200.0, 500.0, 300.0]


def _series(starts, cycle=CYCLE, days=4, noise=0.0, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-07-01", periods=96 * days, freq="15min", tz=TZ)
    v = np.zeros(len(idx))
    for day, slot in enumerate(starts):
        c = np.array(cycle, dtype=float)
        if noise:
            c = c * (1.0 + rng.normal(0.0, noise, len(c)))
        v[day * 96 + slot:day * 96 + slot + len(c)] = c
    return pd.Series(v, index=idx)


def test_runs_are_detected_and_the_profile_is_recovered():
    profile = learn_profile(_series([30, 40, 50]), DT)
    assert profile is not None
    assert profile.n_runs == 3
    assert profile.power_w == pytest.approx(CYCLE)
    assert profile.runtime_minutes == pytest.approx(120.0)
    assert profile.energy_kwh == pytest.approx(1.425, abs=0.01)
    assert profile.usable is True


def test_short_dips_inside_a_run_do_not_split_it():
    """Zwischen Waschen und Schleudern faellt die Leistung kurz auf 0 - das ist
    EIN Lauf, nicht zwei."""
    cycle = [2000.0, 2000.0, 0.0, 0.0, 500.0, 300.0]
    runs = detect_runs(_series([30], cycle=cycle))
    assert len(runs) == 1 and runs[0].slots == 6


def test_a_long_gap_starts_a_new_run():
    idx = pd.date_range("2026-07-01", periods=40, freq="15min", tz=TZ)
    v = np.zeros(40)
    v[2:6] = 1500.0
    v[20:24] = 1500.0          # deutlich spaeter -> eigener Lauf
    assert len(detect_runs(pd.Series(v, index=idx))) == 2


def test_standby_is_not_a_run():
    """Uhr und Display ziehen dauerhaft ein paar Watt - kein Waschgang."""
    idx = pd.date_range("2026-07-01", periods=96, freq="15min", tz=TZ)
    assert detect_runs(pd.Series(np.full(96, 8.0), index=idx)) == []


def test_median_ignores_an_aborted_run():
    """Ein abgebrochener Lauf darf das Profil nicht verziehen - deshalb Median
    je Slotposition statt Mittelwert."""
    s = _series([30, 40, 50])
    s.iloc[3 * 96 + 10:3 * 96 + 12] = 2000.0      # kurzer Fehlstart am 4. Tag
    profile = learn_profile(s, DT)
    assert profile.power_w == pytest.approx(CYCLE)


def test_noisy_runs_still_give_a_sane_profile():
    profile = learn_profile(_series([30, 40, 50, 60], noise=0.08), DT)
    assert profile.n_runs == 4
    assert profile.power_w[0] == pytest.approx(2000.0, rel=0.15)
    assert profile.energy_kwh == pytest.approx(1.425, rel=0.15)


def test_empty_input():
    assert learn_profile(pd.Series(dtype="float64"), DT) is None


# --------------------------------------------------------------------------- #
# Uebernahme
# --------------------------------------------------------------------------- #
def _load(name="Waschmaschine"):
    cfg = make_config()
    for ld in cfg.controllable_loads:
        if ld.type == "deferrable":
            return ld
    from ems.config import ControllableLoad
    return ControllableLoad(name=name, type="deferrable", enabled=True)


def test_apply_writes_profile_and_runtime(tmp_path, monkeypatch):
    written = {}
    monkeypatch.setattr("ems.config.save_override",
                        lambda p, k, v: written.update({k: v}))
    profile = learn_profile(_series([30, 40, 50]), DT)
    out = maybe_apply(profile, _load(), str(tmp_path / "config.yaml"))
    assert out is not None
    keys = sorted(written)
    assert any(k.endswith("power_profile_w") for k in keys)
    assert any(k.endswith("runtime_minutes") for k in keys)
    assert written[[k for k in keys if k.endswith("power_profile_w")][0]] \
        == pytest.approx(CYCLE)


def test_apply_refuses_a_single_run(tmp_path, monkeypatch):
    """Ein einzelner Lauf ist kein Profil - erst ab mehreren uebernehmen."""
    monkeypatch.setattr("ems.config.save_override",
                        lambda *a, **k: pytest.fail("darf nicht schreiben"))
    profile = learn_profile(_series([30]), DT)
    assert profile.n_runs < APPLY_MIN_RUNS
    assert maybe_apply(profile, _load(), str(tmp_path / "config.yaml")) is None
