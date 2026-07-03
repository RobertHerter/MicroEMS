"""Prüft die Einheit von awattar.total3 in der InfluxDB und empfiehlt scale.

Auf dem Pi ausführen (dort besteht DB-Zugang):
    python -m test_awattar_einheit --config config.yaml

Das Skript liest die letzten Werte von awattar.total3, zeigt sie an und leitet
aus der Größenordnung die Einheit + die passende scale (Ziel: ct/kWh) ab.
Es schreibt NICHTS in die Datenbank.
"""
from __future__ import annotations

import argparse
import statistics

from ems.config import load_config


def infer_scale(median_abs: float):
    """Leitet Einheit und scale (-> ct/kWh) aus der Größenordnung ab."""
    if median_abs == 0:
        return None, None, "alle Werte 0 – keine Aussage möglich"
    if median_abs < 0.005:
        return 100000.0, "€/Wh", "z.B. 0.00027 -> 27 ct/kWh"
    if median_abs < 1.0:
        return 100.0, "€/kWh", "z.B. 0.27 -> 27 ct/kWh"
    if median_abs < 100.0:
        return 1.0, "ct/kWh", "z.B. 27 -> 27 ct/kWh (bereits ct/kWh)"
    if median_abs < 1000.0:
        return 0.1, "€/MWh", "z.B. 270 -> 27 ct/kWh"
    return None, "unbekannt", "Größenordnung unklar – bitte manuell prüfen"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if cfg.influxdb.version != 1:
        print("Hinweis: Skript ist für InfluxDB 1.x ausgelegt.")
    v1 = cfg.influxdb.v1
    spec = cfg.influxdb.signals["electricity_price"]

    from influxdb import InfluxDBClient

    client = InfluxDBClient(
        host=v1.get("host", "localhost"), port=int(v1.get("port", 8086)),
        username=v1.get("username") or None, password=v1.get("password") or None,
        database=v1["database"], ssl=bool(v1.get("ssl", False)),
        verify_ssl=bool(v1.get("verify_ssl", False)),
    )

    q = (f'SELECT "{spec.field}" AS value FROM "{spec.measurement}" '
         f'ORDER BY time DESC LIMIT {args.limit}')
    print(f"Abfrage: {q}\n")
    pts = list(client.query(q, epoch="s").get_points())
    if not pts:
        print("Keine Werte gefunden. Feld/Measurement/Tags prüfen.")
        return

    vals = [float(p["value"]) for p in pts if p.get("value") is not None]
    print("Letzte Werte von awattar.total3:")
    for p in pts[:10]:
        print(f"  {p['time']}: {p['value']}")

    med = statistics.median(abs(v) for v in vals)
    scale, unit, example = infer_scale(med)
    print(f"\nMedian |Wert| = {med:.6g}")
    print(f"Vermutete Einheit: {unit}  ({example})")
    if scale is not None:
        print(f"-> Empfohlene Einstellung:  electricity_price.scale = {scale}")
        print(f"   Ergäbe z.B. {med*scale:.2f} ct/kWh für den Median.")
        if not (0.5 <= med * scale <= 80):
            print("   WARNUNG: Ergebnis außerhalb typischer 0,5–80 ct/kWh – bitte prüfen.")
    client.close()


if __name__ == "__main__":
    main()
