#!/usr/bin/env python3
"""Read-only comparison dashboard for MicroEMS and two local EOS instances."""

from __future__ import annotations

import json
import math
import mimetypes
import os
import sqlite3
import tempfile
import urllib.error
import urllib.request
import zlib
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
SNAPSHOTS = ROOT / "snapshots"
MICROEMS_RUN = Path("/opt/ems/last_run_debug.json")
MICROEMS_DB = Path("/opt/ems/e3dc_history.sqlite")
EOS_FORECAST_ARCHIVE = Path("/opt/eos/comparison/forecast_history.sqlite")
HORIZON_HOURS = 48
QUARTERS = HORIZON_HOURS * 4
QUALITY_LOOKBACK_DAYS = 14

EOS_SYSTEMS = (
    {
        "id": "eos_import",
        "name": "EOS · MicroEMS-Daten",
        "shortName": "EOS Import",
        "url": "http://127.0.0.1:8513",
        "source": "MicroEMS-Prognosen · EOS-Optimierer",
        "forecast": "Import: Last, PV, Preis, Wetter",
    },
    {
        "id": "eos_native",
        "name": "EOS · eigene Prognosen",
        "shortName": "EOS nativ",
        "url": "http://127.0.0.1:8523",
        "source": "EOS-Prognosen · EOS-Optimierer",
        "forecast": "Akkudoktor Last · Forecast.Solar · Energy-Charts",
    },
)


def number(value: Any, digits: int = 4) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return round(result, digits)


def value(values: list[Any], index: int, default: float = 0.0) -> float:
    if index >= len(values):
        return default
    parsed = number(values[index])
    return default if parsed is None else parsed


def parse_datetime(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def iso_utc(raw: str | datetime) -> str:
    dt = raw if isinstance(raw, datetime) else parse_datetime(raw)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _parse_timestamp(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _hour_key(raw: str | datetime, tz: ZoneInfo) -> str:
    stamp = raw if isinstance(raw, datetime) else _parse_timestamp(raw)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(tz).replace(minute=0, second=0, microsecond=0).isoformat()


def _midnight_window(day: date, tz: ZoneInfo) -> tuple[datetime, datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start, start + timedelta(minutes=30), start + timedelta(days=1)


def _first_local_midnight_row(
    rows: list[tuple[str, bytes]], day: date, tz: ZoneInfo
) -> tuple[str, bytes] | None:
    """Return the first 00:00-00:30 vintage independent of stored ISO offset."""
    start, window_end, _ = _midnight_window(day, tz)
    candidates: list[tuple[datetime, str, bytes]] = []
    for issue_ts, payload in rows:
        try:
            issued = _parse_timestamp(issue_ts)
        except (TypeError, ValueError):
            continue
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=timezone.utc)
        local = issued.astimezone(tz)
        if start <= local < window_end:
            candidates.append((issued, issue_ts, payload))
    if not candidates:
        return None
    _, issue_ts, payload = min(candidates, key=lambda item: item[0])
    return issue_ts, payload


def _read_actual_dispatch_day(
    database: Path, day: date, tz: ZoneInfo
) -> tuple[dict[str, dict[str, float]], int]:
    """Return hourly E3DC gross import mean power and SoC at the hour boundary."""
    start, _, end = _midnight_window(day, tz)
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        rows = connection.execute(
            "SELECT ts,grid_w,soc FROM actuals WHERE ts>=? AND ts<? ORDER BY ts",
            (start.astimezone(timezone.utc).isoformat(),
             end.astimezone(timezone.utc).isoformat()),
        ).fetchall()
        connection.close()
    except (OSError, sqlite3.Error):
        return {}, 0
    buckets: dict[str, dict[str, float]] = {}
    valid_slots = 0
    for timestamp, grid_w, soc in rows:
        stamp = _parse_timestamp(timestamp)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        local = stamp.astimezone(tz)
        key = _hour_key(local, tz)
        bucket = buckets.setdefault(
            key, {"import_sum_w": 0.0, "grid_slots": 0.0, "soc_pct": math.nan}
        )
        if grid_w is not None:
            bucket["import_sum_w"] += max(float(grid_w), 0.0)
            bucket["grid_slots"] += 1.0
            valid_slots += 1
        if soc is not None and (local.minute == 0 or math.isnan(bucket["soc_pct"])):
            bucket["soc_pct"] = float(soc)
    hourly: dict[str, dict[str, float]] = {}
    for key, bucket in buckets.items():
        if bucket["grid_slots"] < 3 or math.isnan(bucket["soc_pct"]):
            continue
        hourly[key] = {
            "grid_import_w": bucket["import_sum_w"] / bucket["grid_slots"],
            "soc_pct": bucket["soc_pct"],
        }
    return hourly, valid_slots


def _read_micro_midnight_dispatch(
    database: Path, day: date, tz: ZoneInfo
) -> tuple[str | None, dict[str, dict[str, float]]]:
    """Read MicroEMS' first local-midnight dispatch from archived debug runs."""
    start, _, day_end = _midnight_window(day, tz)
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        rows = connection.execute(
            "SELECT generated,payload FROM debug_snapshot WHERE status='Optimal'"
        ).fetchall()
        connection.close()
        row = _first_local_midnight_row(rows, day, tz)
    except (OSError, sqlite3.Error):
        row = None
    if not row:
        return None, {}
    try:
        payload = json.loads(zlib.decompress(row[1]).decode("utf-8"))
        timestamps = payload["inputs"]["index"]
        plan = payload["plan"]
        imported = plan["grid_import_w"]
        exported = plan["grid_export_w"]
        soc_values = plan["house_soc_percent"]
    except (KeyError, TypeError, ValueError, zlib.error, json.JSONDecodeError):
        return None, {}
    buckets: dict[str, dict[str, float]] = {}
    for timestamp, grid_import_w, grid_export_w in zip(timestamps, imported, exported):
        stamp = _parse_timestamp(timestamp)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        local = stamp.astimezone(tz)
        if not (start <= local < day_end):
            continue
        if grid_import_w is None and grid_export_w is None:
            continue
        key = _hour_key(local, tz)
        bucket = buckets.setdefault(
            key, {"import_sum_w": 0.0, "grid_slots": 0.0, "soc_pct": math.nan}
        )
        bucket["import_sum_w"] += float(grid_import_w or 0.0)
        bucket["grid_slots"] += 1.0
    # MicroEMS stores end-of-slot SoC under the slot start. Move it 15 minutes
    # forward before selecting the values at common hourly boundaries.
    for timestamp, soc in zip(timestamps, soc_values):
        if soc is None:
            continue
        stamp = _parse_timestamp(timestamp)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        local = (stamp + timedelta(minutes=15)).astimezone(tz)
        if start <= local < day_end and local.minute == 0:
            key = _hour_key(local, tz)
            bucket = buckets.setdefault(
                key, {"import_sum_w": 0.0, "grid_slots": 0.0, "soc_pct": math.nan}
            )
            bucket["soc_pct"] = float(soc)
    hourly = {
        key: {
            "grid_import_w": bucket["import_sum_w"] / bucket["grid_slots"],
            "soc_pct": bucket["soc_pct"],
        }
        for key, bucket in buckets.items()
        if bucket["grid_slots"] >= 3 and not math.isnan(bucket["soc_pct"])
    }
    issued = _parse_timestamp(row[0])
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    return issued.astimezone(tz).isoformat(), hourly


def _read_eos_midnight_dispatch(
    database: Path, system_id: str, day: date, tz: ZoneInfo
) -> tuple[str | None, dict[str, dict[str, float]]]:
    if not database.exists():
        return None, {}
    start, _, day_end = _midnight_window(day, tz)
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        rows = connection.execute(
            "SELECT issue_ts,payload FROM forecast_snapshots WHERE system_id=?",
            (system_id,),
        ).fetchall()
        connection.close()
        row = _first_local_midnight_row(rows, day, tz)
    except (OSError, sqlite3.Error):
        row = None
    if not row:
        return None, {}
    try:
        payload = json.loads(zlib.decompress(row[1]).decode("utf-8"))
    except (TypeError, ValueError, zlib.error, json.JSONDecodeError):
        return None, {}
    hourly: dict[str, dict[str, float]] = {}
    for timestamp, grid_import_wh, soc_pct in zip(
        payload.get("timestamps", []),
        payload.get("grid_import_wh", []),
        payload.get("soc_pct", []),
    ):
        if grid_import_wh is None or soc_pct is None:
            continue
        stamp = _parse_timestamp(timestamp)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        local = stamp.astimezone(tz)
        if not (start <= local < day_end):
            continue
        # An hourly Wh value is numerically the mean power in W for that hour;
        # EOS SoC is documented as the state at the beginning of the hour.
        hourly[_hour_key(local, tz)] = {
            "grid_import_w": float(grid_import_wh), "soc_pct": float(soc_pct)
        }
    issued = _parse_timestamp(row[0])
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    return issued.astimezone(tz).isoformat(), hourly


def _dispatch_error(
    actual: dict[str, dict[str, float]],
    forecast: dict[str, dict[str, float]],
    grid_reference_w: float,
    hours: set[str] | None = None,
) -> dict[str, float] | None:
    common = sorted((set(actual) & set(forecast)) if hours is None else hours)
    if len(common) < 22:
        return None
    grid_errors = [
        forecast[hour]["grid_import_w"] - actual[hour]["grid_import_w"]
        for hour in common
    ]
    soc_errors = [forecast[hour]["soc_pct"] - actual[hour]["soc_pct"] for hour in common]
    grid_mae_w = sum(abs(value) for value in grid_errors) / len(grid_errors)
    soc_mae_pp = sum(abs(value) for value in soc_errors) / len(soc_errors)
    score = ((grid_mae_w / max(1.0, grid_reference_w) * 100.0) + soc_mae_pp) / 2.0
    return {
        "hours": float(len(common)),
        "grid_abs_error_w": sum(abs(value) for value in grid_errors),
        "grid_error_count": float(len(grid_errors)),
        "soc_abs_error_pp": sum(abs(value) for value in soc_errors),
        "soc_error_count": float(len(soc_errors)),
        "grid_mae_kw": grid_mae_w / 1000.0,
        "grid_bias_kw": sum(grid_errors) / len(grid_errors) / 1000.0,
        "soc_mae_pp": soc_mae_pp,
        "soc_bias_pp": sum(soc_errors) / len(soc_errors),
        "distance_score_pct": score,
        "planned_import_kwh": sum(
            forecast[hour]["grid_import_w"] for hour in common
        ) / 1000.0,
        "actual_import_kwh": sum(
            actual[hour]["grid_import_w"] for hour in common
        ) / 1000.0,
    }


def build_forecast_quality(
    micro_db: Path = MICROEMS_DB,
    eos_archive_db: Path = EOS_FORECAST_ARCHIVE,
    timezone_name: str = "Europe/Berlin",
    now: datetime | None = None,
    lookback_days: int = QUALITY_LOOKBACK_DAYS,
    grid_reference_w: float = 12_000.0,
) -> dict[str, Any]:
    """Score midnight dispatch plans against measured net grid and battery SoC."""
    tz = ZoneInfo(timezone_name)
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    current = current.astimezone(tz)
    metadata = (
        ("microems", "MicroEMS"),
        ("eos_import", "EOS Import"),
        ("eos_native", "EOS nativ"),
    )
    captured_days = {system_id: 0 for system_id, _ in metadata}
    daily: list[dict[str, Any]] = []
    common_records: list[tuple[str, dict[str, dict[str, float]]]] = []
    for offset in range(1, max(1, int(lookback_days)) + 1):
        target_day = current.date() - timedelta(days=offset)
        actual, actual_slots = _read_actual_dispatch_day(micro_db, target_day, tz)
        expected_slots = round(
            (_midnight_window(target_day, tz)[2].astimezone(timezone.utc)
             - _midnight_window(target_day, tz)[0].astimezone(timezone.utc))
            .total_seconds() / 900
        )
        if actual_slots < expected_slots - 4 or len(actual) < 22:
            continue
        micro_issue, micro = _read_micro_midnight_dispatch(micro_db, target_day, tz)
        import_issue, imported = _read_eos_midnight_dispatch(
            eos_archive_db, "eos_import", target_day, tz
        )
        native_issue, native = _read_eos_midnight_dispatch(
            eos_archive_db, "eos_native", target_day, tz
        )
        forecasts = {
            "microems": (micro_issue, micro),
            "eos_import": (import_issue, imported),
            "eos_native": (native_issue, native),
        }
        joint_hours = set(actual)
        if all(values for _, values in forecasts.values()):
            for _, values in forecasts.values():
                joint_hours &= set(values)
        else:
            joint_hours = set()
        is_common_day = len(joint_hours) >= 22
        day_systems: dict[str, Any] = {}
        raw_metrics: dict[str, dict[str, float]] = {}
        for system_id, (issued_at, values) in forecasts.items():
            metrics = _dispatch_error(
                actual, values, grid_reference_w,
                hours=joint_hours if is_common_day else None,
            ) if values else None
            if metrics is None:
                day_systems[system_id] = {
                    "available": False, "issuedAt": issued_at
                }
                continue
            captured_days[system_id] += 1
            raw_metrics[system_id] = metrics
            public = {
                "available": True,
                "issuedAt": issued_at,
                "hours": int(metrics["hours"]),
                "gridImportMaeKw": number(metrics["grid_mae_kw"], 3),
                "gridImportBiasKw": number(metrics["grid_bias_kw"], 3),
                "socMaePp": number(metrics["soc_mae_pp"], 2),
                "socBiasPp": number(metrics["soc_bias_pp"], 2),
                "distanceScorePct": number(metrics["distance_score_pct"], 2),
                "plannedImportKwh": number(metrics["planned_import_kwh"], 2),
            }
            day_systems[system_id] = public
        if is_common_day and len(raw_metrics) == len(metadata):
            common_records.append((str(target_day), raw_metrics))
        daily.append({
            "day": str(target_day),
            "common": is_common_day,
            "actualImportKwh": number(sum(
                row["grid_import_w"] for row in actual.values()
            ) / 1000, 2),
            "actualSocStartPct": number(next(iter(actual.values()))["soc_pct"], 1),
            "actualSocEndPct": number(next(reversed(actual.values()))["soc_pct"], 1),
            "systems": day_systems,
        })

    systems: list[dict[str, Any]] = []
    for system_id, name in metadata:
        metrics = [record[system_id] for _, record in common_records]
        grid_abs = sum(item["grid_abs_error_w"] for item in metrics)
        grid_count = sum(item["grid_error_count"] for item in metrics)
        soc_abs = sum(item["soc_abs_error_pp"] for item in metrics)
        soc_count = sum(item["soc_error_count"] for item in metrics)
        grid_mae_kw = grid_abs / grid_count / 1000.0 if grid_count else None
        soc_mae_pp = soc_abs / soc_count if soc_count else None
        score = (
            ((grid_mae_kw * 1000.0 / max(1.0, grid_reference_w) * 100.0)
             + soc_mae_pp) / 2.0
            if grid_mae_kw is not None and soc_mae_pp is not None else None
        )
        systems.append({
            "id": system_id,
            "name": name,
            "days": len(common_records),
            "capturedDays": captured_days[system_id],
            "gridImportMaeKw": number(grid_mae_kw, 3),
            "socMaePp": number(soc_mae_pp, 2),
            "distanceScorePct": number(score, 2),
            "firstDay": common_records[-1][0] if common_records else None,
            "lastDay": common_records[0][0] if common_records else None,
        })
    available = [item for item in systems if item["distanceScorePct"] is not None]
    comparison_ready = bool(common_records) and len(available) == len(systems)
    winner_ids: list[str] = []
    if comparison_ready:
        best = min(float(item["distanceScorePct"]) for item in available)
        winner_ids = [
            item["id"] for item in available
            if abs(float(item["distanceScorePct"]) - best) <= 0.01
        ]
    return {
        "lookbackDays": int(lookback_days),
        "metric": "Dispatch distance",
        "gridReferenceKw": number(grid_reference_w / 1000.0, 2),
        "commonDays": len(common_records),
        "comparisonReady": comparison_ready,
        "winnerIds": winner_ids,
        "systems": systems,
        "days": daily,
        "note": (
            "Verglichen wird der um Mitternacht festgelegte Fahrplan mit dem "
            "später gemessenen E3DC-Netzbezug und Batterie-SOC. Die Rangfolge "
            "nutzt ausschließlich gemeinsame abgeschlossene Tage und dasselbe "
            "Stundenraster. Der Abstand mittelt SOC-MAE und den auf 12 kW "
            "normierten Bezugs-MAE; Einspeisung verrechnet einen Bezug nicht. "
            "Niedriger ist besser."
        ),
    }


def fetch_json(url: str, timeout: float = 5.0) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def load_eos(
    system: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any], bool, str | None]:
    snapshot = SNAPSHOTS / f"{system['id']}.json"
    error: str | None = None
    cached = False
    try:
        solution = fetch_json(f"{system['url']}/v1/energy-management/optimization/solution", 6)
        genetic = fetch_json(
            f"{system['url']}/v1/energy-management/optimization/solution/GENETIC", 6
        )
        health = fetch_json(f"{system['url']}/v1/health", 3)
        atomic_json(snapshot, {"solution": solution, "genetic": genetic, "health": health})
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        error = str(exc)
        if not snapshot.exists():
            raise
        stored = json.loads(snapshot.read_text(encoding="utf-8"))
        solution = stored["solution"]
        genetic = stored.get("genetic")
        health = stored.get("health", {})
        cached = True
    return solution, genetic, health, cached, error


def micro_intent(plan: dict[str, list[Any]], i: int, mode: str = "auto") -> str:
    """Describe MicroEMS' battery intent without mistaking zero flow for a hold."""
    ac = value(plan.get("batt_ac_charge_w", []), i)
    dc = value(plan.get("batt_dc_charge_w", []), i)
    discharge = value(plan.get("batt_discharge_w", []), i)
    if ac > 50:
        return "Netzladen"
    if dc > 50:
        return "PV-Laden"
    if discharge > 50:
        return "Entladen"
    charge_limited = value(plan.get("charge_limited", []), i) > 0.5
    discharge_limited = value(plan.get("discharge_limited", []), i) > 0.5
    charge_limit = value(plan.get("batt_charge_limit_w", []), i)
    discharge_limit = value(plan.get("batt_discharge_limit_w", []), i)
    if mode == "hold" or (discharge_limited and discharge_limit < 50):
        return "Entladen gesperrt"
    if mode == "block_charge" or (charge_limited and charge_limit < 50):
        return "Laden gesperrt"
    if mode == "limit_discharge" or discharge_limited:
        return "Entladen begrenzt"
    if mode == "limit_charge" or charge_limited:
        return "Laden begrenzt"
    return "Automatik"


def normalize_microems() -> dict[str, Any]:
    payload = json.loads(MICROEMS_RUN.read_text(encoding="utf-8"))
    inputs = payload["inputs"]
    plan = payload["plan"]
    modes = payload.get("plan_mode") or []
    count = min(QUARTERS, len(inputs["index"]), *(len(v) for v in plan.values()))
    points: list[dict[str, Any]] = []
    purchase = revenue = imported = exported = load = pv = losses = 0.0

    for i in range(count):
        grid_import = value(plan["grid_import_w"], i)
        grid_export = value(plan["grid_export_w"], i)
        load_w = value(plan["house_load_w"], i)
        pv_w = value(plan["pv_w"], i)
        price = value(plan["price_ct_kwh"], i)
        feedin = value(plan["feedin_ct_kwh"], i)
        imported += grid_import * 0.25 / 1000
        exported += grid_export * 0.25 / 1000
        load += load_w * 0.25 / 1000
        pv += pv_w * 0.25 / 1000
        purchase += grid_import * 0.25 / 1000 * price / 100
        revenue += grid_export * 0.25 / 1000 * feedin / 100
        battery_w = (
            value(plan.get("batt_dc_charge_w", []), i)
            + value(plan.get("batt_ac_charge_w", []), i)
            - value(plan.get("batt_discharge_w", []), i)
        )
        points.append(
            {
                "t": iso_utc(inputs["index"][i]),
                "loadKw": number(load_w / 1000, 3),
                "pvKw": number(pv_w / 1000, 3),
                "gridKw": number((grid_import - grid_export) / 1000, 3),
                "gridImportKw": number(grid_import / 1000, 3),
                "gridExportKw": number(grid_export / 1000, 3),
                "batteryKw": number(battery_w / 1000, 3),
                "socPct": number(value(plan["house_soc_percent"], i), 2),
                "priceCt": number(price, 3),
                "intent": micro_intent(
                    plan, i, mode=str(modes[i]) if i < len(modes) else "auto"
                ),
            }
        )

    slot_cost = sum(value(plan.get("slot_cost_ct", []), i) for i in range(count)) / 100
    return {
        "id": "microems",
        "name": "MicroEMS",
        "shortName": "MicroEMS",
        "source": "MicroEMS-Prognosen · MILP-Optimierer",
        "forecast": "ML/Statistik: Last, PV, Preis, Wetter",
        "optimizer": "MILP · 15-Minuten-Schritte",
        "sourceRasterMin": 15,
        "displayRasterMin": 15,
        "status": "online" if payload.get("status") == "Optimal" else "warning",
        "statusText": payload.get("status", "unbekannt"),
        "generatedAt": iso_utc(payload.get("generated", inputs["index"][0])),
        "cached": False,
        "points": points,
        "totals": {
            "costEur": number(purchase, 2),
            "revenueEur": number(revenue, 2),
            "netEur": number(slot_cost, 2),
            "importKwh": number(imported, 1),
            "exportKwh": number(exported, 1),
            "loadKwh": number(load, 1),
            "pvKwh": number(pv, 1),
            "lossKwh": number(losses, 1),
        },
    }


def eos_genes(
    row: dict[str, Any], genetic: dict[str, Any] | None, row_index: int
) -> tuple[float, float, float]:
    """Return genes used by the simulation, avoiding EOS' local/UTC serialization shift."""
    if genetic is not None:
        gene_index = int(genetic.get("start_hour", 0)) + row_index
        try:
            return (
                float(genetic["ac_charge"][gene_index]),
                float(genetic["dc_charge"][gene_index]),
                float(genetic["discharge_allowed"][gene_index]),
            )
        except (IndexError, KeyError, TypeError, ValueError):
            pass
    return (
        number(row.get("genetic_ac_charge_factor")) or 0,
        number(row.get("genetic_dc_charge_factor")) or 0,
        number(row.get("genetic_discharge_allowed_factor")) or 0,
    )


def eos_intent(
    row: dict[str, Any],
    genetic: dict[str, Any] | None = None,
    row_index: int = 0,
    grid_import_kw: float | None = None,
    grid_export_kw: float | None = None,
    battery_kw: float | None = None,
) -> str:
    ac, dc, discharge = eos_genes(row, genetic, row_index)
    if ac > 0.01:
        if grid_import_kw is None or grid_export_kw is None:
            return "AC-Ladebefehl"
        if grid_import_kw <= 0.001:
            return "AC-Ladebefehl · ohne Bezug"
        if grid_export_kw > grid_import_kw + 0.001:
            return "AC-Laden + Einspeisen"
        return "Netzladen"
    # The charge/discharge genes describe permissions and limits. Prefer the
    # simulated battery flow as the primary action; otherwise an ordinary PV
    # charge with discharge disabled is misleadingly shown as a discharge hold.
    if battery_kw is not None and battery_kw > 0.05:
        return "PV-Laden"
    if battery_kw is not None and battery_kw < -0.05:
        return "Entladen"
    if discharge > 0.5:
        return "Entladen frei"
    if dc > 0.01:
        return "Entladen gesperrt"
    return "Halten"


def normalize_eos(system: dict[str, str]) -> dict[str, Any]:
    solution, genetic, health, cached, fetch_error = load_eos(system)
    solution_rows = solution["solution"]["data"]
    prediction_rows = solution["prediction"]["data"]
    timestamps = sorted(solution_rows, key=parse_datetime)[:HORIZON_HOURS]
    points: list[dict[str, Any]] = []
    imported = exported = load = pv = costs = revenue = losses = 0.0
    previous_soc: float | None = None

    for row_index, timestamp in enumerate(timestamps):
        row = solution_rows[timestamp]
        prediction = prediction_rows.get(timestamp, {})
        soc = (number(row.get("battery1_soc_factor")) or 0) * 100
        battery_kw = 0.0 if previous_soc is None else (soc - previous_soc) / 100 * 20.6
        previous_soc = soc
        load_wh = number(row.get("load_energy_wh")) or 0
        import_wh = number(row.get("grid_consumption_energy_wh")) or 0
        export_wh = number(row.get("grid_feedin_energy_wh")) or 0
        pv_wh = number(prediction.get("pvforecast_ac_energy_wh")) or 0
        price = (number(prediction.get("elec_price_amt_kwh")) or 0) * 100
        imported += import_wh / 1000
        exported += export_wh / 1000
        load += load_wh / 1000
        pv += pv_wh / 1000
        costs += number(row.get("costs_amt")) or 0
        revenue += number(row.get("revenue_amt")) or 0
        losses += (number(row.get("losses_energy_wh")) or 0) / 1000
        base = parse_datetime(timestamp)
        intent = eos_intent(
            row,
            genetic,
            row_index,
            grid_import_kw=import_wh / 1000,
            grid_export_kw=export_wh / 1000,
            battery_kw=battery_kw,
        )
        for quarter in range(4):
            points.append(
                {
                    "t": iso_utc(base + timedelta(minutes=15 * quarter)),
                    "loadKw": number(load_wh / 1000, 3),
                    "pvKw": number(pv_wh / 1000, 3),
                    "gridKw": number((import_wh - export_wh) / 1000, 3),
                    "gridImportKw": number(import_wh / 1000, 3),
                    "gridExportKw": number(export_wh / 1000, 3),
                    "batteryKw": number(battery_kw, 3),
                    "socPct": number(soc, 2),
                    "priceCt": number(price, 3),
                    "intent": intent,
                }
            )

    version = health.get("version", "EOS")
    return {
        "id": system["id"],
        "name": system["name"],
        "shortName": system["shortName"],
        "source": system["source"],
        "forecast": system["forecast"],
        "optimizer": "EOS GENETIC · intern 60 Min, auf 15 Min gehalten",
        "sourceRasterMin": 60,
        "displayRasterMin": 15,
        "status": "cached" if cached else "online",
        "statusText": f"{version}{' · Snapshot' if cached else ''}",
        "generatedAt": solution["generated_at"],
        "cached": cached,
        "fetchError": fetch_error if cached else None,
        "points": points[:QUARTERS],
        "totals": {
            "costEur": number(costs, 2),
            "revenueEur": number(revenue, 2),
            "netEur": number(costs - revenue, 2),
            "importKwh": number(imported, 1),
            "exportKwh": number(exported, 1),
            "loadKwh": number(load, 1),
            "pvKwh": number(pv, 1),
            "lossKwh": number(losses, 1),
        },
    }


def comparisons(systems: list[dict[str, Any]]) -> dict[str, Any]:
    maps = [{point["t"]: point for point in system["points"]} for system in systems]
    common = sorted(set.intersection(*(set(mapping) for mapping in maps)))
    pairwise: list[dict[str, Any]] = []
    for left in range(len(systems)):
        for right in range(left + 1, len(systems)):
            diffs = [abs(maps[left][t]["gridKw"] - maps[right][t]["gridKw"]) for t in common]
            pairwise.append(
                {
                    "left": systems[left]["id"],
                    "right": systems[right]["id"],
                    "maeGridKw": number(sum(diffs) / len(diffs), 2) if diffs else None,
                }
            )

    agreement = 0
    divergence: list[dict[str, Any]] = []
    for timestamp in common:
        points = [mapping[timestamp] for mapping in maps]
        intents = [point["intent"] for point in points]
        if len(set(intents)) == 1:
            agreement += 1
        grid = [point["gridKw"] for point in points]
        divergence.append(
            {
                "t": timestamp,
                "spreadKw": number(max(grid) - min(grid), 2),
                "values": {systems[i]["id"]: grid[i] for i in range(len(systems))},
                "intents": {systems[i]["id"]: intents[i] for i in range(len(systems))},
            }
        )
    divergence.sort(key=lambda item: item["spreadKw"], reverse=True)
    selected: list[dict[str, Any]] = []
    for candidate in divergence:
        when = parse_datetime(candidate["t"])
        if all(abs((when - parse_datetime(item["t"])).total_seconds()) >= 3600 for item in selected):
            selected.append(candidate)
        if len(selected) == 6:
            break
    return {
        "commonPoints": len(common),
        "intentAgreementPct": number(agreement / len(common) * 100, 0) if common else None,
        "pairwise": pairwise,
        "divergence": selected,
    }


def build_payload() -> dict[str, Any]:
    systems = [normalize_microems()]
    errors: list[str] = []
    for system in EOS_SYSTEMS:
        try:
            systems.append(normalize_eos(system))
        except Exception as exc:  # keep the dashboard useful if one engine is down
            errors.append(f"{system['shortName']}: {exc}")
    try:
        quality = build_forecast_quality()
    except Exception as exc:
        quality = {
            "lookbackDays": QUALITY_LOOKBACK_DAYS,
            "metric": "Dispatch distance",
            "commonDays": 0,
            "comparisonReady": False,
            "winnerIds": [],
            "systems": [],
            "days": [],
            "note": "Die Mitternachtsprognosen konnten nicht ausgewertet werden.",
            "error": str(exc),
        }
        errors.append(f"Prognosegüte: {exc}")
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "horizonHours": HORIZON_HOURS,
        "displayRasterMin": 15,
        "systems": systems,
        "quality": quality,
        "comparison": comparisons(systems) if len(systems) > 1 else {},
        "errors": errors,
        "note": (
            "EOS optimiert in dieser Version stündlich; die Anzeige hält Stundenwerte auf "
            "dem gemeinsamen 15-Minuten-Raster. Die Netzkurve zeigt den Saldo aus Bruttobezug "
            "minus Bruttoeinspeisung. EOS kann beide Flüsse in derselben Stunde ausweisen; "
            "solche AC-Ladebefehle werden im Steuerband ausdrücklich gekennzeichnet."
        ),
    }


class Handler(SimpleHTTPRequestHandler):
    server_version = "EMSComparison/1.0"

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'",
        )
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/comparison":
            try:
                self.send_json(build_payload())
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if path == "/health":
            self.send_json({"status": "alive"})
            return
        if path == "/":
            path = "/index.html"
        target = (STATIC / path.lstrip("/")).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            path = "/index.html"
        target = (STATIC / path.lstrip("/")).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()


def main() -> None:
    host = os.environ.get("EMS_COMPARE_HOST", "0.0.0.0")
    port = int(os.environ.get("EMS_COMPARE_PORT", "8530"))
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"EMS comparison dashboard listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
