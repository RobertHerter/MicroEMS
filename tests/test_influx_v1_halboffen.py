"""InfluxDB 1.x: Fensterabfragen sind halboffen [start, end) wie im v2-Pfad.

Mit ``time <= end`` lieferte ein Punkt genau auf der Slotgrenze ``end`` einen
Zusatz-Bucket bei ``end``. ``read_slots`` schnitt ihn beim Reindex weg,
``read_slots_output`` (Drift-Pruefung) gab ihn roh zurueck - v1 und v2
lieferten dort verschiedene Raster.

Der Fake unten rechnet ``GROUP BY time()`` wie InfluxQL und liest den
Vergleichsoperator am Fensterende aus dem Abfragetext: der Test faellt also
durch, sobald dort wieder ``<=`` steht. ``read_latest`` ("letzter Wert bis
jetzt") muss das Ende dagegen weiter einschliessen.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pandas as pd

from ems.config import SignalSpec
from ems.influx import InfluxRepository, InfluxV1Backend
from tests.test_influx_v2_slotanfang import _backend as _v2_backend

SLOT = 900
SPEC = SignalSpec(measurement="power", field="value")
START = pd.Timestamp("2026-06-01 12:00", tz="UTC")
END = pd.Timestamp("2026-06-01 12:45", tz="UTC")


class FakeInfluxQLClient:
    """Wertet das Zeitfenster und GROUP BY time(Ns) wie InfluxQL aus:
    Buckets auf dem Epochenraster, Stempel = Bucket-Anfang, fill(none)."""

    def __init__(self, punkte: pd.Series):
        # asi8 unten rechnet in ns - die Aufloesung des Index festnageln.
        self.punkte = punkte.set_axis(punkte.index.as_unit("ns"))
        self.queries: list[str] = []

    def query(self, q, epoch=None):
        self.queries.append(q)
        m = re.search(r"time >= (\d+) AND time (<=?) (\d+)", q)
        lo, op, hi = int(m.group(1)), m.group(2), int(m.group(3))
        ns = self.punkte.index.asi8
        maske = (ns >= lo) & ((ns <= hi) if op == "<=" else (ns < hi))
        werte = self.punkte[maske]
        g = re.search(r"GROUP BY time\((\d+)s\)", q)
        if g is None:                                    # SELECT last(...)
            pts = ([{"time": int(werte.index.asi8[-1]),
                     "value": float(werte.iloc[-1])}] if len(werte) else [])
        else:
            schritt = int(g.group(1)) * 10**9
            mittel = werte.groupby(werte.index.asi8 // schritt * schritt).mean()
            pts = [{"time": int(t), "value": float(v)} for t, v in mittel.items()]
        return SimpleNamespace(get_points=lambda: iter(pts))


def _punkte():
    # Minutenwerte 12:00-12:45 UTC EINSCHLIESSLICH eines Punkts genau auf dem
    # Fensterende (12:45 = 999): der gehoert zum naechsten Slot.
    idx = pd.date_range("2026-06-01 12:00", periods=46, freq="1min", tz="UTC")
    return pd.Series([100.0] * 15 + [200.0] * 15 + [300.0] * 15 + [999.0],
                     index=idx)


def _v1_backend(punkte: pd.Series):
    backend = InfluxV1Backend.__new__(InfluxV1Backend)    # ohne echten Client
    backend.database = "ems"
    backend.client = FakeInfluxQLClient(punkte)
    return backend


def test_v1_slot_query_excludes_the_window_end():
    backend = _v1_backend(_punkte())
    backend.read_slots(SPEC, START, END, SLOT)
    q = backend.client.queries[-1]
    assert f"time >= {START.value} AND time < {END.value} " in q
    assert "<=" not in q


def test_v1_and_v2_return_the_same_grid_and_values():
    v1 = _v1_backend(_punkte()).read_slots(SPEC, START, END, SLOT)
    v2 = _v2_backend(_punkte()).read_slots(SPEC, START, END, SLOT)
    assert list(v1.index) == list(
        pd.date_range("2026-06-01 12:00", periods=3, freq="15min", tz="UTC"))
    assert list(v1.index) == list(v2.index)
    assert list(v1.values) == list(v2.values) == [100.0, 200.0, 300.0]


def test_read_slots_output_has_no_bucket_at_the_window_end():
    repo = InfluxRepository.__new__(InfluxRepository)
    repo.config = SimpleNamespace(
        general=SimpleNamespace(slot_minutes=15),
        influxdb=SimpleNamespace(outputs={"predicted_state": "power"}))
    repo.tz = "Europe/Berlin"
    repo.slot_seconds = SLOT
    repo.backend = _v1_backend(_punkte())
    s = repo.read_slots_output("predicted_state", "value", START, END)
    assert len(s) == 3
    assert s.index.max() == pd.Timestamp("2026-06-01 12:30", tz="UTC")


def test_v1_read_latest_still_includes_the_window_end():
    """Skalar "letzter Wert bis jetzt": ein Punkt genau bei `end` zaehlt."""
    backend = _v1_backend(_punkte())
    assert backend.read_latest(SPEC, START, END) == 999.0
    assert f"time <= {END.value}" in backend.client.queries[-1]
