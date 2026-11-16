#!/usr/bin/env python3
"""Export MicroEMS' read-only E3DC history into EOS measurements."""
from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
import time
import urllib.error
import urllib.request
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


DEFAULT_DB = Path("/opt/ems/e3dc_history.sqlite")
DEFAULT_URL = "http://127.0.0.1:8513"
OPTIMIZATION_LOCK = Path("/run/lock/eos-comparison-optimization.lock")
DEFAULT_FORECAST_ARCHIVE = Path("/opt/eos/comparison/forecast_history.sqlite")


def request_json(
    url: str, method: str, path: str, payload: Any = None, timeout: float = 30
) -> dict:
    data = None if method == "GET" else (
        b"" if payload is None else json.dumps(payload).encode()
    )
    request = urllib.request.Request(
        f"{url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body) if body else {}


def solution_generated_at(
    url: str, request_fn: Callable[[str, str, Any], dict]
) -> str | None:
    try:
        solution = request_fn("GET", "/v1/energy-management/optimization/solution", None)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return solution.get("generated_at")


def _utc_iso(raw: str) -> str:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
        timezone.utc
    ).isoformat()


def archive_solution_forecast(
    database: Path,
    system_id: str,
    solution: dict,
    retention_days: int = 120,
) -> int:
    """Persist compact immutable EOS inputs and dispatch plan for later scoring."""
    issue_raw = solution.get("generated_at")
    prediction_rows = solution.get("prediction", {}).get("data", {})
    solution_rows = solution.get("solution", {}).get("data", {})
    if not issue_raw or not prediction_rows or not solution_rows:
        raise ValueError("EOS solution has no generated_at, prediction, or plan data")

    def numeric(value: Any, multiplier: float = 1.0) -> float | None:
        try:
            return float(value) * multiplier
        except (TypeError, ValueError):
            return None

    timestamps: list[str] = []
    pv_wh: list[float | None] = []
    load_wh: list[float | None] = []
    price_ct_kwh: list[float | None] = []
    grid_import_wh: list[float | None] = []
    grid_export_wh: list[float | None] = []
    grid_net_wh: list[float | None] = []
    soc_pct: list[float | None] = []
    for timestamp in sorted(prediction_rows, key=_utc_iso):
        row = prediction_rows[timestamp]
        plan = solution_rows.get(timestamp, {})
        timestamps.append(_utc_iso(timestamp))
        pv_wh.append(numeric(row.get("pvforecast_ac_energy_wh")))
        load_wh.append(numeric(row.get("loadforecast_energy_wh")))
        price_ct_kwh.append(numeric(row.get("elec_price_amt_kwh"), 100.0))
        imported = numeric(plan.get("grid_consumption_energy_wh"))
        exported = numeric(plan.get("grid_feedin_energy_wh"))
        grid_import_wh.append(imported)
        grid_export_wh.append(exported)
        grid_net_wh.append(
            None if imported is None or exported is None else imported - exported
        )
        soc_factor = next(
            (numeric(value) for key, value in plan.items()
             if key.endswith("_soc_factor") and "battery" in key),
            None,
        )
        soc_pct.append(None if soc_factor is None else round(soc_factor * 100.0, 6))

    payload = zlib.compress(json.dumps({
        "timestamps": timestamps,
        "pv_wh": pv_wh,
        "load_wh": load_wh,
        "price_ct_kwh": price_ct_kwh,
        "grid_import_wh": grid_import_wh,
        "grid_export_wh": grid_export_wh,
        "grid_net_wh": grid_net_wh,
        # EOS reports battery SoC at the beginning of each hour.
        "soc_pct": soc_pct,
    }, separators=(",", ":"), allow_nan=False).encode("utf-8"), level=6)
    issue = _utc_iso(str(issue_raw))
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=30)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS forecast_snapshots ("
        "system_id TEXT NOT NULL, issue_ts TEXT NOT NULL, payload BLOB NOT NULL, "
        "PRIMARY KEY(system_id, issue_ts))"
    )
    cursor = connection.execute(
        "INSERT OR IGNORE INTO forecast_snapshots(system_id,issue_ts,payload) "
        "VALUES(?,?,?)",
        (system_id, issue, sqlite3.Binary(payload)),
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(
        days=max(1, int(retention_days))
    )).isoformat()
    connection.execute("DELETE FROM forecast_snapshots WHERE issue_ts < ?", (cutoff,))
    connection.commit()
    inserted = int(cursor.rowcount > 0)
    connection.close()
    return inserted


@contextmanager
def optimization_lock(path: Path = OPTIMIZATION_LOCK) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def run_optimization(
    url: str,
    timeout: float = 600,
    poll_interval: float = 5,
    request_fn: Callable[[str, str, Any], dict] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> str:
    """Run one automatic EOS optimization and always restore safe disabled mode."""
    url = url.rstrip("/")
    if request_fn is None:
        request_fn = lambda method, path, payload=None: request_json(
            url, method, path, payload, timeout=30
        )
    previous = solution_generated_at(url, request_fn)
    try:
        request_fn("PUT", "/v1/config/ems/interval", 60.0)
        request_fn("PUT", "/v1/config/ems/mode", "OPTIMIZATION")
        deadline = monotonic_fn() + timeout
        while monotonic_fn() < deadline:
            generated = solution_generated_at(url, request_fn)
            if generated is not None and generated != previous:
                return generated
            sleep_fn(poll_interval)
        raise TimeoutError(f"EOS optimization at {url} did not finish within {timeout:.0f}s")
    finally:
        request_fn("PUT", "/v1/config/ems/mode", "DISABLED")
        request_fn("PUT", "/v1/config/ems/interval", 900.0)
        request_fn("PUT", "/v1/config/file", None)


def ensure_idle(
    url: str,
    request_fn: Callable[[str, str, Any], dict] | None = None,
    pv_provider: str | None = None,
) -> list[str]:
    """Force the safe idle state before a run and return what was repaired.

    run_optimization() restores DISABLED/900 s in its ``finally`` block, but that
    cannot run when the EOS container dies mid-run. EOS then persists
    OPTIMIZATION/60 s, optimizes every minute without PV data and resets the PV
    provider to its demo default, so every later import fails with HTTP 400
    (2026-11-06 until 2026-11-14 on the import instance). Checking at the START of
    each run makes the next run repair such a leftover by itself.
    """
    url = url.rstrip("/")
    if request_fn is None:
        request_fn = lambda method, path, payload=None: request_json(
            url, method, path, payload, timeout=30
        )
    repaired: list[str] = []
    ems = request_fn("GET", "/v1/config/ems", None) or {}
    if ems.get("mode") != "DISABLED":
        request_fn("PUT", "/v1/config/ems/mode", "DISABLED")
        repaired.append(f"ems.mode {ems.get('mode')!r} -> 'DISABLED'")
    if ems.get("interval") != 900.0:
        request_fn("PUT", "/v1/config/ems/interval", 900.0)
        repaired.append(f"ems.interval {ems.get('interval')!r} -> 900.0")
    if pv_provider is not None:
        current = request_fn("GET", "/v1/config/pvforecast/provider", None)
        if current != pv_provider:
            request_fn("PUT", "/v1/config/pvforecast/provider", pv_provider)
            repaired.append(f"pvforecast.provider {current!r} -> {pv_provider!r}")
    if repaired:
        request_fn("PUT", "/v1/config/file", None)
    return repaired


def put_series(url: str, key: str, values: dict[str, float]) -> None:
    if not values:
        return
    payload = json.dumps({"data": values, "dtype": "float64", "tz": "UTC"}).encode()
    request = urllib.request.Request(
        f"{url}/v1/measurement/series?key={key}",
        data=payload,
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"EOS returned HTTP {response.status} for {key}")


def put_prediction(url: str, provider: str, payload: dict) -> None:
    request = urllib.request.Request(
        f"{url}/v1/prediction/import/{provider}",
        data=json.dumps(payload, allow_nan=False).encode(),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"EOS returned HTTP {response.status} for {provider}")


def refresh_predictions(url: str) -> None:
    request = urllib.request.Request(
        f"{url}/v1/prediction/update?force_update=false&force_enable=false",
        data=b"",
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        if response.status != 200:
            raise RuntimeError(f"EOS returned HTTP {response.status} for prediction refresh")


def latest_optimizer_forecast(connection: sqlite3.Connection) -> tuple[str, dict] | None:
    row = connection.execute(
        "SELECT issue_ts, payload FROM optimizer_forecast_snapshots "
        "ORDER BY issue_ts DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return row[0], json.loads(zlib.decompress(row[1]).decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--hours", type=int, default=24 * 30,
                        help="Number of 15-minute slots to export (default: 30 days).")
    parser.add_argument("--measurements-only", action="store_true",
                        help="Do not import MicroEMS forecast series.")
    parser.add_argument("--refresh-predictions", action="store_true",
                        help="Ask EOS to refresh its configured native providers afterwards.")
    parser.add_argument("--optimize", action="store_true",
                        help="Run one EOS optimization and restore DISABLED mode afterwards.")
    parser.add_argument("--optimization-timeout", type=float, default=600,
                        help="Maximum seconds to wait for a new EOS solution.")
    parser.add_argument("--archive-system",
                        help="System id used to archive compact forecast vintages.")
    parser.add_argument("--archive-db", type=Path, default=DEFAULT_FORECAST_ARCHIVE,
                        help="SQLite archive for EOS forecast vintages.")
    parser.add_argument("--archive-retention-days", type=int, default=120,
                        help="Retention for compact EOS forecast vintages.")
    args = parser.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()
    connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT ts, pv_w, house_w, grid_w, battery_w, soc FROM actuals "
        "ORDER BY ts"
    ).fetchall()
    forecast = latest_optimizer_forecast(connection)
    connection.close()

    columns = {
        # MicroEMS: + = charge. EOS: + = discharge.
        "battery1-power-3-phase-sym-w": 4,
        "battery1-soc-factor": 5,
    }
    series: dict[str, dict[str, float]] = {key: {} for key in columns}
    meters = {
        "e3dc-house-emr": 0.0,
        "e3dc-pv-emr": 0.0,
        "e3dc-grid-import-emr": 0.0,
        "e3dc-grid-export-emr": 0.0,
    }
    meter_series: dict[str, dict[str, float]] = {key: {} for key in meters}
    for row in rows:
        timestamp = row[0].replace(" ", "T")
        # E3DC history is a 15-minute mean-power series. Convert it to EOS'
        # required cumulative kWh meter readings before filtering export rows.
        for key, watts in (
            ("e3dc-house-emr", row[2]),
            ("e3dc-pv-emr", row[1]),
            ("e3dc-grid-import-emr", max(float(row[3] or 0.0), 0.0)),
            ("e3dc-grid-export-emr", max(-float(row[3] or 0.0), 0.0)),
        ):
            if watts is not None:
                meters[key] += float(watts) / 4000.0
        if row[0] < since:
            continue
        for key, index in columns.items():
            value = row[index]
            if value is not None:
                series[key][timestamp] = (-float(value) if index == 4 else
                                          float(value) / 100.0 if index == 5 else float(value))
        for key, value in meters.items():
            meter_series[key][timestamp] = value

    # Repair a leftover OPTIMIZATION mode (and, on the import instance, the PV
    # provider) from an earlier run that could not restore the idle state.
    repaired = ensure_idle(
        args.url,
        pv_provider=None if args.measurements_only or not forecast
        else "PVForecastImport",
    )
    if repaired:
        print("Repaired EOS idle state: " + "; ".join(repaired))

    exported_rows = len(next(iter(series.values()), {}))
    for key, values in series.items():
        put_series(args.url.rstrip("/"), key, values)
    for key, values in meter_series.items():
        put_series(args.url.rstrip("/"), key, values)

    forecast_rows = 0
    if forecast and not args.measurements_only:
        issue_time, payload = forecast
        timestamps = payload["timestamps"]
        values = payload["series"]
        common = {
            "start_datetime": timestamps[0],
            "interval": "15 minutes",
        }
        put_prediction(args.url.rstrip("/"), "LoadImport", {
            **common,
            "loadforecast_power_w": values["house_load_w"],
        })
        put_prediction(args.url.rstrip("/"), "PVForecastImport", {
            **common,
            "pvforecast_ac_power": values["pv_w"],
            # MicroEMS liefert eine an der AC-Seite verwendbare Gesamtprognose.
            # EOS benötigt zusätzlich DC; ohne getrennte Messgröße identisch setzen.
            "pvforecast_dc_power": values["pv_w"],
        })
        put_prediction(args.url.rstrip("/"), "ElecPriceImport", {
            **common,
            # MicroEMS ct/kWh -> EOS EUR/Wh.
            "elecprice_marketprice_wh": [
                None if value is None else float(value) / 100_000.0
                for value in values["price_ct_kwh"]
            ],
        })
        put_prediction(args.url.rstrip("/"), "FeedInTariffImport", {
            **common,
            "feed_in_tariff_wh": [
                None if value is None else float(value) / 100_000.0
                for value in values["feedin_ct_kwh"]
            ],
        })
        put_prediction(args.url.rstrip("/"), "WeatherImport", {
            **common,
            "weather_temp_air": values["ambient_temp_c"],
            "weather_ghi": values["solar_w_m2"],
        })
        forecast_rows = len(timestamps)
        print(f"Imported MicroEMS forecast {issue_time} with {forecast_rows} 15-minute rows.")

    if args.refresh_predictions:
        refresh_predictions(args.url.rstrip("/"))
        print("Refreshed EOS native prediction providers.")

    print(f"Exported {exported_rows} E3DC 15-minute measurement rows to EOS.")

    if args.optimize:
        with optimization_lock():
            generated = run_optimization(
                args.url.rstrip("/"), timeout=args.optimization_timeout
            )
            if args.archive_system:
                solution = request_json(
                    args.url.rstrip("/"), "GET",
                    "/v1/energy-management/optimization/solution", timeout=30,
                )
                inserted = archive_solution_forecast(
                    args.archive_db,
                    args.archive_system,
                    solution,
                    retention_days=args.archive_retention_days,
                )
                print(
                    f"Archived {args.archive_system} forecast vintage "
                    f"{generated} ({'new' if inserted else 'existing'})."
                )
        print(f"Generated fresh EOS optimization solution {generated}.")


if __name__ == "__main__":
    main()
