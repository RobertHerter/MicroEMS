"""Tests der Archiv-Ansicht: archivierten Optimierer-Lauf gegen die Ist-Werte
legen (ems/archive.py + Routing in ems/main.py).
"""
from __future__ import annotations

import pathlib

import numpy as np
import pandas as pd
import pytest

from ems import main as m
from ems.archive import archive_html, list_runs, run_detail
from ems.local_history import (write_actuals, write_debug_snapshot,
                               write_spot)
from tests.test_synthetic import make_config

TZ = "Europe/Berlin"


def _cfg(tmp_path):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "hist.sqlite")
    return cfg


def _snapshot(index, *, generated, status="Optimal", infeasible=False):
    """Debug-Schnappschuss in der Form, die ems/debugdump.py schreibt."""
    n = len(index)
    pv = np.linspace(0.0, 4000.0, n)
    house = np.full(n, 900.0)
    return {
        "generated": pd.Timestamp(generated).isoformat(),
        "status": status,
        "infeasible": infeasible,
        "infeasible_reason": "Pool-Kopplung" if infeasible else None,
        "solver_hit_limit": False,
        "total_cost_eur": -1.75,
        "violations": [],
        "inputs": {
            "index": [ts.isoformat() for ts in index],
            "house_load_w": list(house),
            "pv_w": list(pv),
            "pv10_w": list(pv * 0.6),
            "price_ct_kwh": list(np.full(n, 27.5)),
            "feedin_ct_kwh": list(np.full(n, 8.0)),
            "initial_house_soc_wh": 5000.0,
        },
        "plan": {
            "batt_dc_charge_w": list(np.full(n, 1200.0)),
            "batt_ac_charge_w": list(np.zeros(n)),
            "batt_discharge_w": list(np.zeros(n)),
            "grid_import_w": list(np.full(n, 100.0)),
            "grid_export_w": list(np.zeros(n)),
            "house_soc_percent": list(np.linspace(50.0, 80.0, n)),
        },
        "plan_mode": ["peak"] * n,
    }


def _seed(tmp_path, *, actual_slots=8, n=32):
    cfg = _cfg(tmp_path)
    index = pd.date_range(pd.Timestamp("2026-07-20 06:00", tz=TZ),
                          periods=n, freq="15min")
    gen = index[0]
    write_debug_snapshot(cfg.e3dc_rscp.history_db_path,
                         _snapshot(index, generated=gen))
    # Ist-Werte nur fuer die ersten Slots - wie bei einem noch laufenden Tag.
    for k in range(actual_slots):
        write_actuals(cfg.e3dc_rscp.history_db_path, index[k], {
            "pv_w": 300.0 * k, "house_load_w": 1100.0, "grid_w": -200.0,
            "battery_w": 1000.0, "soc_percent": 50.0 + k})
    return cfg, index, gen


# --------------------------------------------------------------------------- #
# Aufbereitung
# --------------------------------------------------------------------------- #
def test_run_detail_puts_plan_and_actual_on_the_same_grid(tmp_path):
    cfg, index, gen = _seed(tmp_path)
    d = run_detail(cfg, gen.isoformat())
    assert d is not None
    assert d["slots"] == len(index) and d["status"] == "Optimal"
    assert d["mode"] == "peak" and d["total_cost_eur"] == -1.75
    # Beide Seiten haben genau ein Element je Slot -> im Browser deckungsgleich.
    for key in ("pv_w", "house_w", "battery_w", "soc_percent"):
        assert len(d["plan"][key]) == len(index)
        assert len(d["actual"][key]) == len(index)
    # Ist nur am linken Rand vorhanden, danach None (keine erfundenen Werte).
    assert d["actual_slots"] == 8
    assert d["actual"]["pv_w"][7] is not None
    assert d["actual"]["pv_w"][8] is None


def test_run_detail_signs_match_the_actual_convention(tmp_path):
    """Akku positiv = laden, Netz positiv = Bezug - sonst laufen die Kurven
    der beiden Seiten gegeneinander."""
    cfg, index, gen = _seed(tmp_path)
    d = run_detail(cfg, gen.isoformat())
    # Plan: 1200 W DC-Laden, 0 entladen -> +1200; Ist: +1000 (laden).
    assert d["plan"]["battery_w"][0] == pytest.approx(1200.0)
    assert d["actual"]["battery_w"][0] == pytest.approx(1000.0)
    # Plan: 100 W Bezug -> +100; Ist: -200 (Einspeisung).
    assert d["plan"]["grid_w"][0] == pytest.approx(100.0)
    assert d["actual"]["grid_w"][0] == pytest.approx(-200.0)


def test_run_detail_splits_price_into_estimate_and_published(tmp_path):
    """Der Plan nutzt fuer noch nicht veroeffentlichte Slots eine (gedaempfte)
    Schaetzung. Beides muss getrennt sichtbar sein: was der Lauf annahm und was
    der Boersenpreis wirklich wurde."""
    cfg, index, gen = _seed(tmp_path)
    # Echte Spotpreise fuer die erste Haelfte - Plan nutzte durchweg 27.5 ct.
    write_spot(cfg.e3dc_rscp.history_db_path,
               {ts.tz_convert("UTC").isoformat(): 10.0 for ts in index[:16]})
    d = run_detail(cfg, gen.isoformat())
    real = d["actual"]["price_ct_kwh"]
    assert real is not None
    # Wo ein Boersenpreis vorliegt, steht er da (durch das Tarifmodell) ...
    assert real[0] is not None and real[0] != pytest.approx(27.5)
    # ... danach bleibt die Kurve leer statt fortgeschrieben zu werden.
    assert real[20] is None
    assert d["deviation"]["price_mae_ct"] is not None


def test_price_line_never_claims_an_unpublished_price(tmp_path):
    """Die Schaetz-Maske deckt nur den archivierten Prognosebereich ab; darueber
    hinaus ist sie None. NULL heisst 'unbekannt', nicht 'war veroeffentlicht' -
    sonst erscheint der uebermorgige Schaetzwert als Boersenpreis, den es zur
    Laufzeit gar nicht geben konnte. Die Seite entscheidet dann anhand des
    heute vorliegenden Ist-Preises; dieser Test haelt die Regel fest.
    """
    est = [0] * 4 + [1] * 4 + [None] * 4          # bekannt / geschaetzt / offen
    actual = [20.0] * 4 + [None] * 8              # veroeffentlicht nur vorne
    plan = [21.0] * 12

    def real(i):
        return actual[i]

    def guessed(i):
        return bool(est[i]) if est[i] in (0, 1) else real(i) is None

    solid = [real(i) if real(i) is not None
             else (None if guessed(i) else plan[i]) for i in range(12)]
    # Durchgezogen NUR dort, wo wirklich ein Boersenpreis vorliegt.
    assert solid[:4] == [20.0] * 4
    assert solid[4:] == [None] * 8
    # Und der Bereich ohne Maske zaehlt als Schaetzung, nicht als bekannt.
    assert [guessed(i) for i in range(12)] == [False] * 4 + [True] * 8


def test_run_detail_price_estimate_mask_is_optional(tmp_path):
    """Ohne Prognose-Archiv gibt es keine Schaetz-Maske - das darf die
    Aufbereitung nicht stoeren (aeltere Laeufe, frische Installation)."""
    cfg, index, gen = _seed(tmp_path)
    d = run_detail(cfg, gen.isoformat())
    assert d["plan"]["price_estimated"] is None
    assert "price_estimated_mae_ct" not in d["deviation"]


def test_run_detail_reports_deviation_only_where_actuals_exist(tmp_path):
    cfg, index, gen = _seed(tmp_path)
    d = run_detail(cfg, gen.isoformat())
    dev = d["deviation"]
    # Last: Plan 900 W gegen Ist 1100 W in den 8 gemessenen Slots -> genau 200.
    assert dev["house_mae_w"] == pytest.approx(200.0, abs=0.5)
    assert dev["pv_mae_w"] is not None and dev["soc_mae_pp"] is not None


def test_run_detail_tolerates_an_unencoded_timestamp(tmp_path):
    """In einem Query-String wird '+' als Leerzeichen dekodiert."""
    cfg, index, gen = _seed(tmp_path)
    mangled = gen.isoformat().replace("+", " ")
    assert mangled != gen.isoformat()
    assert run_detail(cfg, mangled) is not None


def test_run_detail_without_snapshot_is_none(tmp_path):
    cfg = _cfg(tmp_path)
    assert run_detail(cfg) is None
    assert run_detail(cfg, "2026-01-01T00:00:00+01:00") is None
    assert list_runs(cfg) == []


def test_list_runs_marks_infeasible_runs(tmp_path):
    cfg, index, gen = _seed(tmp_path)
    later = index[0] + pd.Timedelta(minutes=15)
    write_debug_snapshot(cfg.e3dc_rscp.history_db_path,
                         _snapshot(index, generated=later,
                                   status="Infeasible", infeasible=True))
    runs = list_runs(cfg)
    assert len(runs) == 2
    assert runs[0]["infeasible"] is True          # neueste zuerst
    assert runs[1]["infeasible"] is False
    d = run_detail(cfg, runs[0]["generated"])
    assert d["infeasible"] is True and d["infeasible_reason"] == "Pool-Kopplung"


# --------------------------------------------------------------------------- #
# Seite und Routing
# --------------------------------------------------------------------------- #
def test_archive_page_is_self_contained(tmp_path):
    html = archive_html().decode("utf-8")
    assert "api/archive-runs.json" in html and "api/archive-run.json" in html
    assert 'id="run"' in html and 'id="chart"' in html
    assert 'id="day"' in html            # Tagesfilter (10 Tage Vorhaltung)
    # Preis wie im Dashboard: durchgezogen der tatsaechliche Boersenpreis,
    # gestrichelt NUR die Schaetzung des Laufs (nicht beides doppelt).
    assert "'Börsenpreis'" in html and "'Preis (Schätzung)'" in html
    # NULL in der Maske darf nicht als "war veröffentlicht" gelten.
    assert "est[i]===0||est[i]===1" in html
    assert "'Preis Ist'" not in html and "price_estimated" in html
    # Plotly lokal (kein Internet), Rueckweg zum Dashboard, Theme-Umschalter.
    assert '<script src="plotly.min.js">' in html
    assert "ems-theme" in html
    # Kopfzeile wie im Dashboard, Dashboard-Rueckweg als Button VOR Darstellung.
    assert 'class="app-header"' in html and 'class="header-actions"' in html
    assert html.index('href="/"') < html.index('id="theme"')


def test_archive_page_themes_the_plotly_overlays():
    """Hover-Box und Werkzeugleiste muessen mitgefaerbt werden - Plotly ist
    per Default hell und damit im Dark-Mode weiss auf weiss."""
    html = archive_html().decode("utf-8")
    assert "hoverlabel:hoverlabel" in html and "modebar:modebar" in html
    assert "--card" in html and "activecolor" in html


def test_snapshot_history_keeps_more_than_three_days(tmp_path):
    """Fuer den Tagesvergleich im Archiv muessen mehrere Tage vorgehalten und
    auch ausgeliefert werden (frueher 300 Laeufe bzw. 120 in der Liste)."""
    cfg = _cfg(tmp_path)
    index = pd.date_range(pd.Timestamp("2026-07-20 00:00", tz=TZ),
                          periods=2, freq="15min")
    base = pd.Timestamp("2026-07-14 00:00", tz=TZ)
    for k in range(420):                 # gut 4 Tage im 15-min-Raster
        write_debug_snapshot(cfg.e3dc_rscp.history_db_path,
                             _snapshot(index,
                                       generated=base + pd.Timedelta(minutes=15 * k)))
    runs = list_runs(cfg)
    assert len(runs) == 420
    assert len({r["ts_local"][:10] for r in runs}) >= 4    # mehrere Tage waehlbar


def test_get_routes_for_archive(tmp_path):
    cfg = _cfg(tmp_path)

    def r(path):
        return m._resolve_get_route(path, cfg, has_schedule_runner=False)

    assert r("/archiv") == ("archive_page",)
    assert r("/archiv/") == ("archive_page",)
    assert r("/archive") == ("archive_page",)
    assert r("/api/archive-runs.json") == ("archive_list",)
    assert r("/api/archive-run.json") == ("archive_run",)


def _display_table(index, **extra):
    n = len(index)
    cols = {
        "house_load_w": np.full(n, 800.0), "pv_w": np.zeros(n),
        "price_ct_kwh": np.full(n, 25.0), "feedin_ct_kwh": np.full(n, 8.0),
        "batt_dc_charge_w": np.zeros(n), "batt_ac_charge_w": np.zeros(n),
        "batt_discharge_w": np.full(n, 800.0), "grid_import_w": np.zeros(n),
        "grid_export_w": np.zeros(n), "house_soc_percent": np.full(n, 60.0),
        "mode": ["auto"] * n, "car_charge_w": np.zeros(n),
        "slot_cost_ct": np.zeros(n),
    }
    cols.update(extra)
    return pd.DataFrame(cols, index=index)


def test_dashboard_shows_the_morning_price_estimate_against_the_published_price():
    """Im laufenden Plan sind geschaetzte Slots genau die OHNE Boersenpreis -
    ein Ist dazu gibt es nie. Sichtbar wird der Schaetzfehler erst, wenn man die
    Schaetzung des 00:00-Plans gegen den inzwischen veroeffentlichten Preis legt.
    """
    from ems.dashboard import build_dashboard
    cfg = make_config()
    index = pd.date_range(pd.Timestamp("2026-07-20 06:00", tz=TZ),
                          periods=8, freq="15min")
    table = _display_table(
        index,
        price_estimated=np.zeros(8),              # inzwischen veroeffentlicht
        plan0_price_ct_kwh=np.full(8, 31.0))      # 00:00 hatte 31 statt 25 ct
    html = pathlib.Path(
        build_dashboard(cfg, table, total_cost_ct=0.0)).read_text(encoding="utf-8")
    assert "Sch\\u00e4tzung 00:00" in html or "Schätzung 00:00" in html

    # Ohne Abweichung bleibt die Kurve weg (keine doppelte Linie auf der Ist-Linie).
    plain = _display_table(index, price_estimated=np.zeros(8),
                           plan0_price_ct_kwh=np.full(8, 25.0))
    html2 = pathlib.Path(
        build_dashboard(cfg, plain, total_cost_ct=0.0)).read_text(encoding="utf-8")
    assert "Sch\\u00e4tzung 00:00" not in html2 and "Schätzung 00:00" not in html2


def test_dashboard_links_to_the_archive_page():
    from ems.dashboard import build_dashboard
    cfg = make_config()
    index = pd.date_range(pd.Timestamp("2026-07-20 06:00", tz=TZ),
                          periods=8, freq="15min")
    table = pd.DataFrame({
        "house_load_w": np.full(8, 800.0), "pv_w": np.zeros(8),
        "price_ct_kwh": np.full(8, 25.0), "feedin_ct_kwh": np.full(8, 8.0),
        "batt_dc_charge_w": np.zeros(8), "batt_ac_charge_w": np.zeros(8),
        "batt_discharge_w": np.full(8, 800.0), "grid_import_w": np.zeros(8),
        "grid_export_w": np.zeros(8), "house_soc_percent": np.full(8, 60.0),
        "mode": ["auto"] * 8, "car_charge_w": np.zeros(8),
        "slot_cost_ct": np.zeros(8),
    }, index=index)
    # build_dashboard gibt den Pfad der geschriebenen HTML zurueck.
    out = build_dashboard(cfg, table, total_cost_ct=0.0)
    html = pathlib.Path(out).read_text(encoding="utf-8")
    assert 'href="/archiv"' in html and 'id="archive-link"' in html


# --------------------------------------------------------------------------- #
# Zeitraum-Schalter und Kopfzeile
# --------------------------------------------------------------------------- #
def test_archive_has_the_same_horizon_switch_as_the_dashboard():
    """24/48/Alles wie im Dashboard - ein archivierter 72-h-Lauf ist sonst nur
    als Ganzes zu betrachten, und die interessanten ersten Stunden sind
    zusammengedrueckt."""
    html = archive_html().decode("utf-8")
    assert 'id="horizon"' in html
    for stunden in ("24", "48", "all"):
        assert f'data-hours="{stunden}"' in html
    assert "ems-archive-hours" in html          # Auswahl bleibt erhalten


def test_archive_can_switch_from_plan_actual_lines_to_delta_areas():
    """Der direkte Linienvergleich bleibt erhalten, eine gespeicherte zweite
    Ansicht zeigt dagegen Ist minus Plan als gefuellte Flaeche um null."""
    html = archive_html().decode("utf-8")
    assert 'id="archive-view"' in html
    assert 'data-view="lines"' in html and 'data-view="delta"' in html
    assert "ems-archive-view" in html
    assert "fill:'tozeroy'" in html
    assert "?a-p:null" in html
    assert "Ist − Plan" in html
    # SoC darf in der Differenzansicht nicht auf den absoluten Bereich
    # 0..100 % festgeklemmt bleiben.
    assert "deltaMode?{}:{range:[0,100]}" in html


def test_archive_never_computes_time_ranges_in_utc():
    """Die Zeitachse traegt den UTC-Versatz.

    ``toISOString()`` rechnet nach UTC: als Bereichsgrenze haette es den
    Ausschnitt um den Versatz verschoben, und fuer "heute" lieferte es zwischen
    Mitternacht und dem Versatz (im Sommer bis 02:00) den VORTAG - die
    Tagesvorauswahl traf dann den falschen Tag. Derselbe Fehler ist im
    Dashboard schon einmal aufgetreten und dort per Test verboten.
    """
    html = archive_html().decode("utf-8")
    code = "\n".join(z for z in html.splitlines()
                     if "toISOString" in z and not z.strip().startswith("//"))
    assert not code, f"UTC-Datumsrechnung im Archiv: {code}"
    # Die Grenzen kommen aus der Zeitachse selbst (versatz- und DST-fest).
    assert "'xaxis.range':[x[0],x[bis]]" in html


def test_archive_header_uses_a_house_for_the_dashboard():
    """Der Rueckkehrpfeil sagte "zurueck", nicht WOHIN."""
    html = archive_html().decode("utf-8")
    assert "⌂ <span class=\"button-label\">Dashboard</span>" in html
    assert "↩" not in html


def test_archive_and_dashboard_use_the_same_curve_colours():
    """Beide Seiten zeigen dieselben Groessen - in verschiedenen Farben.

    Netz war blau gegen grau, Ladezustand schwarz gegen blau, Preis braun gegen
    violett, und selbst PV unterschied sich im Ton. Wer zwischen den Seiten
    wechselt, musste die Farben neu lernen. Sie kommen jetzt aus einer Quelle.
    """
    import json
    import re

    from ems.dashboard import CURVE_FAMILIES

    html = archive_html().decode("utf-8")
    eingesetzt = json.loads(
        re.search(r"var EMS_CURVES=(\{.*?\});", html, re.S).group(1))
    assert eingesetzt == {k: list(v) for k, v in CURVE_FAMILIES.items()}
    assert "__CURVES__" not in html, "Platzhalter nicht ersetzt"

    # Keine eigenen Kurvenfarben mehr im Archiv-Skript.
    zeichnen = html[html.index("function add("):html.index("const ax=")]
    eigene = re.findall(r"'#[0-9a-fA-F]{6}'", zeichnen)
    assert not eigene, f"Archiv fuehrt eigene Kurvenfarben: {sorted(set(eigene))}"

    # Jede Familie hat eine helle UND eine dunkle Fassung.
    for name, (hell, dunkel) in CURVE_FAMILIES.items():
        assert hell != dunkel, f"{name}: gleiche Farbe fuer beide Themen"


def test_run_detail_puts_the_plan_soc_on_the_measurement_axis(tmp_path):
    """Plan-SoC gehoert einen Slot spaeter als gespeichert.

    Der Optimierer legt unter Slot t den SoC am SlotENDE ab, das Ist wird am
    Slotanfang gemessen. Ungerichtet laufen die beiden Kurven im Archiv um
    einen Slot versetzt - beim Laden sieht der Plan dadurch zu niedrig aus.
    """
    cfg, index, gen = _seed(tmp_path)
    d = run_detail(cfg, gen.isoformat())
    roh = list(np.linspace(50.0, 80.0, len(index)))     # wie in _snapshot
    soc = d["plan"]["soc_percent"]

    assert len(soc) == len(index)
    assert soc[0] is None                                # davor gab es nichts
    # Jeder Wert ist um genau einen Slot nach hinten gerueckt.
    for k in range(1, len(index)):
        assert soc[k] == pytest.approx(round(roh[k - 1], 2))


def test_run_detail_soc_alignment_makes_a_perfectly_executed_plan_exact(tmp_path):
    """Gegenprobe mit einem Akku, der GENAU wie geplant laeuft.

    Der zu Slot t geplante SoC (Slotende) wird zu Beginn von Slot t+1
    gemessen. Richtig ausgerichtet muss die Abweichung dann null sein - roh
    bleibt genau ein Slot-Hub uebrig, und der ist bei steilem Laden gross.
    """
    cfg = _cfg(tmp_path)
    n = 32
    index = pd.date_range(pd.Timestamp("2026-07-20 06:00", tz=TZ),
                          periods=n, freq="15min")
    write_debug_snapshot(cfg.e3dc_rscp.history_db_path,
                         _snapshot(index, generated=index[0]))
    roh = list(np.linspace(50.0, 80.0, n))
    hub = roh[1] - roh[0]
    # Perfekte Ausfuehrung: Ist(Slot k) == Plan(Slot k-1).
    for k in range(1, 9):
        write_actuals(cfg.e3dc_rscp.history_db_path, index[k], {
            "pv_w": 300.0 * k, "house_load_w": 1100.0, "grid_w": -200.0,
            "battery_w": 1000.0, "soc_percent": roh[k - 1]})

    d = run_detail(cfg, index[0].isoformat())
    plan, ist = d["plan"]["soc_percent"], d["actual"]["soc_percent"]
    paare = [k for k in range(n) if ist[k] is not None and plan[k] is not None]

    assert paare, "ohne Paare prueft der Test nichts"
    gerichtet = [abs(ist[k] - plan[k]) for k in paare]
    versetzt = [abs(ist[k] - round(roh[k], 2)) for k in paare]
    assert max(gerichtet) == pytest.approx(0.0, abs=0.01)
    assert min(versetzt) == pytest.approx(hub, abs=0.01)


# --------------------------------------------------------------------------- #
# Temperatur thermischer Lasten
# --------------------------------------------------------------------------- #
def _pool_cfg(cfg):
    from ems.config import ControllableLoad, LoadStage
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", volume_l=7000.0, target_c=28.0,
        min_c=27.0, max_c=28.5, loss_w_per_k=100.0, switch_penalty_ct=0.0,
        stages=[LoadStage(name="klein", power_w=400.0, heat_w=3000.0)])]
    return cfg


def test_run_detail_lays_the_planned_pool_temperature_over_the_measured_one(
        tmp_path):
    """Die Temperatur kommt aus zwei Quellen - Plan aus dem Schnappschuss, Ist
    aus ``load_temp`` (MQTT, nicht der Wechselrichter). Beide muessen auf dem
    Raster des Laufs liegen, sonst vergleicht die Ansicht versetzte Kurven."""
    from ems.local_history import write_load_temp

    cfg, index, gen = _seed(tmp_path)
    _pool_cfg(cfg)
    n = len(index)
    # Plan: gleichmaessig von 27 auf 29 C. Ist: konstant 1 K darunter.
    snap = _snapshot(index, generated=gen)
    snap["plan"]["load_Pool_temp_c"] = list(np.linspace(27.0, 29.0, n))
    write_debug_snapshot(cfg.e3dc_rscp.history_db_path, snap)
    for k in range(6):
        write_load_temp(cfg.e3dc_rscp.history_db_path, index[k], "Pool",
                        float(np.linspace(27.0, 29.0, n)[k]) - 1.0)

    d = run_detail(cfg, gen.isoformat())
    plan = d["plan"]["load_temp_c"]["Pool"]
    ist = d["actual"]["load_temp_c"]["Pool"]
    assert len(plan) == n and len(ist) == n, "nicht auf dem Raster des Laufs"
    assert plan[0] == pytest.approx(27.0, abs=0.01)
    assert ist[0] == pytest.approx(26.0, abs=0.01)
    assert ist[6] is None, "Ist darf ueber den Messbereich hinaus nichts erfinden"
    # Der Abstand ist konstruiert 1 K - genau das muss die Kachel zeigen.
    assert d["deviation"]["load_temp_mae_k"]["Pool"] == pytest.approx(1.0,
                                                                     abs=0.05)
    # Komfortband, damit die Kurve ohne Blick in die Konfiguration lesbar ist.
    assert d["comfort_band"]["Pool"] == [27.0, 28.5]


def test_run_detail_without_a_thermal_load_draws_no_temperature(tmp_path):
    """Ohne thermische Last darf kein leerer Kurvensatz und keine vierte Achse
    entstehen - sonst zeigt die Ansicht eine Skala ohne Inhalt."""
    cfg, index, gen = _seed(tmp_path)          # make_config: keine Lasten
    d = run_detail(cfg, gen.isoformat())
    assert d["plan"]["load_temp_c"] is None
    assert d["actual"]["load_temp_c"] is None
    assert d["comfort_band"] is None
    assert d["deviation"]["load_temp_mae_k"] is None


def test_run_detail_reports_no_temperature_deviation_without_measurements(
        tmp_path):
    """Geplante Kurve ohne Messwerte: zeichnen ja, Abweichung nein."""
    cfg, index, gen = _seed(tmp_path)
    _pool_cfg(cfg)
    snap = _snapshot(index, generated=gen)
    snap["plan"]["load_Pool_temp_c"] = list(np.full(len(index), 27.5))
    write_debug_snapshot(cfg.e3dc_rscp.history_db_path, snap)

    d = run_detail(cfg, gen.isoformat())
    assert d["plan"]["load_temp_c"]["Pool"][0] == pytest.approx(27.5)
    assert d["actual"]["load_temp_c"] is None
    assert d["deviation"]["load_temp_mae_k"] is None, (
        "ohne Messwerte gibt es keine Modellabweichung zu berichten")


def test_archive_page_draws_the_temperature_on_its_own_scale():
    """22-30 C auf der 0-100-Prozent-Achse waere ein Strich am Boden."""
    html = archive_html().decode("utf-8")
    assert "yaxis4" in html and "overlaying:'y2'" in html
    assert "tempNames" in html, "Kurven werden nur bei Daten gezeichnet"
    assert "comfort_band" in html, "Komfortband fehlt"
    # Dieselbe Farbquelle wie das Dashboard, sonst laufen die Seiten auseinander.
    from ems.dashboard import CURVE_FAMILIES
    assert "temp" in CURVE_FAMILIES
    assert CURVE_FAMILIES["temp"][0] in html
