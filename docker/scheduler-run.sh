#!/bin/sh
# Wrapper für die geplanten MicroEMS-Jobs im Container (Pendant zu den
# systemd-Timern). Kapselt Arbeitsverzeichnis + PYTHONPATH, damit die
# Profil-Dateien im gemounteten Daten-Volume landen.
#
# cron ruft dieses Skript als root auf (nur root darf die Ausgabe nach
# /proc/1/fd/1, also ins Container-Log, umleiten). Die eigentliche Arbeit
# laeuft als Benutzer ems: das Skript uebergibt noetigenfalls die Mounts und
# startet sich dann selbst ohne root-Rechte neu. Altes Verhalten:
# EMS_RUN_AS_ROOT=1 am scheduler-Dienst (siehe docker/privs.sh).
set -eu

. /app/docker/privs.sh
ems_prepare
if [ "$EMS_DROP" = "1" ]; then
    ems_exec_dropped "$0" "$@"
fi

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
    python -m ems.battery_calibration --config "$CFG" --apply
    python -m ems.load_learning --config "$CFG" --apply
    python -m ems.archive_thinning --config "$CFG" --apply
    ;;
  *)
    echo "usage: scheduler-run.sh {savings|kalibrierung}" >&2
    exit 2
    ;;
esac
