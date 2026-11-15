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
# Stellgroessen des Backup-Jobs: cron reicht die Container-Umgebung nicht
# weiter, und nach dem Abgeben der Rechte ist /proc/1/environ nicht mehr
# lesbar - deshalb hier (noch als root) holen und exportieren, wie privs.sh
# es mit EMS_UID tut.
[ -n "${EMS_BACKUP_DIR+x}" ] || EMS_BACKUP_DIR="$(ems_pid1_env EMS_BACKUP_DIR || true)"
[ -n "${EMS_BACKUP_DB+x}" ] || EMS_BACKUP_DB="$(ems_pid1_env EMS_BACKUP_DB || true)"
export EMS_BACKUP_DIR EMS_BACKUP_DB
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
  backup)
    # Die kleinen, unversionierten Dateien sichern (Pendant zu
    # ems-backup.timer): config.yaml und config_overrides.yaml liegen neben
    # der Config, Kalibrierprofile und savings_state.json im Daten-Volume -
    # backup.sh sammelt je Dateiname den ersten Fundort ein. Die Historie ist
    # in der Vorgabe NICHT dabei (rund 2 GB); nur mit EMS_BACKUP_DB=1 am
    # scheduler-Dienst - siehe docs/howto-docker.md, Abschnitt "Sicherung".
    # umask: das Archiv enthaelt Zugangsdaten und soll schon beim Anlegen nur
    # fuer ems lesbar sein, nicht erst nach dem chmod 600 in backup.sh.
    umask 077
    EMS_SRC="$(dirname "$CFG")"
    EMS_SRC_EXTRA=/app/data
    EMS_DB="${EMS_DB:-/app/data/e3dc_history.sqlite}"
    export EMS_SRC EMS_SRC_EXTRA EMS_DB
    exec /app/backup.sh "${EMS_BACKUP_DIR:-/app/data/backup}"
    ;;
  *)
    echo "usage: scheduler-run.sh {savings|kalibrierung|backup}" >&2
    exit 2
    ;;
esac
