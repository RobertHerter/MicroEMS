#!/bin/sh
# Wrapper für die geplanten MicroEMS-Jobs im Container (Pendant zu den
# systemd-Timern). Kapselt Arbeitsverzeichnis + PYTHONPATH, damit die
# Profil-Dateien im gemounteten Daten-Volume landen.
set -eu
export PYTHONPATH=/app
CFG="${EMS_CONFIG:-/app/config/config.yaml}"

case "${1:-}" in
  savings)
    # Ersparnis der Vortage gegen die echten E3DC-Zähler validieren + persistieren.
    cd /app
    exec python savings_check.py --config "$CFG" --persist --days 2
    ;;
  kalibrierung)
    # Verbrauchs-/PV-Kalibrierung + Pool-Thermomodell. cwd = Daten-Volume, damit
    # kalibrierung_profil.yaml und kalibrierung.yaml dort persistiert werden
    # (calibration.pv_profile in der Config entsprechend auf /app/data/... setzen).
    cd /app/data
    python -m kalibrierung --config "$CFG" --lookback-days 730 --test-days 365
    python -m ems.pool_calibration --config "$CFG" --apply
    ;;
  *)
    echo "usage: scheduler-run.sh {savings|kalibrierung}" >&2
    exit 2
    ;;
esac
