"""Alarm bei gescheiterten Hintergrundlaeufen: welcher Schritt, welcher Fehler."""
from __future__ import annotations

import json

import paho.mqtt.publish

from ems import unit_alarm
from ems.homey_mqtt import publish_alert_once
from ems.local_history import read_dashboard_events
from tests.test_synthetic import make_config


def _exec(argv, code="(null)", status="0/0", ignore="no"):
    return (f"ExecStart={{ path={argv.split()[0]} ; argv[]={argv} ; "
            f"ignore_errors={ignore} ; start_time=[n/a] ; stop_time=[n/a] ; "
            f"pid=0 ; code={code} ; status={status} }}")


PY = "/opt/ems/.venv/bin/python"
# Stand von ems-kalibrierung.service nach dem Abbruch am 18.10.2026.
SONNTAG_18_10 = "\n".join([
    _exec(f"{PY} -m kalibrierung --config /opt/ems/config.yaml",
          code="exited", status="0"),
    _exec(f"{PY} -m ems.pool_calibration --config /opt/ems/config.yaml --apply",
          code="exited", status="1"),
    _exec(f"{PY} -m ems.battery_calibration --config c.yaml --apply"),
    _exec(f"{PY} -m ems.load_learning --config c.yaml --apply"),
    _exec(f"{PY} -m ems.archive_thinning --config c.yaml --apply"),
    _exec(f"/usr/bin/nice -n 19 {PY} tools/pv_nowcast_sweep.py --tage 9",
          ignore="yes"),
])
TRACEBACK = [
    "Traceback (most recent call last):",
    '  File "/opt/ems/ems/pool_calibration.py", line 300, in fit_thermal_params',
    "    .reindex(grid, method=\"nearest\", tolerance=tol)",
    "TypeError: Cannot compare dtypes int64 and datetime64[us, Europe/Berlin]",
]


def test_abbruch_am_18_10_nennt_schritt_und_ausgefallene():
    info = unit_alarm.auswertung(unit_alarm.exec_schritte(SONNTAG_18_10))

    assert info["schritt"] == "ems.pool_calibration"
    assert (info["nummer"], info["von"]) == (2, 6)
    assert info["status"] == "exited 1"
    # Auch die Messung mit "-" ist ausgefallen - sie lief nie an.
    assert info["ausgefallen"] == ["ems.battery_calibration", "ems.load_learning",
                                   "ems.archive_thinning", "pv_nowcast_sweep.py"]

    text = unit_alarm.meldung("ems-kalibrierung.service", info,
                              unit_alarm.fehlerzeile(TRACEBACK))
    assert text.startswith("EMS: ems-kalibrierung abgebrochen in Schritt 2/6 "
                           "(ems.pool_calibration), exited 1 - TypeError: "
                           "Cannot compare dtypes")
    assert text.endswith("Danach nicht gelaufen: 4 Schritte.")


def test_erfolg_und_ignorierter_fehler_sind_kein_abbruch():
    erfolg = "\n".join([_exec(f"{PY} -m a", code="exited", status="0/SUCCESS"),
                        _exec(f"{PY} -m b", code="exited", status="0")])
    nur_ignoriert = _exec("/bin/false", code="exited", status="1", ignore="yes")

    assert unit_alarm.auswertung(unit_alarm.exec_schritte(erfolg)) == {}
    assert unit_alarm.auswertung(unit_alarm.exec_schritte(nur_ignoriert)) == {}


def test_signal_zaehlt_als_abbruch_und_einzelschritt_ohne_nummer():
    info = unit_alarm.auswertung(unit_alarm.exec_schritte(
        _exec("/opt/ems/backup.sh", code="killed", status="9/KILL")))

    assert info["schritt"] == "backup.sh"
    text = unit_alarm.meldung("ems-backup.service", info, None)
    assert text == "EMS: ems-backup abgebrochen, killed 9/KILL"


def test_fehlerzeile_ohne_traceback_ist_die_letzte_zeile():
    assert unit_alarm.fehlerzeile(["sichere config.yaml",
                                   "rsync: connection unexpectedly closed", ""]
                                  ) == "rsync: connection unexpectedly closed"
    assert unit_alarm.fehlerzeile(TRACEBACK + ["  (Folgezeile)"]).startswith(
        "TypeError:")
    assert unit_alarm.fehlerzeile([]) is None


def test_ohne_schrittstand_meldet_das_ergebnis_der_unit():
    text = unit_alarm.meldung("ems-savings.service", {}, None, "timeout")
    assert text == "EMS: ems-savings gescheitert (timeout)"


def test_alarm_geht_ins_dashboard_auch_wenn_mqtt_scheitert(tmp_path, monkeypatch):
    cfg = make_config()
    cfg.e3dc_rscp.history_db_path = str(tmp_path / "h.sqlite")
    cfg.mqtt.enabled = True

    def broker_weg(*args, **kwargs):
        raise ConnectionRefusedError("Broker nicht erreichbar")
    monkeypatch.setattr(paho.mqtt.publish, "single", broker_weg)

    assert unit_alarm.melden(cfg, "EMS: x abgebrochen", {"schritt": "x"}) == 1
    ereignis, = read_dashboard_events(cfg.e3dc_rscp.history_db_path,
                                      "Europe/Berlin")
    assert (ereignis["kind"], ereignis["level"]) == ("alarm", "error")
    assert ereignis["details"] == {"schritt": "x"}


def test_einzelalarm_ohne_last_will_und_ohne_retain(monkeypatch):
    """Ein Will (ems/status = offline) wuerde den laufenden Dienst in Homey als
    offline melden, sobald der kurzlebige Alarmprozess endet."""
    cfg = make_config().mqtt
    cfg.enabled = True
    gesendet = {}
    monkeypatch.setattr(paho.mqtt.publish, "single",
                        lambda topic, payload, **kw: gesendet.update(
                            topic=topic, payload=payload, **kw))

    assert publish_alert_once(cfg, "error", "EMS: Test") is True
    assert gesendet["topic"] == f"{cfg.base_topic}/alert"
    assert gesendet["retain"] is False
    assert gesendet.get("will") is None and not gesendet.get("client_id")
    assert json.loads(gesendet["payload"])["message"] == "EMS: Test"

    cfg.enabled = False
    gesendet.clear()
    assert publish_alert_once(cfg, "error", "EMS: Test") is False
    assert gesendet == {}
