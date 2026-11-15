"""Anzeigetabelle des Dashboards (heute 00:00 bis Horizontende).

Aus ems/main.py ausgelagert (reine Verschiebung). ``ems.main`` re-exportiert
``_build_display_frame``; der Logger bleibt ``ems.main``.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd

from .forecast import LoadForecaster, intraday_factor_series
from .local_history import read_actual_signal
from .quality import planned_soc_on_measurement_axis
from . import solcast

log = logging.getLogger("ems.main")



def _build_display_frame(repo, config, now, history, result,
                         intraday=(None, None), hist_pv=None, fut_pv=None,
                         e3dc=None) -> pd.DataFrame:
    """Anzeigetabelle heute 00:00 -> Horizontende.

    Enthält Prognosewerte über den gesamten Bereich (pv_w, house_load_w,
    price_ct_kwh) sowie – zum Vergleich – die heutigen IST-Werte bis 'jetzt'
    (actual_*). Steuerbefehle und Prognose-SoC stammen aus der Optimierung
    (Zukunft). Fehlende Signale werden robust übersprungen.
    """
    # Spaet ueber ems.main nachschlagen: dort liegt der Laufzeitzustand, und
    # Tests ersetzen Namen per monkeypatch auf ems.main (kein Zirkelimport,
    # weil erst beim Aufruf importiert wird).
    from . import main as _main
    freq = f"{config.general.slot_minutes}min"
    tz = config.general.timezone
    slot = pd.Timedelta(freq)
    day_start = now.normalize()
    end = result.table.index[-1]
    # Vom Tagesbeginn bis zum letzten optimierten Slot anzeigen.
    full = pd.date_range(day_start, end, freq=freq, tz=tz)  # inkl. Ende
    df = pd.DataFrame(index=full)

    # ---- Prognose über den gesamten Bereich ----
    try:
        temp = _main._read_temp(repo, config,
                          now - timedelta(days=config.forecast.lookback_days), end + slot)
        forecaster = LoadForecaster(config)
        pred_load = forecaster.forecast(history, day_start, len(full),
                                        hist_temp=temp, fut_temp=temp,
                                        hist_pv=hist_pv, fut_pv=fut_pv)
        df["house_load_w"] = pred_load.reindex(full)
        load_p10, load_p90 = forecaster.uncertainty_band(
            history, pred_load, hist_temp=temp, fut_temp=temp)
        df["house_load_p10_w"] = load_p10.reindex(full)
        df["house_load_p90_w"] = load_p90.reindex(full)
    except Exception as exc:  # pragma: no cover
        log.warning("Verbrauchsprognose fürs Dashboard fehlgeschlagen: %s", exc)
    try:
        from .calibration import (apply_pv_correction, load_profile,
                                  pv_profile_for_source)
        prof = pv_profile_for_source(
            load_profile(config.calibration.pv_profile)
            if config.calibration.enabled else None,
            solcast.selected_source(config),
            "solcast" if config.solcast.enabled else "pvlib")
        for col, signal in (("pv_w", "pv_forecast"),
                            ("pv10_w", "pv_forecast_p10"),
                            ("pv90_w", "pv_forecast_p90")):
            if not solcast.available(config, repo, signal):
                continue
            pv = solcast.read_pv_signal(config, repo, signal, day_start, end + slot)
            if prof:
                pv = apply_pv_correction(pv, prof, tz)
            df[col] = pv.reindex(full)
    except Exception as exc:  # pragma: no cover
        log.warning("PV-Prognose fürs Dashboard fehlgeschlagen: %s", exc)
    try:
        price, estimated = _main._price_series(repo, config, full, now, return_estimated=True)
        df["price_ct_kwh"] = price
        df["price_estimated"] = estimated.astype(float)  # 1 = Schätzung, 0 = Börsenpreis
    except Exception as exc:
        log.debug("Preisreihe fuers Dashboard nicht verfuegbar: %s", exc)
    # Was der 00:00-Plan für die inzwischen VERÖFFENTLICHTEN Slots nur geschätzt
    # hatte. Nur dieser Vergleich macht die Preisunsicherheit im Dashboard
    # sichtbar: im laufenden Plan sind die geschätzten Slots zwangsläufig genau
    # die ohne Börsenpreis - ein "Ist" dazu gibt es im selben Lauf nie. Der
    # Folgetagspreis erscheint erst ab ~13:00, dann wird die Morgen-Schätzung
    # nachträglich bewertbar.
    try:
        from .local_history import read_optimizer_forecast_asof
        _, first = read_optimizer_forecast_asof(
            config.e3dc_rscp.history_db_path,
            day_start + timedelta(minutes=20), day_start, end + slot, tz)
        if first is not None and "price_ct_kwh" in first.columns:
            est_col = next((c for c in ("price_ct_kwh_estimated",
                                        "price_estimated")
                            if c in first.columns), None)
            guess = first["price_ct_kwh"].reindex(full)
            if est_col is not None:      # nur der geschätzte Teil ist relevant
                guess = guess.where(
                    first[est_col].reindex(full).fillna(0.0) > 0.5)
            if guess.notna().any():
                df["plan0_price_ct_kwh"] = guess
    except Exception as exc:            # pragma: no cover - nur Anzeige
        log.debug("Preis-Schätzung des 00:00-Plans nicht lesbar (%s).", exc)

    # Intraday-Korrektur nur auf den ZUKUNFTS-Teil anwenden (konsistent mit
    # der Optimierung); im Vergangenheits-Teil bleibt die rohe Modellprognose
    # sichtbar (Vergleich Ist vs. Modell).
    load_ratio, pv_ratio = intraday
    for col, ratio in (("house_load_w", load_ratio),
                       ("house_load_p10_w", load_ratio),
                       ("house_load_p90_w", load_ratio), ("pv_w", pv_ratio),
                       ("pv10_w", pv_ratio), ("pv90_w", pv_ratio)):
        if ratio is not None and col in df.columns:
            decay = (config.forecast.intraday_load_decay_hours
                     if col.startswith("house_load_") or col == "house_load_w"
                     else config.forecast.intraday_pv_decay_hours)
            is_load = col.startswith("house_load_") or col == "house_load_w"
            fac = intraday_factor_series(
                ratio, full, now, decay,
                max_slots=(None if is_load else
                           config.forecast.intraday_pv_operational_slots),
                slot_minutes=config.general.slot_minutes,
                # Damit archivierte und angezeigte Reihen dieselbe Korrektur
                # tragen wie die produktive Prognose.
                per_hour=(_main._intraday_hour_ratios(config, now) if is_load
                          else None),
                timezone=config.general.timezone)
            fac[full <= now] = 1.0
            df[col] = df[col] * fac

    # ---- Zukunftswerte aus der Optimierung (jetzt -> Ende) ----
    ot = result.table
    for c in ["house_soc_percent", "car_soc_percent", "batt_dc_charge_w",
              "batt_ac_charge_w", "batt_discharge_w", "batt_charge_limit_w",
              "batt_discharge_limit_w", "batt_grid_charge_w",
              "batt_grid_discharge_w", "car_charge_w",
              "grid_import_w", "grid_export_w", "export_line_w", "mode",
              "feedin_ct_kwh", "spot_price_ct_kwh", "pv_curtail_w", "decision_reason",
              "execution_path", "execution_label", "execution_detail",
              "decision_energy_kwh", "decision_value_ct",
              "decision_reference_time"]:
        if c in ot.columns:
            df[c] = ot[c].reindex(full)
    for c in ot.columns:                    # steuerbare Lasten (load_*_w / _temp_c)
        if c.startswith("load_"):
            df[c] = ot[c].reindex(full)

    # ---- Historischer, damals tatsächlich publizierter Sollstand ----
    # Der aktuelle Optimierungslauf beginnt bei ``now``. Ohne den lokalen
    # Verlauf wären alle Steuerkurven davor NaN und die Prognose würde mit dem
    # heutigen Wissensstand nachträglich neu gerechnet. Stattdessen je
    # vergangenem Slot den finalen Forecast-Origin und den zuletzt publizierten
    # Ausführungsplan einblenden.
    try:
        from .local_history import (read_execution_plan_range,
                                    read_optimizer_forecast_origins)
        history_end = now + slot
        forecast_history = read_optimizer_forecast_origins(
            config.e3dc_rscp.history_db_path, day_start, history_end, tz,
            config.general.slot_minutes)
        plan_history = read_execution_plan_range(
            config.e3dc_rscp.history_db_path, day_start, history_end, tz)
        historical_mask = full <= now
        for archived in (forecast_history, plan_history):
            if archived.empty:
                continue
            aligned = archived.reindex(full)
            for c in aligned.columns:
                values = aligned[c]
                valid = historical_mask & values.notna().to_numpy()
                if not valid.any():
                    continue
                if c not in df.columns:
                    df[c] = pd.NA
                df.loc[valid, c] = values.loc[valid]

        # Reale Ein/Aus- und Leistungsrückmeldungen getrennt vom Sollplan.
        # Fehlende/veraltete Rückmeldung bleibt NaN ("unbekannt"), niemals "aus".
        from .loads import _slug as _load_slug
        from .local_history import (read_load_stage_on,
                                    read_load_stage_power)
        for load in getattr(config, "controllable_loads", []) or []:
            stage_names = ([stage.name for stage in load.stages]
                           if load.type == "thermal" else ["__load__"])
            actual_stages = read_load_stage_on(
                config.e3dc_rscp.history_db_path, load.name, stage_names,
                day_start, history_end, tz)
            actual_power = read_load_stage_power(
                config.e3dc_rscp.history_db_path, load.name, stage_names,
                day_start, history_end, tz)
            display_stages = (
                [(stage.name, _load_slug(stage.name)) for stage in load.stages]
                if load.type == "thermal" else
                [("__load__", None)])
            for stage_name, stage_slug in display_stages:
                suffix = f"_{stage_slug}" if stage_slug else ""
                # BEWUSST unverschoben: die Rueckmeldung ist eine Messung zu
                # ihrem Zeitstempel und bleibt es. Der Slot-Versatz an den
                # Schaltflanken wird in der Anzeige als eigener Zustand
                # gefuehrt (dashboard: SWITCH), nicht durch Verbiegen der
                # Messreihe - eine Verschiebung war bei WP Pinguin richtig und
                # bei WP klein falsch (gemessen 03.09.2026: 173 statt 166
                # Treffer bzw. 163 statt 169).
                series = actual_stages.get(stage_name)
                if series is not None and not series.empty:
                    col = f"actual_load_{_load_slug(load.name)}{suffix}_on"
                    df[col] = series.reindex(full).where(historical_mask)
                power = actual_power.get(stage_name)
                if power is not None and not power.empty:
                    col = (f"actual_load_{_load_slug(load.name)}"
                           f"{suffix}_power_w")
                    df[col] = power.reindex(full).where(historical_mask)
    except Exception as exc:  # Verlauf ist Diagnose, nie Lauf-kritisch
        log.warning("Historischer Sollfahrplan fürs Dashboard nicht lesbar: %s",
                    exc)

    # Einheitliche Sollkurven für den direkten Vergleich mit den Istwerten.
    # Historische Werte kommen aus execution_plan, Zukunftswerte aus dem
    # aktuellen Optimierungsergebnis.
    # min_count=1: nur fehlende Einzelkomponenten als 0 behandeln. Fehlen ALLE
    # Planwerte eines Slots, bleibt die Sollkurve NaN statt fälschlich 0 W.
    planned_battery = _main._signed_plan_total(
        df, ("batt_dc_charge_w", "batt_ac_charge_w"),
        ("batt_discharge_w",))
    planned_grid = _main._signed_plan_total(
        df, ("grid_import_w",), ("grid_export_w",))
    if "planned_battery_w" in df.columns:
        df["planned_battery_w"] = pd.to_numeric(
            df["planned_battery_w"], errors="coerce").combine_first(planned_battery)
    else:
        df["planned_battery_w"] = planned_battery
    if "planned_grid_w" in df.columns:
        df["planned_grid_w"] = pd.to_numeric(
            df["planned_grid_w"], errors="coerce").combine_first(planned_grid)
    else:
        df["planned_grid_w"] = planned_grid
    if "mode" in df.columns:
        df["mode"] = df["mode"].fillna("auto")
    else:
        df["mode"] = "auto"

    # Einspeisevergütung durchgehend anzeigen (wie der Preis): die Optimierung
    # liefert sie nur für die Zukunft, die Vergangenheit wird aufgefüllt.
    if "feedin_ct_kwh" in df.columns:
        if config.feed_in.mode == "db" and repo.signal_available("feed_in_tariff"):
            try:
                fi = repo.read_slots("feed_in_tariff", day_start, end + slot)
                df["feedin_ct_kwh"] = df["feedin_ct_kwh"].fillna(fi.reindex(full))
            except Exception as exc:  # pragma: no cover
                log.debug("Einspeisetarif aus der Datenbank nicht lesbar: %s", exc)
        df["feedin_ct_kwh"] = df["feedin_ct_kwh"].fillna(config.feed_in.fixed_ct_kwh)
        if config.feed_in.zero_at_negative_price and "spot_price_ct_kwh" in df.columns:
            df["feedin_ct_kwh"] = df["feedin_ct_kwh"].where(
                ~(df["spot_price_ct_kwh"] < 0.0), 0.0)

    # ---- Heutige IST-Werte (bis jetzt) zum Vergleich ----
    # Bis 'now' lesen (aktuellen Slot einschließen) und im Ist-Bereich vorwärts
    # füllen, damit die Ist-Linie den Jetzt-Marker erreicht und keine Lücke zur
    # Prognose entsteht (überbrückt Mess-Lag der Datenquelle).
    slot = pd.Timedelta(freq)
    past_mask = full <= now
    for col, signal in [("actual_load_w", "house_consumption"),
                        ("actual_pv_w", "pv_generation"),
                        ("actual_soc_percent", "battery_soc"),
                        ("actual_battery_w", "battery_power"),
                        ("actual_grid_w", "grid_power")]:
        try:
            if config.e3dc_rscp.history_source:
                # Lokale Ist-Werte (RSCP), Lücken (z.B. am Umstelltag, bevor die
                # actuals-Tabelle gefüllt war) fürs Dashboard aus der InfluxDB
                # ergänzen, solange dort vorhanden. Rein kosmetisch.
                s = read_actual_signal(config, repo, signal, day_start, now + slot)
                if repo.signal_available(signal):
                    inf = repo.read_slots(signal, day_start, now + slot, fill=False)
                    s = s.combine_first(inf) if not s.empty else inf
            elif repo.signal_available(signal):
                s = read_actual_signal(config, repo, signal, day_start, now + slot)
            else:
                continue
            if not s.empty:
                s = s.reindex(full)
                df[col] = s.where(past_mask).ffill().where(past_mask)
        except Exception as exc:  # pragma: no cover
            log.debug("Ist-Reihe %s fuers Dashboard nicht aufbereitbar: %s", col, exc)

    # Leistungswerte sind Intervallgrößen. Die zu Beginn eines Rechenlaufs
    # gespeicherten Momentanwerte können noch den vorherigen Slot abbilden und
    # erzeugen dann eine scheinbare Soll/Ist-Verschiebung um 15 Minuten.
    # Zeitgewichtete 5-s-Mittel ersetzen sie bei ausreichender Abdeckung;
    # Snapshots bleiben nur als Lücken-Fallback.
    try:
        from .local_history import read_live_slot_averages
        live_slots = read_live_slot_averages(
            config.e3dc_rscp.history_db_path, day_start, now + slot,
            config.general.timezone, config.general.slot_minutes,
            config.forecast.live_nowcast_min_coverage_seconds,
            config.forecast.live_nowcast_max_gap_seconds)
        _main._overlay_live_slot_actuals(df, live_slots, past_mask)
    except Exception as exc:  # Diagnose darf das Dashboard nie verhindern
        log.debug("Live-Slotmittel fürs Dashboard nicht lesbar (%s).", exc)

    # Ist-Hauslast bevorzugt aus dem 15-min-MITTEL (house_load, Energiebilanz)
    # statt aus dem MOMENTAN-Snapshot (actuals.house_w). Der Snapshot ist ein
    # Spot-Messwert je Zyklus und schwankt stark; bei einem Neustart mitten im
    # Slot schreibt er einen willkürlichen Intra-Slot-Moment (oft ein
    # Momentan-Tief -> tiefer Notch, "Ist zu tief"). Das gereifte 15-min-Mittel
    # ist die korrekte Slot-Ist-Last. Der Momentan-Snapshot füllt nur die
    # jüngsten, noch nicht gereiften Slots (letzte ~Stunde), damit die Ist-Linie
    # den Jetzt-Marker erreicht.
    if config.e3dc_rscp.history_source and "actual_load_w" in df.columns:
        try:
            from .local_history import read_house_load
            hl = read_house_load(config.e3dc_rscp.history_db_path,
                                 day_start, now + slot, config.general.timezone)
            hl = hl.where(hl > 0.0)                 # 0/negativ = unreife Bilanz
            mean = hl.reindex(full) if not hl.empty else pd.Series(index=full, dtype=float)
            mean = mean.where(past_mask)
            # Jüngste, noch nicht in house_load gereifte Slots direkt aus der
            # E3DC-15-min-Historie (Energiebilanz-MITTEL) nachholen - so ist die
            # Ist-Kurve bis JETZT glatt statt momentanwert-verzackt. Read-only,
            # gedeckelt auf die letzten ~2 h; schlägt er fehl, bleibt der
            # Momentan-Snapshot als Fallback.
            if e3dc is not None:
                covered = mean.dropna()
                gap_from = (covered.index.max() + slot if not covered.empty
                            else now - pd.Timedelta(hours=2))
                gap_from = max(gap_from, now - pd.Timedelta(hours=2))
                if gap_from <= now:
                    try:
                        recent = e3dc.read_house_load_15min(gap_from, now + slot)
                        if recent:
                            rs = pd.Series({pd.Timestamp(k): v for k, v in recent.items()},
                                           dtype=float).tz_convert(config.general.timezone)
                            rs = rs.where(rs > 0.0).reindex(full).where(past_mask)
                            mean = mean.combine_first(rs)
                    except Exception as exc:  # pragma: no cover
                        log.debug("Nowcast-Mittel der Hauslast nicht einmischbar: %s", exc)
            snap = df["actual_load_w"]               # Momentan-Fallback (Tail)
            df["actual_load_w"] = (mean.combine_first(snap)
                                   .where(past_mask).ffill().where(past_mask))
        except Exception as exc:  # pragma: no cover
            log.debug("Ist-Hauslast fuers Dashboard nicht aufbereitbar: %s", exc)
    # Plan-SoC auf die Messachse legen (Slotende vs. Slotanfang, siehe
    # quality.planned_soc_on_measurement_axis). Eigene Spalte: die rohe
    # house_soc_percent bleibt unangetastet, an ihr haengen Archiv und
    # Ersparnisrechnung.
    for raw_col, aligned_col in (("house_soc_percent",
                                 "planned_soc_aligned_percent"),
                                ("car_soc_percent",
                                 "planned_car_soc_aligned_percent")):
        if raw_col in df.columns:
            df[aligned_col] = planned_soc_on_measurement_axis(
                pd.to_numeric(df[raw_col], errors="coerce"),
                config.general.slot_minutes).reindex(df.index)

    # Explizite Soll-Ist-Differenzen für API, Slot-Details und weitere
    # Auswertungen. Positiv bedeutet: Istwert liegt über der damaligen Prognose.
    for actual_col, planned_col, deviation_col in (
            ("actual_load_w", "house_load_w", "load_deviation_w"),
            ("actual_pv_w", "pv_w", "pv_deviation_w"),
            ("actual_grid_w", "planned_grid_w", "grid_deviation_w"),
            ("actual_battery_w", "planned_battery_w", "battery_deviation_w"),
            ("actual_soc_percent", "planned_soc_aligned_percent",
             "soc_deviation_percent")):
        if actual_col in df.columns and planned_col in df.columns:
            actual = pd.to_numeric(df[actual_col], errors="coerce")
            planned = pd.to_numeric(df[planned_col], errors="coerce")
            decimals = 1 if deviation_col == "soc_deviation_percent" else 0
            df[deviation_col] = (actual - planned).where(
                actual.notna() & planned.notna()).round(decimals)
    return df
