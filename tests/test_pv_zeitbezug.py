"""Zeitbezug der PV-Prognose beim Lesen: Intervallmittel -> Slotraster.

Gespeichert sind Mittelwerte auf dem Intervall-ANFANG (Solcast 30 min, pvlib
60 min). Ein solches Mittel steht fuer die Intervall-MITTE; die Leser muessen
es an der SLOTMITTE auswerten. Linear zwischen den Marken interpoliert lag die
Kurve (L - Slot)/2 zu frueh: 22,5 min bei pvlib, 7,5 min bei Solcast.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems import local_history, pv_eval
from ems.local_history import intervallmittel_auf_slots, quellschritt

TZ = "Europe/Berlin"


def _wahrheit(tag="2026-06-10", gipfel_h=11.6):
    """Minutengenaue Glockenkurve (W) eines Tages in UTC."""
    idx = pd.date_range(f"{tag} 00:00", periods=24 * 60, freq="1min", tz="UTC")
    stunde = (idx.hour + idx.minute / 60.0).to_numpy()
    return pd.Series(6000.0 * np.exp(-0.5 * ((stunde - gipfel_h) / 2.3) ** 2),
                     index=idx)


def _schwerpunkt_min(s: pd.Series) -> float:
    s = s.dropna()
    minuten = np.asarray((s.index - s.index[0]).total_seconds()) / 60.0
    werte = s.to_numpy(dtype=float)
    return float((minuten * werte).sum() / werte.sum())


@pytest.mark.parametrize("quelle_min", [60, 30])
def test_centroid_matches_the_truth(quelle_min):
    """Kern der Korrektur: der Schwerpunkt der Slotreihe liegt dort, wo die
    echte Erzeugung liegt - nicht (L - Slot)/2 davor."""
    wahr = _wahrheit()
    quelle = wahr.resample(f"{quelle_min}min").mean()      # Stempel = Anfang
    soll = wahr.resample("15min").mean()
    ist = intervallmittel_auf_slots(quelle, soll.index, 15)
    assert abs(_schwerpunkt_min(ist) - _schwerpunkt_min(soll)) < 1.5

    # Gegenprobe - so wurde bis 14.11.2026 gelesen: zwischen den STEMPELN
    alt = (quelle.reindex(quelle.index.union(soll.index))
           .interpolate(method="time", limit_area="inside").reindex(soll.index))
    versatz = _schwerpunkt_min(alt) - _schwerpunkt_min(soll)
    assert versatz == pytest.approx(-(quelle_min - 15) / 2.0, abs=1.5)
    # ... und der Fehler der Slotwerte wird deutlich kleiner
    fehler_neu = float((ist - soll).abs().sum())
    fehler_alt = float((alt - soll).abs().sum())
    assert fehler_neu < 0.5 * fehler_alt


@pytest.mark.parametrize("quelle_min", [60, 30])
def test_daily_energy_is_preserved(quelle_min):
    wahr = _wahrheit()
    quelle = wahr.resample(f"{quelle_min}min").mean()
    raster = pd.date_range(wahr.index[0], periods=96, freq="15min")
    ist = intervallmittel_auf_slots(quelle, raster, 15)
    kwh_quelle = float(quelle.sum()) * quelle_min / 60.0 / 1000.0
    kwh_slots = float(ist.sum()) * 0.25 / 1000.0
    assert kwh_slots == pytest.approx(kwh_quelle, rel=0.01)


def test_edges_hold_the_own_interval_and_never_extrapolate():
    quelle = pd.Series([100.0, 300.0], index=pd.to_datetime(
        ["2026-06-10 10:00", "2026-06-10 11:00"], utc=True))
    raster = pd.date_range("2026-06-10 09:30", "2026-06-10 12:30",
                           freq="15min", tz="UTC", inclusive="left")
    s = intervallmittel_auf_slots(quelle, raster, 15)
    assert s[:"2026-06-10 09:45"].isna().all()             # vor dem 1. Intervall
    assert list(s["2026-06-10 10:00":"2026-06-10 10:15"]) == [100.0, 100.0]
    assert s["2026-06-10 10:30"] == pytest.approx(125.0)   # 1/8 des Wegs
    assert s["2026-06-10 11:15"] == pytest.approx(275.0)   # 7/8 des Wegs
    assert list(s["2026-06-10 11:30":"2026-06-10 11:45"]) == [300.0, 300.0]
    assert s["2026-06-10 12:00":].isna().all()             # nach dem letzten
    assert float(s.max()) <= 300.0 and float(s.min()) >= 100.0


def test_no_interpolation_across_a_gap():
    """Fehlt eine Stundenmarke, wird nicht ueber die Luecke hinweg
    interpoliert: die Nachbarn halten ihr Mittel, die Luecke bleibt leer."""
    quelle = pd.Series([100.0, 200.0, 900.0, 1000.0], index=pd.to_datetime(
        ["2026-06-10 08:00", "2026-06-10 09:00",
         "2026-06-10 11:00", "2026-06-10 12:00"], utc=True))
    raster = pd.date_range("2026-06-10 08:00", "2026-06-10 13:00",
                           freq="15min", tz="UTC", inclusive="left")
    s = intervallmittel_auf_slots(quelle, raster, 15)
    assert list(s["2026-06-10 09:30":"2026-06-10 09:45"]) == [200.0, 200.0]
    assert s["2026-06-10 10:00":"2026-06-10 10:45"].isna().all()
    assert list(s["2026-06-10 11:00":"2026-06-10 11:15"]) == [900.0, 900.0]
    # eine fehlende 30-min-Marke (60 min Abstand) wird dagegen ueberbrueckt
    halb = pd.Series([100.0, 200.0, 400.0], index=pd.to_datetime(
        ["2026-06-10 08:00", "2026-06-10 08:30", "2026-06-10 09:30"], utc=True))
    assert quellschritt(halb.index) == pd.Timedelta(minutes=30)
    t = intervallmittel_auf_slots(halb, raster, 15)
    assert t["2026-06-10 08:00":"2026-06-10 09:45"].notna().all()


def test_slot_grid_sources_are_unchanged():
    idx = pd.date_range("2026-06-10 10:00", periods=8, freq="15min", tz="UTC")
    quelle = pd.Series(np.arange(8.0) * 10.0, index=idx)
    pd.testing.assert_series_equal(
        intervallmittel_auf_slots(quelle, idx, 15), quelle, check_freq=False)


@pytest.mark.parametrize("tag", ["2026-10-25", "2026-03-29"])
def test_dst_change_does_not_move_anything(tag):
    """Am Umstellungstag (lokales Raster mit 100 bzw. 92 Slots) kommt dasselbe
    heraus wie auf dem UTC-Raster - gerechnet wird in UTC."""
    wahr = _wahrheit(tag)
    quelle = wahr.resample("60min").mean()
    morgen = (pd.Timestamp(tag) + pd.Timedelta(days=1)).tz_localize(TZ)
    lokal = pd.date_range(pd.Timestamp(tag, tz=TZ), morgen,
                          freq="15min", inclusive="left")
    assert len(lokal) in (92, 100)
    a = intervallmittel_auf_slots(quelle.tz_convert(TZ), lokal, 15)
    b = intervallmittel_auf_slots(quelle, lokal.tz_convert("UTC"), 15)
    np.testing.assert_allclose(a.to_numpy(), b.to_numpy(), equal_nan=True)
    assert a.index.equals(lokal)
    soll = wahr.resample("15min").mean().reindex(lokal.tz_convert("UTC"))
    gueltig = a.notna().to_numpy()
    assert abs(_schwerpunkt_min(a[gueltig])
               - _schwerpunkt_min(soll[gueltig].set_axis(a.index[gueltig]))) < 1.5


# --------------------------------------------------------------------------- #
# Die Leser benutzen den Helfer - alle gleich
# --------------------------------------------------------------------------- #
def _cache(db, quelle, reihe):
    local_history.write_pv_forecast(
        db, quelle, {ts.isoformat(): (float(v), 0.7 * float(v), 1.2 * float(v))
                     for ts, v in reihe.items()})


def _archiv(db, quelle, issue, reihe):
    local_history.write_pv_forecast_archive(
        db, quelle, issue,
        {ts.isoformat(): (float(v), 0.7 * float(v), 1.2 * float(v))
         for ts, v in reihe.items()})


def test_read_pv_forecast_reads_centre_to_centre(tmp_path):
    db = str(tmp_path / "h.sqlite")
    wahr = _wahrheit()
    _cache(db, "pvmodel:Ost", wahr.resample("60min").mean())
    start = wahr.index[0].tz_convert(TZ)
    s = local_history.read_pv_forecast(
        db, start, start + pd.Timedelta(days=1), TZ, 15, "sum", "pv",
        sources=["pvmodel:Ost"])
    soll = wahr.resample("15min").mean()
    assert abs(_schwerpunkt_min(s) - _schwerpunkt_min(soll)) < 1.5


def test_read_pv_forecast_uses_each_sources_own_resolution(tmp_path):
    """Eine 30-min- und eine 60-min-Quelle zusammen: jede wird mit IHRER
    Intervalllaenge gelesen, erst danach summiert."""
    db = str(tmp_path / "h.sqlite")
    wahr = _wahrheit()
    halb, voll = wahr.resample("30min").mean(), wahr.resample("60min").mean()
    _cache(db, "sc", halb)
    _cache(db, "pvmodel:Ost", voll)
    start = wahr.index[0].tz_convert(TZ)
    ende = start + pd.Timedelta(days=1)
    raster = pd.date_range(start, ende, freq="15min", inclusive="left")
    s = local_history.read_pv_forecast(db, start, ende, TZ, 15, "sum", "pv",
                                       sources=["sc", "pvmodel:Ost"])
    soll = (intervallmittel_auf_slots(halb, raster, 15)
            + intervallmittel_auf_slots(voll, raster, 15))
    pd.testing.assert_series_equal(s, soll, check_names=False, check_freq=False)
    # ohne Quellfilter (Altpfad) dasselbe - nicht mehr ueber gemischte Marken
    ohne = local_history.read_pv_forecast(db, start, ende, TZ, 15, "sum", "pv")
    pd.testing.assert_series_equal(ohne, soll, check_names=False,
                                   check_freq=False)


def test_operation_and_archive_readers_agree(tmp_path):
    """Kalibrierung (read_group_asof, read_pv_forecast_asof) und Betrieb
    (read_pv_forecast) muessen dieselbe Kurve sehen - die Faktoren werden auf
    der einen gefittet und auf die andere angewandt."""
    db = str(tmp_path / "h.sqlite")
    wahr = _wahrheit()
    voll = wahr.resample("60min").mean()
    _cache(db, "pvmodel:Ost", voll)
    issue = wahr.index[0] - pd.Timedelta(hours=6)
    _archiv(db, "pvmodel:Ost", issue, voll)
    start = wahr.index[0].tz_convert(TZ)
    ende = start + pd.Timedelta(days=1)
    betrieb = local_history.read_pv_forecast(
        db, start, ende, TZ, 15, "sum", "pv", sources=["pvmodel:Ost"])
    gruppe = pv_eval.read_group_asof(db, ["pvmodel:Ost"], start, ende, TZ, 15)
    asof = local_history.read_pv_forecast_asof(
        db, issue, start, ende, TZ, 15, "sum", "pv", sources=["pvmodel:Ost"])
    for name, andere in (("read_group_asof", gruppe),
                         ("read_pv_forecast_asof", asof)):
        pd.testing.assert_series_equal(andere, betrieb, check_names=False,
                                       check_freq=False, obj=name)


# --------------------------------------------------------------------------- #
# read_pv_forecast_asof: Quellfilter und Stundenquellen
# --------------------------------------------------------------------------- #
def _gemischtes_archiv(db, issue, start):
    """Archiv wie am Produktivsystem: 2 Solcast-Quellen (30 min), 2 pvmodel-
    Aggregate und 6 Ensemble-Mitglieder (60 min) - alle 1000 W konstant."""
    halb = pd.Series(1000.0, index=pd.date_range(start, periods=12,
                                                 freq="30min"))
    voll = pd.Series(1000.0, index=pd.date_range(start, periods=6, freq="60min"))
    for quelle in ("sc-ost", "sc-west"):
        _archiv(db, quelle, issue, halb)
    for feld in ("Ost", "West"):
        _archiv(db, f"pvmodel:{feld}", issue, voll)
        for modell in ("best_match", "dwd_icon", "ecmwf_ifs"):
            _archiv(db, f"pvmodel-member:{modell}:{feld}", issue, voll)


def test_asof_reader_sums_only_the_requested_sources(tmp_path):
    db = str(tmp_path / "h.sqlite")
    start = pd.Timestamp("2026-06-10 08:00", tz="UTC")
    issue = start - pd.Timedelta(hours=1)
    _gemischtes_archiv(db, issue, start)
    ende = start + pd.Timedelta(hours=6)
    lesen = lambda ids: local_history.read_pv_forecast_asof(  # noqa: E731
        db, issue, start, ende, TZ, 15, "sum", "pv", sources=ids)
    solcast = lesen(["sc-ost", "sc-west"])
    pvlib = lesen(["pvmodel:Ost", "pvmodel:West"])
    # nicht 10 000 W (alle zehn Quellen), sondern die zwei verlangten
    assert len(solcast) == 24 and solcast.eq(2000.0).all()
    # Stundenquellen ohne Saegezahn: jeder Slot belegt
    assert len(pvlib) == 24 and pvlib.eq(2000.0).all()
    assert lesen([]).empty


def test_asof_reader_interpolates_hourly_sources_without_holes(tmp_path):
    db = str(tmp_path / "h.sqlite")
    wahr = _wahrheit()
    voll = wahr.resample("60min").mean()
    issue = wahr.index[0] - pd.Timedelta(hours=1)
    _archiv(db, "pvmodel:Ost", issue, voll)
    start = wahr.index[0].tz_convert(TZ)
    s = local_history.read_pv_forecast_asof(
        db, issue, start, start + pd.Timedelta(days=1), TZ, 15, "sum", "pv",
        sources=["pvmodel:Ost"])
    assert len(s) == 96 and s.notna().all()
    assert abs(_schwerpunkt_min(s)
               - _schwerpunkt_min(wahr.resample("15min").mean())) < 1.5


def test_asof_reader_never_uses_a_later_issue(tmp_path):
    db = str(tmp_path / "h.sqlite")
    start = pd.Timestamp("2026-06-10 08:00", tz="UTC")
    voll = pd.Series(1000.0, index=pd.date_range(start, periods=4, freq="60min"))
    _archiv(db, "pvmodel:Ost", start - pd.Timedelta(hours=2), voll)
    _archiv(db, "pvmodel:Ost", start + pd.Timedelta(minutes=30), voll * 9.0)
    s = local_history.read_pv_forecast_asof(
        db, start, start, start + pd.Timedelta(hours=4), "UTC", 15, "sum",
        "pv", sources=["pvmodel:Ost"])
    assert s.eq(1000.0).all()


def test_calibration_reads_the_archive_with_the_operating_sources(tmp_path):
    """kalibrierung._issue_time_archive_reader muss dieselben Quellen-IDs
    lesen wie der Betrieb - nicht die Summe ueber das ganze Archiv."""
    import kalibrierung
    from ems.config import PvArray, SolcastSource
    from tests.test_synthetic import make_config

    db = str(tmp_path / "h.sqlite")
    start = pd.Timestamp("2026-06-10 08:00", tz="UTC")
    issue = start - pd.Timedelta(hours=1)
    _gemischtes_archiv(db, issue, start)
    cfg = make_config()
    cfg.general.timezone = TZ
    cfg.general.slot_minutes = 15
    cfg.e3dc_rscp.history_db_path = db
    cfg.weather.enabled = False
    cfg.solcast.enabled = True
    cfg.solcast.combine = "sum"
    cfg.solcast.sources = [SolcastSource(api_key="k", resource_id="sc-ost"),
                           SolcastSource(api_key="k", resource_id="sc-west")]
    cfg.pv_model.shadow = True
    cfg.pv_model.arrays = [PvArray(name="Ost", kwp=5.0, tilt=20.0, azimuth=90.0),
                           PvArray(name="West", kwp=5.0, tilt=20.0,
                                   azimuth=270.0)]
    lesen = kalibrierung._issue_time_archive_reader(cfg)
    zukunft = lesen(start.tz_convert(TZ),
                    (start + pd.Timedelta(hours=6)).tz_convert(TZ))
    assert zukunft["complete"]
    assert zukunft["pv"].eq(2000.0).all()
