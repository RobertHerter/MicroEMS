"""MILP-Optimierer für die Steuertabelle (48 h, 15-min-Slots).

Minimiert die Netto-Stromkosten (Import * Preis - Export * Einspeisevergütung)
über den Horizont. Steuergrößen je Slot:

  * dc_charge  – Haus-Akku DC-Ladeleistung (nur aus PV)         [W]
  * ac_charge  – Haus-Akku AC-Ladeleistung (aus dem Netz)       [W]
  * dis        – Haus-Akku Entladeleistung (AC-seitig geliefert)[W]
  * car        – Auto-Ladeleistung (semikontinuierlich)         [W]

Nebenbedingungen: SoC-Grenzen Haus/Auto, Leistungsgrenzen, Wechselrichter-
Durchsatz, kein gleichzeitiges Laden/Entladen (Binärvariablen), Auto-Ziel-SoC
zur Abfahrtzeit.

Modellannahmen (dokumentiert):
  * PV ist am DC-Bus verfügbar; DC-Laden reduziert die zu Wechselrichter/AC
    geführte PV-Leistung: pv_to_ac = pv - dc_charge (>= 0).
  * Wechselrichter-Durchsatz begrenzt: pv_to_ac + dis + ac_charge <= WR_max.
  * Auto lädt AC-seitig und zählt nicht in den WR-Batterieport-Durchsatz.
  * Wirkungsgrade: Laden/Entladen wirken auf die SoC-Bilanz.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd
import pulp

from .config import Config

log = logging.getLogger("ems.optimizer")
_solver_serial = threading.Lock()


@dataclass
class OptimizerInputs:
    index: pd.DatetimeIndex            # tz-aware, N Slots
    house_load_w: np.ndarray
    pv_w: np.ndarray
    price_ct_kwh: np.ndarray
    feedin_ct_kwh: np.ndarray
    initial_house_soc_wh: float
    initial_car_soc_wh: Optional[float] = None
    car_present: bool = False
    # Pessimistische PV-Vorhersage (Solcast p10, W). Im Auto-Modus dient sie
    # der konservativen Tageswahl und Fruehladeabsicherung; explizites Peak
    # bleibt reines Peak-Shaving anhand des Erwartungswerts.
    pv10_w: Optional[np.ndarray] = None
    # Außentemperatur (°C) je Slot – Wärmeverlust thermischer Lasten (Pool).
    ambient_temp_c: Optional[np.ndarray] = None
    # Solar-Einstrahlung (W/m² Globalstrahlung) je Slot – solarer Wärmeeintrag
    # thermischer Lasten (Pool, ems.loads._add_thermal).
    solar_w_m2: Optional[np.ndarray] = None
    # Ist-Zustand steuerbarer Lasten beim Start: {load_name: aktuelle Temperatur °C}
    # für thermische Lasten (T[0]). Fehlt ein Wert -> target_c als Startwert.
    load_state: Optional[dict] = None
    # Tatsächlich rückgemeldeter Stufenzustand {"Pool/WP": bool}; dient als
    # Anfangszustand für Schaltkosten und verhindert unnötiges Takten.
    load_feedback: Optional[dict] = None
    # Unveränderter Börsenpreis. Nur dieser entscheidet über
    # zero_at_negative_price; None hält alte Debug-Snapshots kompatibel.
    spot_price_ct_kwh: Optional[np.ndarray] = None


@dataclass
class OptimizerResult:
    table: pd.DataFrame                # je Slot alle Größen + prognostizierte SoCs
    total_cost_ct: float
    status: str
    infeasible: bool = False
    export_line_w: Optional[float] = None   # Einspeise-Linie L (nur Peak-Modus)
    # Fehlmenge (Wh) zum Auto-Ziel-SoC bei Abfahrt (weiche Nebenbedingung);
    # > 0 = Ziel im Plan nicht erreichbar -> Alarm.
    car_target_shortfall_wh: float = 0.0
    # Über die Hausanschluss-Grenze (max_import_w) hinaus geplante Energie (Wh)
    # über den Horizont (weiche Nebenbedingung); > 0 = eine Lastspitze war im
    # Plan nicht ohne Grenzüberschreitung deckbar -> Alarm.
    grid_overload_wh: float = 0.0
    # Solver hat das Zeitlimit erreicht: CBC liefert dann den besten
    # Zwischenstand und PuLP meldet trotzdem "Optimal" - der Plan kann
    # deutlich suboptimal sein (z.B. sinnlose Dumps/Sperren) -> Alarm.
    solver_hit_limit: bool = False
    # Steuerbare Lasten: [(control_topic, Ergebnis-Spaltenname)] für die Sollwert-Ausgabe.
    load_mqtt_map: Optional[list] = None
    # Laufzeit-Telemetrie des Hauptsolves inkl. Modellgroesse und MIP-Luecke.
    solver_seconds: float = 0.0
    solver_polish_seconds: float = 0.0
    solver_slots: int = 0
    solver_variables: int = 0
    solver_binaries: int = 0
    solver_constraints: int = 0
    solver_warm_start: bool = False
    solver_mip_gap: Optional[float] = None
    # Auto-Peak-Entscheidungsbasis je Tag {tag: {p10_kwh, expected_kwh,
    # threshold_kwh, basis, mode, ...}} - macht die p10-gestützte Peak/asap-Wahl
    # im Dashboard sichtbar (robuste PV-Planung). Leer außer bei charge_strategy=auto.
    auto_peak_basis: Optional[dict] = None


# --------------------------------------------------------------------------- #
# MIP-Warmstart: Lösung des letzten Laufs (prozess-lokal). Zwischen zwei
# 15-min-Zyklen ändert sich der Plan kaum - die um den Zeitversatz verschobene
# alte Lösung ist ein sehr guter Startpunkt: HiGHS bekommt sofort einen
# brauchbaren Incumbent (statt ihn per Heuristik suchen zu müssen) und
# repariert unpassende kontinuierliche Werte selbst per LP. Passt die alte
# Lösung gar nicht mehr (Eingaben stark geändert), verwirft HiGHS sie einfach.
# --------------------------------------------------------------------------- #
_warm_cache: dict = {}

# HiGHS kann einen laufenden Solve aus einem zweiten Thread sauber abbrechen.
# Das ist wichtig fuer systemctl stop/restart: ohne cancelSolve blieb der Dienst
# bis zum Solver-Zeitlimit im nativen C++-Aufruf haengen.
_solver_cancel = threading.Event()
_solver_active = threading.Event()


class SolverCancelled(RuntimeError):
    """Geordneter Abbruch eines laufenden Optimierungslaufs."""


def request_solver_cancel() -> None:
    _solver_cancel.set()


def clear_solver_cancel() -> None:
    _solver_cancel.clear()


def solver_is_running() -> bool:
    return _solver_active.is_set()

_TRAIL_IDX = re.compile(r"^(.+)_(\d+)$")

# Nur diese Präfixe sind echt SLOT-indiziert und dürfen beim Warmstart um den
# Zeitversatz verschoben werden. Alle anderen Endungen `name_<int>` tragen einen
# ANDEREN Index - Tag (L_day_, lateshort_), Segment (batsuff_, termseg_),
# Reserve (evres_deficit_) oder Block (cl_<last>_<stufe>_b<b>) - und würden durch
# das Verschieben auf einen falschen Variablennamen gesetzt. Ein falsch
# benannter Startwert ist schädlicher als gar keiner.
_WARM_SLOT_PREFIXES = frozenset({
    "dc", "ac", "dis", "curt", "gimp", "gexp", "soc",
    "full", "atmax", "bgrid",
    "car", "iscar", "soccar", "carshort", "carstart",
    "batholdblock", "matimp", "partdis", "hasbat", "avoidimp", "p10s", "hold",
    "plan_delta_dc", "plan_delta_ac", "plan_delta_dis", "plan_delta_car",
})


def _complete_pv10(pv10_w, expected_pv_w):
    """Fehlende p10-Slots konservativ aus dem vorhandenen Band ergaenzen.

    Ein einzelnes NaN am Horizontende darf nicht die belastbaren Solcast-
    Quantile aller anderen Tage deaktivieren. Fuer Luecken wird das robuste
    Medianverhaeltnis p10/Erwartungswert der vorhandenen Tageslicht-Slots
    verwendet; ohne verwertbares Band gilt konservativ 65 %.
    Rueckgabe: (vollstaendige Reihe, Anzahl Ersetzungen, verwendeter Faktor).
    """
    expected = np.maximum(np.asarray(expected_pv_w, dtype=float), 0.0)
    if pv10_w is None:
        return None, 0, None
    raw = np.asarray(pv10_w, dtype=float).reshape(-1)
    if raw.size != expected.size:
        return 0.65 * expected, int(expected.size), 0.65
    finite = np.isfinite(raw)
    missing = int((~finite).sum())
    if not missing:
        return np.maximum(raw, 0.0), 0, None
    usable = finite & (expected >= 100.0) & (raw >= 0.0)
    ratios = raw[usable] / expected[usable]
    factor = (float(np.median(ratios)) if ratios.size else 0.65)
    factor = float(np.clip(factor, 0.05, 1.0))
    completed = np.where(finite, raw, factor * expected)
    return np.maximum(completed, 0.0), missing, factor


def _seasonal_peak_values(optimization, ts) -> tuple[float, float, float]:
    """Auto-Peak-Schwelle und Rampenmalus glatt nach Sonnenjahreszeit.

    Der Faktor ist am 21. Juni 1 und am 21. Dezember nahezu 0. Dadurch gibt es
    keine Spruenge an Monats- oder Saisonwechseln. Rueckgabe:
    (Schwelle in %, Rampenmalus ct/kW, Sommerfaktor).
    """
    base_threshold = float(getattr(
        optimization, "auto_peak_threshold_percent", 85.0))
    base_ramp = float(getattr(
        optimization, "peak_charge_ramp_penalty_ct_kw", 0.0) or 0.0)
    if not bool(getattr(optimization, "seasonal_peak_tuning", False)):
        return float(np.clip(base_threshold, 0.0, 100.0)), max(0.0, base_ramp), 0.5

    stamp = pd.Timestamp(ts)
    year = stamp.year
    year_days = 366 if pd.Timestamp(f"{year}-12-31").dayofyear == 366 else 365
    summer_doy = pd.Timestamp(f"{year}-06-21").dayofyear
    delta = abs(stamp.dayofyear - summer_doy)
    delta = min(delta, year_days - delta)
    summer_factor = 0.5 + 0.5 * np.cos(2.0 * np.pi * delta / year_days)

    winter_threshold = float(getattr(
        optimization, "auto_peak_threshold_winter_percent", 95.0))
    summer_threshold = float(getattr(
        optimization, "auto_peak_threshold_summer_percent", 75.0))
    winter_ramp = float(getattr(
        optimization, "peak_charge_ramp_penalty_winter_ct_kw", 0.5))
    summer_ramp = float(getattr(
        optimization, "peak_charge_ramp_penalty_summer_ct_kw", 2.0))
    threshold = winter_threshold + summer_factor * (
        summer_threshold - winter_threshold)
    ramp = winter_ramp + summer_factor * (summer_ramp - winter_ramp)
    return (float(np.clip(threshold, 0.0, 100.0)), max(0.0, float(ramp)),
            float(summer_factor))


def _shifted_warm_values(new_start, slot_minutes: int) -> Optional[dict]:
    """Warm-Werte des letzten Laufs auf den neuen Horizont verschieben:
    alter Slot-Index -> neuer = alt - Versatz. Verschoben werden AUSSCHLIESSLICH
    echt slot-indizierte Variablen (_WARM_SLOT_PREFIXES); alles andere (Tages-,
    Segment-, Reserve- oder Block-Index) wird ausgelassen, weil ein Verschieben
    dort einen falsch benannten - und damit schädlichen - Startwert erzeugte.
    None, wenn kein (nutzbarer) letzter Lauf vorliegt."""
    c = _warm_cache
    if not c or c.get("slot_min") != slot_minutes:
        return None
    delta_min = (pd.Timestamp(new_start) - c["start"]).total_seconds() / 60.0
    shift = int(round(delta_min / slot_minutes))
    if abs(delta_min / slot_minutes - shift) > 1e-6 or shift < 0:
        return None
    if shift * slot_minutes > 240:      # > 4 h alt: Plan zu weit weg, wertlos
        return None
    out = {}
    for name, val in c["values"].items():
        m = _TRAIL_IDX.match(name)
        if not m or m.group(1) not in _WARM_SLOT_PREFIXES:
            continue
        i = int(m.group(2)) - shift
        if i >= 0:
            out[f"{m.group(1)}_{i}"] = val
    return out or None


def _store_warm_solution(prob, start, slot_minutes: int) -> None:
    """Lösung fürs Warmstarten des nächsten Laufs merken (nur Werte != 0;
    fehlende Variablen starten ohnehin bei 0).

    Die Lade-/Entlade-Ein-Aus-Binären (isch_/isdi_) werden BEWUSST NICHT
    übernommen: sie sind aus der LP-Relaxation billig herleitbar, aber ein
    Warmstart kann eine stale Ein-Slot-Sperre (z.B. 19:15-„hold": Import trotz
    vollem Akku am PV->Akku-Übergang) über Zyklen weiterschleppen, weil ihre
    Mehrkosten (~1 ct) unter der Gap-Toleranz liegen. Ohne diesen Startwert
    entscheidet der Solver Laden/Entladen jeden Zyklus frisch (Kalt-Optimum),
    während die teuren kombinatorischen Binären (is_full/Peak-Linien/Pool)
    weiter warmgestartet werden."""
    vals = {}
    for v in prob.variables():
        x = v.varValue
        if x is not None and abs(x) > 1e-9 and not v.name.startswith(("isch_", "isdi_")):
            vals[v.name] = float(x)
    _warm_cache.clear()
    _warm_cache.update({"start": pd.Timestamp(start), "slot_min": slot_minutes,
                        "values": vals})


class _WarmHiGHS(pulp.HiGHS):
    """pulp.HiGHS + MIP-Startlösung via highspy.setSolution (der PuLP-Wrapper
    selbst kennt keinen Warmstart). Ein unvollständiger/unpassender Start ist
    unkritisch: HiGHS prüft ihn, repariert Kontinuierliches per LP oder
    verwirft ihn kommentarlos."""

    def __init__(self, warm_values=None, **kwargs):
        super().__init__(**kwargs)
        self.warm_values = warm_values or {}

    def callSolver(self, lp):
        if self.warm_values:
            try:
                import highspy
                col = [0.0] * lp.solverModel.getNumCol()
                hits = 0
                for var in lp.variables():
                    x = self.warm_values.get(var.name)
                    if x is not None:
                        col[var.index] = float(x)
                        hits += 1
                if hits:
                    sol = highspy.HighsSolution()
                    sol.col_value = col
                    lp.solverModel.setSolution(sol)
                    log.info("Warmstart: %d Variablen aus dem letzten Lauf vorbelegt.",
                             hits)
            except Exception as exc:   # pragma: no cover - nur defensive Hülle
                log.debug("Warmstart übersprungen (%s).", exc)
        watcher_done = threading.Event()

        def _cancel_watcher():
            while not watcher_done.wait(0.1):
                if _solver_cancel.is_set():
                    try:
                        lp.solverModel.cancelSolve()
                    except Exception as exc:  # pragma: no cover - defensiv
                        log.debug("Solver-Abbruch fehlgeschlagen (%s).", exc)
                    return

        watcher = threading.Thread(target=_cancel_watcher,
                                   name="ems-solver-cancel", daemon=True)
        _solver_active.set()
        watcher.start()
        try:
            super().callSolver(lp)
        finally:
            _solver_active.clear()
            watcher_done.set()
            watcher.join(timeout=0.5)
        if _solver_cancel.is_set():
            raise SolverCancelled("Optimierung wegen Dienstende abgebrochen")


def _polish_continuous(prob, cfg, free_names=None) -> bool:
    """Politur: Binärentscheidungen der Lösung fixieren und den Rest exakt
    (gap-frei) nachoptimieren.

    Die MIP-Gap-Toleranz (gapAbs/gapRel) beendet die Suche früh - der
    Incumbent kann dann ökonomisch dominierte Reste enthalten. Zwei Klassen:

    * KONTINUIERLICH (real beobachtet: 5 kW Netzladen bei 16,5 ct, dessen
      Energie nur den ohnehin bald vollen Akku früher füllt und dafür
      späteren PV-Export zu 7,77 ct verdrängt - ~10 ct Verlust): mit
      fixierten Binären ist das Problem ein LP -> exaktes Optimum in
      Sekunden, solche Reste verschwinden beweisbar.
    * BINÄRE Einzel-Blasen (real beobachtet: Entladesperre GENAU in der
      teuersten Reststunde, Import 888 W bei 42,8 ct, Nachbar-Slots entladen
      normal - ~3 ct Schaden): dafür lässt der Aufrufer die verdächtigen
      Binärvariablen über `free_names` FREI - es bleibt ein Mini-MIP mit
      einer Handvoll freier Binärer, ebenfalls Sekunden.

    Schlägt die Politur fehl (numerische Randfälle), wird die ursprüngliche
    Lösung wiederhergestellt. Rückgabe: True, wenn poliert wurde."""
    free_names = free_names or set()
    ints = [v for v in prob.variables() if v.cat == pulp.LpInteger]
    if not ints:
        return False
    snapshot = {v: v.varValue for v in prob.variables()}
    bounds = [(v, v.lowBound, v.upBound) for v in ints]
    try:
        for v in ints:
            if v.name in free_names:
                continue
            x = int(round(v.varValue or 0.0))
            v.lowBound = v.upBound = x
        # Keine relative oder absolute MIP-Lücke in der Politur: genau sie
        # soll die Artefakte der schnellen Hauptoptimierung entfernen.
        prob.solve(make_solver(cfg, exact=True))
        if prob.status == pulp.LpStatusOptimal:
            return True
        log.debug("Politur nicht optimal (%s) - ursprüngliche Lösung bleibt.",
                  pulp.LpStatus[prob.status])
    except SolverCancelled:
        raise
    except Exception as exc:   # pragma: no cover - reine Absicherung
        log.debug("Politur übersprungen (%s).", exc)
    finally:
        for v, lo, hi in bounds:
            v.lowBound, v.upBound = lo, hi
    for v, x in snapshot.items():
        v.varValue = x
    prob.status = pulp.LpStatusOptimal
    return False


def natural_battery_step(soc_wh: float, pv_w: float, load_w: float, hb, dt_hours: float,
                         max_export_w: Optional[float] = None):
    """Ein Slot natürliches E3DC-Eigenverbrauchsverhalten (ohne EMS-Eingriffe):
    PV-Überschuss lädt den Akku, Defizit entlädt ihn; Rest geht ins/kommt vom Netz.

    Wird für den Infeasibility-Fallback UND als "Ohne-EMS"-Baseline des
    Ersparnis-Trackings genutzt. max_export_w: Einspeisebegrenzung am
    Netzanschluss (Überschuss darüber wird abgeregelt).
    Rückgabe: (neuer SoC Wh, Laden W, Entladen W, Netzbezug W, Einspeisung W).
    """
    pv_w, load_w = max(0.0, float(pv_w)), max(0.0, float(load_w))
    surplus = pv_w - load_w
    charge = dis = 0.0
    if surplus >= 0.0:
        room_w = (hb.max_soc_wh - soc_wh) / (hb.charge_efficiency * dt_hours)
        charge = min(surplus, hb.max_dc_charge_w, max(0.0, room_w))
        soc_wh += hb.charge_efficiency * charge * dt_hours
    else:
        avail_w = (soc_wh - hb.min_soc_wh) * hb.discharge_efficiency / dt_hours
        dis = min(-surplus, hb.max_discharge_w, max(0.0, avail_w))
        soc_wh -= dis * dt_hours / hb.discharge_efficiency
    imp = max(0.0, load_w - pv_w - dis)
    exp = max(0.0, pv_w - load_w - charge)
    if max_export_w is not None:
        exp = min(exp, float(max_export_w))   # Rest wird abgeregelt
    return soc_wh, charge, dis, imp, exp


def make_solver(cfg: Config, warm_values: Optional[dict] = None,
                exact: bool = False):
    """CBC-Solver: bevorzugt das System-CBC (COIN_CMD, coinor-cbc), da
    PULP_CBC_CMD ab PuLP 4.0 entfällt; sonst Fallback auf den PuLP-CBC.
    Optional: HiGHS-Solver falls in der Konfiguration gewählt; warm_values
    ({Variablenname: Wert}, z.B. verschobene Lösung des letzten Laufs) werden
    dort als MIP-Startlösung gesetzt (CBC: ignoriert)."""
    threads = cfg.optimization.solver_threads or max(1, (os.cpu_count() or 2) - 1)
    kwargs = dict(timeLimit=cfg.optimization.solver_time_limit_s, msg=0,
                  threads=threads)
    # Relative Optimalitätslücke: kappt den teuren "Optimalität beweisen"-Endlauf
    # (v. a. an peak-Tagen mit Pool-Binärvariablen). gapRel wird von HiGHS und
    # COIN_CMD unterstützt; 0 = exakt.
    # Die Politur muss die Lösung mit fixierten Binären tatsächlich exakt
    # nachrechnen. Die regulären MIP-Gaps dort weiterzuverwenden, ließ
    # ökonomisch dominierte Einzelentscheidungen stehen (z.B. eine isolierte
    # Entladesperre vor einem günstigeren Entlade-Slot).
    gap = 0.0 if exact else float(
        getattr(cfg.optimization, "solver_mip_gap", 0.0) or 0.0)
    # Nicht nur weglassen: HiGHS hat eigene kleine Standard-Gaps. Für die
    # Politur müssen beide explizit 0 sein, sonst können Cent-Bruchteile
    # (z.B. eine 15-min-Entladesperre vor einem billigeren Slot) bleiben.
    if exact or gap > 0:
        kwargs["gapRel"] = gap
    # Absolute Lücke (ct) zusätzlich: schützt vor dem "1 % von einem großen
    # Ziel sind viele Euro"-Effekt (konstante Malusterme) und beendet auf
    # Instanzen mit kleinem Zielwert die teure Beweisphase früher.
    gap_abs = 0.0 if exact else float(
        getattr(cfg.optimization, "solver_mip_gap_abs_ct", 0.0) or 0.0)
    if exact or gap_abs > 0:
        kwargs["gapAbs"] = gap_abs

    solver_name = getattr(cfg.optimization, "solver", "cbc").lower()
    if solver_name == "highs":
        # Fester Zufallsseed + threads=1: HiGHS' Branch-and-Bound-Suche
        # (parallele Baumsuche, Zweigwahl, Heuristik-Timing) ist sonst nicht
        # deterministisch - bei mehreren innerhalb der Gap-Toleranz gleich-
        # wertigen Lösungen kann derselbe Input je Lauf eine ANDERE davon
        # liefern (beobachtet: identischer Plan lieferte einmal ein sinn-
        # loses Netzladen, im Neulauf nicht). threads>1 macht die parallele
        # Baumsuche zusätzlich lauf-timing-abhängig (welcher Zweig zuerst
        # einen Incumbent findet, ist nicht allein vom Seed bestimmt) - nur
        # threads=1 + Seed ergibt "derselbe Input -> IMMER dieselbe Lösung".
        # Kostet auf dieser Instanz kaum Zeit (Solve bleibt << Zeitlimit).
        highs_kwargs = dict(kwargs, random_seed=42, threads=1)
        try:
            highs = _WarmHiGHS(warm_values=warm_values, **highs_kwargs)
            if highs.available():
                log.info("Solver: HiGHS (highspy).")
                return highs
            log.warning("HiGHS als Solver konfiguriert, aber nicht verfügbar "
                        "(ist 'highspy' installiert?). Fallback auf CBC – OHNE "
                        "Warmstart und OHNE garantierten Determinismus; "
                        "Plan-Stabilität und Warmstart-Artefakt-Erkennung sind "
                        "dann eingeschränkt.")
        except Exception as exc:
            log.warning("Fehler beim Laden des HiGHS-Solvers: %s. Fallback auf "
                        "CBC – ohne Warmstart/garantierten Determinismus.", exc)

    # CBC-Fallback: nutzt die konfigurierten Threads (Default cpu-1). Mehrfädiges
    # CBC ist NICHT reproduzierbar, und der Rest des Systems (Plan-Stabilität,
    # Warmstart-Artefakt-Erkennung, Kalt-Resolve) setzt "gleicher Input ->
    # gleicher Plan" voraus. Ein erzwungenes threads=1 wurde dennoch BEWUSST NICHT
    # gewählt: es legt CBC innerhalb der MIP-Gap auf teils schlechtere Inkumbenten
    # fest (Regressionstests belegen z.B. Export bei Negativpreis). Der
    # Produktivpfad ist HiGHS (mit Seed + threads=1 deterministisch); der
    # CBC-Fallback ist Notlauf und oben laut gewarnt.
    coin = pulp.COIN_CMD(**kwargs)
    if coin.available():
        return coin
    return pulp.PULP_CBC_CMD(**kwargs)


class Optimizer:
    def __init__(self, config: Config, *, stabilize_plan: bool = False,
                 store_warm: bool = True):
        self.cfg = config
        # Nur der fortlaufende Produktivdienst darf gegen den vorherigen Lauf
        # stabilisieren. Backtests, Debug-Replays und voneinander unabhaengige
        # Tests duerfen trotz prozessweitem Warmstart nicht gekoppelt werden.
        self.stabilize_plan = bool(stabilize_plan)
        self.store_warm = bool(store_warm)

    def _departure_slot_indices(self, index: pd.DatetimeIndex) -> List[int]:
        """Slot-Indizes der Abfahrtzeiten im Horizont (je Wochentag; Tage ohne
        Abfahrt - z.B. Wochenende - liefern keinen Slot).

        Abfahrtszeiten, die nicht exakt auf dem Slot-Raster liegen (z.B. 07:50
        bei 15-min-Slots), werden auf den FRÜHEREN Slot abgerundet (07:45) - das
        Auto ist dann rechtzeitig VOR der Abfahrt geladen. Ohne dieses Abrunden
        fand die exakte Gleichheitsprüfung keinen Slot und das Ladeziel entfiel
        für den Tag still."""
        if not (self.cfg.vehicle.enabled):
            return []
        veh = self.cfg.vehicle
        slot = max(1, int(self.cfg.general.slot_minutes))
        local = index.tz_convert(self.cfg.general.timezone)
        out = []
        for i, ts in enumerate(local):
            dep = veh.departure_for_weekday(ts.weekday())
            if dep is None:
                continue
            snapped = (dep.hour * 60 + dep.minute) // slot * slot
            if ts.hour * 60 + ts.minute == snapped:
                out.append(i)
        return out

    def _neutral_result(self, inp: OptimizerInputs, status: str) -> OptimizerResult:
        """Fallback ohne Eingriffe ("alles auto"), wenn keine Lösung vorliegt.

        Der E3DC regelt dann eigenständig im Eigenverbrauchsmodus. Für plausible
        Prognosewerte (SoC, Netz, Kosten) wird dieses natürliche Verhalten
        simuliert: PV-Überschuss lädt den Akku, Defizit entlädt ihn.
        """
        hb = self.cfg.house_battery
        dt = self.cfg.general.dt_hours
        kwh = dt / 1000.0
        soc = min(hb.max_soc_wh, max(hb.min_soc_wh, float(inp.initial_house_soc_wh)))
        rows = []
        for t in range(len(inp.index)):
            pv_t = float(np.nan_to_num(inp.pv_w[t]))
            load_t = float(np.nan_to_num(inp.house_load_w[t]))
            price_t = float(np.nan_to_num(inp.price_ct_kwh[t]))
            feedin_t = float(np.nan_to_num(inp.feedin_ct_kwh[t]))
            pv_t, load_t = max(0.0, pv_t), max(0.0, load_t)
            soc, charge, dis, imp, exp = natural_battery_step(
                soc, pv_t, load_t, hb, dt,
                max_export_w=self.cfg.inverter.max_export_w)
            row = {
                "house_load_w": load_t, "pv_w": pv_t,
                "price_ct_kwh": price_t, "feedin_ct_kwh": feedin_t,
                "batt_dc_charge_w": round(charge, 1), "batt_ac_charge_w": 0.0,
                "batt_discharge_w": round(dis, 1),
                "batt_charge_limit_w": hb.max_dc_charge_w,
                "batt_discharge_limit_w": hb.max_discharge_w,
                "batt_grid_charge_w": 0.0, "batt_grid_discharge_w": 0.0,
                "charge_limited": 0.0, "discharge_limited": 0.0,
                "mode": "auto", "car_charge_w": 0.0,
                "grid_import_w": round(imp, 1), "grid_export_w": round(exp, 1),
                "house_soc_wh": round(soc, 1),
                "house_soc_percent": round(100.0 * soc / hb.capacity_wh, 2),
                "export_line_w": np.nan,
                "slot_cost_ct": round((imp * price_t - exp * feedin_t) * kwh, 4),
            }
            if inp.car_present and inp.initial_car_soc_wh is not None:
                row["car_soc_wh"] = round(float(inp.initial_car_soc_wh), 1)
                row["car_soc_percent"] = round(
                    100.0 * float(inp.initial_car_soc_wh) / self.cfg.vehicle.capacity_wh, 2)
            rows.append(row)
        table = pd.DataFrame(rows, index=inp.index)
        return OptimizerResult(
            table=table, total_cost_ct=float(table["slot_cost_ct"].sum()),
            status=status, infeasible=True, export_line_w=None,
        )

    def solve(self, inp: OptimizerInputs) -> OptimizerResult:
        """Solverlaeufe pro Prozess serialisieren.

        Dashboard-Vergleiche koennen damit nie parallel zum produktiven Lauf
        PuLP/HiGHS-Zustand und CPU teilen. Der Lock wird PRO solve()-Aufruf
        genommen und freigegeben – der mehrstufige Schattenvergleich (mehrere
        aufeinanderfolgende solve()) haelt ihn also NICHT am Stueck. Ein faellig
        werdender Produktivlauf wartet damit hoechstens auf EINEN laufenden Solve
        (Sekunden), nicht auf die ganze Vergleichsserie – kein Sonder-Prioritaets-
        mechanismus noetig, der den kritischen Solver-Pfad verkomplizieren wuerde.
        """
        with _solver_serial:
            return self._solve(inp)

    def _solve(self, inp: OptimizerInputs) -> OptimizerResult:
        clear_solver_cancel()
        cfg = self.cfg
        hb = cfg.house_battery
        veh = cfg.vehicle
        dt = cfg.general.dt_hours
        N = len(inp.index)

        # Eingaben validieren: NaN/Inf (z.B. komplett fehlende PV-Vorhersage)
        # würden das LP unbemerkt unbrauchbar machen.
        bad = [name for name, arr in (
            ("house_load_w", inp.house_load_w), ("pv_w", inp.pv_w),
            ("price_ct_kwh", inp.price_ct_kwh), ("feedin_ct_kwh", inp.feedin_ct_kwh),
        ) if not np.all(np.isfinite(np.asarray(arr, dtype=float)))]
        if bad or not np.isfinite(inp.initial_house_soc_wh):
            log.error("Ungültige Optimierer-Eingaben (NaN/Inf) in %s – "
                      "Fallback 'auto' ohne Eingriffe.", bad or ["initial_house_soc_wh"])
            return self._neutral_result(inp, "InvalidInput")

        # p10 (optional): einzelne Luecken slotweise auffuellen. Zuvor hat ein
        # einziges NaN (real: letzter Nacht-Slot) das konservative Band fuer
        # den kompletten 48-h-Horizont deaktiviert.
        pv10, p10_missing, p10_factor = _complete_pv10(
            inp.pv10_w, inp.pv_w)
        if p10_missing:
            log.warning("PV-p10: %d fehlende Slots konservativ mit %.0f %% "
                        "des Erwartungswerts ersetzt.",
                        p10_missing, 100.0 * float(p10_factor))

        # Derselbe verschobene Vorplan dient sowohl als Solver-Warmstart als
        # auch (nur bei wirklich fortgeschrittener Zeit) als Stabilitaetsanker.
        # Warmstart NUR im Produktivlauf LESEN (store_warm): sonst würden
        # Dashboard-/Backtest-/Schattenläufe vom prozessweiten Inkumbenten des
        # Produktivlaufs geseedet und wären nicht unabhängig reproduzierbar
        # (und könnten Warmstart-Artefakte erben). store_warm=False -> kalt.
        warm = (_shifted_warm_values(inp.index[0], int(round(dt * 60)))
                if self.store_warm else None)
        previous_plan = (warm if self.stabilize_plan and warm and _warm_cache
                         and pd.Timestamp(inp.index[0]) > _warm_cache["start"]
                         else None)

        use_car = bool(veh.enabled and inp.car_present and inp.initial_car_soc_wh is not None)

        prob = pulp.LpProblem("EMS", pulp.LpMinimize)

        # ---- Leistungs-Reserve (#2): Nennleistungen fürs Planen deraten, damit
        # dem Echtzeit-Regler Reserve für Sub-Slot-Lastspitzen bleibt (Default
        # hedge=1.0 -> unverändert). Betrifft Akku-Lade/-Entlade + WR-Durchsatz.
        hedge = max(0.0, min(1.0, 1.0 - cfg.optimization.power_headroom_percent / 100.0))
        max_dc = hb.max_dc_charge_w * hedge
        max_ac = hb.max_ac_charge_w * hedge
        max_dis = hb.max_discharge_w * hedge
        max_tot_ch = hb.max_total_charge_w * hedge
        max_inv = cfg.inverter.max_ac_power_w * hedge
        # Mindest-Entladung (#1): 0 oder >= min_dis (auf die Max-Entladung gekappt).
        min_dis = min(cfg.optimization.min_discharge_w, max_dis)
        standby_w = cfg.optimization.standby_discharge_w   # WR-Sockellast (#1)

        # ---- Entscheidungsvariablen ------------------------------------- #
        dc = [pulp.LpVariable(f"dc_{t}", 0, max_dc) for t in range(N)]
        ac = [pulp.LpVariable(f"ac_{t}", 0, max_ac) for t in range(N)]
        dis = [pulp.LpVariable(f"dis_{t}", 0, max_dis) for t in range(N)]
        # PV-Abregelung – realistische Obergrenze je nach vorhandenem Aktor, damit
        # der Plan keine physikalisch unmögliche Abregelung annimmt:
        #  * steuerbares RSCP-Derating -> beliebig abregelbar (None).
        #  * NUR statische Einspeisegrenze -> ein Limiter kappt die PV nur so weit,
        #    bis die Einspeisung auf max_export_w liegt; er kann NICHT darunter
        #    drücken. Obergrenze = physisches WR-Clipping + (PV - Last - Grenze)+.
        #    (Zuvor unbeschränkt -> der Plan konnte am Negativpreis auf 0 Einspeisung
        #    abregeln und "sparte" Kosten, die real weiter anfielen.)
        #  * gar kein Aktor -> nur das ohnehin physische WR-Clipping oberhalb der
        #    AC-Nennleistung (verhindert zugleich Infeasibility bei PV > WR).
        curtail_actuator = bool(
            cfg.e3dc_rscp.control_enabled
            and getattr(cfg.e3dc_rscp, "curtailment_control_enabled", False))
        static_cap = (float(cfg.inverter.max_export_w)
                      if cfg.inverter.max_export_w is not None else None)

        def _curt_ub(t):
            if curtail_actuator:
                return None
            clip = max(0.0, float(inp.pv_w[t]) - max_inv)
            if static_cap is not None:
                load_t = float(np.nan_to_num(inp.house_load_w[t]))
                return clip + max(0.0, float(inp.pv_w[t]) - load_t - static_cap)
            return clip

        curt = [pulp.LpVariable(f"curt_{t}", 0, _curt_ub(t)) for t in range(N)]
        g_imp = [pulp.LpVariable(f"gimp_{t}", 0) for t in range(N)]
        g_exp = [pulp.LpVariable(f"gexp_{t}", 0) for t in range(N)]
        is_ch = [pulp.LpVariable(f"isch_{t}", cat="Binary") for t in range(N)]
        is_di = [pulp.LpVariable(f"isdi_{t}", cat="Binary") for t in range(N)]

        # SoC-Zustände (Wh), soc[0] = Anfangswert
        soc = [pulp.LpVariable(f"soc_{t}", hb.min_soc_wh, hb.max_soc_wh) for t in range(N + 1)]

        if use_car:
            car = [pulp.LpVariable(f"car_{t}", 0, veh.max_charge_w) for t in range(N)]
            is_car = [pulp.LpVariable(f"iscar_{t}", cat="Binary") for t in range(N)]
            soc_car = [
                pulp.LpVariable(f"soccar_{t}", veh.min_soc_wh, veh.capacity_wh)
                for t in range(N + 1)
            ]
        else:
            car = [0.0] * N
            soc_car = None
        car_short: List = []   # Slack je Abfahrt: Fehlmenge zum Ziel-SoC (Wh)
        grid_overload: List = []   # Slack je Slot: Netzbezug über max_import_w (W)

        # ---- Anfangszustände -------------------------------------------- #
        prob += soc[0] == max(hb.min_soc_wh, min(hb.max_soc_wh, inp.initial_house_soc_wh))
        if use_car:
            prob += soc_car[0] == max(
                veh.min_soc_wh, min(veh.capacity_wh, inp.initial_car_soc_wh)
            )

        dep_slots = set(self._departure_slot_indices(inp.index)) if use_car else set()

        M_ch = max_dc + max_ac

        # Netz-Entladen (Akku -> Netz) nur wirtschaftlich sinnvoll: erlaubt in
        # Slot t, wenn der künftige Importpreis unter die Einspeisung fällt (jetzt
        # zur Einspeisung verkaufen, später günstiger zurückladen) ODER der aktuelle
        # Preis negativ ist. Sonst bleibt Akku->Netz gesperrt (verhindert das
        # unwirtschaftliche "Dumpen").
        price = inp.price_ct_kwh
        feedin = inp.feedin_ct_kwh
        gd_allowed = [False] * N
        if cfg.optimization.allow_grid_discharge:
            for t in range(N):
                future_min = min(price[t + 1:]) if t + 1 < N else float("inf")
                gd_allowed[t] = bool(future_min < feedin[t] or price[t] < 0.0)

        # Binär je Slot: entweder Netzbezug ODER Einspeisung – nie gleichzeitig.
        # Damit schließen sich AC-Laden (Import) und Einspeisen physikalisch aus.
        b_grid = [pulp.LpVariable(f"bgrid_{t}", cat="Binary") for t in range(N)]
        # Big-M aus den Anlagenwerten ableiten statt fester 60 kW: knappe obere
        # Schranken für Import (Last + Auto + AC-Laden) und Export (PV + Ent-
        # laden). Engere Big-Ms beschleunigen CBC und skalieren mit der Anlage.
        load_peak = float(np.max(np.maximum(inp.house_load_w, 0.0))) if N else 0.0
        pv_peak = float(np.max(np.maximum(inp.pv_w, 0.0))) if N else 0.0
        car_max = veh.max_charge_w if use_car else 0.0
        # max. gleichzeitige Leistung der steuerbaren Lasten (fürs Import-Big-M)
        cl_peak = 0.0
        for _ld in getattr(cfg, "controllable_loads", []):
            if not _ld.enabled:
                continue
            if _ld.type == "thermal":
                cl_peak += sum(st.power_w for st in _ld.stages)
            elif _ld.power_profile_w:
                cl_peak += max(_ld.power_profile_w)
            else:
                cl_peak += _ld.power_w
        BIGG = 1.05 * max(load_peak + car_max + hb.max_ac_charge_w + cl_peak,
                          pv_peak + hb.max_discharge_w, 1000.0)

        # Eigenverbrauchs-Priorität (harte Regel): Einspeisen nur erlaubt, wenn der
        # Akku VOLL ist (is_full) ODER mit maximaler Leistung lädt (at_max). Dadurch
        # lädt jeder PV-Überschuss zuerst in den Akku – früh und deterministisch,
        # erst der echte Überlauf wird eingespeist. In Arbitrage-Slots (gd_allowed)
        # entfällt die Regel (dann darf der Akku ins Netz entladen).
        is_full = [pulp.LpVariable(f"full_{t}", cat="Binary") for t in range(N)]
        at_max = [pulp.LpVariable(f"atmax_{t}", cat="Binary") for t in range(N)]
        EPS_SOC = 20.0   # Wh
        EPS_P = 20.0     # W
        strategy = getattr(cfg.optimization, "charge_strategy", "asap")
        auto_peak_basis = None       # nur bei strategy="auto" gefüllt (fürs Dashboard)
        # Peak-Modus: Einspeise-Linie PRO TAG. Einspeisung wird je Kalendertag auf
        # L_tag gedeckelt (alles darunter eingespeist), die PV-Spitze DARÜBER lädt
        # den Akku. Jedes L_tag wird minimiert -> so tief wie möglich, sodass der
        # Akku an JEDEM Tag seine Spitze abschöpft und voll wird.
        _local = inp.index.tz_convert(cfg.general.timezone)
        _daykey = [ts.date() for ts in _local]
        _uniq = sorted(set(_daykey))
        _dayidx = {d: i for i, d in enumerate(_uniq)}
        slot_day = [_dayidx[k] for k in _daykey]
        seasonal_peak = [_seasonal_peak_values(
            cfg.optimization, pd.Timestamp(d).tz_localize(
                cfg.general.timezone) + pd.Timedelta(hours=12)) for d in _uniq]
        export_line = [pulp.LpVariable(f"L_day_{i}", 0) for i in range(len(_uniq))]
        # Linie nur auf Tage anwenden, deren Nachmittags-/Erzeugungsspitze im
        # Horizont liegt. Reine Vormittags-Teiltage am Rand (letzter Tag) bekommen
        # KEINE Linie -> keine sinnlose 0-Linie/Zwangsladung am Horizontende.
        _day_last_hour = {}
        for _k, _ts in zip(_daykey, _local):
            _day_last_hour[_k] = max(_day_last_hour.get(_k, -1), _ts.hour)
        line_day = [bool(_day_last_hour[_uniq[i]] >= 15) for i in range(len(_uniq))]

        # Ladestrategie PRO TAG. "auto": Die Peak-Schwelle beruecksichtigt den
        # pessimistisch fortgeschriebenen SoC zum Beginn des Tagesueberschusses:
        # freie Kapazitaet + Reserve, gedeckelt auf einen konfigurierbaren Anteil
        # der gesamten nutzbaren Kapazitaet. So muss ein teilgeladener Akku nicht
        # mehr so behandelt werden, als waere er morgens sicher leer.
        # "peak"/"asap"/"late" = alle Tage gleich.
        # Für die Entscheidung zählt das pessimistische p10 (falls vorhanden):
        # ein nur auf dem Erwartungswert "sichere" Peak-Tag wird konservativ
        # als asap behandelt (früh einsammeln statt auf die Spitze wetten).
        usable_wh = hb.max_soc_wh - hb.min_soc_wh
        pv_for_mode = pv10 if pv10 is not None else inp.pv_w
        _day_surplus = [0.0] * len(_uniq)
        _day_expected_surplus = [0.0] * len(_uniq)
        for i in range(N):
            _day_surplus[slot_day[i]] += max(0.0, float(pv_for_mode[i]) - float(inp.house_load_w[i])) * dt
            _day_expected_surplus[slot_day[i]] += max(
                0.0, float(inp.pv_w[i]) - float(inp.house_load_w[i])) * dt
        if strategy == "auto":
            # p10-basierte SoC-Fortschreibung bis zum ersten belastbaren
            # Tagesueberschuss. Sie ist absichtlich unabhaengig vom spaeteren
            # Optimierungsergebnis und damit nicht zirkulaer.
            sim_soc = min(hb.max_soc_wh, max(hb.min_soc_wh,
                                             float(inp.initial_house_soc_wh)))
            dawn_soc = [None] * len(_uniq)
            day_first_soc = [None] * len(_uniq)
            for i in range(N):
                d = slot_day[i]
                if day_first_soc[d] is None:
                    day_first_soc[d] = sim_soc
                net = float(pv_for_mode[i]) - float(inp.house_load_w[i])
                if dawn_soc[d] is None and net >= 100.0:
                    dawn_soc[d] = sim_soc
                if net >= 0.0:
                    sim_soc = min(hb.max_soc_wh, sim_soc
                                  + hb.charge_efficiency
                                  * min(net, hb.max_dc_charge_w) * dt)
                else:
                    sim_soc = max(hb.min_soc_wh, sim_soc
                                  - min(-net, hb.max_discharge_w) * dt
                                  / hb.discharge_efficiency)

            reserve_ratio = max(0.0, float(getattr(
                cfg.optimization, "auto_peak_soc_reserve_percent", 10.0)) / 100.0)
            p10_floor_ratio = max(0.0, float(getattr(
                cfg.optimization, "auto_peak_p10_floor_percent", 60.0)) / 100.0)
            expected_override_ratio = max(1.0, float(getattr(
                cfg.optimization, "auto_peak_expected_override_percent", 150.0)) / 100.0)
            auto_threshold = []
            for d in range(len(_uniq)):
                soc0 = dawn_soc[d] if dawn_soc[d] is not None else day_first_soc[d]
                free_wh = max(0.0, hb.max_soc_wh - float(soc0))
                cap_ratio = seasonal_peak[d][0] / 100.0
                auto_threshold.append(min(cap_ratio * usable_wh,
                                          free_wh + reserve_ratio * usable_wh))
            robust_peak = [
                (_day_surplus[i] >= auto_threshold[i]
                 or (pv10 is not None
                     and _day_surplus[i] >= p10_floor_ratio * auto_threshold[i]
                     and _day_expected_surplus[i]
                     >= expected_override_ratio * auto_threshold[i]))
                for i in range(len(_uniq))
            ]
            day_mode = ["peak" if line_day[i] and robust_peak[i] else "asap"
                        for i in range(len(_uniq))]
            auto_peak_basis = {
                str(_uniq[i]): {
                    "p10_kwh": round(_day_surplus[i] / 1000.0, 1),
                    "expected_kwh": round(
                        _day_expected_surplus[i] / 1000.0, 1),
                    "soc_start_pct": round(100.0 * float(
                        dawn_soc[i] if dawn_soc[i] is not None else day_first_soc[i]
                    ) / hb.capacity_wh, 1),
                    "threshold_kwh": round(auto_threshold[i] / 1000.0, 1),
                    "threshold_pct": round(seasonal_peak[i][0], 1),
                    "ramp_penalty_ct_kw": round(seasonal_peak[i][1], 2),
                    "basis": ("p10" if _day_surplus[i] >= auto_threshold[i]
                              else "expected+p10-floor" if robust_peak[i]
                              else "insufficient"),
                    "mode": day_mode[i],
                } for i in range(len(_uniq))
            }
            log.info("Auto-Peak-Bewertung: %s", auto_peak_basis)
        elif strategy == "peak":
            day_mode = ["peak"] * len(_uniq)
        elif strategy == "late":
            day_mode = ["late"] * len(_uniq)
        else:
            day_mode = ["asap"] * len(_uniq)
        log.info("Ladestrategie '%s'%s -> Tage: %s", strategy,
                 " (p10-basiert)" if pv10 is not None and strategy == "auto" else "",
                 {str(_uniq[i]): day_mode[i] for i in range(len(_uniq))})

        # Late-Modus: Zielzeit ist der LETZTE erwartete PV-Ueberschuss eines
        # vollständigen Horizont-Tages. Der letzte angeschnittene Tag wird wie
        # beim Peak-Modus ausgelassen, damit ein endender Vormittag nicht zu
        # einer verfrühten Volladung führt.
        late_target_slot = {}
        if strategy == "late":
            for d in range(len(_uniq)):
                surplus = [t for t in range(N) if slot_day[t] == d
                           and float(inp.pv_w[t]) > float(inp.house_load_w[t]) + 1.0]
                if line_day[d] and surplus:
                    late_target_slot[d] = max(surplus)
            log.info("Late-Zielzeiten: %s", {
                str(_uniq[d]): _local[t].strftime("%H:%M")
                for d, t in late_target_slot.items()})

        # Steuerbare/verschiebbare Lasten (Pool-WP etc.) – leere Liste = No-op.
        from .loads import add_controllable_loads
        cl_power, cl_cost, cl_outputs, cl_mqtt = add_controllable_loads(
            prob, cfg, inp, N, dt, g_imp=g_imp)

        for t in range(N):
            pv_t = float(max(0.0, inp.pv_w[t]))
            load_t = float(max(0.0, inp.house_load_w[t]))

            # DC-Laden und Abregelung nur aus PV
            prob += dc[t] + curt[t] <= pv_t
            pv_to_ac = pv_t - dc[t] - curt[t]   # >= 0 durch obige Schranke
            # DC-Laden zusätzlich auf den PV-ÜBERSCHUSS begrenzen: sonst
            # "tarnt" das LP Netzladen als DC-Laden (ganze PV in den Akku,
            # Hauslast aus dem Netz kaufen) - das kann der E3DC im Automatik-
            # modus nicht ausführen und es umginge den AC-Wirkungsgrad.
            # Echtes Netzladen läuft explizit über ac (Befehl batt_grid_charge_w).
            prob += dc[t] <= max(0.0, pv_t - load_t)

            # Wechselrichter-Durchsatz (mit Leistungs-Reserve #2)
            prob += pv_to_ac + dis[t] + ac[t] <= max_inv

            # AC-Knotenbilanz: Import - Export = Last + Auto + steuerbare Lasten
            #                  + AC-Laden - PV_AC - Entladung
            prob += (
                g_imp[t] - g_exp[t]
                == load_t + car[t] + cl_power[t] + ac[t] - pv_to_ac - dis[t]
            )

            # kein gleichzeitiges Laden/Entladen
            prob += dc[t] + ac[t] <= M_ch * is_ch[t]
            prob += dis[t] <= max_dis * is_di[t]
            prob += is_ch[t] + is_di[t] <= 1
            # Mindest-Entladeleistung (#1): 0 oder >= min_dis
            if min_dis > 0.0:
                prob += dis[t] >= min_dis * is_di[t]

            # Entladen nur zur Deckung der Restlast (+ steuerbare Lasten + Auto).
            # Bei PV-Überschuss kann der E3DC im Automatikmodus nicht "für den
            # Export" entladen - das wäre Akku->Netz und braucht den expliziten
            # grid_discharge-Pfad (gd_allowed). Verhindert zudem, dass die Linien-
            # Minimierung des Folgetags den Akku abends zur Einspeisevergütung
            # leerverkauft. WICHTIG: steuerbare Lasten (Pool-WP) gehören zur
            # lokalen Last - ohne cl_power dürfte der Akku sie nicht decken und
            # ihr Verbrauch (z.B. nachts) käme unnötig teuer aus dem Netz.
            if not gd_allowed[t]:
                prob += dis[t] <= max(0.0, load_t - pv_t) + cl_power[t] + car[t]
                # Da cl_power eine VARIABLE ist, ist die Obergrenze allein nicht
                # dicht: der Akku dürfte "für den Pool" entladen, obwohl die PV
                # den Pool schon deckt - die frei werdende PV ginge ins Netz
                # (getarntes Akku->Netz zur Einspeisevergütung). Dicht wird es
                # mit dem Ausschluss: NIE gleichzeitig entladen UND einspeisen
                # (im Ein-Knoten-Modell ist jede Entladung bei laufendem Export
                # ökonomisch ein Akku->Netz-Dump; weniger exportieren wäre immer
                # möglich). Arbitrage-Slots (gd_allowed) bleiben ausgenommen.
                prob += g_exp[t] <= BIGG * (1 - is_di[t])

            # physikalische Gesamt-Ladeleistung des Akkus (DC + AC zusammen)
            prob += dc[t] + ac[t] <= max_tot_ch

            # Eigenverbrauchs-Priorität / kein Akku->Netz (außer Arbitrage):
            prob += soc[t + 1] >= (hb.max_soc_wh - EPS_SOC) * is_full[t]
            prob += dc[t] + ac[t] >= (max_tot_ch - EPS_P) * at_max[t]
            # NETZ-Laden heißt Netz: die AC-Ladung darf den Netzbezug des Slots
            # nicht übersteigen. Ohne diese Kopplung durfte der Solver "ac" aus
            # PV-Überschuss speisen (imp=0) - physikalisch sinnlose Doppel-
            # wandlung (dafür gibt es den effizienteren DC-Pfad), die aber als
            # batt_grid_charge_w-Befehl real ECHTES Netzladen kommandiert hätte
            # (z.B. 87 W "Netzladen" bei 38 ct, gespeist aus PV).
            prob += ac[t] <= g_imp[t]
            # Strategie des jeweiligen Tages (peak/asap/late)
            dm = day_mode[slot_day[t]]
            # Peak-Tag: kein Netzladen (reines PV-Peak-Shaving; verhindert auch
            # das Terminal-Nachlade-Artefakt).
            if dm == "peak":
                prob += ac[t] == 0
            elif dm == "late":
                # Das Max-SoC-Ziel dieses Modus wird ausschließlich mit PV
                # verfolgt. Normale ökonomische Netzlade-Arbitrage bleibt den
                # bisherigen Modi vorbehalten und wird nicht versehentlich
                # durch die hohe Zielstrafe ausgelöst.
                prob += ac[t] == 0
            if not gd_allowed[t]:
                if dm == "peak":
                    # Peak-Tag: kein Akku->Netz-Dump; Einspeisung auf die Tages-
                    # Linie deckeln – aber nur an Tagen mit Spitze im Horizont.
                    # Abregelung zählt für die Linie wie Einspeisung, sonst
                    # könnte der Optimierer "abregeln statt Linie anheben".
                    prob += g_exp[t] <= pv_to_ac
                    if line_day[slot_day[t]]:
                        prob += g_exp[t] + curt[t] <= export_line[slot_day[t]]
                elif dm == "late":
                    # Frühes Einspeisen ist ausdrücklich erlaubt; Entladung ins
                    # Netz bleibt ausgeschlossen. Erst die spätesten noch
                    # benötigten Überschüsse werden in den Akku verschoben.
                    prob += g_exp[t] <= pv_to_ac
                else:
                    # asap: Einspeisen nur, wenn Akku voll ODER mit Max-Leistung lädt.
                    prob += g_exp[t] <= BIGG * (is_full[t] + at_max[t])
            # Abregeln nur, wenn der Akku voll ist ODER mit Max-Leistung lädt -
            # sonst MUSS der Überschuss geladen/exportiert werden. Ohne diese
            # Regel darf der Solver innerhalb der MIP-Gap-Toleranz PV "aus
            # Faulheit" wegwerfen (z.B. 350 W abregeln statt laden, wenn das
            # Export-Gate zu ist und der Ladegewinn nur wenige ct beträgt) -
            # sichtbar als sinnlose Abregel-/Ladesperren-Slots im Plan. Immer
            # zulässig bleibt Abregeln bei vollem Akku (Export-Limit/Peak-Linie/
            # Negativpreis) - genau dann ist es real nötig.
            prob += curt[t] <= BIGG * (is_full[t] + at_max[t])
            # Kein gleichzeitiges Netzladen (Import) und Einspeisen (Export)
            prob += g_imp[t] <= BIGG * b_grid[t]
            prob += g_exp[t] <= BIGG * (1 - b_grid[t])
            # Hausanschluss-Grenze (Sicherung): der geplante Netzbezug soll die
            # Anschlussleistung nicht überschreiten. WEICH mit sehr hoher Strafe
            # (statt hart), damit eine einzelne physikalisch unvermeidbare
            # Lastspitze nicht den GESAMTEN Horizont infeasible macht und auf
            # 'auto ohne Eingriff' verwirft - der betroffene Slot zahlt die
            # Strafe und wird als Alarm gemeldet, der Rest bleibt optimiert.
            if cfg.inverter.max_import_w is not None:
                over = pulp.LpVariable(f"gimpover_{t}", 0)
                grid_overload.append(over)
                prob += g_imp[t] <= cfg.inverter.max_import_w + over

            # Einspeisebegrenzung am Netzanschluss (60/70%-Regel / §9 EEG):
            # keine Erlöse einplanen, die real abgeregelt würden.
            if cfg.inverter.max_export_w is not None:
                prob += g_exp[t] <= cfg.inverter.max_export_w

            # SoC-Dynamik Haus. AC-Laden (Netz) hat einen eigenen (schlechteren)
            # Wirkungsgrad als DC-Laden aus PV - sonst rechnet sich Netzladen
            # systematisch zu günstig.
            prob += soc[t + 1] == (
                soc[t]
                + hb.charge_efficiency * dc[t] * dt
                + hb.eff_ac_charge * ac[t] * dt
                - (1.0 / hb.discharge_efficiency) * dis[t] * dt
                # WR-Sockellast (#1): fixer Entnahmeverlust je aktivem Entlade-Slot
                - standby_w * is_di[t] * dt
            )

            if use_car:
                # semikontinuierlich: 0 oder [min,max]
                prob += car[t] <= veh.max_charge_w * is_car[t]
                prob += car[t] >= veh.min_charge_w * is_car[t]
                # Ohne irgendeine konfigurierte Abfahrt gibt es im Horizont
                # kein Ladeziel. Dann ist Laden nicht nur wirtschaftlich
                # dominiert, sondern wird hart ausgeschlossen, damit die
                # MIP-Gap-Toleranz keinen nutzlosen Einzelslot publiziert.
                if not dep_slots and not veh.has_any_departure:
                    prob += car[t] == 0
                # Ladekurve (Taper): oberhalb taper_start sinkt die maximale
                # Leistung linear bis min_charge_w bei 100 %. Unterhalb ist die
                # Schranke lockerer als max_charge_w -> nicht bindend.
                if veh.taper_start_soc_percent < 100.0:
                    taper_wh = veh.capacity_wh * veh.taper_start_soc_percent / 100.0
                    denom = max(veh.capacity_wh - taper_wh, 1.0)
                    prob += car[t] <= veh.min_charge_w + \
                        (veh.max_charge_w - veh.min_charge_w) * \
                        (veh.capacity_wh - soc_car[t]) / denom
                prob += soc_car[t + 1] == soc_car[t] + veh.charge_efficiency * car[t] * dt
                # Ziel-SoC zur Abfahrt: WEICH (Slack mit hoher Strafe). Ein
                # unerreichbares Ziel (spät angesteckt, Taper) darf nicht die
                # gesamte Optimierung unlösbar machen - stattdessen wird so
                # viel wie möglich geladen und die Fehlmenge gemeldet.
                if t in dep_slots:
                    s = pulp.LpVariable(f"carshort_{t}", 0)
                    prob += soc_car[t] + s >= veh.target_soc_wh
                    car_short.append(s)

        # Late-Ziel weich absichern: 100 % bzw. der konfigurierte maximale SoC
        # am Ende des nutzbaren PV-Fensters. Bei zu wenig PV bildet der Slack
        # die Fehlmenge ab, statt den Gesamtplan infeasible zu machen.
        late_short = []
        for d, target_t in late_target_slot.items():
            short = pulp.LpVariable(f"lateshort_{d}", 0)
            prob += soc[target_t + 1] + short >= hb.max_soc_wh
            late_short.append(short)

        # Ziel-SoC am letzten Slot ebenfalls sichern, falls keine Abfahrt im
        # Horizont liegt, aber an ANDEREN Wochentagen eine kommt (Vorbereitung
        # auf die nächste Abfahrt). Gibt es gar keine Abfahrten, entfällt das.
        if use_car and not dep_slots and veh.has_any_departure:
            s = pulp.LpVariable("carshort_end", 0)
            prob += soc_car[N] + s >= veh.target_soc_wh
            car_short.append(s)

        # ---- Zielfunktion ----------------------------------------------- #
        kwh = dt / 1000.0
        cost_terms = []
        zero_negative = bool(getattr(
            cfg.feed_in, "zero_at_negative_price", False))
        # Negativpreis-Trigger AUSSCHLIESSLICH aus dem echten Börsenpreis – wie
        # die Ersparnis-Abrechnung (savings_validate). Fehlt die Spot-Reihe ganz,
        # NICHT auf den Retailpreis zurückfallen: sonst wären Planung und
        # Abrechnung uneinig darüber, ob ein Slot als Negativpreis zählt. raw_spot
        # (mit Retail-Fallback) bleibt nur für die informative Tabellenausgabe.
        spot_series = inp.spot_price_ct_kwh
        raw_spot = spot_series if spot_series is not None else inp.price_ct_kwh
        negative_export_slots = [
            zero_negative and spot_series is not None
            and float(spot_series[t]) < 0.0
            and float(inp.feedin_ct_kwh[t]) <= 0.0
            for t in range(N)]
        negative_export_pen = max(0.0, float(getattr(
            cfg.optimization, "negative_price_export_penalty_ct_kwh", 10.0)))
        for t in range(N):
            cost_terms.append(g_imp[t] * inp.price_ct_kwh[t] * kwh)
            cost_terms.append(-g_exp[t] * inp.feedin_ct_kwh[t] * kwh)
            if negative_export_slots[t] and negative_export_pen:
                # Bei entfallender Vergütung PV zuerst lokal nutzen und den
                # verbleibenden Überschuss abregeln. Der Malus ist auf diese
                # Slots begrenzt und verändert normale Preisfälle nicht.
                cost_terms.append(negative_export_pen * g_exp[t] * kwh)

        # Zyklus-Malus: pen ist als ct je voll ZYKLIERTER kWh gemeint (einmal
        # rein + einmal raus). Auf Lade- UND Entladeleistung angewandt daher
        # 0.5 je Richtung, sonst würde ein Zyklus doppelt bestraft.
        pen = cfg.optimization.cycle_penalty_ct_kwh
        if pen:
            for t in range(N):
                cost_terms.append(0.5 * pen * (dc[t] + ac[t] + dis[t]) * kwh)

        # Zellschonung (optional, Default aus): sanfte Strafe je kWh·h, die der
        # SoC oberhalb der Schwelle gehalten wird -> langes Verweilen bei ~100 %
        # wird leicht unattraktiv, ohne die Ökonomie umzuwerfen. Slack hold[t] >=
        # soc(Slotende) - Schwelle. Nur bei pen>0 gebaut -> sonst 0 zusätzliche
        # Variablen und exakt unverändertes Verhalten.
        # Eigener Name (nicht 'hold_pen'): weiter unten wird 'hold_pen' erneut
        # für den battery_hold_penalty (vermeidbarer Import) vergeben - gleiche
        # lokale Variable für zwei verschiedene Strafen wäre eine stille Falle.
        cell_hold_pen = max(0.0, float(getattr(hb, "full_hold_penalty_ct_kwh", 0.0)))
        if cell_hold_pen > 0.0:
            cell_hold_thr = (max(0.0, min(100.0, float(getattr(
                hb, "full_hold_soc_threshold_percent", 95.0))))
                / 100.0 * hb.capacity_wh)
            cell_hold = [pulp.LpVariable(f"hold_{t}", 0) for t in range(N)]
            for t in range(N):
                prob += cell_hold[t] >= soc[t + 1] - cell_hold_thr   # Slotende-SoC
            cost_terms.append(cell_hold_pen * pulp.lpSum(cell_hold) * kwh)

        # Auto-Ziel-Verfehlung bestrafen (weiche Nebenbedingung, s.o.)
        if car_short:
            cost_terms.append(cfg.optimization.car_target_penalty_ct_kwh *
                              pulp.lpSum(car_short) / 1000.0)
        if late_short:
            late_target_pen = max(0.0, float(getattr(
                cfg.optimization, "late_target_penalty_ct_kwh", 200.0)))
            cost_terms.append(late_target_pen * pulp.lpSum(late_short) / 1000.0)
        # Hausanschluss-Überschreitung (weiche Grenze) sehr teuer bestrafen; die
        # Slacks sind Leistungen (W) -> mit kwh (dt/1000) in Energie umrechnen.
        if grid_overload:
            grid_over_pen = max(0.0, float(getattr(
                cfg.optimization, "grid_overload_penalty_ct_kwh", 1000.0)))
            cost_terms.append(grid_over_pen * pulp.lpSum(grid_overload) * kwh)

        # Wallbox-Schalt-Malus: jeder Einschaltvorgang (0 -> laden) kostet
        # car_switch_penalty_ct. Verhindert Dauer-Takten bei zappeligen
        # Preisen (Schützverschleiß); der erste Slot bleibt frei (Vorzustand
        # unbekannt).
        pen_sw = cfg.optimization.car_switch_penalty_ct
        if use_car and pen_sw:
            car_start = [pulp.LpVariable(f"carstart_{t}", 0, 1) for t in range(1, N)]
            for t in range(1, N):
                prob += car_start[t - 1] >= is_car[t] - is_car[t - 1]
            cost_terms.append(pen_sw * pulp.lpSum(car_start))

        # Batterie-Taktung: Jeden begonnenen Halteblock INNERHALB einer
        # zusammenhängenden Defizitphase und materielle Teilentladung bei
        # gleichzeitigem Netzbezug bestrafen. Ein Blockstart statt nur des
        # Musters 1,0,1 erfasst auch mehrslotige Sperren. Teilentladung umging
        # den Malus sonst, indem der Akku z.B. 382 W statt 0 W weiter entlud.
        # Allgemeine Ein/Aus-Wechsel zu bestrafen wäre falsch: Beim Übergang
        # PV-Überschuss -> Restlast ist das Einschalten natürlich.
        material_import_flags = [None] * N
        partial_discharge_flags = [None] * N
        bat_pen = float(getattr(cfg.optimization, "battery_switch_penalty_ct", 0.0) or 0.0)
        if bat_pen and N > 1:
            hold_blocks = []
            partial_imports = []
            deficits = [max(0.0, float(inp.house_load_w[t])
                            - float(inp.pv_w[t])) for t in range(N)]
            for t in range(1, N):
                # Nur ein echter PV-Ueberschuss trennt Entladephasen natuerlich.
                # Eine kleine positive Haus-Restlast unter min_dis darf den
                # Halteblock-Malus nicht aushebeln: steuerbare Lasten (z.B.
                # Pool-WP) koennen den realen Bedarf im selben Slot deutlich
                # ueber die Mindestentladeleistung anheben.
                if min(deficits[t - 1:t + 1]) <= 1.0:
                    continue
                hold_block = pulp.LpVariable(f"batholdblock_{t}", 0, 1)
                # Die Wiederaufnahme der Entladung beendet genau einen
                # internen Halteblock, unabhängig von dessen Länge. Das
                # natürliche Ende einer Entladephase am Mindest-SoC darf keinen
                # Malus erhalten, weil dort später keine Entladung mehr folgt.
                prob += hold_block >= is_di[t] - is_di[t - 1]
                hold_blocks.append(hold_block)
            # Bis 40 W als Rundungs-/WR-Sockelrest tolerieren. Groessere
            # Teildeckung mit gleichzeitigem Netzbezug bekommt den kleinen
            # Eingriffsmalus; bei realer Last oberhalb der Akku-Maximalleistung
            # bleibt sie weiterhin erlaubt und ueberstimmt den Malus problemlos.
            material_w = 40.0
            for t in range(N):
                has_import = pulp.LpVariable(
                    f"matimp_{t}", 0, 1, cat="Binary")
                partial = pulp.LpVariable(
                    f"partdis_{t}", 0, 1, cat="Binary")
                material_import_flags[t] = has_import
                partial_discharge_flags[t] = partial
                prob += g_imp[t] <= material_w + BIGG * has_import
                prob += partial >= has_import + is_di[t] - 1
                partial_imports.append(partial)
            cost_terms.append(bat_pen * (
                pulp.lpSum(hold_blocks) + pulp.lpSum(partial_imports)))

        # Netzbezug trotz noch nutzbarer Akkuenergie bestrafen (ct/kWh).
        # Anders als ein pauschaler Malus auf alle ungedeckte Restlast ist das
        # NICHT konstant, wenn der Akku insgesamt nicht fuer die ganze Nacht
        # reicht: frueher Netzbezug bei hohem SoC kostet den Malus, spaeter
        # physikalisch unvermeidbarer Bezug am Mindest-SoC nicht. So darf ein
        # Preisvorteil die reale Entladesperre nur bei einem ausreichend grossen
        # Spread rechtfertigen. g_imp-ac nimmt echtes Netzladen aus dem Malus.
        hold_pen = float(getattr(
            cfg.optimization, "battery_hold_penalty_ct_kwh", 0.0) or 0.0)
        hold_energy_flags = [None] * N
        if hold_pen:
            avoidable_import = []
            # Nur echten numerischen/WR-Sockel-Puffer am Mindest-SoC als
            # "keine nutzbare Energie mehr" behandeln. Der fruehere pauschale
            # 100-Wh-Puffer erlaubte bis zu rund 0,4 kWh/Tag vorgezogenen
            # Netzbezug und machte im p10-Fall eine kleine Vor-PV-Reserve
            # attraktiv, obwohl der Akku noch liefern konnte.
            soc_buffer_wh = max(1.0, standby_w * dt)
            usable_wh = max(1.0, hb.max_soc_wh - hb.min_soc_wh)
            base_deficits = np.maximum(
                np.asarray(inp.house_load_w, dtype=float)
                - np.asarray(inp.pv_w, dtype=float), 0.0)
            # Zusammenhaengende Restlastphasen enden mit dem naechsten
            # erwarteten PV-Ueberschuss. Eine Binaervariable je Phase prueft,
            # ob die am Start nutzbare Akkuenergie den gesamten Bedarf deckt.
            deficit_end = [None] * N
            deficit_segments = []
            s = 0
            while s < N:
                if base_deficits[s] <= 1.0:
                    s += 1
                    continue
                e = s + 1
                while e < N and base_deficits[e] > 1.0:
                    e += 1
                deficit_segments.append((s, e))
                for k in range(s, e):
                    deficit_end[k] = e
                s = e

            enforce_sufficiency = not bool(
                getattr(cfg.optimization, "allow_grid_discharge", False))
            for seg_no, (start, end) in enumerate(
                    deficit_segments if enforce_sufficiency else []):
                sufficient = pulp.LpVariable(
                    f"batsuff_{seg_no}", 0, 1, cat="Binary")
                need_wh = pulp.lpSum(
                    (base_deficits[k] + car[k] + cl_power[k])
                    * dt / hb.discharge_efficiency
                    + standby_w * dt
                    for k in range(start, end))
                available_wh = soc[start] - hb.min_soc_wh
                # sufficient=0 ist nur erlaubt, wenn der Bedarf die verfuegbare
                # Energie wirklich uebersteigt (1 Wh numerischer Abstand).
                prob += (available_wh - need_wh + 1.0
                         <= usable_wh * sufficient)
                for k in range(start, end):
                    local_import = g_imp[k] - ac[k]
                    unavoidable_base = max(
                        0.0, float(inp.house_load_w[k])
                        - float(inp.pv_w[k]) - max_dis)
                    prob += (local_import <= unavoidable_base + standby_w
                             + car[k] + cl_power[k]
                             + BIGG * (1 - sufficient))

            for t in range(N):
                has_energy = pulp.LpVariable(
                    f"hasbat_{t}", 0, 1, cat="Binary")
                hold_energy_flags[t] = has_energy
                avoidable = pulp.LpVariable(f"avoidimp_{t}", 0, BIGG)
                # has_energy muss 1 sein, sobald nach dem Slot mehr als der
                # kleine Rundungs-/Sockelpuffer ueber Mindest-SoC verbleibt.
                prob += (soc[t + 1] - hb.min_soc_wh
                         <= soc_buffer_wh + usable_wh * has_energy)
                local_import = g_imp[t] - ac[t]
                prob += avoidable >= local_import - BIGG * (1 - has_energy)
                # Ist die Phase nicht suffizient, darf unvermeidbarer Netzbezug
                # trotzdem nur bei einem deutlichen Preisvorteil vorgezogen
                # werden. Verglichen wird bis zum naechsten PV-Ueberschuss.
                end = deficit_end[t]
                future_prices = (inp.price_ct_kwh[t + 1:end]
                                 if end is not None else [])
                future_high = (float(np.max(future_prices))
                               if len(future_prices) else float("-inf"))
                if (enforce_sufficiency
                        and future_high < float(inp.price_ct_kwh[t]) + hold_pen):
                    unavoidable_base = max(
                        0.0, float(inp.house_load_w[t])
                        - float(inp.pv_w[t]) - max_dis)
                    prob += (local_import <= unavoidable_base + standby_w + car[t]
                             + cl_power[t] + BIGG * (1 - has_energy))
                avoidable_import.append(avoidable)
            cost_terms.append(
                hold_pen * pulp.lpSum(avoidable_import) * kwh)

        # ---- Abend-Reserve (weicher SoC-Boden im Abendfenster) ----------- #
        # Hält den Akku im Fenster [start, end) über einem Mindest-SoC, damit er
        # nicht vor der teuren Abendspitze leerläuft. Als Malus (ct je fehlender
        # kWh) statt harter Grenze: nie infeasible (an trüben Tagen ist die
        # Reserve schlicht unerreichbar), und ein hinreichend großer Preisvorteil
        # überstimmt sie. Nach 'end' greift kein Boden mehr -> gezielte Entladung
        # in die Spitze. soc[t+1] = SoC NACH Slot t.
        ev_pct = float(getattr(
            cfg.optimization, "evening_reserve_soc_percent", 0.0) or 0.0)
        ev_pen = float(getattr(
            cfg.optimization, "evening_reserve_penalty_ct_kwh", 0.0) or 0.0)
        ev_auto = bool(getattr(cfg.optimization, "evening_reserve_auto", False))
        if ev_pen > 0.0 and (ev_auto or ev_pct > 0.0):
            # Jede Reserve = (Ziel-SoC Wh, [Fenster-Slots]). Ein Slack je Reserve =
            # tiefste Unterschreitung im Fenster (Wh); nur der Drawdown-Betrag
            # zählt (nicht die Dauer), damit ev_pen ein sauberer ct/kWh-Regler
            # bleibt, direkt mit einem Preis-Spread vergleichbar.
            reserves = []
            if ev_auto:
                # Datengetrieben je Tag: Höhe = Energie für die Restlast während
                # der Abend-Preisspitze; Fenster = ab hold_from bis Peak-Beginn.
                hold_from = int(getattr(
                    cfg.optimization, "evening_reserve_hold_from_hour", 11))
                pfac = float(getattr(
                    cfg.optimization, "evening_reserve_price_factor", 1.15))
                for d in range(len(_uniq)):
                    day_slots = [t for t in range(N) if slot_day[t] == d]
                    evening = [t for t in day_slots if _local[t].hour >= 15]
                    if not evening:
                        continue
                    dmed = float(np.median([inp.price_ct_kwh[t] for t in day_slots]))
                    evmax = max(inp.price_ct_kwh[t] for t in evening)
                    if evmax < pfac * max(dmed, 0.1):
                        continue                      # flacher Tag -> keine Reserve
                    thr = max(0.9 * evmax, pfac * dmed)
                    peak = [t for t in evening if inp.price_ct_kwh[t] >= thr]
                    if not peak:
                        continue
                    block_start = min(peak)
                    energy = sum(max(0.0, float(inp.house_load_w[t])
                                     - float(inp.pv_w[t])) for t in peak) \
                        * dt / hb.discharge_efficiency
                    target = min(hb.max_soc_wh,
                                 hb.min_soc_wh + min(usable_wh, energy))
                    window = [t for t in day_slots
                              if _local[t].hour >= hold_from and t < block_start]
                    if window and target > hb.min_soc_wh + 1.0:
                        reserves.append((target, window))
                        log.info("Abend-Reserve %s: Ziel %.0f%% ab %02d:00 bis %s",
                                 _uniq[d], 100.0 * target / hb.capacity_wh, hold_from,
                                 _local[block_start].strftime("%H:%M"))
            else:
                ev_start = cfg.optimization.evening_reserve_start
                ev_end = cfg.optimization.evening_reserve_end
                reserve_wh = min(hb.max_soc_wh,
                                 max(hb.min_soc_wh, ev_pct / 100.0 * hb.capacity_wh))

                def _in_evening_window(ts):
                    tod = ts.time()
                    if ev_start <= ev_end:        # normales Fenster am selben Tag
                        return ev_start <= tod < ev_end
                    return tod >= ev_start or tod < ev_end   # über Mitternacht

                window_slots = [t for t in range(N) if _in_evening_window(_local[t])]
                if window_slots:
                    reserves.append((reserve_wh, window_slots))

            for i, (target, slots) in enumerate(reserves):
                deficit = pulp.LpVariable(f"evres_deficit_{i}", 0)
                for t in slots:
                    prob += deficit >= target - soc[t + 1]
                # deficit in Wh -> /1000 auf kWh (wie die p10-/Stabilitäts-Slacks)
                cost_terms.append(ev_pen * deficit / 1000.0)

        # Kleiner Tie-Breaker: DC-Laden (PV) gegenüber AC-Laden (Netz) bevorzugen,
        # wenn kostengleich. So wird AC-Laden nur genutzt, wenn es echten Vorteil
        # bringt (günstiger Netzbezug), nicht zum Wegrouten von PV-Überschuss.
        # Abregelung minimal bestrafen: nur als letzter Ausweg (Akku voll und
        # Export begrenzt/wertlos), nie statt Einspeisung mit Vergütung > 0.
        for t in range(N):
            cost_terms.append(0.02 * ac[t] * kwh)
            # Abregelung spürbar (0,5 ct/kWh) bestrafen - weit unter der
            # Einspeisevergütung (verzerrt keine echte Entscheidung), aber
            # genug, dass die MIP-Gap-Toleranz (gapAbs) nie "Abregeln statt
            # Einspeisen" als gleichwertig durchwinkt.
            if not negative_export_slots[t]:
                cost_terms.append(0.5 * curt[t] * kwh)
            # Peak-Tie-Breaker: Bei gleicher Tageslinie und Energiebilanz
            # PV-Laden spaeter am Tag bevorzugen. So exportiert Peak unterhalb
            # der Linie und sammelt erst die Erzeugungsspitze ein, statt
            # beliebige 15-min-Ladezacken zu verteilen. 0,01 ct/kWh ist rein
            # deterministisch und viel kleiner als wirtschaftliche Zielterme.
            if day_mode[slot_day[t]] == "peak":
                local_minute = _local[t].hour * 60 + _local[t].minute
                early_weight = (24 * 60 - local_minute) / (24 * 60)
                cost_terms.append(0.01 * early_weight * dc[t] * kwh)
            elif day_mode[slot_day[t]] == "late" and slot_day[t] in late_target_slot:
                target_t = late_target_slot[slot_day[t]]
                if t <= target_t:
                    day_slots = max(1, sum(
                        1 for k in range(N) if slot_day[k] == slot_day[t]
                        and k <= target_t))
                    early_weight = max(0.0, (target_t - t) / day_slots)
                    late_delay_pen = max(0.0, float(getattr(
                        cfg.optimization, "late_charge_delay_ct_kwh", 5.0)))
                    cost_terms.append(
                        late_delay_pen * early_weight * dc[t] * kwh)

        # p10-Mindestpfad und "moeglichst spaet"-Tie-Breaker koennen sonst
        # gegeneinander arbeiten: Die noetige Zusatzladung wird in EINEN Slot
        # gepackt (Linie -> volle PV-Ladung -> Linie). Ein kleiner Total-
        # Variation-Malus verteilt sie auf benachbarte Peak-Slots, ohne die
        # Tageslinie oder wirtschaftliche Entscheidungen nennenswert zu
        # veraendern. Einheit: ct je kW Aenderung zwischen zwei 15-min-Slots.
        ramp_pen_by_day = [v[1] for v in seasonal_peak]
        ramp_limit = float(getattr(
            cfg.optimization, "peak_charge_max_ramp_w", 0.0) or 0.0)
        if any(ramp_pen_by_day) or ramp_limit > 0.0:
            for t in range(1, N):
                if (slot_day[t] != slot_day[t - 1]
                        or day_mode[slot_day[t]] != "peak"):
                    continue
                if ramp_limit > 0.0:
                    # Nur den Anstieg begrenzen: beim Erreichen von 100 % SoC
                    # muss die Ladeleistung physikalisch sofort abfallen duerfen.
                    prob += dc[t] - dc[t - 1] <= ramp_limit
                ramp_pen = ramp_pen_by_day[slot_day[t]]
                if ramp_pen:
                    ramp = pulp.LpVariable(f"peakram_d{t}", 0)
                    prob += ramp >= dc[t] - dc[t - 1]
                    prob += ramp >= dc[t - 1] - dc[t]
                    cost_terms.append(ramp_pen * ramp / 1000.0)

        # Peak-Tage: Einspeise-Linie L minimieren -> so tief wie möglich, dass der
        # Akku gerade voll wird (Spitze über L lädt den Akku). asap-Tage: über die
        # harte Nebenbedingung oben.
        pw = cfg.optimization.peak_charge_weight
        _peak_line_days = [i for i in range(len(export_line))
                           if line_day[i] and day_mode[i] == "peak"]
        if pw and _peak_line_days:
            cost_terms.append(pw * pulp.lpSum(
                [export_line[i] for i in _peak_line_days]) / 1000.0)

        # p10-Absicherung an Peak-Tagen: Laden darf nur so weit aufgeschoben
        # werden, dass der RESTLICHE p10-Überschuss das aus dem natürlich
        # prognostizierten Morgen-SoC erreichbare Tagesziel noch schafft.
        # Reicht p10 nicht für 100 %, wird das Ziel entsprechend gekappt;
        # sonst würde der Optimierer vor PV-Beginn Akkuenergie horten, um ein
        # unter p10 physikalisch unmögliches Voll-Ziel anzunähern.
        # Weich (15 ct/kWh Slack): deutlich teurer als entgangene Einspeisung
        # -> es wird früh geladen, wann immer physikalisch möglich; aber kein
        # hartes Veto (Anfangs-SoC kann die Grenze anfangs unterschreiten).
        # Im explizit gewaehlten "peak"-Modus erwartet der Nutzer reines
        # Peak-Shaving entlang der Einspeise-Linie. Die konservative p10-
        # Fruehladung wuerde darunter schon am Vormittag laden und erzeugte
        # ein gezacktes Laden/Exportieren. Sie bleibt deshalb dem adaptiven
        # "auto"-Modus vorbehalten; dort ist die Risikoabsicherung Teil der
        # automatischen Strategieentscheidung.
        if pv10 is not None and _peak_line_days and strategy == "auto":
            P10_PEN_CT_KWH = 15.0
            # Der Mindestpfad darf erst einsetzen, nachdem der p10-Ueberschuss
            # dieses Kalendertags wirklich begonnen hat. Andernfalls koppelt
            # der Pfad den Vorabend/die Nacht an die PV des Folgetags: Der
            # Optimierer haelt dann den Akku nachts kuenstlich voll, nur damit
            # er trotz einer schwachen p10-Prognose am Folgetag 100 % erreicht.
            # Das erzeugt Entladesperren vor Sonnenaufgang und widerspricht dem
            # Zweck der Absicherung (bereits vorhandenes PV-Laden nicht zu weit
            # aufschieben). 100 W blendet reine Prognose-/Rundungsreste aus.
            P10_START_SURPLUS_W = 100.0
            sd_arr = np.asarray(slot_day)
            loads = np.maximum(np.asarray(inp.house_load_w, dtype=float), 0.0)
            surplus10 = np.maximum(pv10 - loads, 0.0)
            for d in _peak_line_days:
                idxs = np.where(sd_arr == d)[0]
                s10 = surplus10[idxs]
                active = np.flatnonzero(s10 >= P10_START_SURPLUS_W)
                if not active.size:
                    continue
                first = int(active[0])
                active_s10 = s10[first:]
                total_charge_wh = (hb.charge_efficiency
                                   * float(active_s10.sum()) * dt)
                anchor_t = int(idxs[first])
                # Ziel = min(max_soc, SoC BEIM BEGINN des belastbaren
                # p10-Ueberschusses + danach noch erwartbare p10-Ladung).
                # Damit hebt vorab gehortete Energie Ziel UND Startpunkt gleich
                # stark an und kann den Mindestpfad nicht mehr kuenstlich
                # leichter machen. Die Min-Verknuepfung wird exakt binaer
                # modelliert, damit das Ziel bei ausreichender PV bei 100 %
                # gedeckelt bleibt.
                target = pulp.LpVariable(
                    f"p10target_d{d}", hb.min_soc_wh, hb.max_soc_wh)
                capped = pulp.LpVariable(
                    f"p10cap_d{d}", 0, 1, cat="Binary")
                anchor_target = soc[anchor_t] + total_charge_wh
                target_big_m = usable_wh + total_charge_wh + 1.0
                prob += target <= anchor_target
                prob += target >= anchor_target - target_big_m * capped
                prob += target >= hb.max_soc_wh - target_big_m * (1 - capped)
                # künftiger aktiver p10-Überschuss NACH Slot j (exklusiv)
                suffix = np.concatenate([
                    np.cumsum(active_s10[::-1])[::-1][1:], [0.0]])
                for aj, t in enumerate(idxs[first:]):
                    # Nur solange noch p10-Überschuss aussteht: die Grenze soll
                    # das AUFSCHIEBEN des Ladens begrenzen. Nach PV-Ende wäre
                    # floor = p10_target und würde das normale (teure!) Abend-
                    # entladen blockieren - genau dann muss sie entfallen.
                    if suffix[aj] <= 0.0:
                        continue
                    slack = pulp.LpVariable(f"p10s_{t}", 0)
                    prob += (soc[t + 1] + slack
                             >= target - hb.charge_efficiency * suffix[aj] * dt)
                    cost_terms.append(P10_PEN_CT_KWH * slack / 1000.0)

        # Kurzfristige Planstabilitaet: Bei wirtschaftlich nahezu identischen
        # Loesungen den bereits publizierten Fahrplan beibehalten. Das ist kein
        # harter Lock: echte Preis-/Prognoseaenderungen duerfen den kleinen
        # Malus jederzeit ueberstimmen. Nur fortgeschrittene Live-Horizonte
        # werden verglichen; Wiederholungsloesungen desselben Ursprungs (Tests,
        # Diagnose) bleiben unbeeinflusst.
        stability_pen = float(getattr(
            cfg.optimization, "plan_change_penalty_ct_kw", 0.0) or 0.0)
        stability_h = float(getattr(
            cfg.optimization, "plan_stability_hours", 0.0) or 0.0)
        if previous_plan and stability_pen and stability_h > 0.0:
            stable_slots = min(N, int(np.ceil(stability_h / dt)))
            decisions = [("dc", dc), ("ac", ac), ("dis", dis)]
            if use_car:
                decisions.append(("car", car))
            deltas = []
            for prefix, variables in decisions:
                for t in range(stable_slots):
                    old = float(previous_plan.get(f"{prefix}_{t}", 0.0))
                    delta = pulp.LpVariable(f"plan_delta_{prefix}_{t}", 0)
                    prob += delta >= variables[t] - old
                    prob += delta >= old - variables[t]
                    deltas.append(delta)
            cost_terms.append(stability_pen * pulp.lpSum(deltas) / 1000.0)

        # Terminalwert des gespeicherten Akku-Inhalts (Nutzen -> negativ),
        # mit Entlade-Wirkungsgrad diskontiert. Bei "auto" als FALLENDE
        # Grenzwert-Kurve in drei Segmenten: erstes Drittel über min_soc = Median-
        # preis, mittleres = unteres Quartil, letztes = nur Einspeisung (morgen
        # füllt PV ohnehin nach). Konkav fallend -> das LP füllt das wertvollste
        # Segment zuerst, keine Binärvariablen nötig. Fester Zahlenwert = flach.
        #
        # BEWUSST konservativ (früher p75/Mittel/p25): mit dem höheren Mittel-
        # segment bewertete der Optimierer die "letzte" gespeicherte kWh nahe am
        # Horizont-Ende teurer als den Netzbezug und HORTETE dann Akku-Energie -
        # er importierte/regelte PV ab, statt zu entladen (sinnlos aussehende
        # Abend-Eingriffe im Sommer). Median/p25/Einspeisung behebt das (die
        # Grenz-kWh liegt jetzt unter dem Bezugspreis) und ist zugleich billiger;
        # der Akku füllt sich weiterhin aus PV, da alle Segmente >= Einspeisung.
        tv = cfg.optimization.terminal_soc_value
        if tv == "auto":
            p = np.asarray(inp.price_ct_kwh, dtype=float)
            fin = float(np.mean(inp.feedin_ct_kwh))
            seg_values = sorted([
                max(float(np.percentile(p, 50)), fin),
                max(float(np.percentile(p, 25)), fin),
                fin,
            ], reverse=True)
        else:
            seg_values = [float(tv)] * 3
        usable_cap = hb.max_soc_wh - hb.min_soc_wh
        term_seg = [pulp.LpVariable(f"termseg_{i}", 0, usable_cap / 3.0)
                    for i in range(3)]
        prob += pulp.lpSum(term_seg) <= soc[N] - hb.min_soc_wh
        for i, v in enumerate(seg_values):
            cost_terms.append(-v * hb.discharge_efficiency * term_seg[i] / 1000.0)

        cost_terms.extend(cl_cost)          # Schalt-Malus + Komfort der Lasten
        prob += pulp.lpSum(cost_terms)

        # ---- Lösen ------------------------------------------------------- #
        variables = prob.variables()
        binary_count = sum(
            1 for v in variables
            if v.cat == pulp.LpInteger and v.lowBound == 0 and v.upBound == 1)
        log.info("MILP-Größe: %d Slots, %d Variablen (%d binär), %d Regeln; "
                 "Warmstart %s.", N, len(variables), binary_count,
                 len(prob.constraints()), "ja" if warm else "nein")
        _t0 = time.monotonic()
        prob.solve(make_solver(cfg, warm_values=warm))
        solve_s = time.monotonic() - _t0
        try:
            mip_gap = float(prob.solverModel.getInfo().mip_gap)
            if not np.isfinite(mip_gap):
                mip_gap = None
        except Exception:
            mip_gap = None
        hit_limit = solve_s >= cfg.optimization.solver_time_limit_s - 2.0
        if hit_limit:
            log.warning("Solver-Zeitlimit erreicht (%.0fs von %ds) – Lösung "
                        "kann deutlich suboptimal sein.", solve_s,
                        cfg.optimization.solver_time_limit_s)
        else:
            log.info("Solver fertig in %.1f s.", solve_s)
        status = pulp.LpStatus[prob.status]
        if prob.status != pulp.LpStatusOptimal:
            # Keine (verlässliche) Lösung: pulp.value() liefert dann None und
            # die Extraktion würde abstürzen. Stattdessen neutralen Fahrplan
            # liefern, damit weiterhin publiziert wird (setzt frühere Eingriffe
            # zurück) und Dashboard/InfluxDB konsistent bleiben.
            log.error("Optimierung nicht optimal gelöst (%s) – "
                      "Fallback 'auto' ohne Eingriffe.", status)
            neutral = self._neutral_result(inp, status)
            neutral.solver_seconds = solve_s
            neutral.solver_slots = N
            neutral.solver_variables = len(variables)
            neutral.solver_binaries = binary_count
            neutral.solver_constraints = len(prob.constraints())
            neutral.solver_warm_start = bool(warm)
            neutral.solver_mip_gap = mip_gap
            neutral.solver_hit_limit = hit_limit
            return neutral
        infeasible = False
        # Politur: Gap-Toleranz-Reste exakt wegoptimieren (Binäre fixiert ->
        # LP), DANN erst die Lösung fürs Warmstarten merken - so startet der
        # nächste Zyklus vom polierten Stand statt vom Incumbent mit Resten.
        # Verdächtige Binäre bleiben dabei frei (Mini-MIP), damit die Politur
        # auch Einzel-Blasen auflösen kann:
        #  * Entladesperren-Blase: Import trotz Entladung 0 bei nutzbarem SoC
        #    (real: Sperre GENAU in der teuersten Reststunde) -> is_di frei.
        #  * Abregel-Blase: curt > 0, obwohl Export möglich wäre (real: 6,7 kW
        #    abgeregelt statt eingespeist, weil b_grid im Slot auf der Import-
        #    Seite festhing und den Export sperrte) -> b_grid/is_full/at_max
        #    des Slots frei. Real NÖTIGE Abregelung (Export-Limit, Peak-Linie,
        #    Negativpreis) bleibt: constraint-erzwungen, nicht Binärwahl.
        #  * is_full-Zwang (koppelt beide): fixierte is_full=1-Folge-Slots
        #    erzwingen den vollen Akku zu einem festen Zeitpunkt. Das zwang
        #    real (a) ein 6,8-kW-Netzladen in den Plan (Vollladung vorziehen,
        #    damit Export früher darf) und (b) hielt eine Entladesperre am
        #    Leben (Entladen um 04:45 hätte die Mittags-Vollladung um einen
        #    Slot verzögert - das LP DURFTE nicht, obwohl is_di frei war).
        #    Daher je Verdachts-Slot die is_full/at_max-Kette bis zum ENDE
        #    DER NÄCHSTEN VOLLPHASE freigeben - die Politur darf die
        #    Vollladung dann verschieben.
        _t1 = time.monotonic()

        def _full_chain(t0):
            """is_full/at_max-Namen ab t0 bis zum Ende der nächsten Vollphase
            (erste zusammenhängende is_full=1-Periode im Incumbent)."""
            names, seen_full = set(), False
            for k in range(t0, N):
                f = (is_full[k].varValue or 0.0) > 0.5
                if seen_full and not f:
                    break
                names.update({is_full[k].name, at_max[k].name})
                seen_full = seen_full or f
            return names

        def _suspect_free_names():
            """Verdachts-Binäre der AKTUELLEN Lösung (varValues)."""
            core, chain = set(), set()
            # Vermeidbarer Import: echter Netzbezug (ohne AC-Laden) DEUTLICH über
            # dem WR-Standby, obwohl der Akku noch Energie hat UND NICHT am
            # Maximum entlädt. Deckt sowohl die volle Entladesperre (dis≈0) als
            # auch eine gedrosselte Teilentladung (0<dis<max) ab - beide sind als
            # Warmstart-Artefakt beobachtet. Die Standby-Schwelle verhindert, dass
            # normale Entladeslots (Import ~= standby_w) fälschlich geflaggt werden.
            _imp_floor = max(80.0, standby_w + 60.0)
            for t in range(N):
                hold_susp = (((g_imp[t].varValue or 0.0) - (ac[t].varValue or 0.0))
                             > _imp_floor
                             and (dis[t].varValue or 0.0) < max_dis - 5.0
                             and (soc[t + 1].varValue or 0.0)
                             > hb.min_soc_wh + 100.0)
                ac_susp = (ac[t].varValue or 0.0) > 5.0
                curt_susp = (curt[t].varValue or 0.0) > 5.0
                if hold_susp:
                    core.add(is_di[t].name)
                    # Ein Hold-Incumbent hat diese Hilfsvariablen typischerweise
                    # auf Import=1/Teilentladung=0 fixiert. Blieben sie in der
                    # Politur fest, erzwingt partdis >= matimp+is_di-1 weiterhin
                    # is_di=0 und die freigegebene Entladung kann gar nicht
                    # anlaufen.
                    for aux in (material_import_flags[t],
                                partial_discharge_flags[t],
                                hold_energy_flags[t]):
                        if aux is not None:
                            core.add(aux.name)
                if curt_susp:
                    core.update({b_grid[t].name, is_full[t].name,
                                 at_max[t].name})
                if ac_susp:
                    core.add(b_grid[t].name)
                if hold_susp or ac_susp:
                    chain |= _full_chain(t)
            # Sicherheitsdeckel: Kern-Verdachte immer behalten, Ketten trimmen.
            if len(core) + len(chain) > 256:
                chain = set(sorted(chain)[:max(0, 256 - len(core))])
            return core | chain

        # ITERIEREN: die Politur kann beim exakten Umverteilen selbst neue
        # Blasen derselben Klassen erzeugen (real beobachtet: um eine fixierte
        # is_full-Vollladung zu erfüllen, kaufte das Politur-LP im billigsten
        # Slot 5,8 kW aus dem Netz - der Verdacht existierte im Incumbent noch
        # nicht, also war die Kette nicht freigegeben). Daher nach jeder Runde
        # die Verdachts-Erkennung auf dem POLIERTEN Stand wiederholen, bis
        # keine neuen Verdachte mehr auftauchen (max. 3 Runden, je ~0,3 s).
        def _run_polish():
            free = _suspect_free_names()
            rounds = 0
            while rounds < 3 and _polish_continuous(prob, cfg, free_names=free):
                rounds += 1
                new = _suspect_free_names() - free
                if not new:
                    break
                free |= new
            return rounds, free

        _rounds, _free = _run_polish()
        if _rounds:
            log.info("Politur in %.1f s (%d Runden, %d freie Binäre).",
                     time.monotonic() - _t1, _rounds, len(_free))

        # Kalt-Resolve-Fallback gegen Warmstart-Artefakte: bleibt trotz Politur ein
        # VERMEIDBARER Import übrig (echter Netzbezug deutlich über WR-Standby,
        # obwohl der Akku noch Energie hat UND nicht am Maximum entlädt), einmal
        # OHNE Warmstart neu lösen. Ein Kalt-Solve trifft diese Slots nachweislich
        # sauber; die Politur kommt aus dem warmgestarteten Incumbent nicht immer
        # heraus. Übernommen nur, wenn nicht teurer; der saubere Stand wird als
        # Warmstart gemerkt -> das Artefakt pflanzt sich nicht mehr fort.
        _imp_floor = max(80.0, standby_w + 60.0)

        def _avoidable_import_present():
            for t in range(N):
                # Akku lädt in diesem Slot nicht (sonst ist Import gewollt: Netzladen)
                not_charging = ((dc[t].varValue or 0.0) + (ac[t].varValue or 0.0)) < 5.0
                if (not_charging
                        and (g_imp[t].varValue or 0.0) > _imp_floor
                        and (dis[t].varValue or 0.0) < max_dis - 5.0
                        and (soc[t + 1].varValue or 0.0) > hb.min_soc_wh + 100.0):
                    return True
            return False

        if warm and not hit_limit and _avoidable_import_present():
            warm_obj = pulp.value(prob.objective)
            saved = {v.name: v.varValue for v in variables}
            _tc = time.monotonic()
            prob.solve(make_solver(cfg, warm_values=None))
            cold_s = time.monotonic() - _tc
            cold_ok = (prob.status == pulp.LpStatusOptimal
                       and cold_s < cfg.optimization.solver_time_limit_s - 2.0)
            cold_obj = pulp.value(prob.objective) if cold_ok else None
            if cold_ok:
                _run_polish()
                cold_obj = pulp.value(prob.objective)
            if (cold_ok and cold_obj is not None
                    and cold_obj <= (warm_obj if warm_obj is not None else cold_obj) + 1.0):
                log.info("Kalt-Resolve gegen vermeidbaren Import übernommen "
                         "(warm %.1f -> kalt %.1f ct, +%.1f s).",
                         warm_obj or 0.0, cold_obj, cold_s)
            else:
                for v in variables:
                    v.varValue = saved.get(v.name)
                log.info("Kalt-Resolve verworfen (kein Optimum/nicht besser) – "
                         "Warmstart-Stand behalten.")
        polish_s = time.monotonic() - _t1
        # Lösung für den Warmstart des nächsten Zyklus merken.
        if self.store_warm:
            _store_warm_solution(prob, inp.index[0], int(round(dt * 60)))

        # ---- Ergebnis extrahieren --------------------------------------- #
        def val(v):
            return float(pulp.value(v)) if not isinstance(v, (int, float)) else float(v)

        shortfall = max((val(s) for s in car_short), default=0.0)
        if shortfall > 100.0:
            log.warning("Auto-Ziel-SoC im Plan nicht erreichbar: es fehlen "
                        "%.1f kWh zur Abfahrt.", shortfall / 1000.0)

        grid_over_wh = (sum(val(o) for o in grid_overload) * dt
                        if grid_overload else 0.0)
        if grid_over_wh > 100.0:
            log.warning("Hausanschluss-Grenze im Plan überschritten: %.1f kWh "
                        "über %.0f W – vermutlich unvermeidbare Lastspitze.",
                        grid_over_wh / 1000.0, float(cfg.inverter.max_import_w))

        # Tages-Linien-Werte (Peak-Modus) je Slot. Ungenutzte L (Tage ohne Linie)
        # bleiben None -> als NaN behandeln.
        def _lv(L):
            v = pulp.value(L)
            return float(v) if v is not None else float("nan")
        # Für Peak-Tage gesetzt, sonst NaN (asap-Tage haben keine Linie).
        line_vals = [_lv(L) for L in export_line]

        rows = []
        for t in range(N):
            dc_v, ac_v, dis_v = val(dc[t]), val(ac[t]), val(dis[t])
            curt_v = val(curt[t])
            car_v = val(car[t]) if use_car else 0.0
            imp_v, exp_v = val(g_imp[t]), val(g_exp[t])
            soc_v = val(soc[t + 1])
            # Mikro-Netzladen (< 100 W, MIP-Gap-Rauschen) NIE als Befehl ausgeben:
            # ein "Netzladen 87 W"-Kommando ist für ein 12-kW-System sinnlos und
            # würde real Mode-4-Netzladen erzwingen. ac <= g_imp (Modell) macht
            # das Streichen exakt bilanz-konsistent (Import sinkt um denselben
            # Betrag); die SoC-Abweichung (< 25 Wh) liegt in der Toleranz.
            if 0.0 < ac_v < 100.0:
                imp_v = max(0.0, imp_v - ac_v)
                ac_v = 0.0
            # Abregel-Krümel (MIP-Gap-Rauschen) deterministisch bereinigen: wird
            # im selben Slot bereits EXPORTIERT (Export-Gate offen, kein Import),
            # ist "abregeln statt mehr einspeisen" beweisbar dominiert -> Rest in
            # Export umbuchen. Exakt bilanz-konsistent (pv_to_ac und g_exp steigen
            # um denselben Betrag; Peak-Linie deckelt g_exp+curt, Summe konstant).
            # Schranken: Netzanschluss-Limit und WR-Durchsatz; nur bei positiver
            # Vergütung (sonst wäre Abregeln ggf. gewollt).
            if curt_v > 0.0 and exp_v > 1.0 and float(inp.feedin_ct_kwh[t]) > 0.0:
                cap = (cfg.inverter.max_export_w if cfg.inverter.max_export_w
                       is not None else float("inf"))
                pv_to_ac_v = max(0.0, float(inp.pv_w[t])) - dc_v - curt_v
                room_inv = max_inv - (pv_to_ac_v + dis_v + ac_v)
                shift = max(0.0, min(curt_v, cap - exp_v, room_inv))
                curt_v -= shift
                exp_v += shift
            slot_cost = (imp_v * inp.price_ct_kwh[t] - exp_v * inp.feedin_ct_kwh[t]) * kwh

            # --- Übersetzung in E3DC-Steuerbefehle -----------------------
            # Der E3DC regelt selbst (Eigenverbrauch): lädt aus PV-Überschuss,
            # entlädt zur Lastdeckung. Es wird nur ein LIMIT gesetzt, wenn der
            # Optimierer vom natürlichen Verhalten abweichen will:
            #   * Lade-/Entladelimit = Hardware-Maximum  -> "frei laufen" (kein Eingriff)
            #   * Lade-/Entladelimit < Maximum           -> gezielte Begrenzung/Sperre
            #   * grid_charge_w > 0                      -> Netzladen erzwingen
            pv_t = float(max(0.0, inp.pv_w[t]))
            # Steuerbare Lasten (Pool-WP etc.) sind für den E3DC ganz normale
            # Hausverbraucher: der Eigenverbrauch bedient sie aus PV, BEVOR der Akku
            # geladen wird. Für die "natürliche" Lade-/Entladeleistung daher zur
            # Hauslast zählen - sonst sieht die Klassifikation die vom Pool
            # verbrauchte PV als fehlende Akku-Ladung und meldet fälschlich
            # "Laden gesperrt/gedrosselt", obwohl gar kein Akku-Eingriff vorliegt.
            cl_v = float(max(0.0, val(cl_power[t]))) if cl_power else 0.0
            load_t = float(max(0.0, inp.house_load_w[t])) + cl_v
            nat_charge = min(max(0.0, pv_t - load_t), hb.max_dc_charge_w)
            nat_dis = min(max(0.0, load_t - pv_t), hb.max_discharge_w)
            tol = 5.0
            # Ein Eingriff (Lade-/Entladesperre/-drossel) wird erst ab einer
            # SPÜRBAREN Abweichung vom Eigenverbrauch gemeldet. Bei PV≈Last ist
            # die natürliche Lade-/Entladeleistung nur ein paar Watt Rauschen -
            # daraus keine sinnlose Sperre ableiten (die real nur Sub-Slot-
            # Netzbezug erzwänge). Peak-Shaping ist davon unberührt (dort ist die
            # Abweichung deutlich größer).
            act_floor = 100.0   # W
            full_tol = 0.02 * hb.capacity_wh   # 2 % Toleranz für "praktisch voll/leer"
            battery_full = soc_v >= hb.max_soc_wh - full_tol
            battery_empty = soc_v <= hb.min_soc_wh + full_tol
            # PV-Laden nur dann als "begrenzt" markieren, wenn der Akku noch Platz
            # hätte (sonst lädt er ohnehin nicht weiter -> kein Eingriff, "auto").
            if dc_v < nat_charge - act_floor and not battery_full:
                charge_limit, charge_limited = round(dc_v, 1), 1
            else:
                charge_limit, charge_limited = hb.max_dc_charge_w, 0
            # Entladen nur dann als "gesperrt/gedrosselt" markieren, wenn noch
            # Ladung vorhanden ist (sonst kann er ohnehin nicht entladen).
            if dis_v < nat_dis - act_floor and not battery_empty:
                dis_limit, dis_limited = round(dis_v, 1), 1
            else:
                dis_limit, dis_limited = hb.max_discharge_w, 0
            # Modus. Im Peak-Modus ist das geformte Laden entlang der Linie
            # NORMAL (kein "Eingriff"), bekommt aber den eigenen Modus "peak",
            # damit es in der Zeitleiste sichtbar ist. Gedrosseltes Laden und
            # Entladen sind getrennte Modi.
            peak_shaped = charge_limited and day_mode[slot_day[t]] == "peak"
            late_shaped = charge_limited and day_mode[slot_day[t]] == "late"
            charge_flag = charge_limited and not peak_shaped and not late_shaped
            if ac_v > tol:
                mode = "grid_charge"
            elif charge_flag and charge_limit < tol:
                mode = "block_charge"
            elif dis_limited and dis_limit < tol:
                mode = "hold"                         # Entladen gesperrt (Akku halten)
            elif dis_limited:
                mode = "limit_discharge"              # Entladen gedrosselt
            elif charge_flag:
                mode = "limit_charge"                 # Laden gedrosselt
            elif peak_shaped:
                mode = "peak"                         # geformtes Laden (Linie)
            elif late_shaped:
                mode = "late"                         # Kapazität für später freihalten
            else:
                mode = "auto"
            # Netz-Entladen (Akku -> Netz): der Teil der Einspeisung, der nicht aus
            # PV stammt. Bei allow_grid_discharge=False ist das 0.
            grid_discharge_v = max(0.0, exp_v - max(0.0, pv_t - dc_v - curt_v))
            if grid_discharge_v > tol:
                mode = "grid_discharge"

            row = {
                "house_load_w": float(inp.house_load_w[t]),
                "pv_w": float(inp.pv_w[t]),
                "price_ct_kwh": float(inp.price_ct_kwh[t]),
                "spot_price_ct_kwh": (float(raw_spot[t])
                                       if np.isfinite(float(raw_spot[t])) else np.nan),
                "feedin_ct_kwh": float(inp.feedin_ct_kwh[t]),
                "batt_dc_charge_w": round(dc_v, 1),
                "batt_ac_charge_w": round(ac_v, 1),
                "batt_discharge_w": round(dis_v, 1),
                # E3DC-Steuerbefehle (Limits nur bei Abweichung vom Eigenverbrauch):
                "batt_charge_limit_w": charge_limit,
                "batt_discharge_limit_w": dis_limit,
                "batt_grid_charge_w": round(ac_v, 1),
                "batt_grid_discharge_w": round(grid_discharge_v, 1),
                "charge_limited": float(charge_limited),
                "discharge_limited": float(dis_limited),
                "mode": mode,
                "car_charge_w": round(car_v, 1),
                "grid_import_w": round(imp_v, 1),
                "grid_export_w": round(exp_v, 1),
                "pv_curtail_w": round(curt_v, 1),
                "house_soc_wh": round(soc_v, 1),
                "house_soc_percent": round(100.0 * soc_v / hb.capacity_wh, 2),
                "export_line_w": (round(line_vals[slot_day[t]], 1)
                                  if (line_day[slot_day[t]] and day_mode[slot_day[t]] == "peak")
                                  else np.nan),
                "slot_cost_ct": round(slot_cost, 4),
            }
            if use_car:
                soc_car_v = val(soc_car[t + 1])
                row["car_soc_wh"] = round(soc_car_v, 1)
                row["car_soc_percent"] = round(100.0 * soc_car_v / veh.capacity_wh, 2)
            for _col, _exprs in cl_outputs.items():   # steuerbare Lasten (W / °C)
                row[_col] = round(val(_exprs[t]), 2)
            rows.append(row)

        table = pd.DataFrame(rows, index=inp.index)
        from .explain import add_plan_explanations
        table = add_plan_explanations(table, cfg)
        total = float(table["slot_cost_ct"].sum())
        line_w = (float(line_vals[slot_day[0]])
                  if (line_day[slot_day[0]] and day_mode[slot_day[0]] == "peak") else None)
        return OptimizerResult(
            table=table, total_cost_ct=total, status=status, infeasible=infeasible,
            export_line_w=line_w, car_target_shortfall_wh=round(shortfall, 1),
            grid_overload_wh=round(grid_over_wh, 1),
            solver_hit_limit=hit_limit, load_mqtt_map=cl_mqtt,
            solver_seconds=solve_s, solver_polish_seconds=polish_s,
            solver_slots=N, solver_variables=len(variables),
            solver_binaries=binary_count, solver_constraints=len(prob.constraints()),
            solver_warm_start=bool(warm), solver_mip_gap=mip_gap,
            auto_peak_basis=auto_peak_basis,
        )
