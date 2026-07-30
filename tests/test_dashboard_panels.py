"""Seitengerüst des Dashboards: Panels, Sprungleiste, Zeitachse.

Sprungleiste und gemerkter Panel-Zustand (ems/dashboard.py).

Die Seite lädt sich bei jedem neuen Plan komplett neu. Der Auf-/Zu-Zustand der
Panels ist reine DOM-Information: ohne Sicherung fällt ein aufgeklapptes Panel
spätestens beim nächsten Zyklus zu - samt allem, was es beim Aufklappen
nachgeladen hat. Beides hängt an einer Bedingung, die man leicht verliert:
JEDES Panel braucht eine stabile, eindeutige id.
"""
from __future__ import annotations

import pathlib
import re

import numpy as np
import pandas as pd

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
        "load_pool_klein_w": np.full(n, 400.0),
        "load_pool_temp_c": np.full(n, 27.5),
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
    achsen = [t["x"] for t in data if isinstance(t.get("x"), list) and t["x"]]
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


def test_navigation_is_desktop_only_but_state_persistence_is_not(tmp_path):
    """Die Leiste kostet mobil dauerhaft Bildschirmhöhe und wird dort
    ausgeblendet - das Merken des Panel-Zustands darf daran NICHT hängen,
    sonst wäre der Nutzen auf dem Handy weg, obwohl er dort am meisten zählt.
    """
    html = _render(tmp_path)
    mobil = _mobile_css(html)
    assert ".panel-nav { display: none; }" in mobil
    # Zustands-Skript und Speicher-Schlüssel liegen AUSSERHALB der Medienregel.
    assert "ems-panel-open" in html.replace(mobil, "")


def test_runtime_strip_is_one_row_on_mobile(tmp_path):
    """Der Statusstreifen stapelte mobil Text, Fortschritt und Knopf in voller
    Breite - rund 120 px, bevor ein einziger Messwert zu sehen war."""
    mobil = _mobile_css(_render(tmp_path))
    assert ".recalc-label { display: none; }" in mobil
    assert "position: absolute" in mobil.split(".runtime-progress")[1][:180]
    # Die Meldung bleibt sichtbar (nur einzeilig) - sie traegt den Fehlergrund.
    assert "display: inline" in mobil.split(".runtime-main small")[1][:80]


def test_navigation_and_state_persistence_are_wired(tmp_path):
    html = _render(tmp_path)
    assert '<nav class="panel-nav" id="panel-nav"' in html
    # Der Zustand landet unter einem eigenen Schlüssel; das Tagespanel behält
    # seinen eigenen (mobile Voreinstellung) und wird bewusst ausgenommen.
    assert "ems-panel-open" in html
    assert "'live-daily-panel':1" in html


def test_panels_stay_collapsed_by_default(tmp_path):
    """Nur das Tagespanel ist offen - sonst wäre die Seite eine Bleiwüste.

    Der gemerkte Zustand wird erst im Browser angewandt; die erzeugte Datei
    selbst darf keine zusätzlichen offenen Panels enthalten.
    """
    offen = [t for t in _panel_tags(_render(tmp_path))
             if re.search(r"\bopen\b", t)]
    assert len(offen) <= 1, f"unerwartet offene Panels: {offen}"
