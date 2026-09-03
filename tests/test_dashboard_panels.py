"""Seitengerüst des Dashboards: Panels, Kopfzeile, Zeitachse, Farben.

Die Seite lädt sich bei jedem neuen Plan komplett neu. Der Auf-/Zu-Zustand der
Panels ist reine DOM-Information: ohne Sicherung fällt ein aufgeklapptes Panel
spätestens beim nächsten Zyklus zu - samt allem, was es beim Aufklappen
nachgeladen hat. Das hängt an einer Bedingung, die man leicht verliert: JEDES
Panel braucht eine stabile, eindeutige id.
"""
from __future__ import annotations

import pathlib
import re

import numpy as np
import pandas as pd
import pytest

from ems.dashboard import build_dashboard
from tests.test_synthetic import make_config


def _render(tmp_path) -> str:
    cfg = make_config(tmp_html=str(tmp_path / "dash.html"))
    index = pd.date_range("2026-07-29 10:00", periods=8, freq="15min",
                          tz=cfg.general.timezone)
    n = len(index)
    table = pd.DataFrame({
        "house_load_w": np.full(n, 800.0), "pv_w": np.zeros(n),
        "price_ct_kwh": np.full(n, 25.0), "feedin_ct_kwh": np.full(n, 8.0),
        "batt_dc_charge_w": np.zeros(n), "batt_ac_charge_w": np.zeros(n),
        "batt_discharge_w": np.zeros(n), "grid_import_w": np.full(n, 800.0),
        "grid_export_w": np.zeros(n), "house_soc_percent": np.full(n, 60.0),
        "mode": ["auto"] * n, "car_charge_w": np.zeros(n),
        "slot_cost_ct": np.zeros(n),
    }, index=index)
    return pathlib.Path(
        build_dashboard(cfg, table, total_cost_ct=0.0)).read_text(
            encoding="utf-8")


def _panel_tags(html: str) -> list[str]:
    return re.findall(r"<details\b[^>]*>", html)


def _mobile_css(html: str) -> str:
    """Der Block mit den Handy-Regeln, über eine Regel darin gefunden.

    Nicht über die erste ``@media``-Marke: der Stil hat mehrere Blöcke mit
    derselben Breite, und ein Test, der den falschen erwischt, prüft nichts.
    """
    marke = "@media (max-width: 620px)"
    for start in [i for i in range(len(html))
                  if html.startswith(marke, i)]:
        block = html[start:html.index("</style>", start)]
        if ".recalc-label" in block:
            return block
    raise AssertionError("Handy-Regeln nicht gefunden")


def _series(werte):
    """Zahlenreihe einer Plotly-Spur lesen.

    Plotly kodiert Zahlenreihen als typisiertes Array (base64 ``bdata``), nicht
    als Liste - ein Test, der nur Listen erwartet, findet stillschweigend
    nichts und geht durch.
    """
    if isinstance(werte, dict) and "bdata" in werte:
        import base64
        return np.frombuffer(base64.b64decode(werte["bdata"]),
                             dtype=werte.get("dtype", "f8"))
    return np.asarray([np.nan if v is None else float(v) for v in werte])


def _figure(html: str):
    """Spuren und Layout des Hauptdiagramms aus der Seite holen."""
    import json
    m = re.search(r'Plotly\.newPlot\(\s*"[^"]+",\s*(\[.*?\]),\s*(\{.*?\}),'
                  r'\s*\{"responsive"', html, re.S)
    assert m, "Hauptdiagramm nicht gefunden"
    return json.loads(m.group(1)), json.loads(m.group(2))


def _render_with_temperature(tmp_path) -> str:
    """Wie _render, aber mit Thermolast und Außentemperatur.

    Die Außentemperatur bringt einen EIGENEN Zeitindex mit (Open-Meteo-Raster)
    und war deshalb die eine Spur, die beim Umstellen der Achse zurückblieb.
    """
    from ems.config import ControllableLoad, LoadStage

    cfg = make_config(tmp_html=str(tmp_path / "dash_temp.html"))
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, target_c=28.0,
        min_c=26.0, max_c=32.0, stages=[LoadStage("klein", 400, 1000)])]
    index = pd.date_range("2026-07-29 10:00", periods=8, freq="15min",
                          tz=cfg.general.timezone)
    n = len(index)
    table = pd.DataFrame({
        "house_load_w": np.full(n, 800.0), "pv_w": np.zeros(n),
        "price_ct_kwh": np.full(n, 25.0), "feedin_ct_kwh": np.full(n, 8.0),
        "batt_dc_charge_w": np.zeros(n), "batt_ac_charge_w": np.zeros(n),
        "batt_discharge_w": np.zeros(n), "grid_import_w": np.full(n, 800.0),
        "grid_export_w": np.zeros(n), "house_soc_percent": np.full(n, 60.0),
        "mode": ["auto"] * n, "car_charge_w": np.zeros(n),
        "slot_cost_ct": np.zeros(n),
        "load_Pool_klein_w": np.full(n, 400.0),
        "load_Pool_temp_c": np.full(n, 27.5),
    }, index=index)
    # Stundenraster - bewusst NICHT das Slot-Raster der Tabelle.
    ambient = pd.Series(
        np.full(3, 21.0),
        index=pd.date_range("2026-07-29 10:00", periods=3, freq="h",
                            tz=cfg.general.timezone))
    return pathlib.Path(build_dashboard(
        cfg, table, total_cost_ct=0.0, ambient_temp_c=ambient,
        load_temp_actual={"Pool": pd.Series(
            np.full(n, 27.0), index=index)})).read_text(encoding="utf-8")


def test_all_traces_share_one_time_format(tmp_path):
    """Eine Spur im ISO-Format auf sonst naiver Achse verrutscht.

    Die Zeitstempel stehen als naive Ortszeit in der Seite statt als ISO mit
    UTC-Versatz - 30 Spuren tragen dieselbe Achse, das waren 245 KB. Sobald
    eine Spur das alte Format behält, deutet Plotly sie anders und legt sie
    gegen alle anderen versetzt. Genau das passierte der Außentemperatur.
    """
    data, _ = _figure(_render_with_temperature(tmp_path))
    # Leere Spuren ueberspringen: die Modusfarben haengen nur als
    # Legendeneintrag in der Figur und zeichnen nichts.
    achsen = [t["x"] for t in data
              if isinstance(t.get("x"), list) and t["x"]
              and t["x"][0] is not None]
    assert len(achsen) >= 5, f"nur {len(achsen)} Spuren mit Zeitachse"
    iso = sorted({a[0] for a in achsen if "T" in str(a[0])})
    assert not iso, f"Spuren im alten ISO-Format: {iso}"
    for a in achsen:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", str(a[0])), a[0]


def test_each_controllable_load_gets_its_own_colour(tmp_path):
    """Vier Geräte in derselben Farbe sind im Diagramm nicht unterscheidbar.

    Vorher trugen alle verschiebbaren Lasten dasselbe Violett - bei zwei
    Waschmaschinen, Trockner und Spülmaschine half nur noch die Legende, also
    nichts im Bild selbst. Ist- und Soll-Kurve EINER Last teilen sich weiter
    eine Farbe; sie unterscheiden sich durch die Strichart.
    """
    from ems.config import ControllableLoad

    cfg = make_config(tmp_html=str(tmp_path / "dash_loads.html"))
    namen = ["Waschmaschine 1", "Waschmaschine 2", "Trockner", "Spülmaschine"]
    cfg.controllable_loads = [
        ControllableLoad(name=n, type="deferrable", enabled=True,
                         power_w=2000.0, runtime_minutes=90.0)
        for n in namen]
    index = pd.date_range("2026-07-29 10:00", periods=8, freq="15min",
                          tz=cfg.general.timezone)
    n = len(index)
    from ems.loads import _slug
    spalten = {
        "house_load_w": np.full(n, 800.0), "pv_w": np.zeros(n),
        "price_ct_kwh": np.full(n, 25.0), "feedin_ct_kwh": np.full(n, 8.0),
        "batt_dc_charge_w": np.zeros(n), "batt_ac_charge_w": np.zeros(n),
        "batt_discharge_w": np.zeros(n), "grid_import_w": np.full(n, 800.0),
        "grid_export_w": np.zeros(n), "house_soc_percent": np.full(n, 60.0),
        "mode": ["auto"] * n, "car_charge_w": np.zeros(n),
        "slot_cost_ct": np.zeros(n),
    }
    for name in namen:
        spalten[f"load_{_slug(name)}_w"] = np.full(n, 2000.0)
        spalten[f"actual_load_{_slug(name)}_power_w"] = np.full(n, 1900.0)
    html = pathlib.Path(build_dashboard(
        cfg, pd.DataFrame(spalten, index=index),
        total_cost_ct=0.0)).read_text(encoding="utf-8")

    data, _ = _figure(html)
    farben = {t["name"]: (t.get("line") or {}).get("color")
              for t in data if (t.get("name") or "").endswith("(Ist)")
              and any(n_ in t["name"] for n_ in namen)}
    assert len(farben) == len(namen), f"nicht alle Lasten gezeichnet: {farben}"
    assert len(set(farben.values())) == len(namen), \
        f"Lasten teilen sich Farben: {farben}"


def test_thermal_load_gets_its_own_power_curve(tmp_path):
    """Der Pool hatte nie eine eigene Leistungskurve, die Waschmaschinen schon.

    Er steckte nur in der Summe "Steuerb. Lasten" und in der Zeitleiste - neben
    den einzeln gezeichneten verschiebbaren Lasten sah das aus, als fehle er.
    Der Name ist bewusst "Heizleistung": "(Soll)" ist im Temperaturpanel schon
    für die Solltemperatur derselben Last vergeben.
    """
    html = _render_with_temperature(tmp_path)
    data, _ = _figure(html)
    namen = [t.get("name") for t in data if t.get("name")]
    assert "Pool Heizleistung (Soll)" in namen, namen
    # Kein Namenskonflikt mit der Temperaturkurve derselben Last.
    assert namen.count("Pool (Soll)") == 1

    kurve = next(t for t in data
                 if t.get("name") == "Pool Heizleistung (Soll)")
    assert kurve.get("yaxis", "y") == "y", "gehört ins Leistungspanel"


def test_shared_meter_is_not_counted_twice(tmp_path):
    """Beide Pool-Stufen hängen an EINEM Shelly - summieren zählt doppelt.

    Der Zähler misst den ganzen Poolkreis; die kleine Wärmepumpe wird per
    Schwelle auf demselben Messwert erkannt. Jede Stufe liefert deshalb
    dieselbe Reihe. Eine naive Summe über die Stufen zeigte die doppelte
    Leistung - und zwar plausibel genug, um nicht aufzufallen.
    """
    from ems.config import ControllableLoad, LoadStage

    topic = "homie/pool/measure-power"
    cfg = make_config(tmp_html=str(tmp_path / "dash_meter.html"))
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, target_c=28.0,
        min_c=26.0, max_c=32.0,
        stages=[LoadStage("gross", 660, 4000, power_topic=topic),
                LoadStage("klein", 400, 3000, power_topic=topic)])]
    index = pd.date_range("2026-07-29 10:00", periods=8, freq="15min",
                          tz=cfg.general.timezone)
    n = len(index)
    table = pd.DataFrame({
        "house_load_w": np.full(n, 800.0), "pv_w": np.zeros(n),
        "price_ct_kwh": np.full(n, 25.0), "feedin_ct_kwh": np.full(n, 8.0),
        "batt_dc_charge_w": np.zeros(n), "batt_ac_charge_w": np.zeros(n),
        "batt_discharge_w": np.zeros(n), "grid_import_w": np.full(n, 800.0),
        "grid_export_w": np.zeros(n), "house_soc_percent": np.full(n, 60.0),
        "mode": ["auto"] * n, "car_charge_w": np.zeros(n),
        "slot_cost_ct": np.zeros(n),
        "load_Pool_gross_w": np.full(n, 660.0),
        "load_Pool_klein_w": np.zeros(n),
        # Derselbe Zähler, an beide Stufen gehängt - so liegt es real vor.
        "actual_load_Pool_gross_power_w": np.full(n, 1050.0),
        "actual_load_Pool_klein_power_w": np.full(n, 1050.0),
    }, index=index)
    html = pathlib.Path(build_dashboard(
        cfg, table, total_cost_ct=0.0)).read_text(encoding="utf-8")
    data, _ = _figure(html)
    ist = next(t for t in data
               if t.get("name") == "Pool Heizleistung (Ist)")
    werte = _series(ist["y"])
    assert float(np.nanmax(werte)) == pytest.approx(1050.0), \
        f"geteilter Zähler doppelt gezählt: {float(np.nanmax(werte))}"


def test_thermal_actual_curve_stops_at_the_present(tmp_path):
    """Eine Ist-Kurve darf nicht in die Zukunft laufen.

    ``sum(axis=1)`` macht in pandas aus jedem NaN eine 0 - die gemessene
    Heizleistung lief damit als 0-W-Linie bis ans Horizontende, obwohl es dort
    keine Messung gibt. Das ist keine Kosmetik: eine Ist-Kurve in der Zukunft
    behauptet eine Messung, die es nicht gibt.
    """
    from ems.config import ControllableLoad, LoadStage

    cfg = make_config(tmp_html=str(tmp_path / "dash_zukunft.html"))
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, target_c=28.0,
        min_c=26.0, max_c=32.0,
        stages=[LoadStage("klein", 400, 1000, power_topic="homie/pool")])]
    index = pd.date_range("2026-07-29 10:00", periods=8, freq="15min",
                          tz=cfg.general.timezone)
    n = len(index)
    gemessen = np.full(n, np.nan)
    gemessen[:3] = [900.0, 950.0, 0.0]          # nur die ersten drei Slots
    table = pd.DataFrame({
        "house_load_w": np.full(n, 800.0), "pv_w": np.zeros(n),
        "price_ct_kwh": np.full(n, 25.0), "feedin_ct_kwh": np.full(n, 8.0),
        "batt_dc_charge_w": np.zeros(n), "batt_ac_charge_w": np.zeros(n),
        "batt_discharge_w": np.zeros(n), "grid_import_w": np.full(n, 800.0),
        "grid_export_w": np.zeros(n), "house_soc_percent": np.full(n, 60.0),
        "mode": ["auto"] * n, "car_charge_w": np.zeros(n),
        "slot_cost_ct": np.zeros(n),
        "load_Pool_klein_w": np.full(n, 400.0),
        "actual_load_Pool_klein_power_w": gemessen,
    }, index=index)
    html = pathlib.Path(build_dashboard(
        cfg, table, total_cost_ct=0.0)).read_text(encoding="utf-8")
    data, _ = _figure(html)
    ist = next(t for t in data if t.get("name") == "Pool Heizleistung (Ist)")
    werte = _series(ist["y"])
    assert np.count_nonzero(~np.isnan(werte)) == 3, \
        f"Ist-Kurve reicht über die Messung hinaus: {werte}"
    assert float(werte[2]) == 0.0, "eine gemessene 0 muss eine 0 bleiben"


def test_thermal_curve_stays_away_when_nothing_is_planned(tmp_path):
    """Ohne geplante Heizleistung keine Nullkurve in der Legende - sie waere
    ein Eintrag mehr in einer ohnehin langen Liste, ohne Aussage."""
    from ems.config import ControllableLoad, LoadStage

    cfg = make_config(tmp_html=str(tmp_path / "dash_kalt.html"))
    cfg.controllable_loads = [ControllableLoad(
        name="Pool", type="thermal", enabled=True, target_c=28.0,
        min_c=26.0, max_c=32.0, stages=[LoadStage("klein", 400, 1000)])]
    index = pd.date_range("2026-07-29 10:00", periods=8, freq="15min",
                          tz=cfg.general.timezone)
    n = len(index)
    table = pd.DataFrame({
        "house_load_w": np.full(n, 800.0), "pv_w": np.zeros(n),
        "price_ct_kwh": np.full(n, 25.0), "feedin_ct_kwh": np.full(n, 8.0),
        "batt_dc_charge_w": np.zeros(n), "batt_ac_charge_w": np.zeros(n),
        "batt_discharge_w": np.zeros(n), "grid_import_w": np.full(n, 800.0),
        "grid_export_w": np.zeros(n), "house_soc_percent": np.full(n, 60.0),
        "mode": ["auto"] * n, "car_charge_w": np.zeros(n),
        "slot_cost_ct": np.zeros(n),
        "load_Pool_klein_w": np.zeros(n),          # geplant: gar nicht heizen
        "load_Pool_temp_c": np.full(n, 29.0),
    }, index=index)
    html = pathlib.Path(build_dashboard(
        cfg, table, total_cost_ct=0.0)).read_text(encoding="utf-8")
    data, _ = _figure(html)
    assert not [t for t in data
                if str(t.get("name")).startswith("Pool Heizleistung")]


def test_shapes_and_annotations_use_the_same_time_format(tmp_path):
    """Jetzt-Linie, Vergangenheitsband und Tagesgrenzen sitzen auf derselben
    Achse wie die Kurven - im falschen Format zeigen sie auf die falsche
    Stelle, ohne dass irgendetwas fehlschlägt."""
    _, layout = _figure(_render_with_temperature(tmp_path))
    marken = [s[k] for s in layout.get("shapes", []) for k in ("x0", "x1")
              if isinstance(s.get(k), str)]
    marken += [a["x"] for a in layout.get("annotations", [])
               if isinstance(a.get("x"), str) and a["x"][:2] == "20"]
    assert marken, "keine Zeitmarken im Layout"
    falsch = [v for v in marken if "T" in v or "+" in v]
    assert not falsch, f"Zeitmarken im alten Format: {falsch}"


def test_every_panel_carries_a_stable_id():
    """Ohne id kann ein Panel weder gemerkt noch angesprungen werden.

    Bewusst über die QUELLE statt über eine gerenderte Seite: welche Panels
    erscheinen, hängt an der Konfiguration (kein Pool -> keine
    Last-Rückkopplung, keine Steuerung -> kein Steuerpanel). Eine Testseite
    zeigt nur 5 der 12 Panels - darunter keines der vier, die ihre id gerade
    erst bekommen haben. Der Test hätte also nichts geprüft.
    """
    source = pathlib.Path("ems/dashboard.py").read_text(encoding="utf-8")
    # Die Panel-Marken stehen in Python-Stringliteralen, teils über zwei Zeilen
    # umbrochen - deshalb bis zum schliessenden '>' ODER Zeilenende lesen.
    tags = re.findall(r"<details\b[^>\n]*", source)
    assert len(tags) >= 12, f"nur {len(tags)} Panel-Marken gefunden"
    ohne_id = [t for t in tags if "id=" not in t]
    assert not ohne_id, f"Panels ohne id: {ohne_id}"


def test_panel_ids_are_unique(tmp_path):
    """Zwei Panels mit derselben id teilten sich einen gespeicherten Zustand
    und ein Sprungziel - die Leiste würde immer nur das erste öffnen."""
    ids = re.findall(r"<details\b[^>]*\bid=[\"']([^\"']+)[\"']",
                     _render(tmp_path))
    doppelt = {i for i in ids if ids.count(i) > 1}
    assert not doppelt, f"doppelte Panel-ids: {doppelt}"


def test_panel_state_survives_the_reload(tmp_path):
    """Die Seite laedt bei jedem neuen Plan komplett neu - ohne Sicherung faellt
    jedes aufgeklappte Panel zu, samt allem, was es nachgeladen hatte.

    Hier hing zeitweise eine Sprungleiste mit dran. Die ist wieder raus; dass
    das Merken NICHT an ihr hing, sichert dieser Test ab.
    """
    html = _render(tmp_path)
    assert "ems-panel-open" in html
    assert "addEventListener('toggle'" in html
    assert "panel-nav" not in html and "pnav-chip" not in html


def test_runtime_status_lives_in_the_header(tmp_path):
    """Als eigener Streifen brauchte der Laufzeitstatus eine zweite Karte mit
    Rahmen, Schatten und Aussenabstand - mobil rund 120 px, bevor ein Messwert
    zu sehen war. Jetzt kostet er nur die Zeilenhoehe der Titelleiste."""
    html = _render(tmp_path)
    kopf = html[html.index("<header class=\"app-header\""):]
    kopf = kopf[:kopf.index("</header>")]
    assert 'class="runtime-strip"' in kopf, "Status steht nicht in der Titelleiste"
    assert 'id="runtime-progress"' in kopf
    mobil = _mobile_css(html)
    assert ".recalc-label { display: none; }" in mobil
    # Die Meldung bleibt lesbar - sie traegt im Fehlerfall den Grund.
    assert "display: none" not in mobil.split("#runtime-meta")[0][-200:]


def test_sources_and_validation_are_tiles_without_losing_detail():
    """Beide standen als eigene Zeile in voller Breite über dem Diagramm.

    Als Kachel ist nur Platz für eine Zahl - die Einzelheiten (welche Quelle
    hängt, welche Invariante verletzt ist) dürfen dabei nicht verschwinden,
    sonst wird aus dem Aufräumen ein Informationsverlust. Sie stehen deshalb
    im Kurzinfotext der Kachel.
    """
    from dataclasses import dataclass

    from ems.dashboard import _sources_tile, _validation_tile

    @dataclass
    class Verstoss:
        severity: str
        text: str

        def __str__(self) -> str:
            return self.text

    quellen = [{"name": "Spotpreis", "level": "ok", "detail": "echt bis 23:45"},
               {"name": "Wetter", "level": "ok", "detail": "vor 5 min"},
               {"name": "Hauslast", "level": "warn", "detail": "vor 75 min"}]
    kachel = _sources_tile(quellen)
    assert "2/3 aktuell" in kachel and "Datenquellen" in kachel
    assert "Hauslast" in kachel                      # der Ausreisser wird genannt
    assert "vor 75 min" in kachel                    # Detail im Kurzinfotext
    assert "echt bis 23:45" in kachel

    schlecht = _validation_tile(
        [Verstoss("error", "SoC unter Minimum in Slot 42")])
    assert "1 Fehler" in schlecht
    assert "SoC unter Minimum in Slot 42" in schlecht

    assert "✓ OK" in _validation_tile([])
    assert _validation_tile(None) == ""               # ungeprueft -> keine Kachel


def test_panels_stay_collapsed_by_default(tmp_path):
    """Nur das Tagespanel ist offen - sonst wäre die Seite eine Bleiwüste.

    Der gemerkte Zustand wird erst im Browser angewandt; die erzeugte Datei
    selbst darf keine zusätzlichen offenen Panels enthalten.
    """
    offen = [t for t in _panel_tags(_render(tmp_path))
             if re.search(r"\bopen\b", t)]
    assert len(offen) <= 1, f"unerwartet offene Panels: {offen}"


def test_all_header_buttons_carry_an_icon(tmp_path):
    """Zwei der vier Kopfknoepfe hatten im Markup gar kein Zeichen - mobil
    bekamen sie eines per :after-Pseudoelement, am Schreibtisch keines. In
    einer Reihe mit Archiv und Konfiguration sah das wie ein Versehen aus.
    """
    html = _render(tmp_path)
    kopf = html[html.index('<header class="app-header"'):]
    kopf = kopf[:kopf.index("</header>")]
    for knopf in ("install-app", "theme-toggle"):
        block = kopf[kopf.index(f'id="{knopf}"'):]
        block = block[:block.index("</button>")]
        assert 'class="config-icon"' in block, f"{knopf} ohne Symbol"
        assert 'class="config-label"' in block, f"{knopf} ohne Beschriftung"
    # Die Notloesung ueber Pseudoelemente ist damit ueberfluessig.
    assert "#theme-toggle:after" not in html
    assert "#install-app:after" not in html


def test_every_panel_header_carries_a_status_dot(tmp_path):
    """Die Planentscheidungs-Box war die einzige Kopfzeile ohne Ampelpunkt.

    In einer Reihe gleich gebauter Kopfzeilen faellt so eine Luecke sofort auf.
    Ihr Punkt ist neutral (grau), denn das Panel hat keinen Zustand: es erklaert
    den Plan, es bewertet ihn nicht.
    """
    from ems.dashboard import _decision_block

    index = pd.date_range("2026-07-29 10:00", periods=2, freq="15min",
                          tz="Europe/Berlin")
    leer = _decision_block(pd.DataFrame(
        {"mode": ["auto"] * 2, "decision_reason": [""] * 2}, index=index),
        index[0])
    assert "an-dot neutral" in leer, "leeres Panel ohne Punkt"

    # Ausgenommen ist die "Heute"-Klappe INNERHALB der Live-Kacheln: sie ist
    # kein Diagnosepanel mit Zustand, sondern ein Auszug der Tageszähler.
    html = _render(tmp_path)
    ohne = []
    for m in re.finditer(r"<details\b[^>]*>\s*<summary\b.*?</summary>",
                         html, re.S):
        if "live-daily-panel" in m.group(0) or "an-dot" in m.group(0):
            continue
        ohne.append(m.group(0)[:80])
    assert not ohne, f"Kopfzeilen ohne Ampelpunkt: {ohne}"


def test_every_dark_curve_colour_is_visible_and_the_mapping_is_complete(tmp_path):
    """Kurvenfarben mussten im Dunkelmodus nachgefaerbt werden - und wurden es
    nur zum Teil.

    Fuenf der verwendeten Familien fallen auf dunklem Grund unter 3:1 (Trockner
    1,75, Pool 2,50, Preis 2,75, Waschmaschine 2 2,77, die schwarzen SoC-Linien
    1,16). Nachgefaerbt wurde bisher nur die letzte Gruppe, ueber eine Liste im
    Browser mit dem ANZEIGENAMEN als Schluessel: eine Umbenennung liess die
    Zuordnung stillschweigend ausfallen. Genau das prueft dieser Test - er
    rechnet den Kontrast nach und verlangt, dass jede zu dunkle Kurve eine
    Entsprechung hat.
    """
    import json

    def leuchtdichte(farbe: str) -> float:
        farbe = farbe.lstrip("#")
        kanaele = [int(farbe[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        kanaele = [k / 12.92 if k <= 0.03928 else ((k + 0.055) / 1.055) ** 2.4
                   for k in kanaele]
        return 0.2126 * kanaele[0] + 0.7152 * kanaele[1] + 0.0722 * kanaele[2]

    def kontrast(a: str, b: str) -> float:
        h, d = leuchtdichte(a), leuchtdichte(b)
        return (max(h, d) + 0.05) / (min(h, d) + 0.05)

    html = _render_with_temperature(tmp_path)
    data, _ = _figure(html)
    zuordnung = json.loads(
        re.search(r"var EMS_DARK_LINES=(\{.*?\});", html, re.S).group(1))
    # Nicht gierig: [^;]* laeuft ueber das schliessende } hinaus bis zum
    # naechsten Semikolon irgendwo im Skript danach. Auf der Testseite ging
    # das zufaellig gut, auf der ausgelieferten Seite nicht.

    DUNKEL = "#18212b"
    ohne = []
    for i, spur in enumerate(data):
        farbe = (spur.get("line") or {}).get("color")
        if not farbe or not farbe.startswith("#"):
            continue
        dunkel = zuordnung.get(str(i))
        if dunkel:
            assert kontrast(dunkel, DUNKEL) >= 4.5, (
                f"{spur.get('name')}: Dunkelfarbe {dunkel} nur "
                f"{kontrast(dunkel, DUNKEL):.2f}:1")
        elif kontrast(farbe, DUNKEL) < 3.0:
            ohne.append(f"{spur.get('name')} {farbe} "
                        f"{kontrast(farbe, DUNKEL):.2f}:1")
    assert not ohne, f"zu dunkle Kurven ohne Entsprechung: {ohne}"

    # Bewusst KEINE Pruefung auf unbenutzte Eintraege: die Palette muss mehr
    # abdecken als ein einzelner Aufbau zeigt - welche Kurven erscheinen, haengt
    # an der Konfiguration (vier Geraete oder keines, Pool oder nicht).
    assert zuordnung, "keine Zuordnung in der Seite"
    for farbe in zuordnung.values():
        assert kontrast(farbe, DUNKEL) >= 4.5, farbe


def test_horizon_toolbar_sits_above_the_chart(tmp_path):
    """Eine Bedienung gehoert vor das, was sie steuert.

    Die Zeitraumwahl stand unter dem Diagramm - man fand sie erst, nachdem man
    an 1178 px Grafik vorbeigescrollt war.
    """
    html = _render(tmp_path)
    leiste = html.index('class="desktop-horizon-toolbar"')
    grafik = html.index('<div class="desktop-plot">')
    assert leiste < grafik, "Zeitraumwahl steht immer noch unter der Grafik"


def test_mode_colours_are_a_legend_group_not_a_footnote(tmp_path):
    """Die Modusfarben standen als Anmerkung UNTER dem Diagramm, waehrend jede
    andere Legende rechts daneben steht - und sie brauchten dort eigenen Platz
    im Fussbereich."""
    data, layout = _figure(_render(tmp_path))
    gruppen = {t.get("legendgroup") for t in data}
    assert "mode" in gruppen, "Modusfarben nicht als Legendengruppe"
    anmerkungen = [a.get("text", "") for a in layout.get("annotations", [])]
    assert not [a for a in anmerkungen if "Modus:" in a], \
        "Modus-Anmerkung unter dem Diagramm ist noch da"


def test_pv_confidence_asap_on_a_dull_day_is_not_a_warning():
    """Ein asap-Tag bei wenig Sonne ist die KONSERVATIVE Wahl, keine
    Auffaelligkeit.

    Die Ampel war gelb, sobald ein Tag nicht auf Basis "p10" entschieden wurde
    - also auch bei jedem asap-Tag. Robert am 03.09.2026: "wird gelb angezeigt,
    aber Peak ist ja eigentlich keine Warnung." Robust sind ZWEI Faelle: peak,
    weil schon der pessimistische Ueberschuss die Schwelle traegt, und asap,
    weil er sie nicht traegt. Gelb gehoert allein dem dritten: peak auf Basis
    der ERWARTUNG, wo p10 allein nicht reicht - nur das ist eine Wette.
    """
    from ems.dashboard import _pv_confidence_block

    robust = {"2026-09-03": {"basis": "p10", "mode": "peak"},
              "2026-09-04": {"basis": "p10", "mode": "peak"},
              "2026-09-05": {"basis": "insufficient", "mode": "asap"}}
    html = _pv_confidence_block(robust)
    assert "an-dot ok" in html, html[:200]
    assert "3/3 Tage ohne Wette" in html

    wette = {"2026-09-03": {"basis": "p10", "mode": "peak"},
             "2026-09-04": {"basis": "expected+p10-floor", "mode": "peak"}}
    html = _pv_confidence_block(wette)
    assert "an-dot warn" in html
    assert "1 T auf Erwartung" in html

    # Nur asap ist ebenfalls in Ordnung - der Optimierer weicht bewusst aus.
    nur_asap = {"2026-09-05": {"basis": "insufficient", "mode": "asap"}}
    assert "an-dot ok" in _pv_confidence_block(nur_asap)


def test_load_timeline_has_a_state_for_switching_slots():
    """Im Flankenslot weichen Soll und Ist regelmaessig um einen Slot ab, weil
    der Befehl wenige Sekunden NACH Slotbeginn hinausgeht und die Rueckmeldung
    davor gelesen wird. Gemessen ueber sechs Tage an beiden Pool-Stufen lagen
    12 der 13 echten Abweichungen an einer Flanke. Sie brauchen einen eigenen
    Zustand statt "geplant, laeuft nicht"."""
    import ems.dashboard as dash

    quelle = dash.__file__
    text = open(quelle, encoding="utf-8").read()
    assert "SWITCH" in text and "schaltet um" in text
    # Farbstufen und Codes muessen dieselbe Zahl haben - sonst verschieben
    # sich im Dunkelmodus die Farben gegen die Zustaende.
    assert "n_states = 8" in text
    assert text.count("[0.875,'#7f9bb5'],[0.9999,'#7f9bb5']") == 1
    assert text.count("[0.875,'#4c6478'],[0.9999,'#4c6478']") == 1
