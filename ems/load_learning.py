"""Lastprofil einer verschiebbaren Last aus der Verbrauchsrückmeldung anlernen.

Eine Waschmaschine zieht nicht konstant Leistung: Heizphase, Waschen, Schleudern
sind völlig verschiedene Stufen. Der Optimierer kann das längst abbilden -
``ControllableLoad.power_profile_w`` legt einen Startzyklus mit Leistung JE SLOT
über das MILP - nur musste man das Profil bisher von Hand eintragen.

Dieses Modul lernt es aus der Rückmeldung (Tabelle ``load_feedback``, geschrieben
sobald für die Last ein ``power_topic`` konfiguriert ist):

  1. Läufe erkennen: zusammenhängende Slots über einer Einschaltschwelle,
     kurze Einbrüche (z.B. Pause zwischen Wasch- und Schleudergang) werden
     überbrückt.
  2. Je Slotposition den MEDIAN über die Läufe bilden - nicht den Mittelwert:
     ein einzelner abgebrochener oder ungewöhnlich langer Lauf soll das Profil
     nicht verziehen.
  3. Dauer = Median der Lauflängen.

Übernommen wird wie bei den anderen Kalibrierungen ins Overlay, mit
Qualitäts-Gates und Plausibilitätsgrenzen.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

from .quality import BOUNDS

log = logging.getLogger("ems.load_learning")

# Ein Lauf zaehlt ab dieser Leistung als "an". Absolut, damit auch Standby-
# Verbraucher (Uhr, Display) nicht als Lauf gelten.
ON_THRESHOLD_W = 50.0
# Ein Lauf ist NICHT beendet, sobald die Leistung auf 0 faellt - Waschpause,
# Abpumpen, Trocknungsphase der Spuelmaschine ziehen minutenlang fast nichts.
# Beendet ist er erst, wenn die Leistung eine WEILE unter der Schwelle bleibt.
# Vier Slots = 1 h: laenger als jede geraeteinterne Pause, kuerzer als der
# typische Abstand zwischen zwei Waschgaengen (sonst wuerden zwei Laeufe zu
# einem verschmelzen).
MAX_GAP_SLOTS = 4
MIN_RUN_SLOTS = 2

# Auto-Uebernahme: erst ab mehreren Laeufen, und nur in plausiblen Grenzen.
APPLY_MIN_RUNS = 3
DURATION_BOUNDS_MIN = BOUNDS["load_profile_minutes"]
ENERGY_BOUNDS_KWH = BOUNDS["load_profile_kwh"]


@dataclass
class Run:
    start: pd.Timestamp
    power_w: List[float]

    @property
    def slots(self) -> int:
        return len(self.power_w)

    def energy_kwh(self, dt_hours: float) -> float:
        return float(np.sum(self.power_w)) * dt_hours / 1000.0


@dataclass
class Profile:
    power_w: List[float]
    runtime_minutes: float
    n_runs: int
    energy_kwh: float
    peak_w: float
    runs: List[Run] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return (self.n_runs >= APPLY_MIN_RUNS
                and DURATION_BOUNDS_MIN[0] <= self.runtime_minutes
                <= DURATION_BOUNDS_MIN[1]
                and ENERGY_BOUNDS_KWH[0] <= self.energy_kwh
                <= ENERGY_BOUNDS_KWH[1])


def detect_runs(power: pd.Series,
                threshold_w: float = ON_THRESHOLD_W) -> List[Run]:
    """Zusammenhängende Läufe aus der Leistungsreihe schneiden."""
    if power is None or power.empty:
        return []
    series = power.dropna().sort_index()
    values = series.to_numpy(dtype="float64")
    on = values > threshold_w
    runs: List[Run] = []
    i, n = 0, len(values)
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = i
        gap = 0
        k = i
        while k + 1 < n:
            if on[k + 1]:
                j = k + 1
                gap = 0
            else:
                gap += 1
                if gap > MAX_GAP_SLOTS:
                    break
            k += 1
        if j - i + 1 >= MIN_RUN_SLOTS:
            # j zeigt auf den letzten Slot UEBER der Schwelle - die Pausen
            # dazwischen gehoeren zum Lauf, ein Nachlauf aus Nullen nicht.
            runs.append(Run(series.index[i],
                            [float(max(0.0, v)) for v in values[i:j + 1]]))
        i = j + 1
    return runs


def learn_profile(power: pd.Series, dt_hours: float,
                  threshold_w: float = ON_THRESHOLD_W) -> Optional[Profile]:
    """Profil aus allen erkannten Läufen bilden (Median je Slotposition)."""
    runs = detect_runs(power, threshold_w)
    if not runs:
        return None
    length = int(np.median([r.slots for r in runs]))
    length = max(MIN_RUN_SLOTS, length)
    # Auf die Median-Laenge bringen: laengere Laeufe abschneiden, kuerzere mit
    # 0 auffuellen. Der Median je Position ist damit robust gegen Ausreisser.
    matrix = np.zeros((len(runs), length), dtype="float64")
    for row, run in enumerate(runs):
        take = min(length, run.slots)
        matrix[row, :take] = run.power_w[:take]
    profile = [round(float(v), 1) for v in np.median(matrix, axis=0)]
    energy = float(np.sum(profile)) * dt_hours / 1000.0
    return Profile(power_w=profile,
                   runtime_minutes=round(length * dt_hours * 60.0, 1),
                   n_runs=len(runs), energy_kwh=round(energy, 3),
                   peak_w=round(max(profile) if profile else 0.0, 1),
                   runs=runs)


def maybe_apply(profile: Profile, load, config_path: str) -> Optional[dict]:
    """Profil ins Overlay übernehmen, wenn genug Läufe vorliegen.

    Nicht gedämpft wie beim Thermomodell: ein Lastprofil ist eine Form, kein
    Skalar - ein Mittelwert aus altem und neuem Profil wäre ein Zwitter, den es
    real nie gab. Stattdessen ist die Hürde die Zahl der Läufe.
    """
    from .config import save_override
    from .loads import _slug
    if not profile.usable:
        print(f"  Keine Übernahme: {profile.n_runs} Läufe (nötig "
              f"{APPLY_MIN_RUNS}), {profile.runtime_minutes:.0f} min, "
              f"{profile.energy_kwh:.2f} kWh - außerhalb der Gates.")
        return None
    slug = _slug(load.name)
    save_override(config_path,
                  f"controllable_loads_overrides.{slug}.power_profile_w",
                  profile.power_w)
    save_override(config_path,
                  f"controllable_loads_overrides.{slug}.runtime_minutes",
                  profile.runtime_minutes)
    print(f"  Übernommen (ins Overlay): {len(profile.power_w)} Slots, "
          f"{profile.runtime_minutes:.0f} min, {profile.energy_kwh:.2f} kWh, "
          f"Spitze {profile.peak_w:.0f} W")
    return {"power_profile_w": profile.power_w,
            "runtime_minutes": profile.runtime_minutes}


def _feedback_series(config, load, start, end) -> pd.Series:
    """Gemessene Leistung der Last auf dem Slot-Raster (alle Stufen summiert).

    Direkt aus ``load_feedback`` - der vorhandene Reader dort ist auf thermische
    Lasten mit Stufen-Nennleistung zugeschnitten; hier zaehlt die ROHE gemessene
    Leistung, denn genau ihr Verlauf soll gelernt werden.
    """
    import sqlite3

    tz = config.general.timezone
    freq = f"{int(config.general.slot_minutes)}min"
    begin, finish = pd.Timestamp(start), pd.Timestamp(end)
    con = sqlite3.connect(config.e3dc_rscp.history_db_path)
    try:
        rows = con.execute(
            "SELECT ts, power_w FROM load_feedback WHERE name=? AND ts>=? "
            "AND ts<? AND power_w IS NOT NULL ORDER BY ts",
            (str(load.name), begin.tz_convert("UTC").isoformat(),
             finish.tz_convert("UTC").isoformat())).fetchall()
    finally:
        con.close()
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([r[0] for r in rows], utc=True,
                         format="ISO8601").tz_convert(tz)
    raw = pd.Series([float(r[1]) for r in rows], index=idx, dtype="float64")
    # Mehrere Stufen/Meldungen je Slot: aufsummieren waere doppelt gezaehlt,
    # wenn dasselbe Geraet mehrfach meldet - der Maximalwert je Slot trifft die
    # tatsaechliche Leistungsaufnahme.
    return raw.groupby(raw.index.floor(freq)).max().sort_index()


def run(config_path: str, days: int = 30, apply: bool = False,
        name: Optional[str] = None) -> int:
    from .config import load_config

    config = load_config(config_path)
    tz = config.general.timezone
    now = pd.Timestamp.now(tz=tz)
    start = now - pd.Timedelta(days=days)
    dt = config.general.dt_hours
    loads = [ld for ld in config.controllable_loads
             if ld.type == "deferrable" and (name is None or ld.name == name)]
    if not loads:
        print("Keine verschiebbare Last konfiguriert (type: deferrable).")
        return 1
    for ld in loads:
        print(f"\n{ld.name}: Lastprofil aus {days} Tagen Rückmeldung")
        try:
            power = _feedback_series(config, ld, start, now)
        except Exception as exc:
            print(f"  Rückmeldung nicht lesbar ({exc}) - power_topic gesetzt?")
            continue
        if power.empty or not (power > ON_THRESHOLD_W).any():
            print("  Keine Läufe in der Rückmeldung gefunden.")
            continue
        profile = learn_profile(power, dt)
        if profile is None:
            print("  Kein vollständiger Lauf erkennbar.")
            continue
        print(f"  {profile.n_runs} Läufe, Median {profile.runtime_minutes:.0f} min, "
              f"{profile.energy_kwh:.2f} kWh, Spitze {profile.peak_w:.0f} W")
        print("  Profil (W je Slot): "
              + ", ".join(f"{v:.0f}" for v in profile.power_w))
        for r in profile.runs[-5:]:
            print(f"    {r.start:%d.%m %H:%M}  {r.slots * dt * 60:5.0f} min  "
                  f"{r.energy_kwh(dt):5.2f} kWh")
        if apply:
            maybe_apply(profile, ld, config_path)
        elif profile.usable:
            print("  Mit --apply ins Overlay übernehmen.")
    return 0


def main() -> int:                                          # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(
        description="Lastprofil verschiebbarer Lasten aus der Rückmeldung lernen")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--name", help="nur diese Last")
    ap.add_argument("--apply", action="store_true",
                    help="gelerntes Profil ins Overlay übernehmen")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    return run(args.config, args.days, apply=args.apply, name=args.name)


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(main())
