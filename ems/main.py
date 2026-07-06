"""Orchestrierung des EMS-Laufs.

Ablauf je Zyklus:
  1. Verbrauchsprognose (72 h) aus Historie erzeugen und in InfluxDB schreiben.
  2. Eingangsdaten für den Optimierungshorizont (48 h) lesen
     (Preis & PV-Vorhersage für die Zukunft, aktueller Haus-/Auto-SoC).
  3. MILP-Optimierung -> Steuertabelle.
  4. Steuerbefehle per MQTT an Homey publizieren.
  5. Steuertabelle + prognostizierte SoCs/Zustände in InfluxDB schreiben.
  6. Dashboard erzeugen.

Aufruf:
  python -m ems.main --config config.yaml          # einmalig
  python -m ems.main --config config.yaml --loop    # Dauerbetrieb (Intervall aus config)
"""
from __future__ import annotations

import argparse
import logging
import os
import time as _time
from datetime import timedelta

import numpy as np
import pandas as pd

from .config import Config, load_config
from .dashboard import build_dashboard
from .forecast import LoadForecaster, load_history
from .homey_mqtt import HomeyMqttPublisher
from .influx import InfluxRepository
from .optimizer import Optimizer, OptimizerInputs

log = logging.getLogger("ems.main")

CONTROL_COLS = [
    # E3DC-Steuerbefehle (Limits nur bei Abweichung vom Eigenverbrauch):
    "batt_charge_limit_w", "batt_discharge_limit_w",
    "batt_grid_charge_w", "batt_grid_discharge_w",
    "charge_limited", "discharge_limited",
    # rohe Optimierer-Leistungen (Referenz/Analyse):
    "batt_dc_charge_w", "batt_ac_charge_w", "batt_discharge_w",
    "car_charge_w", "grid_import_w", "grid_export_w",
]
PREDICTION_COLS = [
    "house_soc_wh", "house_soc_percent", "car_soc_wh", "car_soc_percent",
    "slot_cost_ct", "price_ct_kwh", "feedin_ct_kwh", "pv_w", "house_load_w",
]


def _now_slot(config: Config) -> pd.Timestamp:
    freq = f"{config.general.slot_minutes}min"
    return pd.Timestamp.now(tz=config.general.timezone).floor(freq)


def run_once(config: Config, publisher: HomeyMqttPublisher | None = None) -> None:
    """Ein Rechenzyklus. `publisher`: persistente MQTT-Verbindung im Loop-
    Betrieb (Last Will); ohne wird pro Lauf verbunden und wieder getrennt."""
    repo = InfluxRepository(config)
    one_shot = publisher is None
    try:
        now = _now_slot(config)
        freq = f"{config.general.slot_minutes}min"
        # Horizont bis ENDE des letzten Tages (nächste Mitternacht) aufrunden ->
        # immer ganze Tage, kein verzerrter Teiltag am Ende.
        _raw_end = now + timedelta(hours=config.general.optimization_horizon_hours)
        opt_end = _raw_end.normalize()
        if opt_end <= _raw_end:
            opt_end = opt_end + timedelta(days=1)

        # --- 1) Verbrauchsprognose (72 h) -------------------------------- #
        log.info("Lade Verbrauchs-Historie und erstelle Prognose ...")
        history = load_history(repo, config, now)
        forecast_end = now + timedelta(hours=config.general.forecast_horizon_hours)
        temp = _read_temp(repo, config,
                          now - timedelta(days=config.forecast.lookback_days), forecast_end)
        forecaster = LoadForecaster(config)
        load_fc = forecaster.forecast(history, now, config.general.n_forecast_slots,
                                      hist_temp=temp, fut_temp=temp)
        repo.write_frame(
            "load_forecast",
            pd.DataFrame({"house_load_w": load_fc.values}, index=load_fc.index),
        )
        log.info("Verbrauchsprognose (%d Slots) in InfluxDB geschrieben.", len(load_fc))

        # Optimierungshorizont: jetzt bis Ende des letzten Tages (ganze Tage)
        opt_index = pd.date_range(now, opt_end, freq=freq,
                                  tz=config.general.timezone, inclusive="left")
        house_load = load_fc.reindex(opt_index).ffill().bfill().values

        # --- 2) Eingangsdaten lesen ------------------------------------- #
        pv = repo.read_slots("pv_forecast", now, opt_end).reindex(opt_index).ffill().bfill()
        # Zeitabhängige PV-Korrektur (Monat x Stunde) aus der Kalibrierung anwenden
        if config.calibration.enabled:
            from .calibration import apply_pv_correction, load_profile
            profile = load_profile(config.calibration.pv_profile)
            if profile:
                pv = apply_pv_correction(pv, profile, config.general.timezone)
                log.info("PV-Kalibrierprofil angewandt (%s).", config.calibration.pv_profile)
        price = _price_series(repo, config, opt_index, now)

        if config.feed_in.mode == "db" and repo.signal_available("feed_in_tariff"):
            feedin = repo.read_slots("feed_in_tariff", now, opt_end).reindex(opt_index)
            feedin = feedin.ffill().bfill().fillna(config.feed_in.fixed_ct_kwh)
        else:
            feedin = pd.Series(config.feed_in.fixed_ct_kwh, index=opt_index)

        # Anfangs-SoC Haus
        lookback = now - timedelta(hours=6)
        soc_pct = repo.read_scalar_latest("battery_soc", lookback, now)
        if soc_pct is None:
            log.warning("Kein Haus-SoC gefunden – nehme min_soc an.")
            soc_pct = config.house_battery.min_soc_percent
        init_house_soc = soc_pct / 100.0 * config.house_battery.capacity_wh

        # Anfangs-SoC Auto (optional). Anwesenheit: bevorzugt über den Wallbox-
        # Status (Signal vehicle_connected, 0/1) - ein Tage alter SoC-Wert in
        # der DB heißt sonst nicht, dass das Auto wirklich angesteckt ist.
        init_car_soc = None
        car_present = False
        if config.vehicle.enabled and repo.signal_available("vehicle_soc"):
            car_pct = repo.read_scalar_latest("vehicle_soc", lookback, now)
            connected = None
            if repo.signal_available("vehicle_connected"):
                connected = repo.read_scalar_latest("vehicle_connected", lookback, now)
            if connected is not None and connected < 0.5:
                log.info("Auto nicht angesteckt (vehicle_connected=%.0f) – "
                         "wird nicht mitoptimiert.", connected)
            elif car_pct is not None:
                init_car_soc = car_pct / 100.0 * config.vehicle.capacity_wh
                car_present = True
                log.info("Auto erkannt, SoC %.1f%%%s.", car_pct,
                         " (Wallbox: angesteckt)" if connected is not None else "")

        # --- 3) Optimierung --------------------------------------------- #
        log.info("Starte MILP-Optimierung (%d Slots) ...", len(opt_index))
        inputs = OptimizerInputs(
            index=opt_index,
            house_load_w=np.asarray(house_load, dtype=float),
            pv_w=pv.values.astype(float),
            price_ct_kwh=price.values.astype(float),
            feedin_ct_kwh=feedin.values.astype(float),
            initial_house_soc_wh=init_house_soc,
            initial_car_soc_wh=init_car_soc,
            car_present=car_present,
        )
        result = Optimizer(config).solve(inputs)
        log.info("Optimierung: %s, erwartete Netto-Kosten %.2f € (Horizont).",
                 result.status, result.total_cost_ct / 100.0)

        # --- 4) MQTT (best effort – darf den Lauf nicht abbrechen) ------ #
        try:
            if one_shot:
                publisher = HomeyMqttPublisher(config)
            if result.infeasible:
                publisher.publish_alert(
                    "warning", f"Optimierung nicht optimal ({result.status}) – "
                               f"Fallback 'auto' ohne Eingriffe aktiv.")
            publisher.publish(result.table, now)
            if one_shot:
                publisher.close()
        except Exception as exc:
            log.warning("MQTT-Ausgabe fehlgeschlagen (%s) – InfluxDB-Writeback "
                        "und Dashboard werden trotzdem erstellt.", exc)

        # --- 5) InfluxDB-Writeback -------------------------------------- #
        ctrl = result.table[[c for c in CONTROL_COLS if c in result.table.columns]]
        repo.write_frame("control_table", ctrl)
        pred = result.table[[c for c in PREDICTION_COLS if c in result.table.columns]]
        repo.write_frame("predicted_state", pred)
        log.info("Steuertabelle + Prognosezustände in InfluxDB geschrieben.")

        # --- 5b) Ersparnis-Tracking (Ist vs. Ohne-EMS-Baseline) ---------- #
        savings_eur = None
        if config.savings.enabled:
            try:
                from .savings import SavingsTracker
                savings_eur = SavingsTracker(config).update(repo, now)
            except Exception as exc:
                log.warning("Ersparnis-Tracking fehlgeschlagen (%s).", exc)

        # --- 6) Dashboard ----------------------------------------------- #
        # Anzeige ab heute 00:00: Ist-Werte (bis jetzt) und Prognose (ganzer
        # Bereich) vergleichbar; Steuerung/Prognose-SoC für die Zukunft.
        if config.dashboard.enabled:
            display = _build_display_frame(repo, config, now, history, result)
            build_dashboard(config, display, result.total_cost_ct,
                            export_line_w=result.export_line_w,
                            savings_eur=savings_eur)
    finally:
        repo.close()


def _read_temp(repo, config, start, end):
    """Liest die Temperatur-Vorhersage (falls konfiguriert), sonst None."""
    if not repo.signal_available("temperature"):
        return None
    try:
        return repo.read_slots("temperature", start, end)
    except Exception as exc:  # pragma: no cover
        log.warning("Temperatur konnte nicht gelesen werden (%s).", exc)
        return None


def _price_series(repo, config, index, now, return_estimated=False):
    """Strompreis über `index`: Ist-Werte wo vorhanden, sonst Ähnliche-Tage-
    Prognose für noch fehlende (Folgetag-)Preise – statt einer flachen ffill-Linie.
    Preise dürfen negativ sein (clip_min=None).
    return_estimated=True: zusätzlich Bool-Maske, welche Slots geschätzt sind.
    """
    slot = pd.Timedelta(f"{config.general.slot_minutes}min")
    raw = repo.read_slots("electricity_price", index[0], index[-1] + slot, fill=False).reindex(index)
    estimated = raw.isna()   # Slots ohne echten Börsenpreis -> Schätzung
    if estimated.any():
        try:
            hist = repo.read_slots("electricity_price", now - timedelta(days=90), now,
                                   fill=False).dropna()
            if not hist.empty:
                fc = LoadForecaster(config).forecast(
                    hist, index[0], len(index), clip_min=None, apply_correction=False)
                raw = raw.fillna(fc.reindex(index))
                log.info("Fehlende Folgetag-Preise per Ähnliche-Tage-Prognose ergänzt.")
        except Exception as exc:  # pragma: no cover
            log.warning("Preis-Prognose fehlgeschlagen (%s) – halte letzten Wert.", exc)
    price = raw.ffill().bfill()
    if return_estimated:
        return price, estimated
    return price


def _build_display_frame(repo, config, now, history, result) -> pd.DataFrame:
    """Anzeigetabelle heute 00:00 -> Horizontende.

    Enthält Prognosewerte über den gesamten Bereich (pv_w, house_load_w,
    price_ct_kwh) sowie – zum Vergleich – die heutigen IST-Werte bis 'jetzt'
    (actual_*). Steuerbefehle und Prognose-SoC stammen aus der Optimierung
    (Zukunft). Fehlende Signale werden robust übersprungen.
    """
    freq = f"{config.general.slot_minutes}min"
    tz = config.general.timezone
    slot = pd.Timedelta(freq)
    day_start = now.normalize()
    end = result.table.index[-1]
    # Horizont endet bereits an einer Tagesgrenze (Mitternacht) -> ganze Tage,
    # kein Abschneiden nötig.
    full = pd.date_range(day_start, end, freq=freq, tz=tz)  # inkl. Ende
    df = pd.DataFrame(index=full)

    # ---- Prognose über den gesamten Bereich ----
    try:
        temp = _read_temp(repo, config,
                          now - timedelta(days=config.forecast.lookback_days), end + slot)
        forecaster = LoadForecaster(config)
        pred_load = forecaster.forecast(history, day_start, len(full),
                                        hist_temp=temp, fut_temp=temp)
        df["house_load_w"] = pred_load.reindex(full)
    except Exception as exc:  # pragma: no cover
        log.warning("Verbrauchsprognose fürs Dashboard fehlgeschlagen: %s", exc)
    try:
        pv = repo.read_slots("pv_forecast", day_start, end + slot)
        if config.calibration.enabled:
            from .calibration import apply_pv_correction, load_profile
            prof = load_profile(config.calibration.pv_profile)
            if prof:
                pv = apply_pv_correction(pv, prof, tz)
        df["pv_w"] = pv.reindex(full)
    except Exception as exc:  # pragma: no cover
        log.warning("PV-Prognose fürs Dashboard fehlgeschlagen: %s", exc)
    try:
        price, estimated = _price_series(repo, config, full, now, return_estimated=True)
        df["price_ct_kwh"] = price
        df["price_estimated"] = estimated.astype(float)  # 1 = Schätzung, 0 = Börsenpreis
    except Exception:
        pass

    # ---- Zukunftswerte aus der Optimierung (jetzt -> Ende) ----
    ot = result.table
    for c in ["house_soc_percent", "car_soc_percent", "batt_dc_charge_w",
              "batt_ac_charge_w", "batt_discharge_w", "batt_charge_limit_w",
              "batt_discharge_limit_w", "batt_grid_discharge_w", "car_charge_w",
              "grid_import_w", "grid_export_w", "export_line_w", "mode"]:
        if c in ot.columns:
            df[c] = ot[c].reindex(full)
    if "mode" in df.columns:
        df["mode"] = df["mode"].fillna("auto")
    else:
        df["mode"] = "auto"

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
            if repo.signal_available(signal):
                s = repo.read_slots(signal, day_start, now + slot).reindex(full)
                df[col] = s.where(past_mask).ffill().where(past_mask)
        except Exception:  # pragma: no cover
            pass
    return df


def _sd_notify(message: str) -> None:
    """Meldung an systemd (Type=notify / WatchdogSec); no-op ohne systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        import socket
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.sendto(message.encode(), addr)
        finally:
            s.close()
    except Exception:  # pragma: no cover
        pass


def start_dashboard_server(config: Config) -> None:
    """Startet im Dienstmodus einen kleinen HTTP-Server, der das Dashboard
    im Browser abrufbar macht (http://<host>:<port>/). Läuft als Daemon-Thread."""
    import functools
    import http.server
    import os
    import threading

    out = os.path.abspath(config.dashboard.output_path)
    directory = os.path.dirname(out) or "."
    fname = os.path.basename(out)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            # Leichtgewichtiger Versions-Endpunkt: mtime der Dashboard-Datei.
            # Die Seite pollt diesen und lädt bei Änderung (neue Berechnung) neu.
            if self.path.split("?")[0] in ("/version",):
                try:
                    body = ("%.0f" % os.path.getmtime(out)).encode()
                except OSError:
                    body = b"0"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path in ("/", "/index.html", "/dashboard", "/dashboard.html"):
                self.path = "/" + fname
            return super().do_GET()

        def end_headers(self):
            # HTML/Version immer revalidieren, damit reload() die neue Datei
            # holt; das lokale plotly.min.js darf der Browser cachen.
            if not self.path.endswith(".js"):
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            super().end_headers()

        def log_message(self, *a):  # ruhig bleiben
            pass

    handler = functools.partial(Handler, directory=directory)
    try:
        srv = http.server.ThreadingHTTPServer((config.dashboard.host, config.dashboard.port), handler)
    except OSError as exc:
        log.warning("Dashboard-Server konnte nicht starten (%s).", exc)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Dashboard-Server läuft: http://%s:%d/ (Datei: %s)",
             config.dashboard.host, config.dashboard.port, fname)


def main() -> None:
    parser = argparse.ArgumentParser(description="EMS – Energy Management System")
    parser.add_argument("--config", default="config.yaml", help="Pfad zur Konfiguration")
    parser.add_argument("--loop", action="store_true", help="Dauerbetrieb im Intervall")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)

    if not args.loop:
        run_once(config)
        return

    if config.dashboard.enabled and config.dashboard.serve:
        start_dashboard_server(config)

    interval = config.general.run_interval_minutes * 60
    # Kleiner Versatz, damit die neuen 15-Minuten-Werte (Preis, Zähler) schon in
    # der InfluxDB stehen, bevor gerechnet wird.
    offset = float(getattr(config.general, "run_offset_seconds", 10))
    log.info("Dauerbetrieb: Intervall %d min, auf Uhr-Raster synchronisiert "
             "(+%.0fs Versatz).", config.general.run_interval_minutes, offset)
    # Persistente MQTT-Verbindung mit Last Will: stirbt der Prozess, setzt der
    # Broker ems/status selbst auf "offline" (Watchdog-Signal für Homey).
    publisher = HomeyMqttPublisher(config)
    _sd_notify("READY=1")
    while True:
        try:
            run_once(config, publisher)
        except Exception as exc:  # pragma: no cover
            log.exception("Fehler im EMS-Zyklus – fahre fort.")
            publisher.publish_alert("error", f"EMS-Zyklus fehlgeschlagen: {exc}")
        # Lebenszeichen an systemd (WatchdogSec): bleibt es aus (Prozess hängt),
        # startet systemd den Dienst neu.
        _sd_notify("WATCHDOG=1")
        # Bis zur nächsten glatten Raster-Marke (z. B. :00/:15/:30/:45) warten;
        # ein MQTT-Kommando (ems/cmd/recalc, car_boost) bricht das Warten ab.
        now = _time.time()
        next_mark = (now // interval + 1) * interval + offset
        if next_mark - now < 5.0:      # zu knapp -> erst zur übernächsten Marke
            next_mark += interval
        if publisher.wait_for_recalc(next_mark - _time.time()):
            log.info("Neuberechnung per MQTT-Kommando – Zyklus startet sofort.")


if __name__ == "__main__":
    main()
