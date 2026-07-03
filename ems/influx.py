"""InfluxDB-Abstraktion für Version 1.x (InfluxQL) und 2.x (Flux).

Die Aggregation auf das Slot-Raster erfolgt DB-seitig (GROUP BY time(slot) bzw.
aggregateWindow). Dadurch:
  * ist das Verfahren unabhängig vom Eingangsraster der Rohdaten – egal ob die
    Quelle sekündlich, minütlich oder stündlich schreibt, es wird sauber auf das
    konfigurierte Slot-Raster (z.B. 15 oder 60 min) gemittelt/aggregiert;
  * werden für Leistungen/Preise Mittelwerte (rasterunabhängig, in W bzw. ct/kWh)
    und für Ladezustände der letzte Wert (LAST) gebildet;
  * bleibt die Datenmenge klein (Aggregation in der DB, nicht im Client).

Pro Signal konfigurierbar: measurement, field, tags (inkl. ::tag), aggregation,
retention_policy sowie scale/offset zur Einheiten-Umrechnung.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Optional

import pandas as pd

from .config import Config, SignalSpec

log = logging.getLogger("ems.influx")

_AGG_FUNCS = {"mean", "last", "first", "sum", "max", "min", "median"}


class InfluxBackend(ABC):
    @abstractmethod
    def read_slots(self, spec: SignalSpec, start: datetime, end: datetime,
                   slot_seconds: int) -> pd.Series: ...

    @abstractmethod
    def read_latest(self, spec: SignalSpec, start: datetime, end: datetime) -> Optional[float]: ...

    @abstractmethod
    def write_frame(self, measurement: str, df: pd.DataFrame,
                    tags: Optional[Dict[str, str]] = None) -> None: ...

    def close(self) -> None:  # pragma: no cover
        pass


# --------------------------------------------------------------------------- #
# InfluxDB 1.x (InfluxQL)
# --------------------------------------------------------------------------- #
class InfluxV1Backend(InfluxBackend):
    def __init__(self, cfg: dict):
        from influxdb import InfluxDBClient

        self.database = cfg["database"]
        self.client = InfluxDBClient(
            host=cfg.get("host", "localhost"),
            port=int(cfg.get("port", 8086)),
            username=cfg.get("username") or None,
            password=cfg.get("password") or None,
            database=self.database,
            ssl=bool(cfg.get("ssl", False)),
            verify_ssl=bool(cfg.get("verify_ssl", False)),
        )

    def _from_clause(self, spec: SignalSpec) -> str:
        if spec.retention_policy:
            return f'"{self.database}"."{spec.retention_policy}"."{spec.measurement}"'
        return f'"{spec.measurement}"'

    def _where(self, spec: SignalSpec, start: datetime, end: datetime) -> str:
        start_ns = int(pd.Timestamp(start).tz_convert("UTC").value)
        end_ns = int(pd.Timestamp(end).tz_convert("UTC").value)
        clauses = [f"time >= {start_ns} AND time <= {end_ns}"]
        for k, v in spec.tags.items():
            clauses.append(f"\"{k}\"::tag = '{v}'")
        return " AND ".join(clauses)

    def read_slots(self, spec, start, end, slot_seconds):
        agg = spec.aggregation.lower()
        if agg not in _AGG_FUNCS:
            agg = "mean"
        q = (
            f'SELECT {agg}("{spec.field}") AS value FROM {self._from_clause(spec)} '
            f"WHERE {self._where(spec, start, end)} "
            f"GROUP BY time({slot_seconds}s) fill(none)"
        )
        log.debug("InfluxQL: %s", q)
        rs = self.client.query(q, epoch="ns")
        pts = list(rs.get_points())
        if not pts:
            return pd.Series(dtype="float64")
        idx = pd.to_datetime([p["time"] for p in pts], unit="ns", utc=True)
        return pd.Series([p["value"] for p in pts], index=idx, dtype="float64").sort_index()

    def read_latest(self, spec, start, end):
        q = (
            f'SELECT last("{spec.field}") AS value FROM {self._from_clause(spec)} '
            f"WHERE {self._where(spec, start, end)}"
        )
        pts = list(self.client.query(q, epoch="ns").get_points())
        return float(pts[-1]["value"]) if pts else None

    def write_frame(self, measurement, df, tags=None):
        if df.empty:
            return
        idx_utc = df.index.tz_convert("UTC")
        points = []
        for ts, row in zip(idx_utc, df.itertuples(index=False)):
            fields = {c: float(v) for c, v in zip(df.columns, row) if pd.notna(v)}
            if fields:
                points.append({"measurement": measurement, "tags": tags or {},
                               "time": int(ts.value), "fields": fields})
        if points:
            self.client.write_points(points, time_precision="n", batch_size=5000)

    def close(self):
        self.client.close()


# --------------------------------------------------------------------------- #
# InfluxDB 2.x (Flux)
# --------------------------------------------------------------------------- #
class InfluxV2Backend(InfluxBackend):
    def __init__(self, cfg: dict):
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS

        self.bucket = cfg["bucket"]
        self.org = cfg["org"]
        self.client = InfluxDBClient(url=cfg["url"], token=cfg["token"], org=self.org)
        self._write_api = self.client.write_api(write_options=SYNCHRONOUS)
        self._query_api = self.client.query_api()

    def _filters(self, spec: SignalSpec) -> str:
        s = (f'  |> filter(fn: (r) => r["_measurement"] == "{spec.measurement}")\n'
             f'  |> filter(fn: (r) => r["_field"] == "{spec.field}")\n')
        for k, v in spec.tags.items():
            s += f'  |> filter(fn: (r) => r["{k}"] == "{v}")\n'
        return s

    def read_slots(self, spec, start, end, slot_seconds):
        agg = spec.aggregation.lower()
        fn = {"mean": "mean", "last": "last", "first": "first", "sum": "sum",
              "max": "max", "min": "min", "median": "median"}.get(agg, "mean")
        start_iso = pd.Timestamp(start).tz_convert("UTC").isoformat()
        end_iso = pd.Timestamp(end).tz_convert("UTC").isoformat()
        flux = (
            f'from(bucket: "{self.bucket}")\n'
            f"  |> range(start: {start_iso}, stop: {end_iso})\n"
            f"{self._filters(spec)}"
            f"  |> aggregateWindow(every: {slot_seconds}s, fn: {fn}, createEmpty: false)\n"
            f'  |> keep(columns: ["_time", "_value"])'
        )
        log.debug("Flux: %s", flux)
        df = self._query_api.query_data_frame(flux, org=self.org)
        if isinstance(df, list):
            df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()
        if df is None or df.empty or "_value" not in df:
            return pd.Series(dtype="float64")
        idx = pd.to_datetime(df["_time"], utc=True)
        return pd.Series(df["_value"].astype("float64").values, index=idx).sort_index()

    def read_latest(self, spec, start, end):
        start_iso = pd.Timestamp(start).tz_convert("UTC").isoformat()
        end_iso = pd.Timestamp(end).tz_convert("UTC").isoformat()
        flux = (
            f'from(bucket: "{self.bucket}")\n'
            f"  |> range(start: {start_iso}, stop: {end_iso})\n"
            f"{self._filters(spec)}"
            f"  |> last()"
        )
        df = self._query_api.query_data_frame(flux, org=self.org)
        if isinstance(df, list):
            df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()
        if df is None or df.empty or "_value" not in df:
            return None
        return float(df["_value"].iloc[-1])

    def write_frame(self, measurement, df, tags=None):
        if df.empty:
            return
        from influxdb_client import Point, WritePrecision

        idx_utc = df.index.tz_convert("UTC")
        points = []
        for ts, row in zip(idx_utc, df.itertuples(index=False)):
            p = Point(measurement).time(ts.to_pydatetime(), WritePrecision.NS)
            for k, v in (tags or {}).items():
                p = p.tag(k, v)
            has = False
            for c, v in zip(df.columns, row):
                if pd.notna(v):
                    p = p.field(c, float(v)); has = True
            if has:
                points.append(p)
        if points:
            self._write_api.write(bucket=self.bucket, org=self.org, record=points)

    def close(self):
        self.client.close()


# --------------------------------------------------------------------------- #
# Repository (High-Level)
# --------------------------------------------------------------------------- #
class InfluxRepository:
    def __init__(self, config: Config):
        self.config = config
        self.tz = config.general.timezone
        self.slot_seconds = config.general.slot_minutes * 60
        if config.influxdb.version == 1:
            self.backend: InfluxBackend = InfluxV1Backend(config.influxdb.v1)
        else:
            self.backend = InfluxV2Backend(config.influxdb.v2)

    def _spec(self, name: str) -> Optional[SignalSpec]:
        return self.config.influxdb.signals.get(name)

    def signal_available(self, name: str) -> bool:
        return name in self.config.influxdb.signals

    def read_slots(self, name: str, start: datetime, end: datetime) -> pd.Series:
        """Aggregiert ein Signal DB-seitig auf das Slot-Raster (tz-lokal).

        Unabhängig vom Eingangsraster: die DB bildet je Slot den konfigurierten
        Aggregatwert (mean/last/...). Lücken werden per Zeit-Interpolation und
        anschließendem ffill/bfill gefüllt. scale/offset werden angewandt.
        """
        spec = self._spec(name)
        if spec is None:
            raise KeyError(f"Signal '{name}' ist nicht in influxdb.signals konfiguriert.")

        raw = self.backend.read_slots(spec, start, end, self.slot_seconds)
        freq = f"{self.config.general.slot_minutes}min"
        index = pd.date_range(
            start=pd.Timestamp(start).tz_convert("UTC").floor(freq),
            end=pd.Timestamp(end).tz_convert("UTC"),
            freq=freq, tz="UTC", inclusive="left",
        )
        if raw.empty:
            series = pd.Series(index=index, dtype="float64")
        else:
            raw = raw[~raw.index.duplicated(keep="last")]
            if spec.fill_method == "hold":
                # stufige Signale (Preis): letzten Wert bis zum nächsten halten
                series = raw.reindex(raw.index.union(index)).ffill()
                series = series.reindex(index).ffill().bfill()
            else:
                # glatte Signale (Last, PV): lineare Zeit-Interpolation
                series = raw.reindex(raw.index.union(index)).interpolate(method="time")
                series = series.reindex(index).ffill().bfill()
        series = series * spec.scale + spec.offset
        return series.tz_convert(self.tz)

    def read_scalar_latest(self, name: str, start: datetime, end: datetime) -> Optional[float]:
        spec = self._spec(name)
        if spec is None:
            return None
        val = self.backend.read_latest(spec, start, end)
        if val is None:
            return None
        return val * spec.scale + spec.offset

    def write_frame(self, output_key: str, df: pd.DataFrame, tags=None) -> None:
        measurement = self.config.influxdb.outputs[output_key]
        self.backend.write_frame(measurement, df, tags=tags)

    def close(self) -> None:
        self.backend.close()
