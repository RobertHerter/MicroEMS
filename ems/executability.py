"""Ausführungspfad jeder Optimiererentscheidung transparent kennzeichnen."""
from __future__ import annotations

import pandas as pd


LABELS = {
    "rscp": "direkt per RSCP",
    "mqtt": "MQTT-Sollwert",
    "inverter": "durch Wechselrichter",
    "model": "nur modelliert",
}


def annotate_executability(config, table: pd.DataFrame) -> pd.DataFrame:
    """Ergänzt Ausführungskanal und Begründung ohne den Plan zu verändern."""
    if table is None or table.empty:
        return table
    rscp = bool(config.e3dc_rscp.enabled and config.e3dc_rscp.control_enabled)
    curtail_rscp = bool(
        rscp and getattr(config.e3dc_rscp, "curtailment_control_enabled", False))
    mqtt = bool(getattr(config.mqtt, "enabled", False))
    max_export_w = config.inverter.max_export_w
    max_ac = float(config.inverter.max_ac_power_w)
    load_cols = [name for name in table.columns
                 if name.startswith("load_") and name.endswith("_w")]
    paths, details = [], []
    for _, row in table.iterrows():
        curt = max(0.0, float(row.get("pv_curtail_w", 0.0) or 0.0))
        pv = max(0.0, float(row.get("pv_w", 0.0) or 0.0))
        export = max(0.0, float(row.get("grid_export_w", 0.0) or 0.0))
        inherent_clip = max(0.0, pv - max_ac)
        # Ein STATISCHER Einspeise-Limiter kappt die PV nur so weit, bis die
        # Einspeisung auf max_export_w liegt – er kann NICHT darunter drücken.
        # Ausführbar ist die Abregelung durch ihn also nur, wenn die Einspeisung
        # danach an der Grenze liegt (Limiter erledigt den Überschuss). Regelt der
        # Plan UNTER die Grenze ab, braucht das einen echten Aktor (RSCP-Derating);
        # sonst ist es nur modelliert und der Ausführungs-Audit muss es fangen.
        limiter_realizes = (max_export_w is not None
                            and export >= float(max_export_w) - 5.0)
        battery_command = any(float(row.get(name, 0.0) or 0.0) > 5.0 for name in (
            "batt_grid_charge_w", "batt_grid_discharge_w")) or bool(
                row.get("charge_limited", False) or row.get("discharge_limited", False))
        load_command = any(float(row.get(name, 0.0) or 0.0) > 5.0
                           for name in load_cols)
        if curt > 5.0:
            if curt <= inherent_clip + 5.0 or limiter_realizes:
                path = "inverter"
                detail = "PV-Begrenzung wird durch die physische WR-/Einspeisegrenze erledigt."
            elif curtail_rscp:
                path = "rscp"
                detail = "PV-Grenze wird an den E3DC gesendet und zurückgelesen."
            else:
                path = "model"
                detail = "Für diese PV-Abregelung ist derzeit kein bestätigter Aktor verfügbar."
        elif battery_command:
            if rscp:
                path, detail = "rscp", "Akku-Sollwert wird direkt gesendet und zurückgelesen."
            elif mqtt:
                path, detail = "mqtt", "Akku-Sollwert wird per MQTT ausgegeben; Gerätequittung fehlt."
            else:
                path, detail = "model", "Akku-Eingriff besitzt derzeit keinen aktiven Ausgabekanal."
        elif load_command:
            if mqtt:
                path, detail = "mqtt", "Lastfreigabe wird per MQTT ausgegeben."
            else:
                path, detail = "model", "Lastentscheidung besitzt derzeit keinen aktiven Ausgabekanal."
        else:
            path, detail = "inverter", "Eigenverbrauch und freie Akkuregelung erledigt der Wechselrichter."
        paths.append(path)
        details.append(detail)
    table["execution_path"] = paths
    table["execution_label"] = [LABELS[path] for path in paths]
    table["execution_detail"] = details
    return table
