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

Die Ladeseite wird bewusst NICHT gefittet: Ladephasen enden meist bei 100 %
(abgeregelt) und sind zu kurz gegenüber der SoC-Auflösung - die Messung liefert
dort physikalisch unmögliche Werte über 1,0.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

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
