# shellcheck shell=sh
# Gemeinsame Rechte-Logik fuer entrypoint.sh und scheduler-run.sh (wird per
# `.` eingebunden, nicht ausgefuehrt).
#
# Ziel: Der Container startet wie bisher als root, der EMS-Prozess und die
# geplanten Jobs laufen aber als unprivilegierter Benutzer ems. Bestehende
# Installationen haben ihre ./config- und ./data-Mounts als root angelegt -
# deshalb uebergibt root sie hier EINMALIG an ems und gibt dann die Rechte ab.
#
# Stellgroessen (Umgebung des Containers):
#   EMS_RUN_AS_ROOT=1   altes Verhalten: nichts umstellen, als root laufen
#   EMS_UID / EMS_GID   Ziel-Benutzer. Ohne Angabe: der Eigentuemer von
#                       /app/config, falls das nicht root ist - wer ./config
#                       als normaler Host-Benutzer angelegt hat (und
#                       config.yaml auf 600 gesetzt), behaelt so den Zugriff
#                       ohne sudo. Gehoert /app/config root: 10001:10001
#                       (Benutzer ems im Image).
#   EMS_WRITABLE_DIRS   zu uebergebende Verzeichnisse (Leerzeichen-getrennt),
#                       Vorgabe "/app/config /app/data"

# cron reicht die Container-Umgebung NICHT an seine Jobs weiter. Die drei
# Stellgroessen holt sich ein Job deshalb aus der Umgebung von PID 1 (cron
# selbst, vom compose gestartet). Nur root darf das lesen - nach dem Abgeben
# der Rechte sind die Werte bereits exportiert.
ems_pid1_env() {
    # cat statt Umleitung: bei `docker exec` als root ist die Datei eines
    # nicht-root-PID-1 trotz bestandenem -r nicht zu oeffnen, und die Meldung
    # einer gescheiterten Umleitung liesse sich nicht unterdruecken.
    [ -r /proc/1/environ ] || return 0
    cat /proc/1/environ 2>/dev/null | tr '\0' '\n' | sed -n "s/^$1=//p" | head -n 1
}

[ -n "${EMS_RUN_AS_ROOT+x}" ] || EMS_RUN_AS_ROOT="$(ems_pid1_env EMS_RUN_AS_ROOT || true)"
[ -n "${EMS_UID+x}" ] || EMS_UID="$(ems_pid1_env EMS_UID || true)"
[ -n "${EMS_GID+x}" ] || EMS_GID="$(ems_pid1_env EMS_GID || true)"
EMS_WRITABLE_DIRS="${EMS_WRITABLE_DIRS:-/app/config /app/data}"
# Vorgabe aus dem Eigentuemer von /app/config (siehe oben). Eine einmal
# getroffene Wahl bleibt stabil: nach der Uebergabe gehoert das Verzeichnis
# genau diesem Benutzer.
if [ -z "${EMS_UID:-}" ] && [ -d /app/config ]; then
    _owner="$(stat -c '%u' /app/config 2>/dev/null || echo 0)"
    if [ "$_owner" != "0" ]; then
        EMS_UID="$_owner"
        if [ -z "${EMS_GID:-}" ]; then
            EMS_GID="$(stat -c '%g' /app/config 2>/dev/null || echo 0)"
            [ "$EMS_GID" != "0" ] || EMS_GID=""
        fi
    fi
fi
EMS_UID="${EMS_UID:-10001}"
EMS_GID="${EMS_GID:-10001}"

ems_log() {
    echo "[ems-rechte] $*" >&2
}

# Nur positive Ganzzahlen. UID 0 waere "doch root" durch die Hintertuer -
# dafuer gibt es EMS_RUN_AS_ROOT.
case "$EMS_UID" in
    ''|*[!0-9]*|0) ems_log "EMS_UID='$EMS_UID' ungueltig - nehme 10001."; EMS_UID=10001 ;;
esac
case "$EMS_GID" in
    ''|*[!0-9]*|0) ems_log "EMS_GID='$EMS_GID' ungueltig - nehme 10001."; EMS_GID=10001 ;;
esac
export EMS_RUN_AS_ROOT EMS_UID EMS_GID

ems_truthy() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|ja|on) return 0 ;;
    esac
    return 1
}

# Fuehrt "$@" als Zielbenutzer aus. --init-groups braucht einen passwd-Eintrag;
# eine frei gewaehlte EMS_UID hat keinen, dann ohne Zusatzgruppen.
ems_setpriv() {
    if getent passwd "$EMS_UID" >/dev/null 2>&1; then
        setpriv --reuid "$EMS_UID" --regid "$EMS_GID" --init-groups "$@"
    else
        setpriv --reuid "$EMS_UID" --regid "$EMS_GID" --clear-groups "$@"
    fi
}

# Uebergibt ein Verzeichnis an den Zielbenutzer - aber nur die Eintraege, die
# ihm noch nicht gehoeren. Stimmt schon alles, bleibt es bei einem Durchlauf
# ohne Schreibzugriff, der beim ersten Treffer endete (-quit); die Kosten
# haengen an der Zahl der Dateien, nicht an ihrer Groesse. Der Durchlauf bei
# JEDEM Start ist Absicht: `docker compose exec` und `--entrypoint python`
# laufen weiter als root und koennen neue root-Dateien hinterlassen.
# -xdev: nicht in darunter eingehaengte Fremd-Dateisysteme (NAS) laufen.
# -h: Symlinks selbst umstellen, nie ihr Ziel ausserhalb des Mounts.
ems_fix_owner() {
    _dir="$1"
    [ -d "$_dir" ] || return 0
    if [ -z "$(find "$_dir" -xdev ! -user "$EMS_UID" -print -quit 2>/dev/null)" ]; then
        return 0
    fi
    ems_log "Uebergebe $_dir an $EMS_UID:$EMS_GID (gehoerte bisher teils anderen Benutzern)."
    if ! find "$_dir" -xdev ! -user "$EMS_UID" \
            -exec chown -h "$EMS_UID:$EMS_GID" {} + 2>/dev/null; then
        ems_log "WARNUNG: chown in $_dir fehlgeschlagen (nur lesbar eingehaengt? NFS?)."
        return 1
    fi
}

# Entscheidet, ob die Rechte abgegeben werden: setzt EMS_DROP=1 oder 0.
# Abgegeben wird NICHT, wenn
#   * der Container ohnehin nicht als root laeuft (compose `user:`),
#   * EMS_RUN_AS_ROOT gesetzt ist,
#   * der Wechsel nicht moeglich ist (z.B. `--cap-drop ALL`),
#   * ein Verzeichnis fuer root schreibbar ist, fuer den Zielbenutzer aber
#     nicht (chown gescheitert) - dann lieber wie bisher als root weiterlaufen
#     als den Dienst an einem Update scheitern zu lassen.
ems_prepare() {
    EMS_DROP=0
    [ "$(id -u)" = "0" ] || return 0
    if ems_truthy "$EMS_RUN_AS_ROOT"; then
        ems_log "EMS_RUN_AS_ROOT gesetzt - laufe als root."
        return 0
    fi
    if ! ems_setpriv true 2>/dev/null; then
        ems_log "WARNUNG: Wechsel auf $EMS_UID:$EMS_GID nicht moeglich (fehlen CAP_SETUID/CAP_SETGID?) - laufe als root."
        return 0
    fi
    _ok=1
    for _d in $EMS_WRITABLE_DIRS; do
        [ -d "$_d" ] || continue
        ems_fix_owner "$_d" || true
        # shellcheck disable=SC2016  # $1 gehoert der inneren Shell
        if [ -w "$_d" ] && ! ems_setpriv sh -c '[ -w "$1" ]' sh "$_d"; then
            ems_log "WARNUNG: $_d ist fuer $EMS_UID:$EMS_GID nicht schreibbar."
            _ok=0
        fi
    done
    if [ "$_ok" != "1" ]; then
        ems_log "WARNUNG: laufe deshalb weiter als root. Abhilfe auf dem Host: chown -R $EMS_UID:$EMS_GID <verzeichnis>."
        return 0
    fi
    EMS_DROP=1
}

# Ersetzt den laufenden Prozess durch "$@" unter dem Zielbenutzer. HOME zeigt
# sonst weiter auf /root (nicht lesbar) - Bibliotheken suchen dort Caches.
ems_exec_dropped() {
    export HOME=/app
    if getent passwd "$EMS_UID" >/dev/null 2>&1; then
        exec setpriv --reuid "$EMS_UID" --regid "$EMS_GID" --init-groups "$@"
    fi
    exec setpriv --reuid "$EMS_UID" --regid "$EMS_GID" --clear-groups "$@"
}
