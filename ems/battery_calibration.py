"""Entladewirkungsgrad des Speichers aus den Messdaten bestimmen.

Der Optimierer rechnet mit ``house_battery.discharge_efficiency``: wie viel SoC
eine ans Haus gelieferte kWh kostet. Steht der Wert zu hoch, sagt der Plan einen
zu flachen SoC-Verlauf voraus - über eine Nacht summiert sich das auf zweistellige
Prozentpunkte, ohne dass der Drift-Monitor anschlägt (der vergleicht nur einen
Slot voraus, und der Plan startet alle 15 min neu beim gemessenen SoC).

Gemessen wird über ZUSAMMENHÄNGENDE Entladephasen, nicht je Slot: der SoC kommt
nur in ganzen Prozent (bei 22 kWh sind das ~223 Wh). Eine Slot-weise Zuordnung
greift damit genau die Slots heraus, in denen der Zähler umspringt, und liefert
unsinnige Werte bis über 1,0. Über eine Phase von mehreren Stunden mit
zweistelligem SoC-Hub mittelt sich die Quantisierung dagegen heraus.

    Wirkungsgrad = (gelieferte AC-Energie + Standby) / SoC-Entnahme

Die Ladeseite liefert KEINEN Wirkungsgrad, sondern die Kapazität. Der Grund ist,
dass beide aus SoC-Daten gar nicht trennbar sind: gemessen wird immer nur das
Produkt. Aus einer Ladephase folgt

    Kapazitaet = Ladewirkungsgrad * zugefuehrte Energie / SoC-Hub

Der Anker ist also ``house_battery.charge_efficiency`` - ein DC-Pfad ohne
Wechselrichter, für den ein Herstellerwert belastbar ist. Umgekehrt lieferte die
Rechnung mit einer zu hoch angesetzten Kapazität Ladewirkungsgrade ÜBER 1,0,
also physikalisch Unmögliches. Genau das war der Anlass: ``capacity_wh`` ist der
Nennwert, nicht die gealterte Kapazität, und altert als einziger Modellwert
ungeprüft vor sich hin.

Zwei Fallen der Ladeseite, gegen die hier abgesichert wird:

* **Volles Band.** Nahe 100 % regelt der E3DC ab und das BMS balanciert; der
  Zusammenhang zwischen Energie und SoC ist dort nicht mehr linear. Gewertet
  wird deshalb nur der Teil einer Ladung innerhalb ``CAPACITY_SOC_BAND`` - eine
  Ladung von 12 % auf 100 % wird BESCHNITTEN, nicht verworfen, sonst bliebe an
  einem Sommertag gar keine Phase übrig.
* **Kopplung mit der Entladeseite.** ``discharge_efficiency`` wurde mit der
  ALTEN Kapazität gefittet; im Modell zählt das Produkt beider. Deshalb läuft
  die Kapazität in ``run()`` zuerst und die Entladeseite anschließend mit dem
  neuen Wert - und übernommen wird nur, was die Entladeseite plausibel lässt.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

from .quality import BOUNDS, min_soc_window_wh, soc_energy_wh

log = logging.getLogger("ems.battery_calibration")

# Ein Fenster zählt erst ab dieser Länge und diesem SoC-Hub - kurze Phasen
# werden von der 1-%-Quantisierung dominiert.
MIN_WINDOW_SLOTS = 12           # 3 h bei 15-min-Raster
# Mindest-Hub: leitet sich aus der SoC-Quantisierung ab (ems/quality) statt
# eine feste Wattstundenzahl zu raten - bei kleineren Speichern waere 1 kWh
# sonst ein halber Akku, bei groesseren zu wenig.
MIN_WINDOW_DROP_WH = None       # None = aus der Kapazitaet ableiten

# Auto-Übernahme (--apply), wie bei der Pool-Kalibrierung: gedämpft und
# nur innerhalb physikalisch plausibler Grenzen.
APPLY_MIN_WINDOWS = 6
APPLY_MIN_HOURS = 40.0
APPLY_BLEND = 0.5               # neuer Wert = 0.5*alt + 0.5*Messung
EFF_BOUNDS = BOUNDS["discharge_efficiency"]

# --- Kapazitaet aus Ladephasen ---------------------------------------------
# Nur der lineare Teil der Kennlinie: unten haelt der Optimierer ohnehin Abstand,
# oben regelt der E3DC ab und das BMS balanciert - dort steckt Energie im Akku,
# die der SoC nicht mehr abbildet.
CAPACITY_SOC_BAND = (20.0, 90.0)        # Prozent
CAPACITY_MIN_SWING_PCT = 15.0           # Mindest-Hub einer Ladephase
# Kuerzer als bei der Entladeseite: gegen die Quantisierung schuetzt hier schon
# der Mindest-Hub, und Laden mit voller PV-Leistung durchmisst das Band in
# rund zwei Stunden - mit 3 h bliebe fast jeder Sommertag unberuecksichtigt.
CAPACITY_MIN_SLOTS = 8
CAPACITY_MIN_WINDOWS = 4
# Der letzte gewertete Slot schiebt den SoC ueber die Bandgrenze - das ist
# Konstruktion, kein Fehler. Nur ein grober Ueberschuss zaehlt als Verlassen
# des linearen Bereichs.
CAPACITY_BAND_TOLERANCE_PCT = 5.0
# Alterung geht langsam. Ein Sprung ueber diesen Anteil ist kein gealterter
# Akku, sondern ein kaputtes Signal - dann lieber nichts uebernehmen.
CAPACITY_MAX_STEP = 0.25
# Streuung der Einzelphasen (MAD / Median). Der energiegewichtete Fit ist NICHT
# robust: wenige verzogene Phasen mit viel Energie ziehen ihn mit. Ein Lauf auf
# unbrauchbaren Ist-Werten faellt aber durch seine Streuung auf.
#
# Gemessen am 04.08.2026 ueber dieselben 30 Tage:
#   Slotmittel (ab 27.07.)   n= 8  MAD/Median 0.040  Spanne 17.2-19.9 kWh
#   Momentanwerte (davor)    n=15  MAD/Median 0.128  Spanne 15.0-28.6 kWh
# Die 28.6 kWh liegen ueber der Nennkapazitaet - unmoeglich. Verworfen wird der
# GANZE Lauf, nicht einzelne Phasen: ein Filter auf den oberen Rand schnitte nur
# eine Seite der Fehlerverteilung ab und verzerrte systematisch nach unten.
CAPACITY_MAX_DISPERSION = 0.07
CAPACITY_BOUNDS = BOUNDS["battery_capacity_wh"]


@dataclass
class Window:
    start: pd.Timestamp
    slots: int
    ac_wh: float                # ans Haus geliefert (+ Standby-Modell)
    soc_wh: float               # Entnahme laut SoC
    mean_w: float

    @property
    def efficiency(self) -> float:
        return self.ac_wh / self.soc_wh


@dataclass
class FitResult:
    efficiency: Optional[float]
    n_windows: int
    hours: float
    ac_kwh: float
    soc_kwh: float
    spread: Optional[float]     # max - min der Einzelfenster
    windows: List[Window]

    @property
    def usable(self) -> bool:
        return (self.efficiency is not None
                and self.n_windows >= APPLY_MIN_WINDOWS
                and self.hours >= APPLY_MIN_HOURS)


def discharge_windows(actuals: pd.DataFrame, capacity_wh: float,
                      dt_hours: float, standby_w: float = 0.0) -> List[Window]:
    """Zusammenhängende reine Entladephasen aus den Ist-Werten schneiden.

    ``actuals`` braucht die Spalten ``battery_w`` (positiv = laden) und ``soc``
    (Prozent) auf dem Slot-Raster.
    """
    if actuals is None or actuals.empty:
        return []
    frame = actuals.dropna(subset=["battery_w", "soc"]).sort_index()
    if len(frame) < MIN_WINDOW_SLOTS + 1:
        return []
    discharging = (frame["battery_w"] < -20.0).to_numpy()
    power = (-frame["battery_w"]).to_numpy(dtype="float64")
    soc = frame["soc"].to_numpy(dtype="float64")
    # Zeitlücken trennen Phasen: über einen Ausfall hinweg darf nicht
    # integriert werden (der SoC wäre dann nicht mehr zuzuordnen).
    step_s = frame.index.to_series().diff().dt.total_seconds().fillna(
        dt_hours * 3600.0).to_numpy()
    gap = step_s > dt_hours * 3600.0 * 1.5

    out: List[Window] = []
    i, n = 0, len(frame)
    while i < n:
        if not discharging[i] or gap[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and discharging[j + 1] and not gap[j + 1]:
            j += 1
        slots = j - i + 1
        if slots >= MIN_WINDOW_SLOTS:
            end = min(j + 1, n - 1)     # SoC am Ende der letzten Entladung
            drop = soc_energy_wh(soc[i], soc[end], capacity_wh)
            floor_wh = (MIN_WINDOW_DROP_WH if MIN_WINDOW_DROP_WH is not None
                        else min_soc_window_wh(capacity_wh))
            if drop > floor_wh:
                ac = float(power[i:j + 1].sum()) * dt_hours + \
                    standby_w * slots * dt_hours
                out.append(Window(frame.index[i], slots, ac, float(drop),
                                  float(power[i:j + 1].mean())))
        i = j + 1
    return out


def fit_discharge_efficiency(actuals: pd.DataFrame, capacity_wh: float,
                             dt_hours: float,
                             standby_w: float = 0.0) -> FitResult:
    """Wirkungsgrad über alle Fenster (energiegewichtet, nicht als Mittel der
    Einzelquotienten - lange Phasen sind belastbarer als kurze)."""
    windows = discharge_windows(actuals, capacity_wh, dt_hours, standby_w)
    if not windows:
        return FitResult(None, 0, 0.0, 0.0, 0.0, None, [])
    ac = sum(w.ac_wh for w in windows)
    soc = sum(w.soc_wh for w in windows)
    single = [w.efficiency for w in windows]
    return FitResult(
        efficiency=(round(ac / soc, 3) if soc > 0 else None),
        n_windows=len(windows),
        hours=round(sum(w.slots for w in windows) * dt_hours, 1),
        ac_kwh=round(ac / 1000.0, 2), soc_kwh=round(soc / 1000.0, 2),
        spread=(round(max(single) - min(single), 3) if single else None),
        windows=windows)


@dataclass
class ChargeWindow:
    start: pd.Timestamp
    slots: int
    ac_wh: float                # in den Akku geflossen
    soc_gain_pct: float         # SoC-Zuwachs in Prozentpunkten
    mean_w: float

    def capacity_wh(self, charge_efficiency: float) -> float:
        return charge_efficiency * self.ac_wh / (self.soc_gain_pct / 100.0)


@dataclass
class CapacityFit:
    capacity_wh: Optional[float]
    n_windows: int
    hours: float
    ac_kwh: float
    soc_gain_pct: float
    spread_wh: Optional[float]
    dispersion: Optional[float]     # MAD / Median der Einzelphasen
    windows: List[ChargeWindow]

    @property
    def usable(self) -> bool:
        return (self.capacity_wh is not None
                and self.n_windows >= CAPACITY_MIN_WINDOWS)


def charge_windows(actuals: pd.DataFrame, dt_hours: float) -> List[ChargeWindow]:
    """Zusammenhaengende Ladephasen im linearen SoC-Band schneiden.

    Anders als bei der Entladeseite darf der Mindest-Hub NICHT aus der
    Kapazitaet abgeleitet werden - die ist hier ja gerade die Unbekannte.
    Deshalb eine Schranke in Prozentpunkten.
    """
    if actuals is None or actuals.empty:
        return []
    frame = actuals.dropna(subset=["battery_w", "soc"]).sort_index()
    if len(frame) < CAPACITY_MIN_SLOTS + 1:
        return []
    lo, hi = CAPACITY_SOC_BAND
    soc = frame["soc"].to_numpy(dtype="float64")
    power = frame["battery_w"].to_numpy(dtype="float64")
    charging = (power > 20.0) & (soc >= lo) & (soc <= hi)
    step_s = frame.index.to_series().diff().dt.total_seconds().fillna(
        dt_hours * 3600.0).to_numpy()
    gap = step_s > dt_hours * 3600.0 * 1.5

    out: List[ChargeWindow] = []
    i, n = 0, len(frame)
    while i < n:
        if not charging[i] or gap[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and charging[j + 1] and not gap[j + 1]:
            j += 1
        slots = j - i + 1
        if slots >= CAPACITY_MIN_SLOTS:
            end = min(j + 1, n - 1)     # SoC am Ende der letzten Ladung
            gain = float(soc[end] - soc[i])
            if (gain >= CAPACITY_MIN_SWING_PCT
                    and soc[end] <= hi + CAPACITY_BAND_TOLERANCE_PCT):
                energy = float(power[i:j + 1].sum()) * dt_hours
                out.append(ChargeWindow(frame.index[i], slots, energy, gain,
                                        float(power[i:j + 1].mean())))
        i = j + 1
    return out


def fit_capacity(actuals: pd.DataFrame, dt_hours: float,
                 charge_efficiency: float) -> CapacityFit:
    """Kapazitaet ueber alle Ladefenster, energiegewichtet.

    Nicht als Mittel der Einzelquotienten: lange Phasen mit grossem SoC-Hub
    sind gegenueber der 1-%-Quantisierung deutlich belastbarer als kurze.
    """
    windows = charge_windows(actuals, dt_hours)
    if not windows:
        return CapacityFit(None, 0, 0.0, 0.0, 0.0, None, None, [])
    energy = sum(w.ac_wh for w in windows)
    gain = sum(w.soc_gain_pct for w in windows) / 100.0
    single = [w.capacity_wh(charge_efficiency) for w in windows]
    median = float(np.median(single))
    mad = float(np.median([abs(v - median) for v in single]))
    return CapacityFit(
        capacity_wh=(round(charge_efficiency * energy / gain, 1)
                     if gain > 0 else None),
        n_windows=len(windows),
        hours=round(sum(w.slots for w in windows) * dt_hours, 1),
        ac_kwh=round(energy / 1000.0, 2),
        soc_gain_pct=round(gain * 100.0, 1),
        spread_wh=(round(max(single) - min(single), 1) if single else None),
        dispersion=(round(mad / median, 3) if median > 0 else None),
        windows=windows)


def maybe_apply_capacity(fit: CapacityFit, config) -> Optional[dict]:
    """Gemessene Kapazitaet gedaempft ins Overlay - mit Vertraeglichkeitspruefung.

    Die Kapazitaet steckt auch im Nenner der Entladeseite. Ein neuer Wert, der
    den gemessenen Entladewirkungsgrad aus seinen Grenzen treibt, ist deshalb
    kein besserer Wert, sondern ein Hinweis auf ein Messproblem.
    """
    current = float(config.house_battery.capacity_wh)
    if not fit.usable:
        print(f"  Keine Übernahme: {fit.n_windows} Ladephasen im Band "
              f"{CAPACITY_SOC_BAND[0]:.0f}-{CAPACITY_SOC_BAND[1]:.0f} % "
              f"(nötig {CAPACITY_MIN_WINDOWS}).")
        return None
    if (fit.dispersion is not None
            and fit.dispersion > CAPACITY_MAX_DISPERSION):
        print(f"  Keine Übernahme: Streuung der Einzelphasen "
              f"{fit.dispersion:.3f} über {CAPACITY_MAX_DISPERSION:.2f} "
              f"(MAD/Median) – die Ist-Werte tragen die Messung nicht.")
        return None
    if not (CAPACITY_BOUNDS[0] <= fit.capacity_wh <= CAPACITY_BOUNDS[1]):
        print(f"  Keine Übernahme: {fit.capacity_wh:.0f} Wh außerhalb "
              f"{CAPACITY_BOUNDS}.")
        return None
    step = abs(fit.capacity_wh - current) / max(1.0, current)
    if step > CAPACITY_MAX_STEP:
        print(f"  Keine Übernahme: {fit.capacity_wh:.0f} Wh weicht "
              f"{100 * step:.0f} % von {current:.0f} Wh ab (Deckel "
              f"{100 * CAPACITY_MAX_STEP:.0f} %) – Alterung geht langsamer, "
              f"eher SoC- oder Leistungssignal prüfen.")
        return None
    new = round(APPLY_BLEND * fit.capacity_wh + (1 - APPLY_BLEND) * current, 1)
    return {"capacity_wh": new, "current": current}


def maybe_apply(fit: FitResult, config, config_path: str) -> Optional[dict]:
    """Messwert gedämpft ins Overlay übernehmen, wenn die Qualität reicht.

    config.yaml bleibt unangetastet - dort steht der Anlagen-/Herstellerwert,
    das Overlay trägt den gemessenen (wie beim Pool-Thermomodell).
    """
    from .config import save_override
    current = float(config.house_battery.discharge_efficiency)
    if not fit.usable:
        print(f"  Keine Übernahme: Stichprobe zu klein (Fenster "
              f"{fit.n_windows} >= {APPLY_MIN_WINDOWS}? Stunden {fit.hours} "
              f">= {APPLY_MIN_HOURS}?).")
        return None
    if not (EFF_BOUNDS[0] <= fit.efficiency <= EFF_BOUNDS[1]):
        print(f"  Keine Übernahme: {fit.efficiency} außerhalb {EFF_BOUNDS} – "
              f"Kapazität oder SoC-Signal prüfen.")
        return None
    new = round(APPLY_BLEND * fit.efficiency + (1 - APPLY_BLEND) * current, 3)
    save_override(config_path, "house_battery.discharge_efficiency", new)
    print(f"  Übernommen (gedämpft, ins Overlay): "
          f"discharge_efficiency {current} -> {new}")
    return {"discharge_efficiency": new}


def _run_capacity(frame: pd.DataFrame, config, config_path: str, days: int,
                  *, apply: bool) -> None:
    """Kapazitaet messen, berichten und ggf. uebernehmen.

    Setzt ``config.house_battery.capacity_wh`` bei Uebernahme auch im Speicher,
    damit die anschliessende Entladeseite schon mit dem neuen Wert rechnet -
    sonst fielen die beiden gekoppelten Werte in derselben Sitzung auseinander.
    """
    from .config import save_override
    hb = config.house_battery
    eta_c = float(hb.charge_efficiency)
    fit = fit_capacity(frame, config.general.dt_hours, eta_c)
    band = f"{CAPACITY_SOC_BAND[0]:.0f}-{CAPACITY_SOC_BAND[1]:.0f} %"
    print(f"Nutzbare Kapazität aus {days} Tagen Ladephasen (SoC-Band {band})")
    if fit.capacity_wh is None:
        print("  Keine ausreichenden Ladephasen gefunden.")
        return
    print(f"  {fit.n_windows} Phasen, {fit.hours} h, {fit.ac_kwh} kWh zugeführt "
          f"gegen {fit.soc_gain_pct} pp SoC-Zuwachs")
    print(f"  gemessen {fit.capacity_wh / 1000.0:.2f} kWh  (Streuung "
          f"{(fit.spread_wh or 0) / 1000.0:.2f} kWh, MAD/Median "
          f"{fit.dispersion if fit.dispersion is not None else float('nan'):.3f}, Anker "
          f"charge_efficiency {eta_c}), Modell {hb.capacity_wh / 1000.0:.2f} kWh")
    for w in fit.windows:
        print(f"    {w.start:%d.%m %H:%M}  "
              f"{w.slots * config.general.dt_hours:4.1f} h  "
              f"{w.soc_gain_pct:4.1f} pp  {w.capacity_wh(eta_c) / 1000.0:5.2f} kWh")
    if not apply:
        if fit.usable:
            print("  Wert bei Bedarf manuell übernehmen (oder --apply).")
        return

    candidate = maybe_apply_capacity(fit, config)
    if candidate is None:
        return
    new = candidate["capacity_wh"]
    # Vertraeglichkeit: die Entladeseite teilt durch dieselbe Kapazitaet. Was
    # sie aus ihren Grenzen treibt, ist kein besserer Wert.
    standby = float(getattr(config.optimization, "standby_discharge_w", 0.0))
    probe = fit_discharge_efficiency(frame, new, config.general.dt_hours,
                                     standby)
    if probe.efficiency is not None and not (
            EFF_BOUNDS[0] <= probe.efficiency <= EFF_BOUNDS[1]):
        print(f"  Keine Übernahme: mit {new / 1000.0:.2f} kWh käme der "
              f"Entladewirkungsgrad auf {probe.efficiency} und damit außerhalb "
              f"{EFF_BOUNDS} – die beiden Seiten widersprechen sich.")
        return
    save_override(config_path, "house_battery.capacity_wh", new)
    hb.capacity_wh = new
    print(f"  Übernommen (gedämpft, ins Overlay): capacity_wh "
          f"{candidate['current']:.0f} -> {new:.0f} Wh")


def run(config_path: str, days: int = 30, apply: bool = False) -> int:
    from .config import load_config
    from .local_history import read_actual

    config = load_config(config_path)
    tz = config.general.timezone
    hb = config.house_battery
    db = config.e3dc_rscp.history_db_path
    now = pd.Timestamp.now(tz=tz)
    start = now - pd.Timedelta(days=days)
    frame = pd.DataFrame({
        "battery_w": read_actual(db, "battery_w", start, now, tz),
        "soc": read_actual(db, "soc", start, now, tz)})
    standby = float(getattr(config.optimization, "standby_discharge_w", 0.0))

    _run_capacity(frame, config, config_path, days, apply=apply)

    # Erst jetzt die Entladeseite - hb.capacity_wh kann oben neu gesetzt sein,
    # und der Wirkungsgrad haengt an ihr.
    fit = fit_discharge_efficiency(frame, hb.capacity_wh,
                                   config.general.dt_hours, standby)
    print(f"Entladewirkungsgrad aus {days} Tagen Messdaten "
          f"({config_path})")
    if fit.efficiency is None:
        print("  Keine ausreichenden Entladephasen gefunden.")
        return 1
    print(f"  {fit.n_windows} Phasen, {fit.hours} h, {fit.ac_kwh} kWh geliefert "
          f"gegen {fit.soc_kwh} kWh SoC-Entnahme")
    print(f"  gemessen {fit.efficiency}  (Streuung {fit.spread}), "
          f"Modell {hb.discharge_efficiency}")
    for w in fit.windows:
        print(f"    {w.start:%d.%m %H:%M}  {w.slots * config.general.dt_hours:4.1f} h "
              f"{w.mean_w:6.0f} W  {w.efficiency:.3f}")
    if apply:
        maybe_apply(fit, config, config_path)
    elif fit.usable:
        print("  Wert bei Bedarf manuell übernehmen (oder --apply).")
    return 0


def main() -> int:                                          # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(
        description="Entladewirkungsgrad des Speichers messen")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--apply", action="store_true",
                    help="Messwert gedämpft ins Overlay übernehmen")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    return run(args.config, args.days, apply=args.apply)


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(main())
