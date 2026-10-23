"""Alarm, wenn ein EMS-Hintergrundlauf (Timer-Unit) scheitert.

Anlass: der Sonntagslauf ``ems-kalibrierung.service`` brach am 11. und
18.10.2026 im Pool-Schritt ab. Weil die Unit ihre Schritte nacheinander
abarbeitet, liefen zwei Wochen lang weder Akku-Kalibrierung noch Lastprofile,
Archiv-Ausduennung oder PV-Nowcast-Messung - und niemand merkte es, denn eine
gescheiterte Unit steht nur im Journal.

Jetzt startet systemd bei einem Fehlschlag ``ems-alarm@<unit>.service``
(``OnFailure=``) und das dieses Modul. Es meldet auf denselben Wegen wie der
laufende Dienst: MQTT ``ems/alert`` (Homey-Push) und der Ereignisverlauf im
Dashboard. Die Meldung nennt den gescheiterten Schritt, die letzte Fehlerzeile
und wie viele Schritte danach ausgefallen sind.

Aufruf (systemd setzt MONITOR_UNIT, MONITOR_INVOCATION_ID usw. selbst):
    python -m ems.unit_alarm --config config.yaml --unit ems-kalibrierung.service
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
from typing import List, Optional

log = logging.getLogger("ems.unit_alarm")

# Zeilen der Prozessausgabe, die ins Dashboard-Ereignis gehen.
LOG_ZEILEN = 30
# Laenge der Fehlerzeile in der Push-Meldung.
FEHLER_MAX = 200

_FEHLER = re.compile(
    r"^\s*(?:[A-Za-z_][\w.]*(?:Error|Exception)|KeyboardInterrupt|SystemExit)\b")


def exec_schritte(show_text: str) -> List[dict]:
    """``systemctl show -p ExecStart`` in Schritte zerlegen.

    Je Zeile ``ExecStart={ path=... ; argv[]=... ; ignore_errors=no ; ...
    ; code=exited ; status=1 }``. systemd haelt diesen Stand nach einem
    Fehlschlag fest; Schritte, die nicht mehr liefen, tragen ``code=(null)``.
    """
    schritte = []
    for zeile in show_text.splitlines():
        zeile = zeile.strip()
        if not zeile.startswith("ExecStart={"):
            continue
        innen = zeile[len("ExecStart={"):].rstrip("}").strip()
        felder = {}
        for teil in innen.split(" ; "):
            schluessel, _, wert = teil.partition("=")
            felder[schluessel.strip()] = wert.strip()
        schritte.append({
            "argv": felder.get("argv[]", felder.get("path", "")),
            "ignore_errors": felder.get("ignore_errors") == "yes",
            "code": None if felder.get("code") in (None, "", "(null)")
            else felder["code"],
            "status": felder.get("status", ""),
        })
    return schritte


def schritt_name(argv: str) -> str:
    """Kurzname eines Schritts: das Modul hinter ``-m``, sonst das Skript."""
    teile = argv.split()
    if "-m" in teile and teile.index("-m") + 1 < len(teile):
        return teile[teile.index("-m") + 1]
    for teil in teile:
        if teil.endswith((".py", ".sh")):
            return os.path.basename(teil)
    return os.path.basename(teile[0]) if teile else "?"


def _gescheitert(schritt: dict) -> bool:
    if schritt["ignore_errors"] or schritt["code"] is None:
        return False
    return not (schritt["code"] == "exited"
                and schritt["status"].split("/")[0] == "0")


def auswertung(schritte: List[dict]) -> dict:
    """Gescheiterter Schritt, sein Status und die danach ausgefallenen."""
    for i, schritt in enumerate(schritte):
        if _gescheitert(schritt):
            return {"schritt": schritt_name(schritt["argv"]),
                    "status": f"{schritt['code']} {schritt['status']}".strip(),
                    "nummer": i + 1, "von": len(schritte),
                    "ausgefallen": [schritt_name(s["argv"])
                                    for s in schritte[i + 1:]
                                    if s["code"] is None]}
    return {}


def fehlerzeile(zeilen: List[str]) -> Optional[str]:
    """Letzte Ausnahmezeile (``TypeError: ...``), sonst die letzte Zeile."""
    zeilen = [z.rstrip() for z in zeilen if z.strip()]
    if not zeilen:
        return None
    for zeile in reversed(zeilen):
        if _FEHLER.match(zeile):
            return zeile.strip()[:FEHLER_MAX]
    return zeilen[-1].strip()[:FEHLER_MAX]


def meldung(unit: str, info: dict, fehler: Optional[str],
            ergebnis: str = "") -> str:
    """Die Push-Meldung - kurz, aber ohne Journal verstaendlich."""
    name = unit[:-len(".service")] if unit.endswith(".service") else unit
    if info:
        text = f"EMS: {name} abgebrochen"
        if info["von"] > 1:
            text += f" in Schritt {info['nummer']}/{info['von']} ({info['schritt']})"
        text += f", {info['status']}"
    else:
        text = f"EMS: {name} gescheitert" + (f" ({ergebnis})" if ergebnis else "")
    if fehler:
        text += f" - {fehler}"
    if info.get("ausgefallen"):
        n = len(info["ausgefallen"])
        text += (f". Danach nicht gelaufen: {n} "
                 f"{'Schritt' if n == 1 else 'Schritte'}.")
    return text


def _ausfuehren(befehl: List[str]) -> str:
    try:
        return subprocess.run(befehl, capture_output=True, text=True,
                              timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s nicht ausfuehrbar: %s", befehl[0], exc)
        return ""


def journal_zeilen(unit: str, invocation: Optional[str]) -> List[str]:
    """Ausgabe genau DIESES Laufs (Invocation-ID), sonst die letzten der Unit."""
    filter_ = ([f"_SYSTEMD_INVOCATION_ID={invocation}"] if invocation
               else ["-u", unit])
    return _ausfuehren(["journalctl", *filter_, "-o", "cat", "--no-pager",
                        "-n", str(LOG_ZEILEN)]).splitlines()


def melden(config, text: str, details: dict) -> int:
    """Ueber MQTT und ins Dashboard; Zahl der Wege, die geklappt haben."""
    from .homey_mqtt import publish_alert_once
    from .local_history import write_dashboard_event
    erfolg = 0
    try:
        if publish_alert_once(config.mqtt, "error", text):
            erfolg += 1
    except Exception as exc:
        log.warning("MQTT-Alarm fehlgeschlagen (%s).", exc)
    try:
        write_dashboard_event(config.e3dc_rscp.history_db_path, "alarm", text,
                              level="error", details=details)
        erfolg += 1
    except Exception as exc:
        log.warning("Dashboard-Ereignis nicht speicherbar (%s).", exc)
    return erfolg


def main() -> int:                                          # pragma: no cover
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--unit", default=os.environ.get("MONITOR_UNIT", ""),
                    help="gescheiterte Unit (Standard: $MONITOR_UNIT)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    unit = args.unit or "?"
    invocation = os.environ.get("MONITOR_INVOCATION_ID") or None
    ergebnis = os.environ.get("MONITOR_SERVICE_RESULT", "")
    info = auswertung(exec_schritte(_ausfuehren(
        ["systemctl", "show", unit, "-p", "ExecStart", "--no-pager"])))
    zeilen = journal_zeilen(unit, invocation)
    text = meldung(unit, info, fehlerzeile(zeilen), ergebnis)
    log.info("%s", text)
    from .config import load_config
    try:
        config = load_config(args.config)
    except Exception as exc:
        log.error("Konfiguration nicht ladbar (%s) - Alarm nur im Journal.", exc)
        return 1
    details = {"unit": unit, "ergebnis": ergebnis, "invocation": invocation,
               **info, "log": zeilen[-LOG_ZEILEN:]}
    return 0 if melden(config, text, details) else 1


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(main())
