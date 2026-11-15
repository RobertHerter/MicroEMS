"""InfluxDB 2.x: read_slots muss wie v1 auf den Slot-ANFANG stempeln.

Flux' ``aggregateWindow`` schreibt ohne ``timeSrc`` das Fenster-ENDE in
``_time`` (Vorgabe ``timeSrc: "_stop"``, siehe
https://docs.influxdata.com/flux/v0/stdlib/universe/aggregatewindow/).
InfluxQL (``GROUP BY time``) und der gesamte uebrige Code rechnen mit dem
Slot-Anfang - auf v2-Anlagen lagen alle Reihen einen Slot zu spaet.

Der Fake unten bildet genau dieses Verhalten nach: er liest ``timeSrc`` aus
dem Flux-Text und stempelt ohne die Angabe auf ``_stop``. Der Test faellt
also durch, sobald ``timeSrc: "_start"`` aus der Abfrage verschwindet.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pandas as pd

from ems.config import SignalSpec
from ems.influx import InfluxRepository, InfluxV2Backend

SLOT = 900


class FakeFluxQueryApi:
    """Rechnet range() + aggregateWindow(mean) wie Flux: halboffene Fenster
    auf dem Epochenraster, Randfenster auf die range()-Grenzen beschnitten,
    Stempel aus ``timeSrc`` (Vorgabe ``_stop``)."""

    def __init__(self, punkte: pd.Series):
        self.punkte = punkte
        self.flux = None

    def query_data_frame(self, flux, org=None):
        self.flux = flux
        m = re.search(r"range\(start: (\S+), stop: (\S+)\)", flux)
        start, stop = pd.Timestamp(m.group(1)), pd.Timestamp(m.group(2))
        every = pd.Timedelta(seconds=int(
            re.search(r"aggregateWindow\(every: (\d+)s", flux).group(1)))
        src = re.search(r'timeSrc: "(_start|_stop)"', flux)
        src = src.group(1) if src else "_stop"
        zeilen = []
        w_start = start.floor(every)
        while w_start < stop:
            w_stop = w_start + every
            lo, hi = max(w_start, start), min(w_stop, stop)
            werte = self.punkte[(self.punkte.index >= lo) & (self.punkte.index < hi)]
            if len(werte):                           # createEmpty: false
                zeilen.append({"_time": lo if src == "_start" else hi,
                               "_value": float(werte.mean())})
            w_start = w_stop
        return pd.DataFrame(zeilen)


def _backend(punkte: pd.Series):
    backend = InfluxV2Backend.__new__(InfluxV2Backend)   # ohne echten Client
    backend.bucket, backend.org = "ems", "haus"
    backend._query_api = FakeFluxQueryApi(punkte)
    return backend


def _punkte():
    # Minutenwerte 12:00-12:44 UTC: Slot 12:00 = 100, 12:15 = 200, 12:30 = 300
    idx = pd.date_range("2026-06-01 12:00", periods=45, freq="1min", tz="UTC")
    return pd.Series([100.0] * 15 + [200.0] * 15 + [300.0] * 15, index=idx)


SPEC = SignalSpec(measurement="power", field="value")


def test_flux_query_stamps_the_window_start():
    backend = _backend(_punkte())
    backend.read_slots(SPEC, pd.Timestamp("2026-06-01 12:00", tz="UTC"),
                       pd.Timestamp("2026-06-01 12:45", tz="UTC"), SLOT)
    assert 'timeSrc: "_start"' in backend._query_api.flux
    assert "aggregateWindow(every: 900s, fn: mean" in backend._query_api.flux


def test_v2_series_sits_on_slot_start():
    backend = _backend(_punkte())
    s = backend.read_slots(SPEC, pd.Timestamp("2026-06-01 12:00", tz="UTC"),
                           pd.Timestamp("2026-06-01 12:45", tz="UTC"), SLOT)
    assert list(s.index) == list(
        pd.date_range("2026-06-01 12:00", periods=3, freq="15min", tz="UTC"))
    assert list(s.values) == [100.0, 200.0, 300.0]


def test_v2_start_off_grid_is_stamped_on_the_grid_like_v1():
    # Flux beschneidet das erste Fenster auf 12:05; InfluxQL stempelt es auf
    # 12:00. Der Stempel muss auf dem Raster liegen, sonst faellt der Slot
    # beim Reindex im Repository heraus.
    backend = _backend(_punkte())
    s = backend.read_slots(SPEC, pd.Timestamp("2026-06-01 12:05", tz="UTC"),
                           pd.Timestamp("2026-06-01 12:45", tz="UTC"), SLOT)
    assert list(s.index) == list(
        pd.date_range("2026-06-01 12:00", periods=3, freq="15min", tz="UTC"))


def test_repository_returns_v2_values_in_their_own_slot():
    repo = InfluxRepository.__new__(InfluxRepository)
    repo.config = SimpleNamespace(
        general=SimpleNamespace(slot_minutes=15),
        influxdb=SimpleNamespace(signals={"load": SPEC}))
    repo.tz = "Europe/Berlin"
    repo.slot_seconds = SLOT
    repo.backend = _backend(_punkte())
    s = repo.read_slots("load", pd.Timestamp("2026-06-01 12:00", tz="UTC"),
                        pd.Timestamp("2026-06-01 12:45", tz="UTC"), fill=False)
    assert s.loc[pd.Timestamp("2026-06-01 14:00", tz="Europe/Berlin")] == 100.0
    assert s.loc[pd.Timestamp("2026-06-01 14:30", tz="Europe/Berlin")] == 300.0
    assert s.notna().all()
