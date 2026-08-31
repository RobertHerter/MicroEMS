"""Regimegleiches Zweitfenster der Intraday-Korrektur.

Das 3-h-Fenster ist regimeblind: um 05:45 misst es die ruhige Nacht und traegt
den Faktor in die Morgenrampe. Gemessen am 31.08.2026 im ausgelieferten Stand,
Werktag 06:00 - real 1585 W, Mitternachtsprognose 1610 W, zeitnaher Stand
1212 W. Das Zweitfenster (dieselbe Stunde der Vortage) sagt fuer 07-09 Uhr
x1,23 bis x1,25, wo das 3-h-Fenster x0,67 sagt.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems.forecast import intraday_factor_series
from ems.local_history import read_intraday_hour_ratios, write_intraday_diagnostic

TZ = "Europe/Berlin"


def test_without_a_second_window_nothing_changes():
    """Auslieferungsstand (sameslot_days = 0) muss bitgleich bleiben."""
    idx = pd.date_range("2026-08-31 06:00", periods=8, freq="15min", tz=TZ)
    jetzt = idx[0]
    alt = intraday_factor_series(0.8, idx, jetzt, 3.0)
    neu = intraday_factor_series(0.8, idx, jetzt, 3.0, per_hour=None,
                                 timezone=TZ)
    assert np.allclose(alt.values, neu.values)


def test_the_hour_window_lifts_the_morning_the_short_window_pushes_down():
    """Der Fall, um den es geht: Nachtfaktor gegen Morgenstunde."""
    idx = pd.date_range("2026-08-31 06:00", periods=4, freq="15min", tz=TZ)
    jetzt = idx[0]
    nur_kurz = intraday_factor_series(0.67, idx, jetzt, 3.0)
    gemischt = intraday_factor_series(0.67, idx, jetzt, 3.0,
                                      per_hour={6: 1.25}, timezone=TZ)
    # Geometrisches Mittel: bei lead 0 genau sqrt(0.67 * 1.25).
    assert gemischt.iloc[0] == pytest.approx(np.sqrt(0.67 * 1.25), abs=1e-6)
    assert gemischt.iloc[0] > nur_kurz.iloc[0], "Morgen muss angehoben werden"
    # Und es bleibt eine Daempfung gegenueber gar keiner Korrektur.
    assert gemischt.iloc[0] < 1.0


def test_the_hour_part_survives_the_decay_of_the_moment_part():
    """Der Vortagesanteil ist ein Stundenprofil, keine Momentaufnahme."""
    idx = pd.date_range("2026-08-31 06:00", periods=2, freq="15min", tz=TZ)
    weit = idx + pd.Timedelta(hours=24)
    jetzt = idx[0]
    nah = intraday_factor_series(0.5, idx, jetzt, 3.0, per_hour={6: 1.44},
                                 timezone=TZ)
    fern = intraday_factor_series(0.5, weit, jetzt, 3.0, per_hour={6: 1.44},
                                  timezone=TZ)
    # Nach 24 h ist der Momentanteil abgeklungen -> Wurzel des Stundenanteils.
    assert fern.iloc[0] == pytest.approx(np.sqrt(1.44), abs=0.01)
    assert nah.iloc[0] < fern.iloc[0]


def test_hours_without_enough_pairs_stay_neutral():
    idx = pd.date_range("2026-08-31 06:00", periods=4, freq="15min", tz=TZ)
    f = intraday_factor_series(1.0, idx, idx[0], 3.0, per_hour={9: 1.5},
                               timezone=TZ)
    assert np.allclose(f.values, 1.0), "fremde Stunde darf nicht wirken"


def test_hour_ratios_come_from_the_archived_windows(tmp_path):
    """Quelle sind die schon vorhandenen Intraday-Fenster - keine neu
    gerechneten Prognosen."""
    db = str(tmp_path / "hist.sqlite")
    jetzt = pd.Timestamp("2026-08-31 12:00", tz="UTC")
    # Drei Vortage, 07 Uhr lokal: Ist 25 % ueber der Prognose.
    for tag in range(1, 4):
        basis = (jetzt - pd.Timedelta(days=tag)).tz_convert(TZ).normalize() \
            + pd.Timedelta(hours=7)
        beob = pd.DataFrame(
            {"a": [1250.0] * 4, "p": [1000.0] * 4, "eligible": [1] * 4},
            index=pd.date_range(basis, periods=4, freq="15min", tz=TZ))
        write_intraday_diagnostic(db, basis, "load", basis,
                                  {"observations": beob}, 1.0)
    r = read_intraday_hour_ratios(db, "load", jetzt, 7.0, TZ, max_factor=1.5)
    assert r.get(7) == pytest.approx(1.25, abs=0.01)
    assert 6 not in r, "Stunden ohne Paare fehlen, statt 1.0 zu behaupten"
    # Kappung greift wie beim 3-h-Fenster.
    assert all(1.0 / 1.5 - 1e-9 <= v <= 1.5 + 1e-9 for v in r.values())
