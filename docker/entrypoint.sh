#!/bin/sh
# Einstiegspunkt des MicroEMS-Images.
#
# Startet als root, uebergibt die beschreibbaren Mounts einmalig an den
# Benutzer ems und gibt dann die Rechte ab - der EMS-Prozess laeuft NICHT als
# root. Bestehende Installationen (root-eigene ./config und ./data) muessen
# dafuer nichts umstellen. Altes Verhalten: EMS_RUN_AS_ROOT=1.
# Einzelheiten und Stellgroessen: docker/privs.sh.
#
# Aufruf wie bisher - Argumente gehen an `python -m ems.main`:
#   docker run ... microems --config /app/config/config.yaml --check
# Beginnt das erste Argument NICHT mit "-", ist es ein eigenes Kommando und
# laeuft ebenfalls als ems:
#   docker run ... microems python -m ems.battery_calibration --config ...
# Unter dem Namen `ems-run` liegt das Skript auch im PATH - fuer
# `docker compose exec`, das sonst als root liefe und root-eigene Dateien in
# den Mounts hinterliesse:
#   docker compose exec ems ems-run python -m ems.battery_calibration ...
set -eu

. /app/docker/privs.sh

if [ "$#" -eq 0 ] || [ "${1#-}" != "$1" ]; then
    set -- python -m ems.main "$@"
fi

ems_prepare
if [ "$EMS_DROP" = "1" ]; then
    ems_exec_dropped "$@"
fi
# exec: der Dienst bleibt PID 1 und bekommt das SIGTERM von `docker stop`
# selbst (Fail-safe: E3DC zurueck auf auto).
exec "$@"
