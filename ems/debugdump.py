"""Debug-Schnappschuss + Mail-Versand.

save_snapshot() sichert je Zyklus alles, was zum REPRODUZIEREN eines Laufs
nötig ist: die Optimierer-Eingaben (PV/Last/Preis/Einspeisung/SoC), die
modellrelevante Konfiguration (ohne Zugangsdaten), den Plan, die Verstöße der
Planprüfung und die SoC-Drift - als eine JSON-Datei.

send_report() hängt diese Datei an eine Mail (SMTP) und schickt sie an die in
config.report konfigurierte Adresse. Über den Button im Dashboard kann man so
bei einer Implausibilität die vollständigen Debugdaten anfordern, mit denen
sich der Fehler offline exakt nachstellen lässt (backtest / Optimizer).

Keine Zugangsdaten im Snapshot: die Konfiguration wird auf die Modell-Parameter
reduziert.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, is_dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

from .config import Config

log = logging.getLogger("ems.debug")


def _safe_config(config: Config) -> dict:
    """Nur die modellrelevanten Parameter – KEINE Zugangsdaten (InfluxDB/MQTT/
    SMTP)."""
    def d(obj):
        return {k: (str(v) if not isinstance(v, (int, float, bool, type(None), dict, list)) else v)
                for k, v in asdict(obj).items()} if is_dataclass(obj) else {}
    return {
        "general": d(config.general),
        "house_battery": d(config.house_battery),
        "inverter": d(config.inverter),
        "vehicle": d(config.vehicle),
        "feed_in": d(config.feed_in),
        "optimization": d(config.optimization),
        "forecast": d(config.forecast),
    }


def save_snapshot(config: Config, now, inputs, result, violations,
                  drift_mae: Optional[float] = None) -> str:
    """Schreibt den Debug-Schnappschuss (atomar). Rückgabe: Pfad."""
    path = config.report.snapshot_path
    t = result.table

    def arr(a):
        return [None if (x is None or (isinstance(x, float) and np.isnan(x)))
                else round(float(x), 3) for x in np.asarray(a)]

    snap = {
        "generated": pd.Timestamp(now).isoformat(),
        "status": result.status,
        "infeasible": bool(result.infeasible),
        "solver_hit_limit": bool(result.solver_hit_limit),
        "total_cost_eur": round(result.total_cost_ct / 100.0, 3),
        "car_target_shortfall_wh": round(result.car_target_shortfall_wh, 1),
        "drift_soc_mae_pp": (round(drift_mae, 2) if drift_mae is not None else None),
        "violations": [
            {"rule": v.rule, "severity": v.severity, "count": v.count,
             "detail": v.detail,
             "slots": [s.isoformat() for s in v.slots[:20]]}
            for v in (violations or [])
        ],
        "config": _safe_config(config),
        "inputs": {
            "index": [ts.isoformat() for ts in inputs.index],
            "house_load_w": arr(inputs.house_load_w),
            "pv_w": arr(inputs.pv_w),
            "pv10_w": arr(inputs.pv10_w) if inputs.pv10_w is not None else None,
            "price_ct_kwh": arr(inputs.price_ct_kwh),
            "feedin_ct_kwh": arr(inputs.feedin_ct_kwh),
            "initial_house_soc_wh": round(float(inputs.initial_house_soc_wh), 1),
            "initial_car_soc_wh": (round(float(inputs.initial_car_soc_wh), 1)
                                   if inputs.initial_car_soc_wh is not None else None),
            "car_present": bool(inputs.car_present),
        },
        "plan": {c: arr(t[c]) for c in t.columns if t[c].dtype != object},
        "plan_mode": list(t["mode"]) if "mode" in t.columns else None,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def send_report(config: Config, note: str = "") -> str:
    """Verschickt den zuletzt gesicherten Schnappschuss per Mail.
    Rückgabe: Statustext (für die Dashboard-Rückmeldung)."""
    import smtplib
    from email.message import EmailMessage

    r = config.report
    if not r.enabled:
        return "Mail-Report ist deaktiviert (report.enabled=false in config.yaml)."
    if not (r.smtp_host and r.mail_to and r.mail_from):
        return "SMTP unvollständig konfiguriert (smtp_host/mail_from/mail_to)."
    path = r.snapshot_path
    if not os.path.exists(path):
        return "Noch kein Debug-Schnappschuss vorhanden (erster Lauf abwarten)."

    with open(path, "rb") as fh:
        data = fh.read()
    try:
        meta = json.loads(data)
        vio = meta.get("violations", [])
        errs = sum(1 for v in vio if v["severity"] == "error")
        warns = sum(1 for v in vio if v["severity"] == "warning")
        summary = (f"Status {meta.get('status')}, {errs} Fehler, {warns} Warnungen, "
                   f"Drift {meta.get('drift_soc_mae_pp')} pp, "
                   f"Kosten {meta.get('total_cost_eur')} €")
        stamp = meta.get("generated", "")
    except Exception:
        summary, stamp = "(Schnappschuss nicht lesbar)", ""

    msg = EmailMessage()
    msg["Subject"] = f"EMS Debug-Report {stamp} – {summary}"
    msg["From"] = r.mail_from
    msg["To"] = r.mail_to
    body = ["EMS Debug-Report (per Dashboard-Button angefordert).", "",
            summary, ""]
    if note:
        body += [f"Notiz: {note}", ""]
    for v in (vio if 'vio' in dir() else []):
        body.append(f"  [{v['severity']}] {v['rule']}: {v['detail']}")
    body += ["", "Anhang last_run_debug.json enthält Eingaben + Plan zum "
             "Reproduzieren (backtest/Optimizer). Keine Zugangsdaten enthalten."]
    msg.set_content("\n".join(body))
    msg.add_attachment(data, maintype="application", subtype="json",
                       filename="last_run_debug.json")

    try:
        if r.use_tls:
            with smtplib.SMTP(r.smtp_host, r.smtp_port, timeout=20) as s:
                s.starttls()
                if r.smtp_user:
                    s.login(r.smtp_user, r.smtp_password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(r.smtp_host, r.smtp_port, timeout=20) as s:
                if r.smtp_user:
                    s.login(r.smtp_user, r.smtp_password)
                s.send_message(msg)
    except Exception as exc:
        log.warning("Debug-Report-Mail fehlgeschlagen (%s).", exc)
        return f"Mailversand fehlgeschlagen: {exc}"
    log.info("Debug-Report an %s gesendet.", r.mail_to)
    return f"Debug-Report an {r.mail_to} gesendet."
