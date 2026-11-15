#!/bin/bash
# Sichert die UNVERSIONIERTEN EMS-Dateien (config.yaml mit Zugangsdaten,
# config_overrides.yaml mit kalibrierten Werten und Dashboard-Einstellungen,
# Kalibrierprofile, Ersparnis-Status) als tar.gz und behaelt die letzten 8.
#
# Ziel: 1. Argument oder $EMS_BACKUP_DIR, sonst <Quelle>/backup.
# Quelle: $EMS_SRC, sonst /opt/ems. Liegen die Dateien auf mehrere
# Verzeichnisse verteilt (Docker: Config unter /app/config, Profile und
# Ersparnis-Status unter /app/data), nennt $EMS_SRC_EXTRA die weiteren
# (durch ":" getrennt). Je Dateiname zaehlt der erste Fundort, $EMS_SRC zuerst;
# im Archiv liegen die Dateien wie bisher flach nebeneinander.
# WICHTIG: Das lokale Standardziel schuetzt nur vor versehentlichem Loeschen/
# Verkonfigurieren - gegen einen Platten-/SD-Ausfall ein EXTERNES Ziel
# (USB-Stick, NAS-Mount) angeben, z.B. in ems-backup.service:
#   Environment=EMS_BACKUP_DIR=/mnt/nas/ems-backup
#
# Historie (optional): EMS_BACKUP_DB=1 sichert zusaetzlich die SQLite-Historie
# konsistent per `VACUUM INTO` (im WAL-Modus ohne den Dienst zu blockieren;
# braucht sqlite3 >= 3.27 - ohne die sqlite3-CLI, etwa im Docker-Image, laeuft
# derselbe Befehl ueber python3) und behaelt die letzten 2. Standard AUS - die Datei
# ist rund 2 GB gross, das Ziel braucht also etwa 6 GB frei (2 alte + die
# neue). Datei: $EMS_DB, sonst <Quelle>/e3dc_history.sqlite
# (e3dc_rscp.history_db_path).
set -euo pipefail

SRC="${EMS_SRC:-/opt/ems}"
DEST="${1:-${EMS_BACKUP_DIR:-$SRC/backup}}"
mkdir -p "$DEST"

DIRS=("$SRC")
if [ -n "${EMS_SRC_EXTRA:-}" ]; then
    IFS=: read -r -a EXTRA <<<"$EMS_SRC_EXTRA"
    DIRS+=("${EXTRA[@]}")
fi

FILES=()      # Dateinamen (fuer die Meldung)
TARARGS=()    # je Datei: -C <Verzeichnis> <Name>
for f in config.yaml config_overrides.yaml kalibrierung.yaml \
         kalibrierung_profil.yaml savings_state.json; do
    for d in "${DIRS[@]}"; do
        if [ -n "$d" ] && [ -f "$d/$f" ]; then
            # Absolut: tar wertet ein relatives -C relativ zum VORIGEN -C aus.
            FILES+=("$f")
            TARARGS+=(-C "$(cd "$d" && pwd)" "$f")
            break
        fi
    done
done
if [ ${#FILES[@]} -eq 0 ]; then
    echo "Keine zu sichernden Dateien gefunden." >&2
    exit 1
fi

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$DEST/ems-config-$STAMP.tar.gz"
tar -czf "$OUT" "${TARARGS[@]}"
chmod 600 "$OUT"
echo "Gesichert: $OUT (${FILES[*]})"

# Nur die letzten 8 Sicherungen behalten
ls -1t "$DEST"/ems-config-*.tar.gz 2>/dev/null | tail -n +9 | xargs -r rm --

# Optional: Historie. Schlaegt dieser Schritt fehl, endet das Skript mit
# Fehler (die Config-Sicherung oben ist dann bereits geschrieben).
if [ "${EMS_BACKUP_DB:-0}" = "1" ]; then
    DB="${EMS_DB:-$SRC/e3dc_history.sqlite}"
    if [ ! -f "$DB" ]; then
        echo "EMS_BACKUP_DB=1, aber Historie $DB nicht gefunden." >&2
        exit 1
    fi
    # Werkzeug: die sqlite3-CLI, sonst das sqlite3-Modul von python3 (das
    # Docker-Image bringt die CLI nicht mit, Python aber immer).
    if command -v sqlite3 >/dev/null 2>&1; then
        SQLITE_VER=$(sqlite3 --version | awk '{print $1}')
    elif command -v python3 >/dev/null 2>&1; then
        SQLITE_VER=$(python3 -c 'import sqlite3; print(sqlite3.sqlite_version)')
    else
        echo "EMS_BACKUP_DB=1, aber sqlite3 fehlt (apt install sqlite3)." >&2
        exit 1
    fi
    # VACUUM INTO gibt es erst ab SQLite 3.27 (2019). Vorab pruefen: die
    # Fehlermeldung eines aelteren sqlite3 ("near INTO: syntax error") hilft
    # niemandem weiter.
    if [ "$(printf '%s\n' 3.27.0 "$SQLITE_VER" | sort -V | head -n 1)" != "3.27.0" ]; then
        echo "EMS_BACKUP_DB=1 braucht sqlite3 >= 3.27 (gefunden: $SQLITE_VER)." >&2
        exit 1
    fi
    DBOUT="$DEST/ems-history-$STAMP.sqlite"
    TMP="$DBOUT.teil"
    # VACUUM INTO bricht ab, wenn die Zieldatei schon existiert - ein Rest
    # eines abgebrochenen Laufs muss vorher weg.
    rm -f -- "$TMP"
    # VACUUM INTO liest in EINER Lesetransaktion einen konsistenten Stand und
    # blockiert im WAL-Modus keinen Schreiber; ein blosses cp koennte eine
    # halbe Transaktion erwischen. NICHT `.backup`: die Online-Sicherung
    # beginnt bei jeder Aenderung der Quelle durch eine andere Verbindung von
    # vorn - der Dienst schreibt alle 5 s (live_samples), ein Versuch lief
    # ueber 10 min ohne Ende. VACUUM INTO brauchte fuer 2,1 GB 28 s.
    # Erst unter .teil schreiben: eine abgebrochene Sicherung darf nicht wie
    # eine fertige aussehen (und keine gute aus der Aufbewahrung draengen).
    # Der Pfad steht in einem SQL-Literal - ein ' darin verdoppeln.
    vacuum_into() {
        if command -v sqlite3 >/dev/null 2>&1; then
            local Q="'"
            sqlite3 -cmd ".timeout 60000" "$DB" "VACUUM INTO '${TMP//$Q/$Q$Q}'"
        else
            # isolation_level=None: VACUUM laeuft nicht in einer Transaktion.
            python3 -c 'import sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=60, isolation_level=None)
con.execute("VACUUM INTO ?", (sys.argv[2],))
con.close()' "$DB" "$TMP"
        fi
    }
    if ! vacuum_into; then
        rm -f -- "$TMP"
        echo "Sicherung der Historie fehlgeschlagen ($DB)." >&2
        exit 1
    fi
    chmod 600 "$TMP"
    mv -- "$TMP" "$DBOUT"
    echo "Gesichert: $DBOUT"
    # Nur die letzten 2 Historien-Sicherungen behalten
    ls -1t "$DEST"/ems-history-*.sqlite 2>/dev/null | tail -n +3 | xargs -r rm --
fi
