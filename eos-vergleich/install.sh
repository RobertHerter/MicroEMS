#!/bin/sh
# Spielt die versionierten Dateien des EOS-Vergleichs in den EOS-Klon ein.
# Quelle der Wahrheit ist dieser Ordner; /opt/eos (Klon von Akkudoktor-EOS/EOS)
# erhaelt nur Kopien. Daten (data/, data-native/, comparison/snapshots,
# comparison/forecast_history.sqlite) bleiben unberuehrt.
#
#   ./install.sh            nach /opt/eos
#   EOS_DIR=/pfad ./install.sh
#
# Die systemd-Units werden nach $EOS_DIR abgelegt, aber NICHT nach
# /etc/systemd/system kopiert und nichts wird neu gestartet. Container nie
# mitten in einem Exportlauf (:00/:01, :15/:16 ...) neu anlegen.
set -eu
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
EOS_DIR=${EOS_DIR:-/opt/eos}
[ -d "$EOS_DIR" ] || { echo "EOS-Verzeichnis $EOS_DIR fehlt" >&2; exit 1; }

mkdir -p "$EOS_DIR/scripts" "$EOS_DIR/tests" \
         "$EOS_DIR/comparison/static" "$EOS_DIR/comparison/tests"
cp "$SRC"/scripts/*.py "$EOS_DIR/scripts/"
cp "$SRC"/tests/*.py "$EOS_DIR/tests/"
cp "$SRC"/compose.local.yaml "$SRC"/compose.native.yaml "$EOS_DIR/"
cp "$SRC"/README.md "$EOS_DIR/README-MicroEMS.md"
cp "$SRC"/systemd/eos-*.service "$SRC"/systemd/eos-*.timer "$EOS_DIR/"
cp "$SRC"/systemd/ems-comparison-dashboard.service "$EOS_DIR/comparison/"
cp "$SRC"/comparison/server.py "$EOS_DIR/comparison/"
cp "$SRC"/comparison/static/* "$EOS_DIR/comparison/static/"
cp "$SRC"/comparison/tests/*.py "$EOS_DIR/comparison/tests/"
echo "EOS-Vergleich nach $EOS_DIR eingespielt."
