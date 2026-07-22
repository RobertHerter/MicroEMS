"""Thermomodell-Kalibrierung thermischer Lasten (Pool) aus Messdaten.

Das MILP-Thermomodell (ems/loads._add_thermal) rechnet je Slot
    dT/dt = (A_solar * G  -  loss_w_per_k * (T_pool - T_aussen)) / C
mit  A_solar = surface_m2 * solar_absorption  [W je W/m² Globalstrahlung]
und  C = volume_l * 1.163                     [Wh/K].

loss_w_per_k und solar_absorption stammen aus der Config und sind zunächst
geschätzt - die Planungsgüte hängt aber direkt daran. Dieses Modul fittet
beide Koeffizienten per linearer Regression aus der lokalen Historie:

  * load_temp    - gemessene Pooltemperatur je Zyklus (Ist)
  * temperature  - Außentemperatur (Open-Meteo)
  * radiation    - Globalstrahlung (Open-Meteo)
  * load_cmd     - publizierte Heiz-FREIGABE je Zyklus
  * load_feedback - realer Ein/Aus-Zustand jeder Wärmepumpenstufe

Sind vollständige reale Stufenrückmeldungen vorhanden, werden zusätzlich die
effektiven thermischen Leistungen der Wärmepumpen geschätzt. Bis dahin wird
nur über Fenster mit durchgehend gesperrter Heizfreigabe gefittet; so bleibt
ein vom internen Thermostat unbekannt geschalteter Wärmeeintrag ausgeschlossen.
Da der Temperatursensor grob auflöst (~0,1 K) und dT je 15 min darunter liegt,
wird über mehrstündige Fenster differenziert statt je Slot.

Aufruf (druckt Vorschlagswerte, ändert NICHTS an der Config):
    python -m ems.pool_calibration --config config.yaml [--days 30]
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("ems.pool_calibration")

# Fensterlänge fürs Differenzieren: 2 h = 8 Slots. Lang genug, dass die
# Temperaturänderung über der Sensorauflösung liegt; kurz genug für viele
# (überlappende) Stichproben.
WINDOW_SLOTS = 8
SLOT_HOURS = 0.25


# Auto-Übernahme (--apply): Qualitäts-Gates und Plausibilitätsgrenzen. Die
# Übernahme ist GEDÄMPFT (Mittel aus aktuellem Wert und Fit): der Fit läuft
# wöchentlich über ein 30-Tage-Fenster - einzelne untypische Wochen (Dauer-
# regen, Party mit offener Abdeckung) sollen die Planung nicht voll umwerfen.
APPLY_MIN_WINDOWS = 96          # doppelte Mindest-Stichprobe des reinen Fits
APPLY_MIN_R2 = 0.5
APPLY_BLEND = 0.5               # neuer Wert = 0.5*alt + 0.5*Fit
LOSS_BOUNDS = (30.0, 3000.0)    # W/K
ABSORB_BOUNDS = (0.05, 1.0)     # physikalisch sinnvoller Bereich
HEAT_BOUNDS = (300.0, 30000.0)  # effektive thermische Stufenleistung [W]


@dataclass
class FitResult:
    loss_w_per_k: float
    a_solar_w_per_wm2: float          # = surface_m2 * solar_absorption
    n_windows: int
    r2: float
    stage_heat_w: dict = field(default_factory=dict)
    condition_number: Optional[float] = None

    def solar_absorption(self, surface_m2: float) -> Optional[float]:
        if surface_m2 <= 0:
            return None
        return self.a_solar_w_per_wm2 / surface_m2


def fit_thermal_params(t_pool: pd.Series, t_amb: pd.Series, g_solar: pd.Series,
                       permit: pd.Series, capacity_wh_per_k: float,
                       min_windows: int = 48,
                       stage_on: Optional[dict] = None) -> Optional[FitResult]:
    """Verlust- und Solar-Koeffizient aus sicher-aus-Fenstern fitten.

    Alle Serien tz-aware; permit 0/1 nur an geloggten Zyklen (Lücken =
    unbekannt = unbrauchbar). None, wenn zu wenig verwertbare Fenster
    (< min_windows, Default 48 = 4 Tage à ~3 h Aus-Phase).
    """
    freq = f"{int(SLOT_HOURS * 60)}min"
    stage_on = stage_on or {}
    if (t_pool is None or len(t_pool) == 0 or
            (not stage_on and (permit is None or len(permit) == 0))):
        return None
    grid = pd.date_range(t_pool.index.min().floor(freq),
                         t_pool.index.max().ceil(freq), freq=freq)
    tol = pd.Timedelta(freq)
    # Alle Reihen auf das 15-min-Raster mit method="nearest" ziehen: Ta/G aus der
    # Wetterhistorie sind (anders als Annahme) NICHT bündig auf :00/:15 - ihr
    # Zeitindex trägt den Sekunden-Offset des Abrufzeitpunkts. Ein exaktes
    # reindex(grid) träfe keinen einzigen Punkt (-> alles NaN -> 0 Fenster);
    # nearest+Toleranz mappt jeden Rasterpunkt auf den nächstgelegenen Messwert.
    df = pd.DataFrame({
        "T": pd.Series(t_pool).reindex(grid, method="nearest", tolerance=tol),
        "Ta": pd.Series(t_amb).reindex(grid, method="nearest", tolerance=tol),
        "G": pd.Series(g_solar).reindex(grid, method="nearest", tolerance=tol),
        # KEIN Auffüllen: nur wirklich geloggte Zyklen zählen als bekannt.
        "p": pd.Series(permit).reindex(grid, method="nearest", tolerance=tol),
    })
    stage_cols = []
    for name, series in stage_on.items():
        col = f"stage:{name}"
        stage_cols.append(col)
        df[col] = pd.Series(series).reindex(
            grid, method="nearest", tolerance=pd.Timedelta(freq))
    ok = df["T"].notna() & df["Ta"].notna() & df["G"].notna()
    if stage_cols:
        ok &= df[stage_cols].notna().all(axis=1)
    else:
        ok &= df["p"] == 0

    ys, xs = [], []
    w = WINDOW_SLOTS
    for i in range(len(df) - w):
        if not ok.iloc[i:i + w + 1].all():
            continue
        dT = df["T"].iloc[i + w] - df["T"].iloc[i]
        # mittlerer Wärmestrom [W] über das Fenster = C * dT / Dauer
        ys.append(capacity_wh_per_k * dT / (w * SLOT_HOURS))
        features = [df["G"].iloc[i:i + w].mean(),
                    -(df["T"].iloc[i:i + w] - df["Ta"].iloc[i:i + w]).mean()]
        features.extend(df[col].iloc[i:i + w].mean() for col in stage_cols)
        xs.append(features)
    if len(ys) < min_windows:
        log.info("Zu wenig sicher-aus-Fenster (%d < %d) - noch keine "
                 "Kalibrierung möglich.", len(ys), min_windows)
        return None

    y = np.asarray(ys, dtype=float)
    X = np.asarray(xs, dtype=float)

    def _lsq(Xa, ya):
        coef, *_ = np.linalg.lstsq(Xa, ya, rcond=None)
        return coef

    coef = _lsq(X, y)
    # Ausreißer (Badegäste, Nachfüllen, Sensor-Sprünge) einmal verwerfen.
    resid = y - X @ coef
    keep = np.abs(resid) <= 3.0 * (np.std(resid) or 1.0)
    if keep.sum() >= min_windows and keep.sum() < len(y):
        y, X = y[keep], X[keep]
        coef = _lsq(X, y)

    a_solar, loss = float(coef[0]), float(coef[1])
    scale = np.std(X, axis=0)
    scale = np.where(scale > 1e-9, scale, 1.0)
    condition = float(np.linalg.cond(X / scale))
    heat_reliable = bool(stage_cols and np.isfinite(condition) and condition <= 100.0)
    ss_res = float(np.sum((y - X @ coef) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2)) or 1.0
    return FitResult(loss_w_per_k=max(0.0, loss),
                     a_solar_w_per_wm2=max(0.0, a_solar),
                     n_windows=int(len(y)), r2=1.0 - ss_res / ss_tot,
                     stage_heat_w=({name: max(0.0, float(coef[2 + i]))
                                    for i, name in enumerate(stage_on)}
                                   if heat_reliable else {}),
                     condition_number=condition)


def maybe_apply(fit: FitResult, ld, config_path: str) -> Optional[dict]:
    """Fit-Werte gedämpft ins Overlay (config_overrides.yaml) übernehmen,
    wenn die Qualität reicht. config.yaml bleibt unangetastet (load_config
    merged das Overlay). Rückgabe: {Feld: neuer Wert} oder None."""
    from .config import save_override
    from .loads import _slug
    if fit.n_windows < APPLY_MIN_WINDOWS or fit.r2 < APPLY_MIN_R2:
        print(f"  Keine Übernahme: Qualität reicht nicht (Fenster "
              f"{fit.n_windows} >= {APPLY_MIN_WINDOWS}? R² {fit.r2:.2f} >= "
              f"{APPLY_MIN_R2}?).")
        return None
    slug = _slug(ld.name)
    changed = {}
    if LOSS_BOUNDS[0] <= fit.loss_w_per_k <= LOSS_BOUNDS[1]:
        new_loss = round(APPLY_BLEND * fit.loss_w_per_k
                         + (1 - APPLY_BLEND) * ld.loss_w_per_k, 1)
        save_override(config_path,
                      f"controllable_loads_overrides.{slug}.loss_w_per_k",
                      new_loss)
        changed["loss_w_per_k"] = new_loss
    else:
        print(f"  loss_w_per_k {fit.loss_w_per_k:.0f} außerhalb "
              f"{LOSS_BOUNDS} - nicht übernommen.")
    sa = fit.solar_absorption(ld.surface_m2)
    if sa is not None:
        if ABSORB_BOUNDS[0] <= sa <= ABSORB_BOUNDS[1]:
            new_sa = round(APPLY_BLEND * sa
                           + (1 - APPLY_BLEND) * ld.solar_absorption, 2)
            save_override(config_path,
                          f"controllable_loads_overrides.{slug}.solar_absorption",
                          new_sa)
            changed["solar_absorption"] = new_sa
        else:
            print(f"  solar_absorption {sa:.2f} außerhalb {ABSORB_BOUNDS} - "
                  f"nicht übernommen (surface_m2 prüfen?).")
    for stage in ld.stages:
        fitted = fit.stage_heat_w.get(stage.name)
        if fitted is None:
            continue
        if HEAT_BOUNDS[0] <= fitted <= HEAT_BOUNDS[1]:
            new_heat = round(APPLY_BLEND * fitted
                             + (1 - APPLY_BLEND) * stage.heat_w, 0)
            save_override(
                config_path,
                f"controllable_loads_overrides.{slug}.stage_heat_w."
                f"{_slug(stage.name)}", new_heat)
            changed[f"heat_w:{stage.name}"] = new_heat
        else:
            print(f"  heat_w {stage.name}={fitted:.0f} außerhalb "
                  f"{HEAT_BOUNDS} - nicht übernommen.")
    if changed:
        print(f"  Übernommen (gedämpft, ins Overlay): {changed}")
    return changed or None


def run(config_path: str, days: int = 30, apply: bool = False) -> int:
    from .config import load_config
    from .local_history import (read_load_cmd, read_load_stage_on, read_load_temp,
                                read_radiation, read_temperature,
                                write_thermal_calibration)
    config = load_config(config_path)
    tz = config.general.timezone
    now = pd.Timestamp.now(tz=tz)
    start = now - pd.Timedelta(days=days)
    db = config.e3dc_rscp.history_db_path
    for ld in getattr(config, "controllable_loads", []):
        if ld.type != "thermal":
            continue
        t_pool = read_load_temp(db, ld.name, start, now, tz)
        t_amb = read_temperature(db, start, now, tz, f"{int(SLOT_HOURS * 60)}min")
        g = read_radiation(db, start, now, tz, f"{int(SLOT_HOURS * 60)}min")
        permit = read_load_cmd(db, ld.name, start, now, tz)
        stage_names = [stage.name for stage in ld.stages]
        stage_on = read_load_stage_on(db, ld.name, stage_names, start, now, tz)
        # Nur mit vollständig rückgemeldeten Stufen den Heizterm fitten. Sonst
        # bleibt der bewährte sicher-aus-Fit anhand permit=0 aktiv.
        if set(stage_on) != set(stage_names):
            stage_on = None
        cap = ld.volume_l * 1.163
        fit = fit_thermal_params(t_pool, t_amb, g, permit, cap,
                                 stage_on=stage_on)
        print(f"== {ld.name} ==")
        if fit is None:
            print(f"  Noch zu wenig Daten (sicher-aus-Fenster fehlen) - nach "
                  f"ein paar Tagen erneut ausführen (load_cmd-Log läuft).")
            if apply:
                write_thermal_calibration(db, ld.name, {
                    "ts": now, "status": "insufficient", "n_windows": 0,
                    "message": "noch zu wenig verwertbare Messfenster"})
            continue
        cur_a = ld.surface_m2 * ld.solar_absorption
        print(f"  Stichprobe: {fit.n_windows} Fenster à {WINDOW_SLOTS * SLOT_HOURS:.0f} h, R² = {fit.r2:.2f}")
        print(f"  loss_w_per_k:     {fit.loss_w_per_k:7.0f}   (Config: {ld.loss_w_per_k:.0f})")
        print(f"  Solar-Koeffizient:{fit.a_solar_w_per_wm2:7.2f} W/(W/m²)  (Config: {cur_a:.2f})")
        sa = fit.solar_absorption(ld.surface_m2)
        if sa is not None:
            print(f"  -> solar_absorption bei surface_m2={ld.surface_m2:.1f}: "
                  f"{sa:.2f}   (Config: {ld.solar_absorption:.2f})")
        if fit.r2 < 0.3:
            print("  Achtung: R² niedrig - Werte noch mit Vorsicht genießen "
                  "(mehr Daten abwarten).")
        if fit.stage_heat_w:
            print(f"  Reale Heizleistungen: {fit.stage_heat_w}")
        elif stage_on:
            print(f"  Einzel-Heizleistungen nicht übernommen: Messmatrix "
                  f"nicht eindeutig (Kondition {fit.condition_number:.1f}).")
        changed = maybe_apply(fit, ld, config_path) if apply else None
        if apply:
            write_thermal_calibration(db, ld.name, {
                "ts": now, "status": "applied" if changed else "checked",
                "n_windows": fit.n_windows, "r2": fit.r2,
                "old": {"loss_w_per_k": ld.loss_w_per_k,
                        "solar_absorption": ld.solar_absorption,
                        **{f"heat_w:{s.name}": s.heat_w for s in ld.stages}},
                "fitted": {"loss_w_per_k": fit.loss_w_per_k,
                           "solar_absorption": fit.solar_absorption(ld.surface_m2),
                           **{f"heat_w:{k}": v
                              for k, v in fit.stage_heat_w.items()}},
                "applied": changed or {},
                "message": ("reale Stufenrückmeldung verwendet" if stage_on
                            else "sicher-aus-Fenster aus Heizfreigabe verwendet"),
            })
        if not apply:
            print("  Werte bei Bedarf manuell übernehmen (oder --apply).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=30,
                    help="Historienfenster in Tagen (Default 30)")
    ap.add_argument("--apply", action="store_true",
                    help="Werte bei ausreichender Qualität gedämpft ins "
                         "Overlay (config_overrides.yaml) übernehmen")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return run(args.config, args.days, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
