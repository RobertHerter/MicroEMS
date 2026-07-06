#!/bin/bash
# Sichert die UNVERSIONIERTEN EMS-Dateien (config.yaml mit Zugangsdaten,
# Kalibrierprofile, Ersparnis-Status) als tar.gz und behaelt die letzten 8.
#
# Ziel: 1. Argument oder $EMS_BACKUP_DIR, sonst /opt/ems/backup.
# WICHTIG: Das lokale Standardziel schuetzt nur vor versehentlichem Loeschen/
# Verkonfigurieren - gegen einen Platten-/SD-Ausfall ein EXTERNES Ziel
# (USB-Stick, NAS-Mount) angeben, z.B. in ems-backup.service:
#   Environment=EMS_BACKUP_DIR=/mnt/nas/ems-backup
set -euo pipefail

SRC=/opt/ems
DEST="${1:-${EMS_BACKUP_DIR:-/opt/ems/backup}}"
mkdir -p "$DEST"

FILES=()
for f in config.yaml kalibrierung.yaml kalibrierung_profil.yaml savings_state.json; do
    [ -f "$SRC/$f" ] && FILES+=("$f")
done
if [ ${#FILES[@]} -eq 0 ]; then
    echo "Keine zu sichernden Dateien gefunden." >&2
    exit 1
fi

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$DEST/ems-config-$STAMP.tar.gz"
tar -C "$SRC" -czf "$OUT" "${FILES[@]}"
chmod 600 "$OUT"
echo "Gesichert: $OUT (${FILES[*]})"

# Nur die letzten 8 Sicherungen behalten
ls -1t "$DEST"/ems-config-*.tar.gz 2>/dev/null | tail -n +9 | xargs -r rm --
