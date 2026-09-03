#!/usr/bin/env python3
"""Open-Meteo-only weather evidence pipeline for the Altay autumn monitor."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import gzip
import json
import math
import os
import re
import shutil
import ssl
import sys
import time
from pathlib import Path
from statistics import mean, median
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

try:
    import certifi
except ImportError:  # pragma: no cover - CI installs requirements.txt
    certifi = None


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "points.json"
EJINA_CONFIG_PATH = ROOT / "config" / "ejina_points.json"
LATEST_DIR = ROOT / "data" / "latest"
ARCHIVE_DIR = ROOT / "data" / "archive"
HISTORY_CACHE_DIR = ROOT / "data" / "cache" / "history"
TIMEZONE_NAME = "Asia/Shanghai"
LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
UTC = dt.timezone.utc
SCHEMA_VERSION = "1.1.0"
LEGACY_SCHEMA_VERSION = "1.0.0"
RAW_RETENTION_DAYS = 14
HRES_GRID_QA_LIMIT_KM = 14.0
HISTORY_GRID_QA_LIMIT_KM = 13.5
LONG_RANGE_GRID_QA_LIMIT_KM = 35.0

OPEN_METEO_ENDPOINTS = {
    "hres": "https://api.open-meteo.com/v1/ecmwf",
    "history": "https://archive-api.open-meteo.com/v1/archive",
    "gfs": "https://api.open-meteo.com/v1/gfs",
    "ensemble": "https://ensemble-api.open-meteo.com/v1/ensemble",
    "single_runs": "https://single-runs-api.open-meteo.com/v1/forecast",
}
ALLOWED_HOSTS = {
    "api.open-meteo.com",
    "archive-api.open-meteo.com",
    "ensemble-api.open-meteo.com",
    "single-runs-api.open-meteo.com",
}

HRES_VARIABLES = [
    "temperature_2m",
    "precipitation",
    "snowfall",
    "cloud_cover",
    "cloud_cover_low",
    "sunshine_duration",
    "wind_speed_10m",
    "wind_gusts_10m",
]
HRES_FALLBACK_SOLAR = "shortwave_radiation"
ENSEMBLE_VARIABLES = ["temperature_2m", "precipitation", "snowfall", "wind_gusts_10m"]
LONG_RANGE_MODEL_ID = "ncep_gefs05"
LONG_RANGE_MODEL = "GFS Ensemble 0.5°"
LONG_RANGE_ENSEMBLE_MEMBERS = 31
LONG_RANGE_REQUESTED_FORECAST_DAYS = 36
LONG_RANGE_LEAD_START = 16
LONG_RANGE_LEAD_END = 35
LONG_RANGE_VARIABLES = ["temperature_2m", "precipitation", "snowfall", "wind_gusts_10m"]
LONG_RANGE_ENDPOINT_DOC = "https://open-meteo.com/en/docs/ensemble-api"
LONG_RANGE_MODEL_REGISTRY_DOC = "https://github.com/open-meteo/open-meteo/blob/main/openapi/ensemble.yml"
CORE_REGION_IDS = ("baihaba", "kanas", "hemu", "keketuohai")
HISTORY_MODEL = "ECMWF IFS 9 km historical weather / analysis"
HISTORY_MODEL_PARAMETER = "ecmwf_ifs"
THRESHOLDS_C = (15.0, 10.0, 5.0, 2.0, 0.0)
DEFAULT_HISTORY_YEARS = (2025, 2026)
HISTORY_FORWARD_YEARS = (2023, 2024, 2025)
HISTORY_FORWARD_CUTOFF_MONTH_DAY = "10-06"
HISTORY_FORWARD_WINDOW_KEYS = ("d0_7", "d8_15", "d16_to_10_06")

PRECISION_POLICIES = {
    "hres": [
        {"from_lead_hours": 0, "to_lead_hours_exclusive": 90, "precision_class": "native_hourly"},
        {"from_lead_hours": 90, "to_lead_hours_exclusive": 144, "precision_class": "coarse_3h_interpolated"},
        {"from_lead_hours": 144, "to_lead_hours_exclusive": None, "precision_class": "trend_only_6h_plus"},
    ],
    "gfs": [
        {"from_lead_hours": 0, "to_lead_hours_exclusive": 120, "precision_class": "native_hourly"},
        {"from_lead_hours": 120, "to_lead_hours_exclusive": None, "precision_class": "coarse_3h_interpolated"},
    ],
    "single_runs": [
        {"from_lead_hours": 0, "to_lead_hours_exclusive": 90, "precision_class": "native_hourly"},
        {"from_lead_hours": 90, "to_lead_hours_exclusive": 144, "precision_class": "coarse_3h_interpolated"},
        {"from_lead_hours": 144, "to_lead_hours_exclusive": None, "precision_class": "trend_only_6h_plus"},
    ],
}


class OpenMeteoError(RuntimeError):
    """A request failed against an allow-listed Open-Meteo endpoint."""

    def __init__(self, reason: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.body = body


def log(message: str) -> None:
    print(message, flush=True)


def now_from_input(value: str | None = None) -> dt.datetime:
    raw = value or os.environ.get("ALTAY_MONITOR_NOW")
    if raw:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return dt.datetime.now(UTC)


def iso_utc(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_local(value: dt.datetime) -> str:
    return value.astimezone(LOCAL_TZ).isoformat(timespec="minutes")


def date_string(value: dt.date) -> str:
    return value.isoformat()


def parse_local_api_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value).replace(tzinfo=LOCAL_TZ)


def round_or_none(value: object, digits: int = 3) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    temp_path.replace(path)


def write_compact_json(path: Path, value: object) -> None:
    """Write a machine-facing artifact without pretty-print whitespace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
        handle.write("\n")
    temp_path.replace(path)


def write_gzip_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with gzip.open(temp_path, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    temp_path.replace(path)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("timezone") != TIMEZONE_NAME:
        raise ValueError(f"config timezone must be {TIMEZONE_NAME}")
    if config.get("schema_version") not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
        raise ValueError("unsupported points schema version")
    if config.get("namespace") != "ejina":
        validate_kanas_subregion_config(config)
        validate_hemu_subregion_config(config)
    return config


def load_ejina_config() -> dict:
    config = load_config(EJINA_CONFIG_PATH)
    if config.get("namespace") != "ejina":
        raise ValueError("Ejina config namespace must be ejina")
    return config


def history_years_for_config(config: dict) -> tuple[int, ...]:
    """Return the configured historical years in stable ascending order."""
    raw_years = config.get("history_years", DEFAULT_HISTORY_YEARS)
    if not isinstance(raw_years, (list, tuple)) or not raw_years:
        raise ValueError("history_years must be a non-empty list")
    try:
        years = tuple(int(year) for year in raw_years)
    except (TypeError, ValueError) as error:
        raise ValueError("history_years must contain integers") from error
    if any(year < 1900 or year > 2100 for year in years):
        raise ValueError("history_years contains an out-of-range year")
    if years != tuple(sorted(set(years))):
        raise ValueError("history_years must be unique and ascending")
    return years


def valid_coordinate(latitude: object, longitude: object) -> bool:
    return (
        isinstance(latitude, (int, float))
        and not isinstance(latitude, bool)
        and -90 <= float(latitude) <= 90
        and isinstance(longitude, (int, float))
        and not isinstance(longitude, bool)
        and -180 <= float(longitude) <= 180
    )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def active_points(config: dict) -> dict[str, dict]:
    """Return only VERIFIED points; this is the main-chain trust boundary."""
    points = {}
    for point_id, point in config["points"].items():
        if point.get("status") != "VERIFIED":
            continue
        if not valid_coordinate(point.get("latitude"), point.get("longitude")):
            raise ValueError(f"invalid VERIFIED coordinate: {point_id}")
        points[point_id] = {"id": point_id, **point}
    return points


def core_region_ids(config: dict) -> tuple[str, ...]:
    """Return configured regions with a core point, preserving config order."""
    return tuple(
        region_id
        for region_id, region in config.get("regions", {}).items()
        if region.get("core_point_id")
    )


def point_forecast_end_date(point: dict) -> dt.date | None:
    value = point.get("forecast_end_date")
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError(f"invalid forecast_end_date for {point.get('id')}: {value}") from error


def excluded_points(config: dict) -> dict[str, dict]:
    excluded = {}
    for point_id, point in config["points"].items():
        if point.get("status") != "VERIFIED":
            excluded[point_id] = {
                "name": point.get("name"),
                "region": point.get("region"),
                "status": point.get("status"),
                "usable_for_main_chain": False,
                "reason": point.get("reason") or "PROVISIONAL_POINT_EXCLUDED",
            }
    for slot_id, slot in config.get("route_slots", {}).items():
        excluded[slot_id] = {
            "name": slot.get("name"),
            "status": slot.get("status"),
            "usable_for_main_chain": False,
            "reason": slot.get("reason") or "ROUTE_NOT_VERIFIED",
        }
    return excluded


class ApiClient:
    def __init__(self, retries: int = 3, timeout_seconds: int = 45) -> None:
        self.retries = retries
        self.timeout_seconds = timeout_seconds
        self.ssl_context = ssl.create_default_context(cafile=certifi.where() if certifi else None)

    def get_json(self, endpoint: str, params: dict[str, object], label: str) -> tuple[dict, str]:
        host = endpoint.split("/", 3)[2]
        if host not in ALLOWED_HOSTS:
            raise ValueError(f"blocked non-Open-Meteo host: {host}")
        query = urlencode({key: str(value) for key, value in params.items()})
        url = f"{endpoint}?{query}"
        last_error: OpenMeteoError | None = None
        for attempt in range(1, self.retries + 1):
            try:
                request = Request(url, headers={"Accept": "application/json", "User-Agent": "altay-autumn-monitor/1.1"})
                with urlopen(request, timeout=self.timeout_seconds, context=self.ssl_context) as response:
                    body = response.read().decode("utf-8")
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise OpenMeteoError("OPEN_METEO_INVALID_JSON_OBJECT", body=body[:500])
                if payload.get("error") is True:
                    raise OpenMeteoError(str(payload.get("reason") or "OPEN_METEO_API_ERROR"), body=body[:1000])
                return payload, url
            except HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body)
                    reason = str(parsed.get("reason") or parsed.get("message") or f"HTTP_{error.code}")
                except json.JSONDecodeError:
                    reason = f"HTTP_{error.code}"
                last_error = OpenMeteoError(reason, status_code=error.code, body=body[:1000])
            except (URLError, TimeoutError, OSError, json.JSONDecodeError, OpenMeteoError) as error:
                if isinstance(error, OpenMeteoError):
                    last_error = error
                else:
                    last_error = OpenMeteoError(f"{type(error).__name__}: {error}")
            if attempt < self.retries:
                delay = 2 ** (attempt - 1)
                log(f"[{label}] REQUEST RETRY {attempt}/{self.retries - 1} IN {delay}s: {last_error.reason}")
                time.sleep(delay)
        assert last_error is not None
        raise last_error


def is_variable_error(error: OpenMeteoError) -> bool:
    text = f"{error.reason} {error.body}".lower()
    return any(
        token in text
        for token in (
            "invalid variable",
            "unknown variable",
            "not available",
            "cannot find variable",
            "invalid value for",
        )
    )


def precision_class_for_lead(lead_hours: float, module: str) -> str:
    for item in PRECISION_POLICIES[module]:
        end = item["to_lead_hours_exclusive"]
        if lead_hours >= item["from_lead_hours"] and (end is None or lead_hours < end):
            return item["precision_class"]
    return "undetermined"


def add_precision_classes(hourly: dict, module: str) -> dict:
    output = dict(hourly)
    times = hourly.get("time") or []
    if not times:
        output["precision_class"] = []
        return output
    start = parse_local_api_time(times[0])
    output["precision_class"] = [
        precision_class_for_lead((parse_local_api_time(value) - start).total_seconds() / 3600, module)
        for value in times
    ]
    return output


def daily_precision_class(classes: list[str]) -> str:
    unique = {value for value in classes if value}
    if len(unique) == 1:
        return next(iter(unique))
    if unique:
        return "mixed"
    return "undetermined"


def _values_for_indices(hourly: dict, key: str, indices: list[int]) -> list[float]:
    values = hourly.get(key) or []
    result = []
    for index in indices:
        if index >= len(values) or values[index] is None:
            continue
        result.append(float(values[index]))
    return result


def _complete_values_for_indices(hourly: dict, key: str, indices: list[int]) -> list[float]:
    values = hourly.get(key)
    if not isinstance(values, list) or any(index >= len(values) or values[index] is None for index in indices):
        return []
    return [float(values[index]) for index in indices]


def safe_mean(values: list[float]) -> float | None:
    return round(mean(values), 3) if values else None


def safe_sum(values: list[float]) -> float | None:
    return round(sum(values), 3) if values else None


def daily_metrics(hourly: dict, solar_variable: str | None = None) -> list[dict]:
    times = hourly.get("time") or []
    groups: dict[str, list[int]] = {}
    for index, value in enumerate(times):
        day = parse_local_api_time(value).date().isoformat()
        groups.setdefault(day, []).append(index)
    output = []
    for day, indices in sorted(groups.items()):
        temperatures = _values_for_indices(hourly, "temperature_2m", indices)
        night_indices = [index for index in indices if parse_local_api_time(times[index]).hour <= 6 or parse_local_api_time(times[index]).hour >= 20]
        night_temperatures = _values_for_indices(hourly, "temperature_2m", night_indices)
        precipitation = _values_for_indices(hourly, "precipitation", indices)
        snowfall = _values_for_indices(hourly, "snowfall", indices)
        cloud = _values_for_indices(hourly, "cloud_cover", indices)
        cloud_low = _values_for_indices(hourly, "cloud_cover_low", indices)
        sunshine = _values_for_indices(hourly, "sunshine_duration", indices)
        shortwave = _values_for_indices(hourly, "shortwave_radiation", indices)
        wind = _values_for_indices(hourly, "wind_speed_10m", indices)
        gust = _values_for_indices(hourly, "wind_gusts_10m", indices)
        classes = [
            (hourly.get("precision_class") or ["undetermined"] * len(times))[index]
            for index in indices
            if index < len(hourly.get("precision_class") or [])
        ]
        item = {
            "date": day,
            "complete": len(indices) == 24 and len(temperatures) == len(indices),
            "temperature_min_c": round(min(temperatures), 3) if temperatures else None,
            "temperature_max_c": round(max(temperatures), 3) if temperatures else None,
            "temperature_mean_c": safe_mean(temperatures),
            "night_min_c": round(min(night_temperatures), 3) if night_temperatures else None,
            "precipitation_mm": safe_sum(precipitation),
            "snowfall_cm": safe_sum(snowfall),
            "cloud_cover_mean_pct": safe_mean(cloud),
            "cloud_cover_low_mean_pct": safe_mean(cloud_low),
            "wind_speed_mean_kmh": safe_mean(wind),
            "wind_gust_max_kmh": round(max(gust), 3) if gust else None,
            "precision_class": daily_precision_class(classes),
        }
        if solar_variable == "sunshine_duration" and sunshine:
            item["solar_metric"] = {"variable": solar_variable, "value": round(sum(sunshine), 3), "unit": "seconds"}
        elif solar_variable == "shortwave_radiation" and shortwave:
            item["solar_metric"] = {"variable": solar_variable, "value": round(mean(shortwave), 3), "unit": "W/m² mean"}
        else:
            item["solar_metric"] = None
        output.append(item)
    return output


def trim_incomplete_hourly_rows(hourly: dict, required_variables: list[str]) -> tuple[dict, dict]:
    """Keep only a complete hourly interior; retain an audit of omitted edge rows."""
    output_hourly = copy.deepcopy(hourly)
    times = output_hourly.get("time") if isinstance(output_hourly.get("time"), list) else []
    original_count = len(times)
    leading_count = 0
    leading_variables = set()
    while output_hourly.get("time"):
        missing = [
            variable
            for variable in required_variables
            if isinstance(output_hourly.get(variable), list)
            and output_hourly[variable]
            and output_hourly[variable][0] is None
        ]
        if not missing:
            break
        leading_count += 1
        leading_variables.update(missing)
        for values in output_hourly.values():
            if isinstance(values, list):
                values.pop(0)
    trim_count = 0
    tail_variables = set()
    while output_hourly.get("time"):
        last_index = len(output_hourly["time"]) - 1
        missing = [
            variable
            for variable in required_variables
            if isinstance(output_hourly.get(variable), list)
            and last_index < len(output_hourly[variable])
            and output_hourly[variable][last_index] is None
        ]
        if not missing:
            break
        trim_count += 1
        tail_variables.update(missing)
        for values in output_hourly.values():
            if isinstance(values, list):
                values.pop()
    retained_count = len(output_hourly.get("time") or [])
    audit = {
        "original_timestep_count": original_count,
        "retained_timestep_count": retained_count,
        "leading_missing_rows": leading_count,
        "leading_missing_variables": sorted(leading_variables),
        "trailing_missing_rows": trim_count,
        "trailing_missing_variables": sorted(tail_variables),
        "horizon_status": "TRUNCATED_EDGE_MISSING" if leading_count or trim_count else "COMPLETE",
    }
    return output_hourly, audit


def trim_incomplete_edge_rows(payload: dict, required_variables: list[str]) -> tuple[dict, dict]:
    """Trim API edge rows while preserving the rest of the response metadata."""
    output = copy.deepcopy(payload)
    output["hourly"], audit = trim_incomplete_hourly_rows(output.get("hourly") or {}, required_variables)
    return output, audit


def trim_to_forecast_cutoff(payload: dict, max_date: dt.date | None) -> tuple[dict, dict]:
    """Drop forecast rows after a configured local-date cutoff and audit the drop."""
    output = copy.deepcopy(payload)
    hourly = output.get("hourly") if isinstance(output.get("hourly"), dict) else {}
    times = hourly.get("time") if isinstance(hourly.get("time"), list) else []
    if max_date is None:
        return output, {
            "forecast_cutoff_applied": False,
            "forecast_cutoff_date": None,
            "rows_dropped_after_cutoff": 0,
        }
    keep_indices = []
    invalid_timestamps = []
    for index, value in enumerate(times):
        try:
            keep = parse_local_api_time(value).date() <= max_date
        except (TypeError, ValueError):
            keep = False
            invalid_timestamps.append(index)
        if keep:
            keep_indices.append(index)
    for key, values in list(hourly.items()):
        if isinstance(values, list):
            hourly[key] = [values[index] for index in keep_indices if index < len(values)]
    output["hourly"] = hourly
    return output, {
        "forecast_cutoff_applied": True,
        "forecast_cutoff_date": max_date.isoformat(),
        "rows_before_cutoff": len(times),
        "rows_retained_through_cutoff": len(keep_indices),
        "rows_dropped_after_cutoff": len(times) - len(keep_indices),
        "invalid_timestamp_rows_dropped": invalid_timestamps,
    }


def response_meta(payload: dict) -> dict:
    return {
        "grid_coordinate": {
            "latitude": payload.get("latitude"),
            "longitude": payload.get("longitude"),
        },
        "returned_elevation": payload.get("elevation"),
        "timezone": payload.get("timezone"),
        "utc_offset_seconds": payload.get("utc_offset_seconds"),
        "generationtime_ms": payload.get("generationtime_ms"),
        "returned_model": payload.get("model"),
        "returned_model_id": payload.get("model_id"),
        "model_run_initialization": payload.get("model_run_initialization"),
    }


def validate_payload(
    payload: dict,
    point: dict,
    expected_model: str,
    required_variables: list[str],
    grid_limit_km: float,
    requested_elevation: str = "nan",
    model_run_initialization: str | None = None,
    accepted_model_values: tuple[str, ...] = (),
    accepted_model_ids: tuple[str, ...] = (),
) -> dict:
    response = response_meta(payload)
    grid = response["grid_coordinate"]
    distance = None
    distance_pass = False
    if valid_coordinate(grid.get("latitude"), grid.get("longitude")):
        distance = haversine_km(point["latitude"], point["longitude"], grid["latitude"], grid["longitude"])
        distance_pass = distance <= grid_limit_km
    hourly = payload.get("hourly") if isinstance(payload.get("hourly"), dict) else {}
    times = hourly.get("time") if isinstance(hourly.get("time"), list) else []
    missing_variables = [name for name in ["time", *required_variables] if name not in hourly]
    length_mismatch = [name for name in required_variables if name in hourly and len(hourly[name]) != len(times)]
    null_values = [
        name
        for name in required_variables
        if name in hourly and any(value is None for value in hourly[name])
    ]
    coordinate_pass = valid_coordinate(point["latitude"], point["longitude"]) and valid_coordinate(grid.get("latitude"), grid.get("longitude"))
    timezone_pass = payload.get("timezone") == TIMEZONE_NAME and payload.get("utc_offset_seconds") == 28800
    returned_model = payload.get("model")
    accepted_models = {expected_model.lower(), *(value.lower() for value in accepted_model_values)}
    model_pass = returned_model is None or str(returned_model).lower() in accepted_models
    returned_model_id = payload.get("model_id")
    model_id_pass = not returned_model_id or not accepted_model_ids or str(returned_model_id).lower() in {
        value.lower() for value in accepted_model_ids
    }
    elevation_pass = requested_elevation == "nan"
    data_pass = bool(times) and not missing_variables and not length_mismatch and not null_values
    checks = {
        "coordinate_check": "PASS" if coordinate_pass else "FAIL",
        "distance_check": "PASS" if distance_pass else "FAIL",
        "timezone_check": "PASS" if timezone_pass else "FAIL",
        "model_check": "PASS" if model_pass else "FAIL",
        "model_id_check": "PASS" if model_id_pass else "FAIL",
        "elevation_check": "PASS" if elevation_pass else "FAIL",
        "data_check": "PASS" if data_pass else "FAIL",
    }
    valid = all(value == "PASS" for value in checks.values())
    reason = None
    if not valid:
        reasons = []
        if not coordinate_pass:
            reasons.append("COORDINATE_INVALID")
        if not distance_pass:
            reasons.append("GRID_REPRESENTATIVENESS_FAIL")
        if not timezone_pass:
            reasons.append("TIMEZONE_MISMATCH")
        if not model_pass:
            reasons.append("MODEL_MISMATCH")
        if not model_id_pass:
            reasons.append("MODEL_ID_MISMATCH")
        if missing_variables:
            reasons.append("MISSING_DATA:" + ",".join(missing_variables))
        if length_mismatch:
            reasons.append("ARRAY_LENGTH_MISMATCH:" + ",".join(length_mismatch))
        if null_values:
            reasons.append("NULL_DATA:" + ",".join(null_values))
        reason = ";".join(reasons) or "INVALID"
    return {
        "valid": valid,
        "grid_distance_km": round(distance, 3) if distance is not None else None,
        "grid_distance_limit_km": grid_limit_km,
        "distance_check": checks["distance_check"],
        "timezone_check": checks["timezone_check"],
        "model_check": checks["model_check"],
        "model_id_check": checks["model_id_check"],
        "coordinate_check": checks["coordinate_check"],
        "elevation_check": checks["elevation_check"],
        "elevation_mode": "native_model_grid" if requested_elevation == "nan" else "requested_elevation",
        "requested_elevation": requested_elevation,
        "returned_elevation": response.get("returned_elevation"),
        "data_check": checks["data_check"],
        "missing_variables": missing_variables,
        "array_length_mismatch": length_mismatch,
        "null_data_variables": null_values,
        "model_run_initialization": model_run_initialization or response.get("model_run_initialization"),
        "final_status": "PASS" if valid else "INVALID",
        "reason": reason,
    }


def request_payload(
    client: ApiClient,
    *,
    endpoint: str,
    params: dict[str, object],
    variables: list[str],
    label: str,
) -> tuple[dict, str, str]:
    requested_variables = list(variables)
    solar_variable = "sunshine_duration" if "sunshine_duration" in requested_variables else None
    try:
        payload, url = client.get_json(endpoint, {**params, "hourly": ",".join(requested_variables)}, label)
        return payload, url, solar_variable or ""
    except OpenMeteoError as error:
        if solar_variable and is_variable_error(error):
            fallback = [HRES_FALLBACK_SOLAR if value == solar_variable else value for value in requested_variables]
            log(f"[{label}] SOLAR VARIABLE FALLBACK: {solar_variable} -> {HRES_FALLBACK_SOLAR}")
            payload, url = client.get_json(endpoint, {**params, "hourly": ",".join(fallback)}, label + ":SOLAR_FALLBACK")
            return payload, url, HRES_FALLBACK_SOLAR
        raise


def invalid_record(
    *,
    point: dict,
    source: str,
    endpoint: str,
    model: str,
    request_params: dict,
    reason: str,
    error: OpenMeteoError | None = None,
    model_run_initialization: str | None = None,
) -> dict:
    return {
        "point_id": point.get("id"),
        "point": {
            "name": point.get("name"),
            "region": point.get("region"),
            "status": point.get("status"),
            "latitude": point.get("latitude"),
            "longitude": point.get("longitude"),
        },
        "status": "INVALID",
        "source": source,
        "endpoint": endpoint,
        "model": model,
        "request": {
            "coordinate": {"latitude": point.get("latitude"), "longitude": point.get("longitude")},
            "parameters": request_params,
        },
        "response": None,
        "qa": {
            "valid": False,
            "final_status": "INVALID",
            "reason": reason,
            "model_run_initialization": model_run_initialization,
        },
        "error": {
            "reason": reason,
            "http_status": error.status_code if error else None,
        },
    }


def fetch_point(
    client: ApiClient,
    *,
    point: dict,
    source: str,
    endpoint: str,
    model: str,
    params: dict[str, object],
    variables: list[str],
    required_variables: list[str],
    grid_limit_km: float,
    log_label: str,
    precision_module: str | None = None,
    model_run_initialization: str | None = None,
    accepted_model_values: tuple[str, ...] = (),
    accepted_model_ids: tuple[str, ...] = (),
    max_forecast_date: dt.date | None = None,
) -> dict:
    try:
        payload, url, solar_variable = request_payload(
            client,
            endpoint=endpoint,
            params=params,
            variables=variables,
            label=log_label,
        )
    except OpenMeteoError as error:
        log(f"[{log_label}] FETCH FAILED: {error.reason}")
        return invalid_record(
            point=point,
            source=source,
            endpoint=endpoint,
            model=model,
            request_params={**params, "hourly": ",".join(variables)},
            reason="OPEN_METEO_REQUEST_FAILED:" + error.reason,
            error=error,
            model_run_initialization=model_run_initialization,
        )
    selected_required_variables = [value for value in [*required_variables, solar_variable] if value]
    payload, cutoff_audit = trim_to_forecast_cutoff(payload, max_forecast_date)
    payload, completeness = trim_incomplete_edge_rows(payload, selected_required_variables)
    completeness.update(cutoff_audit)
    qa = validate_payload(
        payload,
        point,
        expected_model=model,
        required_variables=selected_required_variables,
        grid_limit_km=grid_limit_km,
        requested_elevation=str(params.get("elevation", "")),
        model_run_initialization=model_run_initialization,
        accepted_model_values=accepted_model_values,
        accepted_model_ids=accepted_model_ids,
    )
    qa.update(completeness)
    response = response_meta(payload)
    response.update(completeness)
    response["retrieval_time"] = iso_utc(dt.datetime.now(UTC))
    response["endpoint_url"] = url
    response["model_run_initialization"] = model_run_initialization or response.get("model_run_initialization")
    record = {
        "point_id": point.get("id"),
        "point": {
            "name": point.get("name"),
            "region": point.get("region"),
            "status": point.get("status"),
            "latitude": point.get("latitude"),
            "longitude": point.get("longitude"),
        },
        "status": "PASS" if qa["valid"] else "INVALID",
        "source": source,
        "endpoint": endpoint,
        "model": model,
        "request": {
            "coordinate": {"latitude": point.get("latitude"), "longitude": point.get("longitude")},
            "parameters": {**params, "hourly": url.split("hourly=", 1)[1].split("&", 1)[0] if "hourly=" in url else ",".join(variables)},
        },
        "response": response,
        "qa": qa,
        "solar_variable": solar_variable,
    }
    if qa["valid"]:
        hourly = payload["hourly"]
        if precision_module:
            hourly = add_precision_classes(hourly, precision_module)
        record["hourly"] = hourly
        record["daily"] = daily_metrics(hourly, solar_variable)
        operation = (
            "SINGLE_RUN"
            if ":SINGLE_RUN " in log_label
            else "SPATIAL"
            if log_label.endswith(":SPATIAL")
            else log_label.split(":", 1)[1] if ":" in log_label else log_label
        )
        log(f"[{point['id']}] {operation} FETCH OK")
        log(f"[{point['id']}] GRID QA {'PASS' if qa['valid'] else 'FAIL'}")
    else:
        log(f"[{point['id']}] GRID QA FAIL: {qa.get('reason')}")
    return record


def module_status(records: list[dict], expected_count: int) -> str:
    if expected_count > 0 and len(records) == expected_count and all(record.get("status") == "PASS" for record in records):
        return "OK"
    return "FAILED"


def module_header(name: str, generated_at: str, data_date: str, status: str, **extra: object) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "module": name,
        "status": status,
        "generated_at": generated_at,
        "data_date": data_date,
        **extra,
    }


def base_weather_params(point: dict, **extra: object) -> dict[str, object]:
    return {
        "latitude": point["latitude"],
        "longitude": point["longitude"],
        "timezone": TIMEZONE_NAME,
        "cell_selection": "nearest",
        "elevation": "nan",
        **extra,
    }


def run_hres(config: dict, client: ApiClient, generated_at: str, data_date: str) -> dict:
    points = active_points(config)
    records = []
    by_id = {}
    for point_id, point in points.items():
        record = fetch_point(
            client,
            point=point,
            source="Open-Meteo",
            endpoint=OPEN_METEO_ENDPOINTS["hres"],
            model="ECMWF IFS HRES 9 km",
            params=base_weather_params(point, forecast_days=15),
            variables=HRES_VARIABLES,
            required_variables=[value for value in HRES_VARIABLES if value != "sunshine_duration"],
            grid_limit_km=HRES_GRID_QA_LIMIT_KM,
            log_label=f"{point_id}:HRES",
            precision_module="hres",
            max_forecast_date=point_forecast_end_date(point),
        )
        records.append(record)
        by_id[point_id] = record
    status = module_status(records, len(points))
    return module_header(
        "hres",
        generated_at,
        data_date,
        status,
        endpoint=OPEN_METEO_ENDPOINTS["hres"],
        model="ECMWF IFS HRES 9 km",
        native_resolution="9 km",
        precision_policy=PRECISION_POLICIES["hres"],
        interpolation_note="Open-Meteo returns an hourly series; after 90 h and 144 h it represents coarser native IFS time steps.",
        points=by_id,
        excluded_points=excluded_points(config),
        successful_points=sum(record.get("status") == "PASS" for record in records),
        failed_points=sum(record.get("status") != "PASS" for record in records),
    )


def history_date_range(
    completed_date: dt.date,
    year: int,
    start_month_day: str = "08-25",
) -> tuple[str, str] | None:
    try:
        start_month, start_day = (int(value) for value in start_month_day.split("-", 1))
        start = dt.date(year, start_month, start_day)
        current_start = dt.date(2026, start_month, start_day)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid history start month-day: {start_month_day}") from error
    if completed_date < current_start:
        return None
    try:
        end = dt.date(year, completed_date.month, completed_date.day)
    except ValueError:
        end = dt.date(year, completed_date.month, 28)
    return start.isoformat(), end.isoformat()


def history_cache_namespace(config: dict) -> str:
    """Keep historical caches separated between the Altay and Ejina namespaces."""
    return str(config.get("namespace") or "altay")


def history_cache_path(
    config: dict,
    year: int,
    point_id: str,
    cache_dir: Path | None = None,
) -> Path:
    """Return the stable cache path for one namespace/year/VERIFIED point."""
    root = Path(cache_dir) if cache_dir is not None else HISTORY_CACHE_DIR
    return root / history_cache_namespace(config) / str(year) / f"{point_id}.json"


def history_cache_relative_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def history_cache_daily(record: dict) -> list[dict]:
    """Extract daily values for the compact cache without retaining hourly arrays."""
    daily = record.get("daily")
    if isinstance(daily, list):
        return copy.deepcopy(daily)
    hourly = record.get("hourly")
    if isinstance(hourly, dict):
        return daily_metrics(hourly, record.get("solar_variable"))
    return []


def history_cache_identity(config: dict, point: dict, year: int, record: dict) -> dict:
    """Capture every input and returned-grid value that gives a cache its meaning."""
    request = record.get("request") or {}
    parameters = request.get("parameters") or {}
    response = record.get("response") or {}
    qa = record.get("qa") or {}
    grid = response.get("grid_coordinate")
    grid_key = record_grid_cell_key(record)
    return {
        "namespace": history_cache_namespace(config),
        "year": int(year),
        "point_id": point.get("id"),
        "source": record.get("source", "Open-Meteo"),
        "endpoint": record.get("endpoint", OPEN_METEO_ENDPOINTS["history"]),
        "model": record.get("model", HISTORY_MODEL),
        "model_parameter": parameters.get("models", HISTORY_MODEL_PARAMETER),
        "requested_coordinate": request.get("coordinate") or {
            "latitude": point.get("latitude"),
            "longitude": point.get("longitude"),
        },
        "returned_grid_coordinate": copy.deepcopy(grid),
        "returned_elevation": response.get("returned_elevation"),
        "grid_distance_km": qa.get("grid_distance_km"),
        "grid_distance_limit_km": qa.get("grid_distance_limit_km", HISTORY_GRID_QA_LIMIT_KM),
        "grid_cell_key": grid_key,
        "cell_selection": parameters.get("cell_selection"),
        "elevation": parameters.get("elevation"),
        "timezone": parameters.get("timezone") or response.get("timezone"),
        "utc_offset_seconds": response.get("utc_offset_seconds"),
        "solar_variable": record.get("solar_variable"),
    }


def history_cache_key(identity: dict) -> str:
    return ":".join(
        str(identity.get(key) or "UNKNOWN")
        for key in ("namespace", "year", "point_id", "grid_cell_key")
    )


def history_cache_record_metadata(point: dict, record: dict) -> dict:
    return {
        "point": {
            "name": point.get("name"),
            "region": point.get("region"),
            "status": point.get("status"),
            "latitude": point.get("latitude"),
            "longitude": point.get("longitude"),
        },
        "source": record.get("source", "Open-Meteo"),
        "endpoint": record.get("endpoint", OPEN_METEO_ENDPOINTS["history"]),
        "model": record.get("model", HISTORY_MODEL),
        "request": copy.deepcopy(record.get("request") or {}),
        "response": copy.deepcopy(record.get("response") or {}),
        "qa": copy.deepcopy(record.get("qa") or {}),
        "solar_variable": record.get("solar_variable"),
    }


def history_cache_from_record(
    config: dict,
    point: dict,
    year: int,
    record: dict,
    requested_start: str,
    requested_end: str,
    *,
    mode: str,
) -> dict:
    daily = history_cache_daily(record)
    identity = history_cache_identity(config, point, year, record)
    retrieval_time = (
        (record.get("response") or {}).get("retrieval_time")
        or iso_utc(dt.datetime.now(UTC))
    )
    return {
        "cache_schema_version": "1.0.0",
        "schema_version": SCHEMA_VERSION,
        "cache_kind": "historical_daily_weather",
        "cache_key": history_cache_key(identity),
        "namespace": history_cache_namespace(config),
        "year": int(year),
        "point_id": point.get("id"),
        "identity": identity,
        "record_metadata": history_cache_record_metadata(point, record),
        "daily": daily,
        "cached_dates": sorted({day.get("date") for day in daily if day.get("date")}),
        "date_range": {
            "start_date": min((day["date"] for day in daily if day.get("date")), default=None),
            "end_date": max((day["date"] for day in daily if day.get("date")), default=None),
        },
        "retrievals": [{
            "retrieved_at": retrieval_time,
            "requested_start_date": requested_start,
            "requested_end_date": requested_end,
            "mode": mode,
            "status": "PASS",
        }],
        "last_retrieval_time": retrieval_time,
    }


def _history_cache_identity_mismatches(
    cache: dict,
    config: dict,
    point: dict,
    year: int,
) -> list[str]:
    identity = cache.get("identity")
    if not isinstance(identity, dict):
        return ["CACHE_IDENTITY_MISSING"]
    expected_coordinate = {
        "latitude": point.get("latitude"),
        "longitude": point.get("longitude"),
    }
    expected = {
        "namespace": history_cache_namespace(config),
        "year": int(year),
        "point_id": point.get("id"),
        "source": "Open-Meteo",
        "endpoint": OPEN_METEO_ENDPOINTS["history"],
        "model": HISTORY_MODEL,
        "model_parameter": HISTORY_MODEL_PARAMETER,
        "requested_coordinate": expected_coordinate,
        "cell_selection": "nearest",
        "elevation": "nan",
        "timezone": TIMEZONE_NAME,
    }
    mismatches = []
    for key, expected_value in expected.items():
        if identity.get(key) != expected_value:
            mismatches.append(key)
    grid = identity.get("returned_grid_coordinate")
    if not valid_coordinate((grid or {}).get("latitude"), (grid or {}).get("longitude")):
        mismatches.append("returned_grid_coordinate")
    else:
        expected_grid_key = f"{float(grid['latitude']):.6f},{float(grid['longitude']):.6f}"
        if identity.get("grid_cell_key") != expected_grid_key:
            mismatches.append("grid_cell_key")
    distance = identity.get("grid_distance_km")
    limit = identity.get("grid_distance_limit_km", HISTORY_GRID_QA_LIMIT_KM)
    if (
        not isinstance(distance, (int, float))
        or isinstance(distance, bool)
        or not math.isfinite(float(distance))
        or distance < 0
        or not isinstance(limit, (int, float))
        or isinstance(limit, bool)
        or not math.isfinite(float(limit))
        or limit < 0
        or distance > limit
    ):
        mismatches.append("grid_distance_km")
    returned_elevation = identity.get("returned_elevation")
    if (
        not isinstance(returned_elevation, (int, float))
        or isinstance(returned_elevation, bool)
        or not math.isfinite(float(returned_elevation))
    ):
        mismatches.append("returned_elevation")
    if identity.get("utc_offset_seconds") != 28800:
        mismatches.append("utc_offset_seconds")
    if identity.get("solar_variable") not in {None, "sunshine_duration", "shortwave_radiation"}:
        mismatches.append("solar_variable")
    if cache.get("cache_key") != history_cache_key(identity):
        mismatches.append("cache_key")
    qa = cache.get("record_metadata", {}).get("qa") or cache.get("qa") or {}
    if qa.get("final_status") != "PASS":
        mismatches.append("qa_final_status")
    daily = cache.get("daily")
    if not isinstance(daily, list):
        mismatches.append("daily")
    else:
        seen_dates = set()
        for day in daily:
            day_date = day.get("date") if isinstance(day, dict) else None
            if not isinstance(day_date, str):
                mismatches.append("daily_date")
                continue
            try:
                dt.date.fromisoformat(day_date)
            except ValueError:
                mismatches.append("daily_date")
            else:
                if day_date[:4] != str(year):
                    mismatches.append("daily_year")
            if day_date in seen_dates:
                mismatches.append("duplicate_daily_date")
            seen_dates.add(day_date)
    return sorted(set(mismatches))


def load_history_cache(
    config: dict,
    point: dict,
    year: int,
    cache_dir: Path | None = None,
) -> tuple[dict | None, dict]:
    """Load and validate one cache file; invalid identity is never silently used."""
    path = history_cache_path(config, year, point["id"], cache_dir)
    info = {
        "path": history_cache_relative_path(path),
        "status": "MISS",
        "identity_mismatches": [],
    }
    if not path.is_file():
        return None, info
    try:
        with path.open(encoding="utf-8") as handle:
            cache = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        info.update({"status": "INVALID", "identity_mismatches": [f"CACHE_READ_FAILED:{type(error).__name__}"]})
        return None, info
    mismatches = _history_cache_identity_mismatches(cache, config, point, year)
    if mismatches:
        info.update({"status": "INVALID", "identity_mismatches": mismatches})
        return None, info
    info["status"] = "HIT"
    info["cache_key"] = cache.get("cache_key")
    info["cached_dates"] = len(cache.get("daily") or [])
    return cache, info


def _history_date_list(start_date: str, end_date: str) -> list[str]:
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    if end < start:
        return []
    return [
        (start + dt.timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]


def _history_missing_date_ranges(
    start_date: str,
    end_date: str,
    cached_days: list[dict],
) -> list[tuple[str, str]]:
    cached_dates = {
        day.get("date")
        for day in cached_days
        if isinstance(day, dict) and day.get("complete") and isinstance(day.get("date"), str)
    }
    missing = [
        value for value in _history_date_list(start_date, end_date)
        if value not in cached_dates
    ]
    ranges = []
    for value in missing:
        if not ranges:
            ranges.append([value, value])
            continue
        previous = dt.date.fromisoformat(ranges[-1][1])
        current = dt.date.fromisoformat(value)
        if current == previous + dt.timedelta(days=1):
            ranges[-1][1] = value
        else:
            ranges.append([value, value])
    return [(start, end) for start, end in ranges]


def _history_cache_merge_daily(cache: dict, new_daily: list[dict]) -> None:
    by_date = {
        day.get("date"): copy.deepcopy(day)
        for day in cache.get("daily", [])
        if isinstance(day, dict) and isinstance(day.get("date"), str)
    }
    for day in new_daily:
        if isinstance(day, dict) and isinstance(day.get("date"), str):
            by_date[day["date"]] = copy.deepcopy(day)
    cache["daily"] = [by_date[key] for key in sorted(by_date)]
    cache["cached_dates"] = sorted(by_date)
    cache["date_range"] = {
        "start_date": min(by_date) if by_date else None,
        "end_date": max(by_date) if by_date else None,
    }


def _history_cache_record(
    config: dict,
    point: dict,
    year: int,
    cache: dict,
    requested_start: str,
    requested_end: str,
    info: dict,
) -> dict:
    metadata = cache.get("record_metadata") or {}
    daily = [
        copy.deepcopy(day)
        for day in cache.get("daily", [])
        if isinstance(day, dict)
        and isinstance(day.get("date"), str)
        and requested_start <= day["date"] <= requested_end
    ]
    expected = _history_date_list(requested_start, requested_end)
    available = {day["date"] for day in daily if day.get("complete")}
    missing = [value for value in expected if value not in available]
    qa = copy.deepcopy(metadata.get("qa") or {})
    qa["cache_check"] = {
        "status": "PASS" if not missing and info.get("status") not in {"INVALID", "FAILED"} else "INVALID",
        "cache_path": info.get("path"),
        "cache_key": cache.get("cache_key"),
        "requested_start_date": requested_start,
        "requested_end_date": requested_end,
        "missing_dates": missing,
        "identity_mismatches": info.get("identity_mismatches", []),
    }
    record_status = "PASS" if not missing and info.get("status") not in {"INVALID", "FAILED"} else "INVALID"
    if record_status != "PASS":
        qa["valid"] = False
        qa["final_status"] = "INVALID"
        qa["reason"] = (
            "HISTORY_CACHE_IDENTITY_MISMATCH"
            if info.get("status") == "INVALID" and info.get("identity_mismatches")
            else "HISTORY_CACHE_MISSING_DATES"
        )
    else:
        qa.setdefault("valid", True)
        qa.setdefault("final_status", "PASS")
    response = copy.deepcopy(metadata.get("response") or {})
    request = copy.deepcopy(metadata.get("request") or {})
    return {
        "point_id": point.get("id"),
        "point": copy.deepcopy(metadata.get("point") or point),
        "status": record_status,
        "source": metadata.get("source", "Open-Meteo"),
        "endpoint": metadata.get("endpoint", OPEN_METEO_ENDPOINTS["history"]),
        "model": metadata.get("model", HISTORY_MODEL),
        "request": request,
        "response": response,
        "qa": qa,
        "solar_variable": metadata.get("solar_variable"),
        "daily": daily,
        "history_cache": {
            **copy.deepcopy(info),
            "requested_start_date": requested_start,
            "requested_end_date": requested_end,
            "cached_start_date": (cache.get("date_range") or {}).get("start_date"),
            "cached_end_date": (cache.get("date_range") or {}).get("end_date"),
            "cached_complete_dates": len(available),
            "missing_dates": missing,
        },
    }


def _mark_history_cache_record_invalid(record: dict, info: dict, reason: str) -> dict:
    result = copy.deepcopy(record)
    result["status"] = "INVALID"
    result["history_cache"] = copy.deepcopy(info)
    result.setdefault("qa", {})["valid"] = False
    result["qa"]["final_status"] = "INVALID"
    result["qa"]["reason"] = reason
    result.setdefault("error", {})["reason"] = reason
    return result


def history_cache_required_range(
    config: dict,
    point: dict,
    year: int,
    completed_date: dt.date,
    forward_anchor_date: dt.date | None = None,
) -> tuple[str, str] | None:
    """Return the union needed by history comparison and (optionally) forward paths."""
    region_config = config.get("regions", {}).get(point["region"], {})
    history_start = region_config.get(
        "history_start_month_day",
        config.get("history_start_month_day", "08-25"),
    )
    history_range = history_date_range(completed_date, year, history_start)
    ranges = [history_range] if history_range else []
    if (
        forward_anchor_date is not None
        and config.get("namespace") != "ejina"
        and year in HISTORY_FORWARD_YEARS
    ):
        forward_windows = history_forward_windows_for_year(forward_anchor_date, year)
        forward_ranges = [
            (item.get("start_date"), item.get("end_date"))
            for item in forward_windows.values()
            if item.get("status") == "OK" and item.get("start_date") and item.get("end_date")
        ]
        ranges.extend(forward_ranges)
    ranges = [(start, end) for start, end in ranges if start and end]
    if not ranges:
        return None
    return min(start for start, _ in ranges), max(end for _, end in ranges)


def history_cache_record_or_fetch(
    config: dict,
    client: ApiClient,
    point: dict,
    year: int,
    requested_start: str,
    requested_end: str,
    *,
    refresh_history: bool = False,
    cache_dir: Path | None = None,
    log_label: str,
) -> dict:
    """Read a daily cache and fetch only missing contiguous dates from Open-Meteo."""
    path = history_cache_path(config, year, point["id"], cache_dir)
    cache, load_info = load_history_cache(config, point, year, cache_dir)
    original_cache = cache
    if load_info.get("status") == "INVALID" and not refresh_history:
        info = {
            **load_info,
            "status": "INVALID",
            "requested_start_date": requested_start,
            "requested_end_date": requested_end,
        }
        return _history_cache_record(
            config,
            point,
            year,
            {"record_metadata": {}, "daily": [], "cache_key": None},
            requested_start,
            requested_end,
            info,
        )
    if refresh_history:
        ranges = [(requested_start, requested_end)]
        mode = "refresh"
    elif cache is None:
        ranges = [(requested_start, requested_end)]
        mode = "fill"
    else:
        ranges = _history_missing_date_ranges(requested_start, requested_end, cache.get("daily", []))
        mode = "fill_missing"
    info = {
        "path": history_cache_relative_path(path),
        "status": "HIT" if cache is not None and not ranges else "MISS",
        "identity_mismatches": load_info.get("identity_mismatches", []),
        "requested_start_date": requested_start,
        "requested_end_date": requested_end,
        "missing_date_count_before_fetch": sum(
            len(_history_date_list(start, end)) for start, end in ranges
        ),
        "fetched_ranges": [],
        "api_requests": 0,
        "refresh_requested": refresh_history,
    }
    if cache is not None and not ranges:
        info["status"] = "HIT"
        return _history_cache_record(config, point, year, cache, requested_start, requested_end, info)

    for fetch_start, fetch_end in ranges:
        info["api_requests"] += 1
        record = fetch_point(
            client,
            point=point,
            source="Open-Meteo",
            endpoint=OPEN_METEO_ENDPOINTS["history"],
            model=HISTORY_MODEL,
            params=base_weather_params(
                point,
                models=HISTORY_MODEL_PARAMETER,
                start_date=fetch_start,
                end_date=fetch_end,
            ),
            variables=HRES_VARIABLES,
            required_variables=[value for value in HRES_VARIABLES if value != "sunshine_duration"],
            grid_limit_km=HISTORY_GRID_QA_LIMIT_KM,
            log_label=f"{log_label} {fetch_start}/{fetch_end}",
        )
        if record.get("status") != "PASS":
            info["status"] = "FAILED"
            info["error"] = (record.get("error") or {}).get("reason", "OPEN_METEO_REQUEST_FAILED")
            if cache is not None:
                return _mark_history_cache_record_invalid(
                    _history_cache_record(config, point, year, cache, requested_start, requested_end, info),
                    info,
                    "HISTORY_CACHE_MISSING_DATES_AFTER_FETCH_FAILURE",
                )
            return _mark_history_cache_record_invalid(record, info, "OPEN_METEO_REQUEST_FAILED")
        new_daily = history_cache_daily(record)
        new_identity = history_cache_identity(config, point, year, record)
        candidate_cache = {
            "cache_key": history_cache_key(new_identity),
            "identity": new_identity,
            "record_metadata": {"qa": record.get("qa") or {}},
            "daily": new_daily,
        }
        candidate_mismatches = _history_cache_identity_mismatches(
            candidate_cache,
            config,
            point,
            year,
        )
        if candidate_mismatches:
            info["status"] = "INVALID"
            info["identity_mismatches"] = candidate_mismatches
            return _mark_history_cache_record_invalid(record, info, "HISTORY_CACHE_IDENTITY_INVALID")
        if cache is not None:
            old_identity = cache.get("identity") or {}
            identity_keys = (
                "namespace", "year", "point_id", "source", "endpoint", "model",
                "model_parameter", "requested_coordinate", "returned_grid_coordinate",
                "returned_elevation", "grid_distance_km", "grid_distance_limit_km",
                "grid_cell_key", "cell_selection", "elevation", "timezone",
                "utc_offset_seconds", "solar_variable",
            )
            mismatches = [key for key in identity_keys if old_identity.get(key) != new_identity.get(key)]
            if mismatches:
                info["status"] = "INVALID"
                info["identity_mismatches"] = mismatches
                return _mark_history_cache_record_invalid(record, info, "HISTORY_CACHE_IDENTITY_MISMATCH")
        if cache is None or refresh_history and original_cache is None:
            cache = history_cache_from_record(
                config,
                point,
                year,
                record,
                requested_start,
                requested_end,
                mode=mode,
            )
        else:
            cache.setdefault("retrievals", []).append({
                "retrieved_at": (record.get("response") or {}).get("retrieval_time") or iso_utc(dt.datetime.now(UTC)),
                "requested_start_date": fetch_start,
                "requested_end_date": fetch_end,
                "mode": mode,
                "status": "PASS",
            })
            cache["last_retrieval_time"] = cache["retrievals"][-1]["retrieved_at"]
        if cache is not None:
            _history_cache_merge_daily(cache, new_daily)
            cache["last_request"] = {
                "start_date": fetch_start,
                "end_date": fetch_end,
                "mode": mode,
            }
            write_json(path, cache)
        info["fetched_ranges"].append({"start_date": fetch_start, "end_date": fetch_end})

    info["status"] = "REFRESHED" if refresh_history else "FILLED"
    return _history_cache_record(config, point, year, cache, requested_start, requested_end, info)


def history_cache_stats() -> dict:
    return {
        "cache_hits": 0,
        "cache_misses": 0,
        "cache_fills": 0,
        "cache_refreshes": 0,
        "cache_invalid": 0,
        "cache_failed": 0,
        "api_requests": 0,
        "missing_dates_requested": 0,
    }


def update_history_cache_stats(stats: dict, record: dict) -> None:
    info = record.get("history_cache") or {}
    status = info.get("status")
    if status == "HIT":
        stats["cache_hits"] += 1
    elif status == "MISS":
        stats["cache_misses"] += 1
    elif status == "FILLED":
        stats["cache_fills"] += 1
    elif status == "REFRESHED":
        stats["cache_refreshes"] += 1
    elif status == "INVALID":
        stats["cache_invalid"] += 1
    elif status == "FAILED":
        stats["cache_failed"] += 1
    stats["api_requests"] += int(info.get("api_requests", 0) or 0)
    stats["missing_dates_requested"] += int(info.get("missing_date_count_before_fetch", 0) or 0)


def run_history(
    config: dict,
    client: ApiClient,
    generated_at: str,
    data_date: str,
    completed_date: dt.date,
    *,
    refresh_history: bool = False,
    forward_anchor_date: dt.date | None = None,
    cache_dir: Path | None = None,
) -> dict:
    points = active_points(config)
    configured_years = history_years_for_config(config)
    point_results: dict[str, dict] = {}
    all_records = []
    cache_stats = history_cache_stats()
    for point_id, point in points.items():
        region_config = config.get("regions", {}).get(point["region"], {})
        history_start = region_config.get(
            "history_start_month_day",
            config.get("history_start_month_day", "08-25"),
        )
        years: dict[str, dict] = {}
        for year in configured_years:
            date_range = history_date_range(completed_date, year, history_start)
            cache_range = history_cache_required_range(
                config,
                point,
                year,
                completed_date,
                forward_anchor_date,
            )
            if date_range is None:
                if cache_range is None:
                    record = invalid_record(
                        point=point,
                        source="Open-Meteo",
                        endpoint=OPEN_METEO_ENDPOINTS["history"],
                        model=HISTORY_MODEL,
                        request_params={"models": HISTORY_MODEL_PARAMETER, "year": year},
                        reason="HISTORY_NOT_STARTED",
                    )
                else:
                    record = history_cache_record_or_fetch(
                        config,
                        client,
                        point,
                        year,
                        cache_range[0],
                        cache_range[1],
                        refresh_history=refresh_history,
                        cache_dir=cache_dir,
                        log_label=f"{point_id}:HISTORY {year}",
                    )
                    record["status"] = "INVALID"
                    record.setdefault("qa", {})["valid"] = False
                    record["qa"]["final_status"] = "INVALID"
                    record["qa"]["reason"] = "HISTORY_NOT_STARTED"
                    record["daily"] = []
            else:
                start_date, end_date = date_range
                cache_range = cache_range or (start_date, end_date)
                record = history_cache_record_or_fetch(
                    config,
                    client,
                    point,
                    year,
                    cache_range[0],
                    cache_range[1],
                    refresh_history=refresh_history,
                    cache_dir=cache_dir,
                    log_label=f"{point_id}:HISTORY {year}",
                )
                if record.get("status") == "PASS":
                    log(f"[{point_id}] HISTORY {year} OK")
                if record.get("daily") and date_range != cache_range:
                    logical_start, logical_end = date_range
                    record["daily"] = [
                        day for day in record["daily"]
                        if logical_start <= day.get("date", "") <= logical_end
                    ]
                    record.setdefault("history_cache", {})["logical_requested_range"] = {
                        "start_date": logical_start,
                        "end_date": logical_end,
                    }
            years[str(year)] = record
            all_records.append(record)
            update_history_cache_stats(cache_stats, record)
        point_results[point_id] = {
            "point_id": point_id,
            "point": {"name": point["name"], "region": point["region"], "status": point["status"]},
            "history_start_month_day": history_start,
            "years": years,
        }
    comparison = build_history_comparison(config, point_results, data_date)
    status = "OK" if all(record.get("status") == "PASS" for record in all_records) and all_records else "FAILED"
    return module_header(
        "history",
        generated_at,
        data_date,
        status,
        endpoint=OPEN_METEO_ENDPOINTS["history"],
        model=HISTORY_MODEL,
        model_parameter=HISTORY_MODEL_PARAMETER,
        history_years=list(configured_years),
        period_start=" / ".join(
            f"{year}-{config.get('history_start_month_day', '08-25')}"
            for year in configured_years
        ),
        period_end=data_date,
        note="Historical IFS is reanalysis/analysis, not station observation.",
        points=point_results,
        region_summaries=comparison,
        excluded_points=excluded_points(config),
        successful_fetches=sum(record.get("status") == "PASS" for record in all_records),
        failed_fetches=sum(record.get("status") != "PASS" for record in all_records),
        history_cache={
            "enabled": True,
            "directory": history_cache_relative_path(Path(cache_dir) if cache_dir is not None else HISTORY_CACHE_DIR),
            "refresh_requested": refresh_history,
            **cache_stats,
        },
    )


def history_forward_window_definitions(
    forecast_date: dt.date,
    cutoff_month_day: str = HISTORY_FORWARD_CUTOFF_MONTH_DAY,
) -> list[dict]:
    """Build rolling same-calendar-date windows, clipped at the hard cutoff."""
    try:
        cutoff_month, cutoff_day = (int(value) for value in cutoff_month_day.split("-", 1))
        cutoff_date = dt.date(forecast_date.year, cutoff_month, cutoff_day)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid history forward cutoff month-day: {cutoff_month_day}") from error
    raw_windows = (
        ("d0_7", 0, 7),
        ("d8_15", 8, 15),
        ("d16_to_10_06", 16, None),
    )
    definitions = []
    for key, start_offset, end_offset in raw_windows:
        requested_start = forecast_date + dt.timedelta(days=start_offset)
        requested_end = cutoff_date if end_offset is None else forecast_date + dt.timedelta(days=end_offset)
        if requested_start > cutoff_date:
            definitions.append({
                "window": key,
                "offset_start_days": start_offset,
                "offset_end_days": end_offset,
                "requested_start_date": None,
                "requested_end_date": None,
                "start_date": None,
                "end_date": None,
                "cutoff_date": cutoff_date.isoformat(),
                "status": "UNAVAILABLE",
                "reason": "WINDOW_AFTER_CUTOFF",
            })
            continue
        definitions.append({
            "window": key,
            "offset_start_days": start_offset,
            "offset_end_days": end_offset,
            "requested_start_date": requested_start.isoformat(),
            "requested_end_date": min(requested_end, cutoff_date).isoformat(),
            "start_date": requested_start.isoformat(),
            "end_date": min(requested_end, cutoff_date).isoformat(),
            "cutoff_date": cutoff_date.isoformat(),
            "status": "OK",
            "reason": None,
        })
    return definitions


def history_forward_windows_for_year(forecast_date: dt.date, year: int) -> dict[str, dict]:
    """Translate the current calendar windows to one historical calendar year."""
    windows = {}
    for definition in history_forward_window_definitions(forecast_date):
        translated = copy.deepcopy(definition)
        for field in ("requested_start_date", "requested_end_date", "start_date", "end_date", "cutoff_date"):
            value = translated.get(field)
            if value:
                source_date = dt.date.fromisoformat(value)
                translated[field] = dt.date(year, source_date.month, source_date.day).isoformat()
        windows[translated["window"]] = translated
    return windows


def _history_forward_window_unavailable(definition: dict, reason: str) -> dict:
    return {
        "status": "UNAVAILABLE",
        "usable_for_cross_year_comparison": False,
        "start_date": definition.get("start_date"),
        "end_date": definition.get("end_date"),
        "expected_days": 0,
        "days_available": 0,
        "missing_dates": [],
        "incomplete_dates": [],
        "daily": [],
        "metrics": None,
        "reason": reason,
    }


def history_forward_window_summary(days: list[dict], definition: dict) -> dict:
    """Summarize one historical window without inferring missing days."""
    if definition.get("status") != "OK" or not definition.get("start_date") or not definition.get("end_date"):
        return _history_forward_window_unavailable(definition, definition.get("reason") or "WINDOW_UNAVAILABLE")
    start_date = dt.date.fromisoformat(definition["start_date"])
    end_date = dt.date.fromisoformat(definition["end_date"])
    expected_dates = []
    cursor = start_date
    while cursor <= end_date:
        expected_dates.append(cursor.isoformat())
        cursor += dt.timedelta(days=1)
    selected = [
        day for day in days
        if isinstance(day.get("date"), str)
        and start_date <= dt.date.fromisoformat(day["date"]) <= end_date
    ]
    available_dates = {day["date"] for day in selected}
    incomplete_dates = sorted(day["date"] for day in selected if not day.get("complete"))
    missing_dates = [value for value in expected_dates if value not in available_dates]
    complete_days = [day for day in selected if day.get("complete")]
    complete = not missing_dates and not incomplete_dates and len(complete_days) == len(expected_dates)
    metrics = period_metrics(selected)
    daily_means = [metric_value(day, "temperature_mean_c") for day in complete_days]
    daily_means = [value for value in daily_means if value is not None]
    daily_mins = [metric_value(day, "temperature_min_c") for day in complete_days]
    daily_mins = [value for value in daily_mins if value is not None]
    daily_maxs = [metric_value(day, "temperature_max_c") for day in complete_days]
    daily_maxs = [value for value in daily_maxs if value is not None]
    solar_values = [
        float(day["solar_metric"]["value"])
        for day in complete_days
        if isinstance(day.get("solar_metric"), dict)
        and isinstance(day["solar_metric"].get("value"), (int, float))
    ]
    solar_variable = next(
        (
            day["solar_metric"].get("variable")
            for day in complete_days
            if isinstance(day.get("solar_metric"), dict) and day["solar_metric"].get("variable")
        ),
        None,
    )
    solar_unit = next(
        (
            day["solar_metric"].get("unit")
            for day in complete_days
            if isinstance(day.get("solar_metric"), dict) and day["solar_metric"].get("unit")
        ),
        None,
    )
    first_three = daily_means[:3] if len(daily_means) >= 6 else []
    last_three = daily_means[-3:] if len(daily_means) >= 6 else []
    first_mean = safe_mean(first_three)
    last_mean = safe_mean(last_three)
    trend_delta = round(last_mean - first_mean, 3) if first_mean is not None and last_mean is not None else None
    if trend_delta is None:
        trend_direction = "UNDETERMINED"
    elif trend_delta >= 0.5:
        trend_direction = "WARMING"
    elif trend_delta <= -0.5:
        trend_direction = "COOLING"
    else:
        trend_direction = "NEAR_FLAT"
    metrics.update({
        "average_temperature_c": safe_mean(daily_means),
        "minimum_temperature_c": round(min(daily_mins), 3) if daily_mins else None,
        "maximum_temperature_c": round(max(daily_maxs), 3) if daily_maxs else None,
        "total_precipitation_mm": metrics.get("precipitation_mm"),
        "total_snowfall_cm": metrics.get("snowfall_cm"),
        "average_daily_solar_value": safe_mean(solar_values),
        "average_daily_solar_variable": solar_variable,
        "average_daily_solar_unit": solar_unit,
        "average_daily_sunshine_duration_seconds": (
            safe_mean(solar_values) if solar_variable == "sunshine_duration" else None
        ),
        "maximum_wind_gust_kmh": metrics.get("wind_gust_max_kmh"),
        "temperature_trend": {
            "first_3_days_mean_temperature_c": first_mean,
            "last_3_days_mean_temperature_c": last_mean,
            "last_3_minus_first_3_mean_temperature_c": trend_delta,
            "direction": trend_direction,
            "method": "last_3_complete_daily_means_minus_first_3_complete_daily_means; threshold=0.5C",
        },
    })
    return {
        "status": "OK" if complete else "INVALID",
        "usable_for_cross_year_comparison": complete,
        "start_date": definition["start_date"],
        "end_date": definition["end_date"],
        "expected_days": len(expected_dates),
        "days_available": len(complete_days),
        "missing_dates": missing_dates,
        "incomplete_dates": incomplete_dates,
        "daily": selected,
        "metrics": metrics,
        "reason": None if complete else "HISTORY_FORWARD_WINDOW_INCOMPLETE",
    }


def history_forward_same_grid_qa(years: dict[str, dict]) -> dict:
    """Apply the existing historical grid rule to the three forward-reference years."""
    result = historical_same_grid_qa(years, HISTORY_FORWARD_YEARS)
    year_qa = {
        str(year): {
            "record_status": (years.get(str(year)) or {}).get("status", "INVALID"),
            "final_status": ((years.get(str(year)) or {}).get("qa") or {}).get("final_status", "INVALID"),
            "grid_distance_km": ((years.get(str(year)) or {}).get("qa") or {}).get("grid_distance_km"),
            "grid_distance_limit_km": ((years.get(str(year)) or {}).get("qa") or {}).get(
                "grid_distance_limit_km", HISTORY_GRID_QA_LIMIT_KM
            ),
            "distance_check": (
                "PASS"
                if isinstance(((years.get(str(year)) or {}).get("qa") or {}).get("grid_distance_km"), (int, float))
                and ((years.get(str(year)) or {}).get("qa") or {}).get("grid_distance_km") <= HISTORY_GRID_QA_LIMIT_KM
                else "FAIL"
            ),
        }
        for year in HISTORY_FORWARD_YEARS
    }
    result["grid_distance_limit_km"] = HISTORY_GRID_QA_LIMIT_KM
    result["year_qa"] = year_qa
    failed_years = [
        year for year, item in year_qa.items()
        if item["record_status"] != "PASS" or item["final_status"] != "PASS" or item["distance_check"] != "PASS"
    ]
    if failed_years:
        result["status"] = "FAIL"
        result["final_status"] = "FAILED"
        result["reason"] = "HISTORY_FORWARD_YEAR_QA_FAILED:" + ",".join(failed_years)
    result["cross_year_comparison_usable"] = result["final_status"] == "PASS"
    return result


KANAS_SUBREGION_KEYS = ("sanwan", "lake", "guanyutai")
HEMU_SUBREGION_KEYS = ("valley", "backhill")
SUBREGION_KEYS_BY_REGION = {
    "kanas": KANAS_SUBREGION_KEYS,
    "hemu": HEMU_SUBREGION_KEYS,
}
SUBREGION_CONFIG_KEYS = {
    "kanas": "kanas_subregions",
    "hemu": "hemu_subregions",
}
LIGHTWEIGHT_WINDOW_METRIC_KEYS = (
    "temperature_mean_c",
    "temperature_max_mean_c",
    "night_min_mean_c",
    "absolute_min_night_c",
    "nights_below_15c",
    "nights_below_10c",
    "nights_below_5c",
    "nights_below_2c",
    "nights_below_0c",
    "diurnal_temperature_range_mean_c",
    "precipitation_total_mm",
    "snowfall_total_cm",
    "average_daily_sunshine_duration_seconds",
    "average_daily_shortwave_radiation_w_m2",
    "max_wind_gust_kmh",
    "strong_wind_day_count",
)


def validate_subregion_config(
    config: dict,
    region_id: str,
    subregion_keys: tuple[str, ...],
    config_key: str,
) -> None:
    configured = config.get(config_key)
    if not isinstance(configured, dict):
        return
    seen = set()
    for subregion_id in subregion_keys:
        item = configured.get(subregion_id)
        if not isinstance(item, dict) or not isinstance(item.get("point_ids"), list) or not item.get("point_ids"):
            raise ValueError(f"{region_id} subregion registry missing point_ids: {subregion_id}")
        for point_id in item["point_ids"]:
            if point_id in seen:
                raise ValueError(f"{region_id} point appears in multiple subregions: {point_id}")
            seen.add(point_id)
            point = config.get("points", {}).get(point_id)
            if not isinstance(point, dict) or point.get("region") != region_id:
                raise ValueError(f"{region_id} subregion point is not a {region_id} point: {point_id}")
            if point.get("subregion") != subregion_id:
                raise ValueError(f"{region_id} point subregion mismatch: {point_id}")


def validate_kanas_subregion_config(config: dict) -> None:
    validate_subregion_config(config, "kanas", KANAS_SUBREGION_KEYS, "kanas_subregions")


def validate_hemu_subregion_config(config: dict) -> None:
    validate_subregion_config(config, "hemu", HEMU_SUBREGION_KEYS, "hemu_subregions")


def region_subregion_registry(config: dict, region_id: str) -> dict[str, dict]:
    """Return the explicit spatial registry for a registered region."""
    subregion_keys = SUBREGION_KEYS_BY_REGION.get(region_id, ())
    config_key = SUBREGION_CONFIG_KEYS.get(region_id)
    configured = config.get(config_key) if config_key else None
    if not isinstance(configured, dict) or not configured:
        if region_id == "kanas":
            return {
                "sanwan": {
                    "name": "三湾河谷",
                    "point_ids": ["K1", "K2", "K3"],
                    "minimum_verified_unique_grids": 1,
                }
            }
        return {}
    result = {}
    for subregion_id in subregion_keys:
        item = configured.get(subregion_id)
        if not isinstance(item, dict):
            continue
        point_ids = item.get("point_ids")
        if not isinstance(point_ids, list):
            continue
        result[subregion_id] = {
            "name": item.get("name", subregion_id),
            "point_ids": list(dict.fromkeys(str(point_id) for point_id in point_ids)),
            "minimum_verified_unique_grids": max(1, int(item.get("minimum_verified_unique_grids", 1))),
        }
    return result


def kanas_subregion_registry(config: dict) -> dict[str, dict]:
    """Return the explicit Kanas subregion registry with a safe v1 fallback."""
    return region_subregion_registry(config, "kanas")


def hemu_subregion_registry(config: dict) -> dict[str, dict]:
    return region_subregion_registry(config, "hemu")


def region_subregion_point_ids(
    config: dict,
    region_id: str,
    subregion_id: str,
    *,
    verified_only: bool = False,
) -> list[str]:
    registry = region_subregion_registry(config, region_id)
    item = registry.get(subregion_id) or {}
    result = []
    for point_id in item.get("point_ids", []):
        point = config.get("points", {}).get(point_id)
        if not isinstance(point, dict) or point.get("region") != region_id:
            continue
        if verified_only and point.get("status") != "VERIFIED":
            continue
        result.append(point_id)
    return result


def kanas_subregion_point_ids(
    config: dict,
    subregion_id: str,
    *,
    verified_only: bool = False,
) -> list[str]:
    return region_subregion_point_ids(config, "kanas", subregion_id, verified_only=verified_only)


def hemu_subregion_point_ids(
    config: dict,
    subregion_id: str,
    *,
    verified_only: bool = False,
) -> list[str]:
    return region_subregion_point_ids(config, "hemu", subregion_id, verified_only=verified_only)


def history_forward_point_ids(config: dict) -> list[str]:
    """Return only points that the Altay forward-history module is allowed to query."""
    point_ids = []
    points = active_points(config)
    for region_id, region_config in config.get("regions", {}).items():
        if region_id in SUBREGION_KEYS_BY_REGION:
            candidates = [
                point_id
                for subregion_id in SUBREGION_KEYS_BY_REGION[region_id]
                for point_id in region_subregion_point_ids(
                    config,
                    region_id,
                    subregion_id,
                    verified_only=True,
                )
            ]
        else:
            candidates = [region_config.get("core_point_id")]
        for point_id in candidates:
            if point_id in points and point_id not in point_ids:
                point_ids.append(point_id)
    return point_ids


def point_grid_mapping(record: dict, *, point_id: str | None = None, year: int | None = None) -> dict:
    response = record.get("response") or {}
    request = record.get("request") or {}
    qa = record.get("qa") or {}
    mapping = {
        "point_id": point_id or record.get("point_id"),
        "year": year,
        "requested_coordinate": request.get("coordinate"),
        "returned_grid_coordinate": response.get("grid_coordinate"),
        "returned_elevation": response.get("returned_elevation"),
        "grid_distance_km": qa.get("grid_distance_km"),
        "grid_distance_limit_km": qa.get("grid_distance_limit_km"),
        "grid_cell_key": record_grid_cell_key(record),
        "timezone": response.get("timezone"),
        "utc_offset_seconds": response.get("utc_offset_seconds"),
        "source": record.get("source"),
        "endpoint": record.get("endpoint"),
        "model": record.get("model"),
        "status": record.get("status", "INVALID"),
        "qa_final_status": qa.get("final_status", "INVALID"),
    }
    return mapping


def deduplicate_grid_records(records: list[dict]) -> list[dict]:
    """Keep one representative record per returned model grid and retain its mapping."""
    cells: dict[str, dict] = {}
    for record in records:
        if record.get("status") != "PASS" or not record_grid_cell_key(record):
            continue
        mapping = point_grid_mapping(record)
        cell_key = mapping["grid_cell_key"]
        entry = cells.setdefault(
            cell_key,
            {
                "grid_cell_id": cell_key,
                "returned_grid_coordinate": mapping["returned_grid_coordinate"],
                "returned_elevation": mapping["returned_elevation"],
                "representative_point_id": record.get("point_id"),
                "point_ids": [],
                "mappings": [],
                "record": record,
            },
        )
        point_id = record.get("point_id")
        if point_id and point_id not in entry["point_ids"]:
            entry["point_ids"].append(point_id)
        entry["mappings"].append(mapping)
    return list(cells.values())


def grid_sampling_summary(
    config: dict,
    point_ids: list[str],
    point_records: dict[str, dict],
    *,
    minimum_verified_unique_grids: int = 1,
) -> dict:
    """Describe requested points and unique returned grids without weighting duplicates."""
    mappings = []
    valid_records = []
    verified_point_ids = []
    for point_id in point_ids:
        point = config.get("points", {}).get(point_id) or {}
        if point.get("status") == "VERIFIED":
            verified_point_ids.append(point_id)
        record = point_records.get(point_id)
        if record is None:
            mappings.append({
                "point_id": point_id,
                "requested_coordinate": {"latitude": point.get("latitude"), "longitude": point.get("longitude")},
                "status": "EXCLUDED",
                "usable_for_main_chain": False,
                "reason": point.get("reason") or "PROVISIONAL_POINT_EXCLUDED",
            })
            continue
        mapping = point_grid_mapping(record, point_id=point_id)
        mappings.append(mapping)
        if record.get("status") == "PASS" and mapping.get("grid_cell_key") and mapping.get("qa_final_status") in {"PASS", None}:
            valid_records.append(record)
    unique_entries = deduplicate_grid_records(valid_records)
    valid_point_ids = [record.get("point_id") for record in valid_records if record.get("point_id")]
    if not valid_records:
        status = "INVALID"
        reason = "NO_VALID_VERIFIED_GRID"
    elif len(unique_entries) < max(1, minimum_verified_unique_grids):
        status = "PARTIAL"
        reason = "INSUFFICIENT_VERIFIED_UNIQUE_GRIDS"
    else:
        status = "OK"
        reason = None
    return {
        "requested_points": len(point_ids),
        "verified_points": len(verified_point_ids),
        "queried_points": len(point_records),
        "valid_points": len(valid_point_ids),
        "failed_points": max(0, len(point_records) - len(valid_point_ids)),
        "excluded_point_ids": [point_id for point_id in point_ids if point_id not in point_records],
        "valid_point_ids": valid_point_ids,
        "unique_model_grids": len(unique_entries),
        "grid_coordinates": [entry["returned_grid_coordinate"] for entry in unique_entries],
        "grid_cell_ids": [entry["grid_cell_id"] for entry in unique_entries],
        "point_to_grid": mappings,
        "unique_grid_mappings": [
            {
                "grid_cell_id": entry["grid_cell_id"],
                "returned_grid_coordinate": entry["returned_grid_coordinate"],
                "returned_elevation": entry["returned_elevation"],
                "point_ids": entry["point_ids"],
            }
            for entry in unique_entries
        ],
        "minimum_verified_unique_grids": max(1, minimum_verified_unique_grids),
        "status": status,
        "reason": reason,
        "deduplication": "returned_grid_coordinate; one independent sample per returned model grid",
    }


def _window_expected_dates(definition: dict) -> list[str]:
    if definition.get("status") != "OK" or not definition.get("start_date") or not definition.get("end_date"):
        return []
    start = dt.date.fromisoformat(definition["start_date"])
    end = dt.date.fromisoformat(definition["end_date"])
    return [
        (start + dt.timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]


def lightweight_window_metrics(days: list[dict]) -> dict:
    complete_days = sorted(
        [day for day in days if day.get("complete") and day.get("date")],
        key=lambda day: day["date"],
    )
    values = {
        key: [metric_value(day, source_key) for day in complete_days]
        for key, source_key in (
            ("temperature_mean_c", "temperature_mean_c"),
            ("temperature_max_mean_c", "temperature_max_c"),
            ("night_min_mean_c", "night_min_c"),
            ("precipitation_total_mm", "precipitation_mm"),
            ("snowfall_total_cm", "snowfall_cm"),
            ("wind_gust_max_kmh", "wind_gust_max_kmh"),
        )
    }
    for key in values:
        values[key] = [value for value in values[key] if value is not None]
    night_values = values["night_min_mean_c"]
    solar_values = []
    solar_variables = set()
    solar_units = set()
    for day in complete_days:
        solar = day.get("solar_metric")
        if isinstance(solar, dict) and isinstance(solar.get("value"), (int, float)):
            solar_values.append(float(solar["value"]))
            if solar.get("variable"):
                solar_variables.add(solar["variable"])
            if solar.get("unit"):
                solar_units.add(solar["unit"])
    diurnal_values = []
    for day in complete_days:
        high = metric_value(day, "temperature_max_c")
        low = metric_value(day, "temperature_min_c")
        if high is not None and low is not None:
            diurnal_values.append(high - low)
    first_three = [metric_value(day, "temperature_mean_c") for day in complete_days[:3]]
    last_three = [metric_value(day, "temperature_mean_c") for day in complete_days[-3:]]
    first_three = [value for value in first_three if value is not None]
    last_three = [value for value in last_three if value is not None]
    first_mean = safe_mean(first_three)
    last_mean = safe_mean(last_three)
    trend_delta = round(last_mean - first_mean, 3) if first_mean is not None and last_mean is not None else None
    if trend_delta is None:
        trend_direction = "UNDETERMINED"
    elif trend_delta >= 0.5:
        trend_direction = "WARMING"
    elif trend_delta <= -0.5:
        trend_direction = "COOLING"
    else:
        trend_direction = "NEAR_FLAT"
    solar_variable = next(iter(solar_variables), None) if len(solar_variables) == 1 else "mixed" if solar_variables else None
    solar_unit = next(iter(solar_units), None) if len(solar_units) == 1 else "mixed" if solar_units else None
    return {
        "temperature_mean_c": safe_mean(values["temperature_mean_c"]),
        "temperature_max_mean_c": safe_mean(values["temperature_max_mean_c"]),
        "night_min_mean_c": safe_mean(night_values),
        "absolute_min_night_c": round(min(night_values), 3) if night_values else None,
        "nights_below_15c": sum(value < 15 for value in night_values),
        "nights_below_10c": sum(value < 10 for value in night_values),
        "nights_below_5c": sum(value < 5 for value in night_values),
        "nights_below_2c": sum(value < 2 for value in night_values),
        "nights_below_0c": sum(value < 0 for value in night_values),
        "diurnal_temperature_range_mean_c": safe_mean(diurnal_values),
        "precipitation_total_mm": safe_sum(values["precipitation_total_mm"]),
        "snowfall_total_cm": safe_sum(values["snowfall_total_cm"]),
        "average_daily_sunshine_duration_seconds": (
            safe_mean(solar_values) if solar_variable == "sunshine_duration" else None
        ),
        "average_daily_shortwave_radiation_w_m2": (
            safe_mean(solar_values) if solar_variable == "shortwave_radiation" else None
        ),
        "solar_variable": solar_variable,
        "solar_unit": solar_unit,
        "max_wind_gust_kmh": round(max(values["wind_gust_max_kmh"]), 3) if values["wind_gust_max_kmh"] else None,
        "strong_wind_day_count": sum(value >= 50 for value in values["wind_gust_max_kmh"]),
        "temperature_trend": {
            "first_3_days_mean_temperature_c": first_mean,
            "last_3_days_mean_temperature_c": last_mean,
            "last_3_minus_first_3_mean_temperature_c": trend_delta,
            "direction": trend_direction,
            "method": "last_3_complete_daily_means_minus_first_3_complete_daily_means; threshold=0.5C",
        },
    }


def lightweight_window_summary(days: list[dict], definition: dict, *, allow_partial: bool = False) -> dict:
    expected_dates = _window_expected_dates(definition)
    if not expected_dates:
        return {
            "status": "UNAVAILABLE",
            "start_date": definition.get("start_date"),
            "end_date": definition.get("end_date"),
            "expected_days": 0,
            "days_available": 0,
            "missing_dates": [],
            "metrics": None,
            "reason": definition.get("reason") or "WINDOW_UNAVAILABLE",
        }
    start_date = expected_dates[0]
    end_date = expected_dates[-1]
    selected = [
        day for day in days
        if isinstance(day.get("date"), str) and start_date <= day["date"] <= end_date
    ]
    available_complete_dates = {day["date"] for day in selected if day.get("complete")}
    missing_dates = [date_value for date_value in expected_dates if date_value not in available_complete_dates]
    incomplete_dates = sorted(
        day["date"] for day in selected
        if day.get("date") and not day.get("complete")
    )
    complete = not missing_dates and not incomplete_dates
    if complete:
        status = "OK"
    elif available_complete_dates and allow_partial:
        status = "PARTIAL"
    else:
        status = "INVALID"
    return {
        "status": status,
        "start_date": start_date,
        "end_date": end_date,
        "expected_days": len(expected_dates),
        "days_available": len(available_complete_dates),
        "missing_dates": missing_dates,
        "incomplete_dates": incomplete_dates,
        "metrics": lightweight_window_metrics(selected) if available_complete_dates else None,
        "reason": None if complete else "WINDOW_INCOMPLETE",
    }


def aggregate_grid_window(records: list[dict], definition: dict, *, allow_partial: bool = False) -> dict:
    """Aggregate one window over unique grids with equal grid weighting."""
    records = [entry["record"] for entry in deduplicate_grid_records(records)]
    if not records:
        return lightweight_window_summary([], definition, allow_partial=allow_partial) | {
            "status": "INVALID",
            "reason": "NO_VALID_UNIQUE_GRID_RECORDS",
        }
    summaries = [lightweight_window_summary(record.get("daily", []), definition, allow_partial=allow_partial) for record in records]
    usable = [item for item in summaries if item.get("metrics")]
    expected_days = max((item.get("expected_days", 0) for item in summaries), default=0)
    missing_dates = sorted({value for item in summaries for value in item.get("missing_dates", [])})
    status_values = {item.get("status") for item in summaries}
    if all(value == "OK" for value in status_values) and len(usable) == len(records):
        status = "OK"
    elif usable and allow_partial:
        status = "PARTIAL"
    else:
        status = "INVALID"
    if not usable:
        return {
            "status": status,
            "start_date": definition.get("start_date"),
            "end_date": definition.get("end_date"),
            "expected_days": expected_days,
            "days_available": 0,
            "missing_dates": missing_dates,
            "grid_count": len(records),
            "metrics": None,
            "reason": "NO_COMPLETE_UNIQUE_GRID_WINDOW",
        }
    metrics = {}
    mean_keys = [
        key for key in LIGHTWEIGHT_WINDOW_METRIC_KEYS
        if key not in {"absolute_min_night_c", "max_wind_gust_kmh", "strong_wind_day_count"}
    ]
    for key in mean_keys:
        values = [item["metrics"].get(key) for item in usable if isinstance(item["metrics"].get(key), (int, float))]
        metrics[key] = safe_mean([float(value) for value in values]) if values else None
    absolute_mins = [item["metrics"].get("absolute_min_night_c") for item in usable if isinstance(item["metrics"].get("absolute_min_night_c"), (int, float))]
    gusts = [item["metrics"].get("max_wind_gust_kmh") for item in usable if isinstance(item["metrics"].get("max_wind_gust_kmh"), (int, float))]
    metrics["absolute_min_night_c"] = round(min(absolute_mins), 3) if absolute_mins else None
    metrics["max_wind_gust_kmh"] = round(max(gusts), 3) if gusts else None
    trend_items = [item["metrics"].get("temperature_trend") or {} for item in usable]
    trend_delta_values = [item.get("last_3_minus_first_3_mean_temperature_c") for item in trend_items if isinstance(item.get("last_3_minus_first_3_mean_temperature_c"), (int, float))]
    first_values = [item.get("first_3_days_mean_temperature_c") for item in trend_items if isinstance(item.get("first_3_days_mean_temperature_c"), (int, float))]
    last_values = [item.get("last_3_days_mean_temperature_c") for item in trend_items if isinstance(item.get("last_3_days_mean_temperature_c"), (int, float))]
    trend_delta = safe_mean([float(value) for value in trend_delta_values]) if trend_delta_values else None
    trend_direction = "UNDETERMINED" if trend_delta is None else "WARMING" if trend_delta >= 0.5 else "COOLING" if trend_delta <= -0.5 else "NEAR_FLAT"
    metrics["temperature_trend"] = {
        "first_3_days_mean_temperature_c": safe_mean([float(value) for value in first_values]) if first_values else None,
        "last_3_days_mean_temperature_c": safe_mean([float(value) for value in last_values]) if last_values else None,
        "last_3_minus_first_3_mean_temperature_c": trend_delta,
        "direction": trend_direction,
        "method": "equal_mean_of_unique_grid_window_trends; threshold=0.5C",
    }
    return {
        "status": status,
        "start_date": definition.get("start_date"),
        "end_date": definition.get("end_date"),
        "expected_days": expected_days,
        "days_available": min((item.get("days_available", 0) for item in usable), default=0),
        "missing_dates": missing_dates,
        "grid_count": len(records),
        "metrics": metrics,
        "reason": None if status == "OK" else "UNIQUE_GRID_WINDOW_PARTIAL",
        "aggregation": "equal_mean_over_unique_returned_model_grids; min/max fields preserve spatial extremes",
    }


def build_region_history_subregion(
    config: dict,
    region_id: str,
    subregion_id: str,
    point_results: dict[str, dict],
    forecast_date: dt.date,
) -> dict:
    registry_item = region_subregion_registry(config, region_id).get(subregion_id) or {}
    candidate_ids = region_subregion_point_ids(config, region_id, subregion_id)
    minimum_grids = registry_item.get("minimum_verified_unique_grids", 1)
    consistent_point_ids = [
        point_id
        for point_id in candidate_ids
        if point_id in point_results
        and point_results[point_id].get("status") == "OK"
        and (point_results[point_id].get("same_grid_qa") or {}).get("final_status") == "PASS"
    ]
    years = {}
    sampling_by_year = {}
    grid_sets = {}
    for year in HISTORY_FORWARD_YEARS:
        records = {
            point_id: point_results[point_id]["years"][str(year)]
            for point_id in consistent_point_ids
            if str(year) in point_results[point_id].get("years", {})
        }
        sampling = grid_sampling_summary(
            config,
            candidate_ids,
            records,
            minimum_verified_unique_grids=minimum_grids,
        )
        sampling_by_year[str(year)] = sampling
        unique_records = deduplicate_grid_records(list(records.values()))
        grid_sets[str(year)] = [entry["grid_cell_id"] for entry in unique_records]
        definitions = history_forward_windows_for_year(forecast_date, year)
        year_view = {
            "status": "OK",
            "sampling": sampling,
        }
        for key, definition in definitions.items():
            year_view[key] = aggregate_grid_window(
                [entry["record"] for entry in unique_records],
                definition,
                allow_partial=False,
            )
            if year_view[key]["status"] != "OK":
                year_view["status"] = "INVALID"
        years[str(year)] = year_view
    same_grid_set = bool(grid_sets) and len({json.dumps(value, sort_keys=True) for value in grid_sets.values()}) == 1
    all_year_windows_ok = all(
        years.get(str(year), {}).get("status") == "OK"
        for year in HISTORY_FORWARD_YEARS
    )
    all_sampling_ok = all(
        item.get("status") == "OK"
        for item in sampling_by_year.values()
    )
    if not consistent_point_ids:
        status = "INVALID"
        reason = "NO_POINT_WITH_THREE_YEAR_SAME_GRID_QA"
    elif not same_grid_set:
        status = "INVALID"
        reason = f"{region_id.upper()}_SUBREGION_GRID_SET_MISMATCH"
    elif all_year_windows_ok and all_sampling_ok:
        status = "OK"
        reason = None
    else:
        status = "PARTIAL"
        reason = "INSUFFICIENT_VERIFIED_UNIQUE_GRIDS_OR_WINDOW_DATA"
    first_sampling = sampling_by_year.get(str(HISTORY_FORWARD_YEARS[0]), {})
    return {
        "subregion": subregion_id,
        "name": registry_item.get("name", subregion_id),
        "status": status,
        "usable_for_main_chain": bool(consistent_point_ids),
        "cross_year_comparison_usable": status == "OK",
        "point_ids": candidate_ids,
        "verified_point_ids": [
            point_id for point_id in candidate_ids
            if config.get("points", {}).get(point_id, {}).get("status") == "VERIFIED"
        ],
        "consistent_point_ids": consistent_point_ids,
        "sampling": {
            "status": status if status in {"INVALID", "PARTIAL"} else first_sampling.get("status", "INVALID"),
            "minimum_verified_unique_grids": minimum_grids,
            "by_year": sampling_by_year,
            "grid_sets_by_year": grid_sets,
            "same_unique_grid_set_across_years": same_grid_set,
        },
        "years": years,
        "reason": reason,
    }


def build_kanas_history_subregion(
    config: dict,
    subregion_id: str,
    point_results: dict[str, dict],
    forecast_date: dt.date,
) -> dict:
    return build_region_history_subregion(config, "kanas", subregion_id, point_results, forecast_date)


def build_hemu_history_subregion(
    config: dict,
    subregion_id: str,
    point_results: dict[str, dict],
    forecast_date: dt.date,
) -> dict:
    return build_region_history_subregion(config, "hemu", subregion_id, point_results, forecast_date)


def equal_mean_subregion_window(
    items: list[dict],
    definition: dict,
    *,
    region_id: str = "kanas",
) -> dict:
    if not items:
        expected_dates = _window_expected_dates(definition)
        return {
            "status": "INVALID",
            "start_date": definition.get("start_date"),
            "end_date": definition.get("end_date"),
            "expected_days": len(expected_dates),
            "days_available": 0,
            "missing_dates": expected_dates,
            "metrics": None,
            "reason": f"NO_USABLE_{region_id.upper()}_SUBREGION",
        }
    metric_items = [
        item.get("metrics") or {
            key: item.get(key)
            for key in LIGHTWEIGHT_WINDOW_METRIC_KEYS
            if key in item
        }
        for item in items
        if item.get("status") in {"OK", "PARTIAL"}
        and (item.get("metrics") or any(key in item for key in LIGHTWEIGHT_WINDOW_METRIC_KEYS))
    ]
    if len(metric_items) != len(items):
        missing_dates = sorted({date_value for item in items for date_value in item.get("missing_dates", [])})
        if not metric_items and not missing_dates:
            missing_dates = _window_expected_dates(definition)
        status = "PARTIAL" if metric_items else "INVALID"
        return {
            "status": status,
            "start_date": definition.get("start_date"),
            "end_date": definition.get("end_date"),
            "expected_days": max((item.get("expected_days", 0) for item in items), default=0),
            "days_available": min((item.get("days_available", 0) for item in items), default=0),
            "missing_dates": missing_dates,
            "metrics": None,
            "reason": (
                f"{region_id.upper()}_COMPOSITE_SUBREGION_WINDOW_PARTIAL"
                if status == "PARTIAL"
                else "NO_COMPLETE_UNIQUE_GRID_WINDOW"
            ),
            "available_subregions": len(metric_items),
            "expected_subregions": len(items),
        }
    metrics = {}
    for key in LIGHTWEIGHT_WINDOW_METRIC_KEYS:
        if key in {"absolute_min_night_c", "max_wind_gust_kmh"}:
            continue
        values = [item.get(key) for item in metric_items if isinstance(item.get(key), (int, float))]
        metrics[key] = safe_mean([float(value) for value in values]) if values else None
    absolute_mins = [item.get("absolute_min_night_c") for item in metric_items if isinstance(item.get("absolute_min_night_c"), (int, float))]
    gusts = [item.get("max_wind_gust_kmh") for item in metric_items if isinstance(item.get("max_wind_gust_kmh"), (int, float))]
    metrics["absolute_min_night_c"] = round(min(absolute_mins), 3) if absolute_mins else None
    metrics["max_wind_gust_kmh"] = round(max(gusts), 3) if gusts else None
    trend_items = [
        item.get("temperature_trend")
        for item in metric_items
        if isinstance(item.get("temperature_trend"), dict)
    ]
    trend_first = [
        item.get("first_3_days_mean_temperature_c")
        for item in trend_items
        if isinstance(item.get("first_3_days_mean_temperature_c"), (int, float))
    ]
    trend_last = [
        item.get("last_3_days_mean_temperature_c")
        for item in trend_items
        if isinstance(item.get("last_3_days_mean_temperature_c"), (int, float))
    ]
    trend_delta_values = [
        item.get("last_3_minus_first_3_mean_temperature_c")
        for item in trend_items
        if isinstance(item.get("last_3_minus_first_3_mean_temperature_c"), (int, float))
    ]
    trend_delta = safe_mean([float(value) for value in trend_delta_values])
    if trend_delta is None:
        trend_direction = "UNDETERMINED"
    elif trend_delta >= 0.5:
        trend_direction = "WARMING"
    elif trend_delta <= -0.5:
        trend_direction = "COOLING"
    else:
        trend_direction = "NEAR_FLAT"
    metrics["temperature_trend"] = {
        "first_3_days_mean_temperature_c": safe_mean([float(value) for value in trend_first]),
        "last_3_days_mean_temperature_c": safe_mean([float(value) for value in trend_last]),
        "last_3_minus_first_3_mean_temperature_c": trend_delta,
        "direction": trend_direction,
        "method": "equal_mean_of_subregion_window_trends; threshold=0.5C",
    }
    aggregate_status = "OK" if all(item.get("status") == "OK" for item in items) else "PARTIAL"
    return {
        "status": aggregate_status,
        "start_date": definition.get("start_date"),
        "end_date": definition.get("end_date"),
        "expected_days": max(item.get("expected_days", 0) for item in items),
        "days_available": min(item.get("days_available", 0) for item in items),
        "missing_dates": sorted({date_value for item in items for date_value in item.get("missing_dates", [])}),
        "metrics": metrics,
        "reason": None if aggregate_status == "OK" else f"{region_id.upper()}_COMPOSITE_SUBREGION_WINDOW_PARTIAL",
        "aggregation": f"equal_mean_of_{region_id}_subregions; no point-count weighting",
    }


def build_region_history_composite(
    subregions: dict[str, dict],
    subregion_keys: tuple[str, ...],
    forecast_date: dt.date,
    *,
    region_id: str,
) -> dict:
    definitions_by_year = {
        str(year): history_forward_windows_for_year(forecast_date, year)
        for year in HISTORY_FORWARD_YEARS
    }
    years = {}
    for year in HISTORY_FORWARD_YEARS:
        year_view = {"status": "OK"}
        for key, definition in definitions_by_year[str(year)].items():
            items = [
                subregions[subregion_id].get("years", {}).get(str(year), {}).get(key, {})
                for subregion_id in subregion_keys
                if subregion_id in subregions
            ]
            year_view[key] = equal_mean_subregion_window(items, definition, region_id=region_id)
            if year_view[key]["status"] != "OK":
                year_view["status"] = "PARTIAL"
        years[str(year)] = year_view
    statuses = {subregions.get(key, {}).get("status", "INVALID") for key in subregion_keys}
    if statuses == {"OK"} and all(item.get("status") == "OK" for item in years.values()):
        status = "OK"
        reason = None
    elif statuses & {"OK", "PARTIAL"}:
        status = "PARTIAL"
        reason = f"{region_id.upper()}_COMPOSITE_REQUIRES_ALL_SUBREGIONS"
    else:
        status = "INVALID"
        reason = f"NO_USABLE_{region_id.upper()}_SUBREGION"
    subregion_names = ", ".join(subregion_keys)
    return {
        "status": status,
        "usable_for_main_chain": status == "OK",
        "cross_year_comparison_usable": status == "OK",
        "aggregation": f"equal_mean_of_subregions; {subregion_names} each weight=1/{len(subregion_keys)}",
        "subregion_statuses": {key: subregions.get(key, {}).get("status", "INVALID") for key in subregion_keys},
        "missing_or_partial_subregions": [
            key for key in subregion_keys
            if subregions.get(key, {}).get("status") != "OK"
        ],
        "years": years,
        "reason": reason,
    }


def build_kanas_history_composite(subregions: dict[str, dict], forecast_date: dt.date) -> dict:
    return build_region_history_composite(
        subregions,
        KANAS_SUBREGION_KEYS,
        forecast_date,
        region_id="kanas",
    )


def build_hemu_history_composite(subregions: dict[str, dict], forecast_date: dt.date) -> dict:
    return build_region_history_composite(
        subregions,
        HEMU_SUBREGION_KEYS,
        forecast_date,
        region_id="hemu",
    )


def compact_history_forward_year(record: dict) -> dict:
    item = compact_record(record)
    for key in HISTORY_FORWARD_WINDOW_KEYS:
        if key in record:
            item[key] = copy.deepcopy(record[key])
    return item


def run_history_forward(
    config: dict,
    client: ApiClient,
    generated_at: str,
    data_date: str,
    forecast_date: dt.date,
    *,
    refresh_history: bool = False,
    cache_dir: Path | None = None,
) -> dict:
    """Fetch real weather after today's calendar date for Altay reference years."""
    if config.get("namespace") == "ejina":
        raise ValueError("history_forward is an Altay-only module")
    points = active_points(config)
    window_definitions = history_forward_window_definitions(forecast_date)
    regions = {}
    point_results = {}
    all_records = []
    expected_point_ids = history_forward_point_ids(config)
    cache_stats = history_cache_stats()

    for point_id in expected_point_ids:
        point = points[point_id]
        years = {}
        for year in HISTORY_FORWARD_YEARS:
            year_windows = history_forward_windows_for_year(forecast_date, year)
            valid_window_ranges = [
                definition for definition in year_windows.values()
                if definition.get("status") == "OK"
            ]
            if not valid_window_ranges:
                record = invalid_record(
                    point=point,
                    source="Open-Meteo",
                    endpoint=OPEN_METEO_ENDPOINTS["history"],
                    model=HISTORY_MODEL,
                    request_params={"models": HISTORY_MODEL_PARAMETER, "year": year},
                    reason="HISTORY_FORWARD_AFTER_CUTOFF",
                )
            else:
                start_date = min(item["start_date"] for item in valid_window_ranges)
                end_date = max(item["end_date"] for item in valid_window_ranges)
                record = history_cache_record_or_fetch(
                    config,
                    client,
                    point,
                    year,
                    start_date,
                    end_date,
                    refresh_history=refresh_history,
                    cache_dir=cache_dir,
                    log_label=f"{point_id}:HISTORY_FORWARD {year}",
                )
            record["window_definitions"] = year_windows
            for key, definition in year_windows.items():
                record[key] = history_forward_window_summary(record.get("daily", []), definition)
                if record[key]["status"] == "OK":
                    log(f"[{point_id}] HISTORY_FORWARD {year} {key} OK")
                else:
                    log(f"[{point_id}] HISTORY_FORWARD {year} {key} {record[key]['status']}")
            if record.get("status") != "PASS":
                for key in year_windows:
                    if record[key]["status"] == "OK":
                        record[key]["status"] = "INVALID"
                        record[key]["usable_for_cross_year_comparison"] = False
                        record[key]["reason"] = "HISTORY_FORWARD_YEAR_INVALID"
            years[str(year)] = record
            all_records.append(record)
            update_history_cache_stats(cache_stats, record)
        same_grid_qa = history_forward_same_grid_qa(years)
        windows_ok = all(
            all(years[str(year)].get(key, {}).get("status") == "OK" for key in HISTORY_FORWARD_WINDOW_KEYS)
            for year in HISTORY_FORWARD_YEARS
        )
        point_status = "OK" if same_grid_qa["final_status"] == "PASS" and windows_ok else "FAILED"
        point_results[point_id] = {
            "point_id": point_id,
            "point": {
                "name": point["name"],
                "region": point["region"],
                "status": point["status"],
                "subregion": point.get("subregion"),
            },
            "status": point_status,
            "usable_for_main_chain": True,
            "cross_year_comparison_usable": point_status == "OK",
            "same_grid_qa": same_grid_qa,
            "years": years,
        }

    for region_id, region_config in config.get("regions", {}).items():
        core_id = region_config.get("core_point_id")
        core_result = point_results.get(core_id) if core_id else None
        if not core_result:
            regions[region_id] = {
                "region": region_id,
                "core_point_id": core_id,
                "status": "UNAVAILABLE",
                "usable_for_main_chain": False,
                "cross_year_comparison_usable": False,
                "same_grid_qa": None,
                "years": {},
                "reason": "NO_VERIFIED_CORE_POINT",
            }
            log(f"[{region_id}] HISTORY_FORWARD SKIPPED: PROVISIONAL")
            continue
        regions[region_id] = {
            "region": region_id,
            "core_point_id": core_id,
            "status": core_result["status"],
            "usable_for_main_chain": True,
            "cross_year_comparison_usable": core_result["cross_year_comparison_usable"],
            "same_grid_qa": core_result["same_grid_qa"],
            # Keep the v1.1 core-point paths stable for existing readers.
            "years": {
                year: compact_history_forward_year(record)
                for year, record in core_result["years"].items()
            },
            "reason": None if core_result["status"] == "OK" else "HISTORY_FORWARD_NOT_USABLE",
        }

    registered_subregions = {}
    registered_composites = {}
    for registered_region_id, subregion_keys in SUBREGION_KEYS_BY_REGION.items():
        registry = region_subregion_registry(config, registered_region_id)
        if not registry:
            continue
        subregions = {
            subregion_id: build_region_history_subregion(
                config,
                registered_region_id,
                subregion_id,
                point_results,
                forecast_date,
            )
            for subregion_id in subregion_keys
            if subregion_id in registry
        }
        composite = build_region_history_composite(
            subregions,
            subregion_keys,
            forecast_date,
            region_id=registered_region_id,
        )
        registered_subregions[registered_region_id] = subregions
        registered_composites[registered_region_id] = composite
        if registered_region_id in regions:
            regions[registered_region_id]["subregions"] = subregions
            regions[registered_region_id]["composite"] = composite
            regions[registered_region_id]["subregion_aggregation_status"] = composite["status"]
            regions[registered_region_id]["status"] = (
                "FAILED"
                if regions[registered_region_id]["status"] == "FAILED"
                else "OK" if composite["status"] == "OK" else "PARTIAL"
            )
            regions[registered_region_id]["cross_year_comparison_usable"] = composite["cross_year_comparison_usable"]
            regions[registered_region_id]["reason"] = (
                None
                if regions[registered_region_id]["status"] == "OK"
                else composite.get("reason")
            )
        for subregion_id, item in subregions.items():
            log(f"[{registered_region_id}/{subregion_id}] HISTORY_FORWARD SUBREGION {item['status']}")
        log(f"[{registered_region_id}/composite] HISTORY_FORWARD {composite['status']}")

    enabled_regions = [item for item in regions.values() if item.get("usable_for_main_chain")]
    if not enabled_regions or any(item.get("status") == "FAILED" for item in enabled_regions):
        module_status_value = "FAILED"
    elif any(item.get("status") == "PARTIAL" for item in enabled_regions):
        module_status_value = "PARTIAL"
    else:
        module_status_value = "OK"
    partial_points = sum(
        item.get("status") != "OK"
        for subregions in registered_subregions.values()
        for item in subregions.values()
    ) + sum(
        composite.get("status") == "PARTIAL"
        for composite in registered_composites.values()
    )
    aggregation_metadata = {
        region_id: {
            "subregions": list(subregions),
            "composite_status": registered_composites[region_id].get("status"),
            "deduplication": "returned_grid_coordinate; one independent sample per grid",
        }
        for region_id, subregions in registered_subregions.items()
    }
    return module_header(
        "history_forward",
        generated_at,
        data_date,
        module_status_value,
        endpoint=OPEN_METEO_ENDPOINTS["history"],
        model=HISTORY_MODEL,
        model_parameter=HISTORY_MODEL_PARAMETER,
        history_years=list(HISTORY_FORWARD_YEARS),
        forecast_date=forecast_date.isoformat(),
        anchor_date=forecast_date.isoformat(),
        cutoff_date=window_definitions[-1]["cutoff_date"],
        window_definitions=window_definitions,
        interpretation_boundary="Historical weather after the current calendar date; weather reference only.",
        points=point_results,
        regions=regions,
        excluded_points=excluded_points(config),
        successful_fetches=sum(record.get("status") == "PASS" for record in all_records),
        failed_fetches=sum(record.get("status") != "PASS" for record in all_records),
        expected_fetches=len(expected_point_ids) * len(HISTORY_FORWARD_YEARS),
        partial_points=partial_points,
        kanas_aggregation=aggregation_metadata.get("kanas", {}),
        hemu_aggregation=aggregation_metadata.get("hemu", {}),
        subregion_aggregations=aggregation_metadata,
        history_cache={
            "enabled": True,
            "directory": history_cache_relative_path(Path(cache_dir) if cache_dir is not None else HISTORY_CACHE_DIR),
            "refresh_requested": refresh_history,
            **cache_stats,
        },
    )


def failed_history_forward_module(
    config: dict,
    generated_at: str,
    data_date: str,
    forecast_date: dt.date,
    error: Exception,
) -> dict:
    reason = f"{type(error).__name__}:{error}"
    return module_header(
        "history_forward",
        generated_at,
        data_date,
        "FAILED",
        endpoint=OPEN_METEO_ENDPOINTS["history"],
        model=HISTORY_MODEL,
        model_parameter=HISTORY_MODEL_PARAMETER,
        history_years=list(HISTORY_FORWARD_YEARS),
        forecast_date=forecast_date.isoformat(),
        anchor_date=forecast_date.isoformat(),
        cutoff_date=history_forward_window_definitions(forecast_date)[-1]["cutoff_date"],
        window_definitions=history_forward_window_definitions(forecast_date),
        interpretation_boundary="Historical weather after the current calendar date; weather reference only.",
        points={},
        regions={},
        excluded_points=excluded_points(config),
        successful_fetches=0,
        failed_fetches=0,
        expected_fetches=len(history_forward_point_ids(config)) * len(HISTORY_FORWARD_YEARS),
        error=reason,
    )


def metric_value(item: dict, key: str) -> float | None:
    value = item.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def consecutive_cold_night_sequences(days: list[dict], threshold: float) -> list[dict]:
    """Return calendar-contiguous runs whose daily night minimum is below a threshold."""
    complete_days = sorted(
        (day for day in days if day.get("complete") and day.get("date")),
        key=lambda day: day["date"],
    )
    sequences = []
    current: list[dict] = []
    previous_date: dt.date | None = None

    def flush() -> None:
        if current:
            sequences.append({
                "start_date": current[0]["date"],
                "end_date": current[-1]["date"],
                "nights": len(current),
            })

    for day in complete_days:
        day_date = dt.date.fromisoformat(day["date"])
        night_min = metric_value(day, "night_min_c")
        is_contiguous = previous_date is not None and day_date == previous_date + dt.timedelta(days=1)
        if night_min is not None and night_min < threshold and (not current or is_contiguous):
            current.append(day)
        else:
            flush()
            current = []
            if night_min is not None and night_min < threshold:
                current.append(day)
        previous_date = day_date
    flush()
    return sequences


def period_metrics(days: list[dict]) -> dict:
    complete_days = [day for day in days if day.get("complete")]
    numeric_keys = {
        "temperature_min_c": "temperature_min_c",
        "temperature_max_c": "temperature_max_c",
        "temperature_mean_c": "temperature_mean_c",
        "night_min_c": "night_min_c",
        "precipitation_mm": "precipitation_mm",
        "snowfall_cm": "snowfall_cm",
        "cloud_cover_mean_pct": "cloud_cover_mean_pct",
        "cloud_cover_low_mean_pct": "cloud_cover_low_mean_pct",
        "wind_speed_mean_kmh": "wind_speed_mean_kmh",
        "wind_gust_max_kmh": "wind_gust_max_kmh",
    }
    metrics = {key: None for key in numeric_keys}
    for key in ("temperature_min_c", "temperature_max_c", "temperature_mean_c", "night_min_c", "cloud_cover_mean_pct", "cloud_cover_low_mean_pct", "wind_speed_mean_kmh"):
        values = [metric_value(day, key) for day in complete_days]
        values = [value for value in values if value is not None]
        metrics[key] = round(mean(values), 3) if values else None
    for key in ("precipitation_mm", "snowfall_cm"):
        values = [metric_value(day, key) for day in complete_days]
        values = [value for value in values if value is not None]
        metrics[key] = round(sum(values), 3) if values else None
    gusts = [metric_value(day, "wind_gust_max_kmh") for day in complete_days]
    gusts = [value for value in gusts if value is not None]
    metrics["wind_gust_max_kmh"] = round(max(gusts), 3) if gusts else None
    metrics["days_available"] = len(complete_days)
    metrics["threshold_nights"] = {
        f"below_{str(int(threshold))}_c": sum(
            1 for day in complete_days if metric_value(day, "night_min_c") is not None and metric_value(day, "night_min_c") < threshold
        )
        for threshold in THRESHOLDS_C
    }
    metrics["consecutive_cold_nights"] = {
        f"below_{str(int(threshold))}_c": {
            "max_consecutive": max(
                (item["nights"] for item in consecutive_cold_night_sequences(complete_days, threshold)),
                default=0,
            ),
            "sequences": consecutive_cold_night_sequences(complete_days, threshold),
        }
        for threshold in THRESHOLDS_C
    }
    coldness = 0.0
    for day in complete_days:
        daily_mean = metric_value(day, "temperature_mean_c")
        night_min = metric_value(day, "night_min_c")
        if daily_mean is not None:
            coldness += max(0.0, 10.0 - daily_mean)
        if night_min is not None:
            coldness += 2.0 * max(0.0, 5.0 - night_min)
            coldness += 3.0 * max(0.0, 2.0 - night_min)
            coldness += 4.0 * max(0.0, 0.0 - night_min)
    metrics["coldness_index"] = round(coldness, 3)
    metrics["diurnal_temperature_range_c"] = (
        round(metrics["temperature_max_c"] - metrics["temperature_min_c"], 3)
        if metrics["temperature_max_c"] is not None and metrics["temperature_min_c"] is not None
        else None
    )
    solar_values = []
    solar_unit = None
    for day in complete_days:
        solar = day.get("solar_metric")
        if isinstance(solar, dict) and isinstance(solar.get("value"), (int, float)):
            solar_values.append(float(solar["value"]))
            solar_unit = solar.get("unit")
    metrics["solar_metric_total_or_mean"] = round(sum(solar_values), 3) if solar_values else None
    metrics["solar_metric_unit"] = solar_unit
    metrics["period_start"] = complete_days[0]["date"] if complete_days else None
    metrics["period_end"] = complete_days[-1]["date"] if complete_days else None
    return metrics


def numeric_deltas(current: dict, baseline: dict) -> dict:
    delta = {}
    for key, value in current.items():
        base = baseline.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(base, (int, float)) and not isinstance(base, bool):
            delta[key] = round(value - base, 3)
        elif isinstance(value, dict) and isinstance(base, dict):
            nested = numeric_deltas(value, base)
            if nested:
                delta[key] = nested
    return delta


def driver_direction(current: dict, baseline: dict, baseline_year: int = 2025) -> dict:
    current_index = current.get("coldness_index")
    baseline_index = baseline.get("coldness_index")
    current_counts = current.get("threshold_nights", {})
    baseline_counts = baseline.get("threshold_nights", {})
    count_delta = sum(
        int(current_counts.get(key, 0)) - int(baseline_counts.get(key, 0))
        for key in ("below_10_c", "below_5_c", "below_2_c", "below_0_c")
    )
    if current.get("days_available", 0) < 3 or baseline.get("days_available", 0) < 3 or current_index is None or baseline_index is None:
        direction = "UNDETERMINED"
        strength = "WEAK"
    else:
        delta = float(current_index) - float(baseline_index)
        if delta >= 5 or count_delta >= 2:
            direction = "LEADING"
        elif delta <= -5 or count_delta <= -2:
            direction = "LAGGING"
        else:
            direction = "SYNC"
        magnitude = abs(delta)
        strong_cutoff = max(10.0, abs(float(baseline_index)) * 0.25)
        moderate_cutoff = max(5.0, abs(float(baseline_index)) * 0.1)
        strength = "STRONG" if magnitude >= strong_cutoff else "MODERATE" if magnitude >= moderate_cutoff else "WEAK"
    return {
        "direction": direction,
        "strength": strength,
        "evidence": {
            "current_2026": current,
            f"baseline_{baseline_year}": baseline,
            f"delta_2026_minus_{baseline_year}": numeric_deltas(current, baseline),
            "threshold_count_delta_sum": count_delta,
            "interpretation": "weather_driver_only; requires ChatGPT visual evidence for actual phenology assessment",
        },
    }


def undetermined_weather_driver(reason: str) -> dict:
    return {
        "direction": "UNDETERMINED",
        "strength": "WEAK",
        "evidence": {"reason": reason},
    }


def historical_same_grid_qa(years: dict[str, dict], configured_years: tuple[int, ...]) -> dict:
    """Require every configured historical year to use one returned model grid."""
    year_keys = [str(year) for year in configured_years]
    grids = {
        year: (years.get(year, {}).get("response") or {}).get("grid_coordinate")
        for year in year_keys
    }
    requested_coordinates = {
        year: (years.get(year, {}).get("request") or {}).get("coordinate")
        for year in year_keys
    }
    pairwise = {}
    for index, left in enumerate(year_keys):
        for right in year_keys[index + 1:]:
            if grids[left] and grids[right]:
                pairwise[f"{left}_vs_{right}"] = "PASS" if grids[left] == grids[right] else "FAIL"
            else:
                pairwise[f"{left}_vs_{right}"] = "UNAVAILABLE"
    available_grids = [grids[year] for year in year_keys if grids[year]]
    if len(available_grids) != len(year_keys):
        grid_status = "UNAVAILABLE"
    else:
        grid_status = "PASS" if len({json.dumps(grid, sort_keys=True) for grid in available_grids}) == 1 else "FAIL"
    available_coordinates = [requested_coordinates[year] for year in year_keys if requested_coordinates[year]]
    if len(available_coordinates) != len(year_keys):
        coordinate_status = "UNAVAILABLE"
    else:
        coordinate_status = "PASS" if len({json.dumps(coordinate, sort_keys=True) for coordinate in available_coordinates}) == 1 else "FAIL"
    final_status = "PASS" if grid_status == "PASS" and coordinate_status == "PASS" else "FAILED"
    reason = None
    if grid_status == "FAIL":
        reason = "HISTORICAL_GRID_MISMATCH"
    elif coordinate_status == "FAIL":
        reason = "HISTORICAL_REQUEST_COORDINATE_MISMATCH"
    elif final_status == "FAILED":
        reason = "HISTORICAL_GRID_OR_COORDINATE_UNAVAILABLE"
    result = {
        "status": grid_status,
        "final_status": final_status,
        "checked_years": year_keys,
        "returned_grids": grids,
        "pairwise": pairwise,
        "same_requested_coordinate": coordinate_status,
        "requested_coordinates": requested_coordinates,
        "reason": reason,
    }
    # Preserve the v1.0/v1.1 pairwise fields for existing ChatGPT readers.
    for year in ("2025", "2026"):
        result[f"returned_grid_{year}"] = grids.get(year)
    return result


def build_history_comparison(config: dict, point_results: dict[str, dict], data_date: str) -> dict:
    configured_years = history_years_for_config(config)
    current_year = 2026
    comparisons: dict[str, dict] = {}
    for point_id, result in point_results.items():
        years = result["years"]
        daily_by_year = {
            str(year): (
                years.get(str(year), {}).get("daily", [])
                if years.get(str(year), {}).get("status") == "PASS"
                else []
            )
            for year in configured_years
        }
        metrics_by_year = {
            year: period_metrics(days)
            for year, days in daily_by_year.items()
        }
        same_grid_qa = historical_same_grid_qa(years, configured_years)
        history_start = result.get("history_start_month_day")
        if not history_start:
            history_start = config.get("regions", {}).get(result["point"]["region"], {}).get(
                "history_start_month_day",
                config.get("history_start_month_day", "08-25"),
            )
        complete_years = all(
            years.get(str(year), {}).get("status") == "PASS" and daily_by_year[str(year)]
            for year in configured_years
        )
        comparison_status = (
            "OK"
            if complete_years and same_grid_qa["final_status"] == "PASS"
            else "FAILED"
        )
        metrics_2026 = metrics_by_year.get(str(current_year), {})
        comparison_deltas = {
            str(year): numeric_deltas(metrics_2026, metrics_by_year[str(year)])
            for year in configured_years
            if year != current_year and comparison_status == "OK"
        }
        comparison = {
            "point_id": point_id,
            "region": result["point"]["region"],
            "status": comparison_status,
            "period_start": history_start,
            "period_end": data_date,
            "daily": daily_by_year,
            "metrics": metrics_by_year,
            "deltas_2026_minus": comparison_deltas,
            "delta_2026_minus_2023": comparison_deltas.get("2023", {}),
            "delta_2026_minus_2024": comparison_deltas.get("2024", {}),
            "delta_2026_minus_2025": comparison_deltas.get("2025", {}),
            "same_grid_qa": same_grid_qa,
        }
        if config.get("namespace") != "ejina":
            for year in configured_years:
                if year != current_year:
                    comparison[f"weather_driver_vs_{year}"] = driver_direction(
                        metrics_2026,
                        metrics_by_year.get(str(year), {}),
                        baseline_year=year,
                    )
        comparisons[point_id] = comparison
    region_summaries: dict[str, dict] = {}
    for region_id in core_region_ids(config):
        region_config = config["regions"][region_id]
        core_id = region_config.get("core_point_id")
        comparison = comparisons.get(core_id) if core_id else None
        if comparison:
            region_summary = {
                "region": region_id,
                "core_point_id": core_id,
                "status": comparison["status"],
                "usable_for_main_chain": True,
                "metrics": comparison["metrics"],
                "supporting_point_ids": [point_id for point_id, item in comparisons.items() if item["region"] == region_id and point_id != core_id],
            }
            if config.get("namespace") != "ejina":
                region_summary["visit_date"] = region_config.get("primary_visit_date")
                for year in configured_years:
                    if year != current_year:
                        region_summary[f"weather_driver_vs_{year}"] = comparison[f"weather_driver_vs_{year}"]
            region_summaries[region_id] = region_summary
    return {"points": comparisons, "regions": region_summaries}


def run_gfs(config: dict, client: ApiClient, generated_at: str, data_date: str) -> dict:
    points = active_points(config)
    records = {}
    for point_id, point in points.items():
        records[point_id] = fetch_point(
            client,
            point=point,
            source="Open-Meteo",
            endpoint=OPEN_METEO_ENDPOINTS["gfs"],
            model="NCEP GFS Global 0.11°",
            params=base_weather_params(point, forecast_days=16),
            variables=HRES_VARIABLES,
            required_variables=[value for value in HRES_VARIABLES if value != "sunshine_duration"],
            grid_limit_km=19.5,
            log_label=f"{point_id}:GFS",
            precision_module="gfs",
            max_forecast_date=point_forecast_end_date(point),
        )
    values = list(records.values())
    return module_header(
        "gfs",
        generated_at,
        data_date,
        module_status(values, len(points)),
        endpoint=OPEN_METEO_ENDPOINTS["gfs"],
        model="NCEP GFS Global 0.11°",
        native_resolution="0.11° (~13 km)",
        precision_policy=PRECISION_POLICIES["gfs"],
        interpolation_note="Open-Meteo documents GFS as hourly, with 3-hourly native data interpolated after 120 h.",
        points=records,
        excluded_points=excluded_points(config),
        successful_points=sum(record.get("status") == "PASS" for record in values),
        failed_points=sum(record.get("status") != "PASS" for record in values),
    )


SAMPLE_BEARINGS_DEGREES = {
    "N": 0,
    "S": 180,
    "E": 90,
    "W": 270,
    "NE": 45,
    "NW": 315,
    "SE": 135,
    "SW": 225,
}


def offset_coordinate(latitude: float, longitude: float, distance_km: float, direction: str) -> tuple[float, float]:
    bearing = math.radians(SAMPLE_BEARINGS_DEGREES[direction])
    latitude_delta = distance_km * math.cos(bearing) / 111.32
    longitude_delta = distance_km * math.sin(bearing) / (111.32 * math.cos(math.radians(latitude)))
    return round(latitude + latitude_delta, 6), round(longitude + longitude_delta, 6)


def sample_definitions(region_id: str, core_point: dict, radius_km: float, directions: list[str]) -> list[dict]:
    samples = [{
        "sample_id": f"{region_id}:CORE",
        "direction": "CORE",
        "requested_coordinate": {"latitude": core_point["latitude"], "longitude": core_point["longitude"]},
    }]
    for direction in directions:
        latitude, longitude = offset_coordinate(core_point["latitude"], core_point["longitude"], radius_km, direction)
        samples.append({
            "sample_id": f"{region_id}:{direction}",
            "direction": direction,
            "requested_coordinate": {"latitude": latitude, "longitude": longitude},
        })
    return samples


def record_grid_cell_key(record: dict) -> str | None:
    coordinate = (record.get("response") or {}).get("grid_coordinate") or {}
    if not valid_coordinate(coordinate.get("latitude"), coordinate.get("longitude")):
        return None
    return f"{float(coordinate['latitude']):.6f},{float(coordinate['longitude']):.6f}"


def compact_spatial_sample(sample: dict, record: dict) -> dict:
    response = record.get("response") or {}
    return {
        "sample_id": sample["sample_id"],
        "direction": sample["direction"],
        "requested_coordinate": sample["requested_coordinate"],
        "returned_grid_coordinate": response.get("grid_coordinate"),
        "returned_elevation": response.get("returned_elevation"),
        "grid_distance_km": (record.get("qa") or {}).get("grid_distance_km"),
        "grid_cell_key": record_grid_cell_key(record),
        "status": record.get("status"),
        "source": record.get("source"),
        "endpoint": record.get("endpoint"),
        "model": record.get("model"),
        "qa": record.get("qa"),
        "daily": record.get("daily", []),
    }


def spatial_region_summary(region_id: str, samples: list[dict]) -> dict:
    valid_samples = [sample for sample in samples if sample.get("status") == "PASS" and sample.get("grid_cell_key")]
    by_cell: dict[str, dict] = {}
    for sample in valid_samples:
        by_cell.setdefault(sample["grid_cell_key"], sample)
    daily_by_cell: dict[str, dict[str, dict]] = {
        cell: {item["date"]: item for item in sample.get("daily", [])}
        for cell, sample in by_cell.items()
    }
    dates = sorted({date for values in daily_by_cell.values() for date in values})[:7]
    temperatures = []
    for sample in by_cell.values():
        for day in sample.get("daily", [])[:7]:
            for key in ("temperature_min_c", "temperature_max_c"):
                value = metric_value(day, key)
                if value is not None:
                    temperatures.append(value)
    coverage_by_date = []
    for date in dates:
        cells_with_data = [values[date] for values in daily_by_cell.values() if date in values]
        cold_cells = [day for day in cells_with_data if metric_value(day, "night_min_c") is not None and metric_value(day, "night_min_c") < 5]
        total_cells = len(cells_with_data)
        ratio = len(cold_cells) / total_cells if total_cells else None
        label = "undetermined" if ratio is None else "widespread" if ratio >= 0.75 else "mixed" if ratio >= 0.5 else "localized" if ratio > 0 else "none"
        coverage_by_date.append({
            "date": date,
            "threshold": "night_min_c < 5",
            "cold_cells": len(cold_cells),
            "total_cells": total_cells,
            "coverage_ratio": round(ratio, 3) if ratio is not None else None,
            "label": label,
        })
    cells_with_data = set(daily_by_cell)
    cold_cells = {
        cell
        for cell, values in daily_by_cell.items()
        if any(
            metric_value(day, "night_min_c") is not None and metric_value(day, "night_min_c") < 5
            for day in values.values()
        )
    }
    total_cell_observations = sum(item["total_cells"] for item in coverage_by_date)
    cold_cell_observations = sum(item["cold_cells"] for item in coverage_by_date)
    daily_ratio = cold_cell_observations / total_cell_observations if total_cell_observations else None
    unique_ratio = len(cold_cells) / len(cells_with_data) if cells_with_data else None
    any_label = "undetermined" if unique_ratio is None else "widespread" if unique_ratio >= 0.75 else "mixed" if unique_ratio >= 0.5 else "localized" if unique_ratio > 0 else "none"
    return {
        "requested_samples": len(samples),
        "valid_samples": len(valid_samples),
        "failed_samples": len(samples) - len(valid_samples),
        "unique_model_cells": len(by_cell),
        "duplicate_requested_samples": len(valid_samples) - len(by_cell),
        "temperature_range_c": {
            "min": round(min(temperatures), 3) if temperatures else None,
            "max": round(max(temperatures), 3) if temperatures else None,
            "window_days": len(dates),
        },
        "cold_pool_coverage": {
            "threshold": "night_min_c < 5",
            "next_7d": {
                "cold_cells": len(cold_cells),
                "total_cells": len(cells_with_data),
                "cold_cell_observations": cold_cell_observations,
                "cell_observations": total_cell_observations,
                "coverage_ratio": round(daily_ratio, 3) if daily_ratio is not None else None,
                "unique_cell_coverage_ratio": round(unique_ratio, 3) if unique_ratio is not None else None,
                "label": any_label,
            },
            "by_date": coverage_by_date,
        },
    }


def run_spatial(config: dict, client: ApiClient, generated_at: str, data_date: str, hres: dict) -> dict:
    regions: dict[str, dict] = {}
    enabled_region_statuses = []
    points = active_points(config)
    for region_id, region_config in config["regions"].items():
        core_id = region_config.get("core_point_id")
        core_point = points.get(core_id) if core_id else None
        if not core_point:
            log(f"[{region_id}] SKIPPED: PROVISIONAL")
            regions[region_id] = {
                "status": "SKIPPED",
                "usable_for_main_chain": False,
                "reason": "NO_VERIFIED_CORE_POINT",
                "requested_samples": 0,
                "unique_model_cells": 0,
                "samples": [],
            }
            continue
        samples = sample_definitions(
            region_id,
            core_point,
            float(config["sampling"]["radius_km"]),
            list(config["sampling"]["directions"]),
        )
        full_records = []
        compact_samples = []
        sampling_grid_limit_km = float(config["sampling"]["radius_km"]) + float(config["sampling"].get("grid_qa_extra_allowance_km", 4.5))
        for sample in samples:
            if sample["direction"] == "CORE":
                seeded = hres.get("points", {}).get(core_id)
                if seeded and seeded.get("status") == "PASS":
                    record = copy.deepcopy(seeded)
                else:
                    record = None
            else:
                record = None
            if record is None:
                sample_point = {
                    "id": sample["sample_id"],
                    "name": f"{region_config['name']} {sample['direction']} sample",
                    "region": region_id,
                    "status": "VERIFIED",
                    **sample["requested_coordinate"],
                }
                record = fetch_point(
                    client,
                    point=sample_point,
                    source="Open-Meteo",
                    endpoint=OPEN_METEO_ENDPOINTS["hres"],
                    model="ECMWF IFS HRES 9 km",
                    params=base_weather_params(sample_point, forecast_days=7),
                    variables=["temperature_2m", "precipitation", "snowfall", "wind_gusts_10m"],
                    required_variables=["temperature_2m", "precipitation", "snowfall", "wind_gusts_10m"],
                    grid_limit_km=sampling_grid_limit_km,
                    log_label=f"{sample['sample_id']}:SPATIAL",
                    precision_module="hres",
                    max_forecast_date=point_forecast_end_date(core_point),
                )
            full_records.append(record)
            compact_samples.append(compact_spatial_sample(sample, record))
        region_summary = spatial_region_summary(region_id, compact_samples)
        region_status = "OK" if region_summary["failed_samples"] == 0 else "FAILED"
        enabled_region_statuses.append(region_status)
        regions[region_id] = {
            "status": region_status,
            "usable_for_main_chain": True,
            "core_point_id": core_id,
            "analysis_window": {
                "start": next((item["date"] for sample in compact_samples for item in sample.get("daily", [])), None),
                "days": 7,
            },
            **region_summary,
            "samples": compact_samples,
        }
    status = "OK" if enabled_region_statuses and all(value == "OK" for value in enabled_region_statuses) else "FAILED"
    return module_header(
        "spatial_sampling",
        generated_at,
        data_date,
        status,
        endpoint=OPEN_METEO_ENDPOINTS["hres"],
        model="ECMWF IFS HRES 9 km",
        sampling_config=config["sampling"],
        regions=regions,
        excluded_points=excluded_points(config),
    )


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


def ensemble_statistics(values: list[float]) -> dict:
    p25 = percentile(values, 0.25)
    p75 = percentile(values, 0.75)
    return {
        "mean": round(mean(values), 3) if values else None,
        "median": round(median(values), 3) if values else None,
        "p10": percentile(values, 0.10),
        "p25": p25,
        "p75": p75,
        "p90": percentile(values, 0.90),
        "interquartile_spread": round(p75 - p25, 3) if p25 is not None and p75 is not None else None,
        "spread": round(percentile(values, 0.90) - percentile(values, 0.10), 3)
        if values
        else None,
    }


def member_series_keys(hourly: dict, variable: str) -> list[str]:
    member_keys = sorted(
        (key for key in hourly if re.fullmatch(re.escape(variable) + r"_member\d{2}", key)),
        key=lambda key: int(key.rsplit("member", 1)[1]),
    )
    return member_keys


def ensemble_series_keys(hourly: dict, variable: str) -> list[str]:
    return [variable, *member_series_keys(hourly, variable)]


def ensemble_daily_distributions(hourly: dict) -> dict:
    times = hourly.get("time") or []
    groups: dict[str, list[int]] = {}
    for index, value in enumerate(times):
        groups.setdefault(parse_local_api_time(value).date().isoformat(), []).append(index)
    temperature_keys = ensemble_series_keys(hourly, "temperature_2m")
    output = {"night_min": [], "daily_mean": []}
    for day, indices in sorted(groups.items()):
        night_indices = [index for index in indices if parse_local_api_time(times[index]).hour <= 6 or parse_local_api_time(times[index]).hour >= 20]
        night_values = []
        daily_means = []
        for key in temperature_keys:
            night = _values_for_indices(hourly, key, night_indices)
            day_values = _values_for_indices(hourly, key, indices)
            if night:
                night_values.append(min(night))
            if day_values:
                daily_means.append(mean(day_values))
        night_stats = ensemble_statistics(night_values)
        daily_stats = ensemble_statistics(daily_means)
        thresholds = {}
        for threshold in THRESHOLDS_C:
            key = f"below_{str(int(threshold))}_c"
            below = sum(value < threshold for value in night_values)
            thresholds[key] = {
                "members": below,
                "total_members": len(temperature_keys),
                "probability": round(below / len(temperature_keys), 3) if temperature_keys else None,
            }
        output["night_min"].append({"date": day, "statistics_c": night_stats, "thresholds": thresholds})
        output["daily_mean"].append({"date": day, "statistics_c": daily_stats})
    return output


def validate_ensemble_members(record: dict) -> tuple[bool, dict]:
    hourly = record.get("hourly") or {}
    expected = {}
    missing = []
    length_mismatch = []
    null_values = []
    times = hourly.get("time") or []
    for variable in ENSEMBLE_VARIABLES:
        keys = ensemble_series_keys(hourly, variable)
        expected[variable] = keys
        if len(keys) != 51:
            missing.append(f"{variable}:expected_51_got_{len(keys)}")
        for key in keys:
            if len(hourly.get(key, [])) != len(times):
                length_mismatch.append(key)
            if any(value is None for value in hourly.get(key, [])):
                null_values.append(key)
    valid = not missing and not length_mismatch and not null_values
    return valid, {
        "status": "PASS" if valid else "FAIL",
        "expected_members": 51,
        "series_by_variable": expected,
        "missing_or_wrong_count": missing,
        "array_length_mismatch": length_mismatch,
        "null_data_series": null_values,
    }


def run_ensemble(config: dict, client: ApiClient, generated_at: str, data_date: str) -> dict:
    points = active_points(config)
    records: dict[str, dict] = {}
    active_core_ids = []
    for region_id in core_region_ids(config):
        core_id = config["regions"][region_id].get("core_point_id")
        point = points.get(core_id) if core_id else None
        if not point:
            log(f"[{region_id}] ENSEMBLE SKIPPED: PROVISIONAL")
            continue
        active_core_ids.append(core_id)
        record = fetch_point(
            client,
            point=point,
            source="Open-Meteo",
            endpoint=OPEN_METEO_ENDPOINTS["ensemble"],
            model="ECMWF IFS 0.25° Ensemble (51 members)",
            params=base_weather_params(
                point,
                models="ecmwf_ifs025_ensemble",
                forecast_days=7,
            ),
            variables=ENSEMBLE_VARIABLES,
            required_variables=ENSEMBLE_VARIABLES,
            grid_limit_km=37.5,
            log_label=f"{core_id}:ENSEMBLE",
            max_forecast_date=point_forecast_end_date(point),
        )
        if record.get("status") == "PASS":
            members_valid, member_check = validate_ensemble_members(record)
            record["qa"]["ensemble_member_check"] = member_check
            if not members_valid:
                record["status"] = "INVALID"
                record["qa"]["valid"] = False
                record["qa"]["final_status"] = "INVALID"
                record["qa"]["reason"] = "ENSEMBLE_MEMBER_SCHEMA_INVALID"
                log(f"[{core_id}] ENSEMBLE MEMBER QA FAIL")
            else:
                record["ensemble"] = {
                    "model_id": "ecmwf_ifs025_ensemble",
                    "resolution": "0.25° (~25 km)",
                    "total_members": 51,
                    "distributions": ensemble_daily_distributions(record["hourly"]),
                }
        records[core_id] = record
    values = list(records.values())
    return module_header(
        "ensemble",
        generated_at,
        data_date,
        module_status(values, len(active_core_ids)),
        endpoint=OPEN_METEO_ENDPOINTS["ensemble"],
        model="ECMWF IFS 0.25° Ensemble",
        model_id="ecmwf_ifs025_ensemble",
        resolution="0.25° (~25 km)",
        total_members=51,
        notes=[
            "Global 51-member ECMWF IFS ensemble is used for Xinjiang.",
            "Ensemble spread is signal robustness, not village-level temperature precision.",
        ],
        points=records,
        excluded_points=excluded_points(config),
        successful_points=sum(record.get("status") == "PASS" for record in values),
        failed_points=sum(record.get("status") != "PASS" for record in values),
    )


LONG_RANGE_SIGNAL_ORDER = ("NONE", "WEAK", "MODERATE", "STRONG")
LONG_RANGE_UNCERTAINTY_ORDER = ("LOW", "MODERATE", "HIGH", "VERY_HIGH")


def long_range_window_definitions() -> list[tuple[int, int]]:
    windows = []
    start = LONG_RANGE_LEAD_START
    while start <= LONG_RANGE_LEAD_END:
        end = min(start + 2, LONG_RANGE_LEAD_END)
        windows.append((start, end))
        start = end + 1
    return windows


def validate_long_range_members(record: dict) -> tuple[bool, dict]:
    hourly = record.get("hourly") or {}
    times = hourly.get("time") if isinstance(hourly.get("time"), list) else []
    series_by_variable = {}
    variable_availability = {}
    missing_or_wrong_count = []
    array_length_mismatch = []
    null_data_series = []
    member_counts = {}
    for variable in LONG_RANGE_VARIABLES:
        keys = ensemble_series_keys(hourly, variable)
        series_by_variable[variable] = keys
        member_counts[variable] = len(keys)
        if len(keys) != LONG_RANGE_ENSEMBLE_MEMBERS:
            missing_or_wrong_count.append(
                f"{variable}:expected_{LONG_RANGE_ENSEMBLE_MEMBERS}_got_{len(keys)}"
            )
        first_indices = []
        last_indices = []
        for key in keys:
            values = hourly.get(key)
            if not isinstance(values, list) or len(values) != len(times):
                array_length_mismatch.append(key)
                continue
            non_null_indices = [index for index, value in enumerate(values) if value is not None]
            if not non_null_indices:
                null_data_series.append(key)
                continue
            first_index = non_null_indices[0]
            last_index = non_null_indices[-1]
            first_indices.append(first_index)
            last_indices.append(last_index)
            if any(value is None for value in values[first_index : last_index + 1]):
                null_data_series.append(key)
        if first_indices and last_indices:
            first_common = max(first_indices)
            last_common = min(last_indices)
            variable_availability[variable] = {
                "first_timestamp": times[first_common],
                "last_timestamp": times[last_common],
                "first_complete_index": first_common,
                "last_complete_index": last_common,
                "all_members_complete_through_index": last_common,
                "edge_truncated": first_common > 0 or last_common < len(times) - 1,
            }
        else:
            variable_availability[variable] = {
                "first_timestamp": None,
                "last_timestamp": None,
                "first_complete_index": None,
                "last_complete_index": None,
                "all_members_complete_through_index": None,
                "edge_truncated": False,
            }
    valid = bool(times) and not missing_or_wrong_count and not array_length_mismatch and not null_data_series
    return valid, {
        "status": "PASS" if valid else "FAIL",
        "expected_members": LONG_RANGE_ENSEMBLE_MEMBERS,
        "actual_member_counts_by_variable": member_counts,
        "series_by_variable": series_by_variable,
        "variable_availability": variable_availability,
        "edge_truncated_variables": [
            variable for variable, item in variable_availability.items() if item["edge_truncated"]
        ],
        "missing_or_wrong_count": missing_or_wrong_count,
        "array_length_mismatch": array_length_mismatch,
        "null_data_series": null_data_series,
    }


def trim_long_range_member_edges(record: dict) -> dict:
    hourly = record.get("hourly") or {}
    times = hourly.get("time") or []
    audit = {}
    for variable in LONG_RANGE_VARIABLES:
        keys = ensemble_series_keys(hourly, variable)
        bounds = []
        for key in keys:
            values = hourly.get(key) or []
            non_null = [index for index, value in enumerate(values) if value is not None]
            if non_null:
                bounds.append((non_null[0], non_null[-1]))
        first_common = max((item[0] for item in bounds), default=None)
        last_common = min((item[1] for item in bounds), default=None)
        audit[variable] = {
            "series_count": len(keys),
            "original_timestep_count": len(times),
            "all_members_first_complete_timestamp": times[first_common] if first_common is not None and first_common < len(times) else None,
            "all_members_last_complete_timestamp": times[last_common] if last_common is not None and last_common < len(times) else None,
            "leading_missing_rows": first_common or 0 if first_common is not None else None,
            "trailing_missing_rows": len(times) - 1 - last_common if last_common is not None else None,
            "horizon_status": "TRUNCATED_EDGE_MISSING" if first_common not in {None, 0} or last_common not in {None, len(times) - 1} else "COMPLETE",
        }
    record.setdefault("response", {}).update({"member_edge_audit": audit})
    record.setdefault("qa", {})["member_edge_audit"] = audit
    record["daily"] = daily_metrics(hourly)
    return record


def long_range_daily_member_values(hourly: dict) -> tuple[dt.date, dict[int, dict[str, dict]]]:
    times = hourly.get("time") or []
    if not times:
        raise ValueError("long-range hourly time is empty")
    origin_date = parse_local_api_time(times[0]).date()
    groups: dict[str, list[int]] = {}
    for index, value in enumerate(times):
        day = parse_local_api_time(value).date().isoformat()
        groups.setdefault(day, []).append(index)
    temperature_keys = ensemble_series_keys(hourly, "temperature_2m")
    daily_by_lead: dict[int, dict[str, dict]] = {}
    for day, indices in sorted(groups.items()):
        day_date = dt.date.fromisoformat(day)
        lead_day = (day_date - origin_date).days
        daily_by_lead[lead_day] = {}
        for key in temperature_keys:
            temperatures = _complete_values_for_indices(hourly, key, indices)
            suffix = key.removeprefix("temperature_2m")
            precipitation = _complete_values_for_indices(hourly, f"precipitation{suffix}", indices)
            snowfall = _complete_values_for_indices(hourly, f"snowfall{suffix}", indices)
            gusts = _complete_values_for_indices(hourly, f"wind_gusts_10m{suffix}", indices)
            if not temperatures:
                continue
            daily_by_lead[lead_day][key] = {
                "date": day,
                "temperature_mean_c": round(mean(temperatures), 3),
                "temperature_min_c": round(min(temperatures), 3),
                "precipitation_mm": round(sum(precipitation), 3) if precipitation else None,
                "snowfall_cm": round(sum(snowfall), 3) if snowfall else None,
                "wind_gust_max_kmh": round(max(gusts), 3) if gusts else None,
            }
    return origin_date, daily_by_lead


def long_range_horizon_check(daily_by_lead: dict[int, dict[str, dict]]) -> dict:
    leads = sorted(daily_by_lead)
    expected = list(range(0, LONG_RANGE_LEAD_END + 1))
    contiguous = leads == list(range(leads[0], leads[-1] + 1)) if leads else False
    usable_background = bool(leads) and leads[0] == 0 and contiguous and leads[-1] >= LONG_RANGE_LEAD_START
    status = "PASS" if leads == expected and all(daily_by_lead[lead] for lead in expected) else "PARTIAL" if usable_background else "FAIL"
    return {
        "status": status,
        "expected_lead_day_range": [0, LONG_RANGE_LEAD_END],
        "actual_lead_day_range": [leads[0], leads[-1]] if leads else None,
        "actual_lead_days": len(leads),
        "contiguous": contiguous,
        "usable_background_through_lead_day": leads[-1] if usable_background else None,
        "missing_lead_days": [lead for lead in expected if lead not in daily_by_lead],
    }


def long_range_member_window_values(
    daily_by_lead: dict[int, dict[str, dict]],
    start_lead: int,
    end_lead: int,
) -> dict[str, dict[str, object]]:
    leads = list(range(start_lead, end_lead + 1))
    member_keys = sorted({key for lead in leads for key in daily_by_lead.get(lead, {})})
    values = {}
    for member_key in member_keys:
        days = [daily_by_lead.get(lead, {}).get(member_key) for lead in leads]
        if any(day is None for day in days):
            continue
        def complete_metric(key: str, operation: str) -> float | None:
            metric_values = [day.get(key) for day in days]
            if any(value is None for value in metric_values):
                return None
            if operation == "sum":
                return round(sum(metric_values), 3)
            return round(max(metric_values), 3)

        values[member_key] = {
            "temperature_mean_c": round(mean(day["temperature_mean_c"] for day in days), 3),
            "temperature_min_c": round(min(day["temperature_min_c"] for day in days), 3),
            "precipitation_mm": complete_metric("precipitation_mm", "sum"),
            "snowfall_cm": complete_metric("snowfall_cm", "sum"),
            "wind_gust_max_kmh": complete_metric("wind_gust_max_kmh", "max"),
        }
    return values


def signal_from_support(support: float | None) -> str:
    if support is None:
        return "UNDETERMINED"
    if support >= 0.7:
        return "STRONG"
    if support >= 0.4:
        return "MODERATE"
    if support >= 0.2:
        return "WEAK"
    return "NONE"


def support_for_window_metric(
    member_values: dict[str, dict[str, object]],
    key: str,
    predicate,
) -> tuple[float | None, int]:
    available = [item.get(key) for item in member_values.values() if item.get(key) is not None]
    if not available:
        return None, 0
    return round(sum(predicate(float(value)) for value in available) / len(available), 3), len(available)


def temperature_background(temperature_stats: dict, reference_mean: float | None) -> dict:
    if reference_mean is None or temperature_stats.get("mean") is None:
        return {
            "direction": "UNDETERMINED",
            "strength": "WEAK",
            "reference_status": "UNAVAILABLE",
        }
    delta = float(temperature_stats["mean"]) - reference_mean
    if abs(delta) < 0.5:
        direction = "NEAR_REFERENCE"
    elif delta < 0:
        direction = "COLDER_THAN_REFERENCE"
    else:
        direction = "WARMER_THAN_REFERENCE"
    strength = "STRONG" if abs(delta) >= 3 else "MODERATE" if abs(delta) >= 1.5 else "WEAK"
    return {
        "direction": direction,
        "strength": strength,
        "reference_status": "PASS",
    }


def long_range_temperature_stats(member_values: dict[str, dict[str, object]]) -> dict:
    values = [float(item["temperature_mean_c"]) for item in member_values.values()]
    return ensemble_statistics(values)


def long_range_cold_window_signal(
    current_values: dict[str, dict[str, object]],
    previous_values: dict[str, dict[str, object]],
    start_date: str,
    end_date: str,
) -> tuple[dict, dict]:
    common_members = sorted(set(current_values) & set(previous_values))
    if not common_members:
        return (
            {
                "signal": "UNDETERMINED",
                "window": f"{start_date}/{end_date}",
                "member_support": None,
                "persistence_runs": 0,
            },
            {"status": "NO_ROBUST_SIGNAL", "reason": "NO_PREVIOUS_WINDOW_DATA"},
        )
    changes = [
        float(current_values[key]["temperature_mean_c"])
        - float(previous_values[key]["temperature_mean_c"])
        for key in common_members
    ]
    member_support = sum(change <= -1.0 for change in changes) / len(changes)
    current_stats = long_range_temperature_stats({key: current_values[key] for key in common_members})
    previous_stats = long_range_temperature_stats({key: previous_values[key] for key in common_members})
    mean_change = current_stats["mean"] - previous_stats["mean"]
    median_change = current_stats["median"] - previous_stats["median"]
    spread = current_stats["spread"] or 0
    if mean_change <= -3 and median_change <= -2 and member_support >= 0.7 and spread <= 8:
        signal = "STRONG"
    elif mean_change <= -1.5 and median_change <= -1 and member_support >= 0.6 and spread <= 10:
        signal = "MODERATE"
    elif mean_change <= -0.8 and member_support >= 0.5 and spread <= 12:
        signal = "WEAK"
    else:
        signal = "NONE"
    return (
        {
            "signal": signal,
            "window": f"{start_date}/{end_date}",
            "member_support": round(member_support, 3),
            "persistence_runs": 0,
        },
        {
            "status": "ASSESSED",
            "mean_change_c": round(mean_change, 3),
            "median_change_c": round(median_change, 3),
            "spread_c": spread,
            "member_count": len(common_members),
        },
    )


def long_range_uncertainty(
    temperature_stats: dict,
    event_supports: list[float | None],
    start_lead: int,
) -> tuple[str, list[str]]:
    level = 0
    drivers = []
    spread = temperature_stats.get("spread")
    if spread is None:
        level = 3
        drivers.append("temperature_spread_unavailable")
    elif spread > 10:
        level = max(level, 3)
        drivers.append("temperature_spread")
    elif spread > 7:
        level = max(level, 2)
        drivers.append("temperature_spread")
    elif spread > 4:
        level = max(level, 1)
        drivers.append("temperature_spread")
    disagreement = [support for support in event_supports if support is not None and 0.25 <= support <= 0.75]
    if disagreement:
        level = max(level, 2)
        drivers.append("member_event_disagreement")
    if any(support is None for support in event_supports):
        level = max(level, 2)
        drivers.append("event_variable_horizon")
    if start_lead >= 28:
        level = max(level, 2)
        drivers.append("longer_lead_time")
    if not drivers:
        drivers.append("long_range_horizon")
    return LONG_RANGE_UNCERTAINTY_ORDER[level], drivers


def reference_mean_for_window(
    reference_days: dict[str, dict],
    origin_date: dt.date,
    start_lead: int,
    end_lead: int,
) -> tuple[float | None, int]:
    values = []
    for lead in range(start_lead, end_lead + 1):
        target = origin_date + dt.timedelta(days=lead)
        reference = reference_days.get(f"2025-{target.month:02d}-{target.day:02d}")
        if reference and reference.get("complete") and metric_value(reference, "temperature_mean_c") is not None:
            values.append(metric_value(reference, "temperature_mean_c"))
    return (round(mean(values), 3), len(values)) if values else (None, 0)


def build_long_range_windows(
    origin_date: dt.date,
    daily_by_lead: dict[int, dict[str, dict]],
    reference_days: dict[str, dict],
    max_date: dt.date | None = None,
) -> list[dict]:
    windows = []
    for start_lead, end_lead in long_range_window_definitions():
        effective_end_lead = end_lead
        if max_date is not None:
            max_lead = (max_date - origin_date).days
            if max_lead < start_lead:
                continue
            effective_end_lead = min(end_lead, max_lead)
        current_values = long_range_member_window_values(daily_by_lead, start_lead, effective_end_lead)
        start_date = origin_date + dt.timedelta(days=start_lead)
        end_date = origin_date + dt.timedelta(days=effective_end_lead)
        if not current_values:
            windows.append({
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "horizon_class": f"D{start_lead}_D{effective_end_lead}",
                "requested_horizon_class": f"D{start_lead}_D{end_lead}",
                "confidence": "VERY_LOW",
                "status": "UNAVAILABLE",
                "reason": "WINDOW_DATA_MISSING",
            })
            continue
        temperature_stats = long_range_temperature_stats(current_values)
        reference_mean, reference_days_available = reference_mean_for_window(
            reference_days, origin_date, start_lead, effective_end_lead
        )
        previous_values = long_range_member_window_values(daily_by_lead, start_lead - 3, start_lead - 1)
        cold_signal, cold_diagnostics = long_range_cold_window_signal(
            current_values,
            previous_values,
            start_date.isoformat(),
            end_date.isoformat(),
        )
        precipitation_support, precipitation_members = support_for_window_metric(
            current_values,
            "precipitation_mm",
            lambda value: value >= 1,
        )
        snowfall_support, snowfall_members = support_for_window_metric(
            current_values,
            "snowfall_cm",
            lambda value: value > 0.1,
        )
        wind_support, wind_members = support_for_window_metric(
            current_values,
            "wind_gust_max_kmh",
            lambda value: value >= 50,
        )
        wet_snow_values = [
            item
            for item in current_values.values()
            if item.get("snowfall_cm") is not None and item.get("temperature_min_c") is not None
        ]
        wet_snow_members = len(wet_snow_values)
        wet_snow_support = (
            round(
                sum(
                    float(item["snowfall_cm"]) > 0.1 and float(item["temperature_min_c"]) <= 2
                    for item in wet_snow_values
                ) / wet_snow_members,
                3,
            )
            if wet_snow_values
            else None
        )
        coarse_thresholds = {}
        for threshold in (5.0, 2.0, 0.0):
            support = sum(
                float(item["temperature_min_c"]) < threshold
                for item in current_values.values()
            ) / len(current_values)
            coarse_thresholds[f"below_{int(threshold)}c"] = {
                "member_support": round(support, 3),
                "threshold_c": threshold,
                "definition": "at least one coarse-grid daily minimum in this 3-day window",
            }
        uncertainty, uncertainty_drivers = long_range_uncertainty(
            temperature_stats,
            [precipitation_support, snowfall_support, wind_support, wet_snow_support],
            start_lead,
        )
        confidence = "VERY_LOW" if uncertainty in {"HIGH", "VERY_HIGH"} or start_lead >= 28 else "LOW"
        windows.append({
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "horizon_class": f"D{start_lead}_D{effective_end_lead}",
            "requested_horizon_class": f"D{start_lead}_D{end_lead}",
            "confidence": confidence,
            "status": "OK",
            "temperature_distribution_c": temperature_stats,
            "temperature_background": temperature_background(temperature_stats, reference_mean),
            "historical_reference": {
                "kind": "historical_reference",
                "model": "ECMWF IFS 9 km historical weather / analysis",
                "mean_temperature_c": reference_mean,
                "days_available": reference_days_available,
            },
            "cold_window_signal": cold_signal,
            "precipitation_background": {
                "signal": signal_from_support(precipitation_support),
                "member_support": precipitation_support,
                "available_members": precipitation_members,
                "threshold": "window precipitation total >= 1 mm",
            },
            "snow_background": {
                "signal": signal_from_support(snowfall_support),
                "member_support": snowfall_support,
                "available_members": snowfall_members,
                "threshold": "window snowfall total > 0.1 cm",
            },
            "wet_snow_assessment": {
                "status": "UNAVAILABLE" if wet_snow_support is None else "COARSE_POTENTIAL" if wet_snow_support else "NO_SIGNAL",
                "member_support": wet_snow_support,
                "available_members": wet_snow_members,
                "reason": "coarse 0.5 degree member overlap of snowfall and <=2C daily minimum; not local phase certainty",
            },
            "coarse_grid_threshold_signal": {
                "usable_for_local_absolute_temperature": False,
                "thresholds": coarse_thresholds,
                "reason": "0.5 degree ensemble grid cannot represent village-level absolute temperature",
            },
            "strong_wind_background": {
                "signal": signal_from_support(wind_support),
                "member_support": wind_support,
                "available_members": wind_members,
                "threshold": "window maximum gust >= 50 km/h",
            },
            "forecast_uncertainty": uncertainty,
            "uncertainty_drivers": uncertainty_drivers,
            "diagnostics": {
                "cold_window": cold_diagnostics,
                "member_count": len(current_values),
            },
            "signal_evolution": {
                "status": "INSUFFICIENT_HISTORY",
                "runs_seen": 0,
                "trend": "UNDETERMINED",
            },
        })
    return windows


def load_long_range_snapshots(
    current_date: dt.date,
    limit: int = 5,
    namespace: str | None = None,
) -> list[dict]:
    snapshots = []
    if not ARCHIVE_DIR.exists():
        return snapshots
    pattern = f"*/{namespace}/long_range.json" if namespace else "*/long_range.json"
    for path in ARCHIVE_DIR.glob(pattern):
        archive_date_path = path.parent.parent if namespace else path.parent
        try:
            archive_date = dt.date.fromisoformat(archive_date_path.name)
        except ValueError:
            continue
        if archive_date >= current_date:
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("status") in {"OK", "PARTIAL"}:
            snapshots.append(value)
    snapshots.sort(key=lambda value: value.get("generated_at", ""), reverse=True)
    return snapshots[:limit]


def apply_signal_evolution(region_id: str, windows: list[dict], snapshots: list[dict]) -> list[dict]:
    for window in windows:
        if window.get("status") == "UNAVAILABLE" or "cold_window_signal" not in window:
            window["signal_evolution"] = {
                "status": "INSUFFICIENT_HISTORY",
                "runs_seen": 0,
                "trend": "UNDETERMINED",
            }
            continue
        current_signal = window.get("cold_window_signal", {}).get("signal")
        matches = []
        for snapshot in snapshots:
            region = (snapshot.get("regions") or {}).get(region_id) or {}
            match = next(
                (item for item in region.get("windows", []) if item.get("horizon_class") == window.get("horizon_class")),
                None,
            )
            if match:
                matches.append(match)
        previous_signals = [item.get("cold_window_signal", {}).get("signal") for item in matches]
        previous_signal = previous_signals[0] if previous_signals else None
        persistence_runs = 1 if current_signal in {"WEAK", "MODERATE", "STRONG"} else 0
        for signal in previous_signals:
            if signal in {"WEAK", "MODERATE", "STRONG"} and persistence_runs:
                persistence_runs += 1
            else:
                break
        if not matches:
            evolution_status = "INSUFFICIENT_HISTORY"
            trend = "UNDETERMINED"
        elif current_signal == "NONE" and previous_signal in {"WEAK", "MODERATE", "STRONG"}:
            evolution_status = "DISAPPEARED"
            trend = "WEAKENING"
        elif current_signal in {"WEAK", "MODERATE", "STRONG"} and previous_signal not in {"WEAK", "MODERATE", "STRONG"}:
            evolution_status = "NEW"
            trend = "STRENGTHENING"
        elif current_signal not in LONG_RANGE_SIGNAL_ORDER or previous_signal not in LONG_RANGE_SIGNAL_ORDER:
            evolution_status = "INSUFFICIENT_HISTORY"
            trend = "UNDETERMINED"
        else:
            current_rank = LONG_RANGE_SIGNAL_ORDER.index(current_signal) if current_signal in LONG_RANGE_SIGNAL_ORDER else 0
            previous_rank = LONG_RANGE_SIGNAL_ORDER.index(previous_signal) if previous_signal in LONG_RANGE_SIGNAL_ORDER else 0
            current_start = dt.date.fromisoformat(window["start_date"])
            previous_start = dt.date.fromisoformat(matches[0]["start_date"])
            if abs((current_start - previous_start).days) > 1:
                evolution_status = "SHIFTING"
                trend = "SHIFTING"
            elif current_rank > previous_rank:
                evolution_status = "STRENGTHENING"
                trend = "STRENGTHENING"
            elif current_rank < previous_rank:
                evolution_status = "WEAKENING"
                trend = "WEAKENING"
            else:
                evolution_status = "PERSISTENT" if current_signal != "NONE" else "INSUFFICIENT_HISTORY"
                trend = "STABLE" if evolution_status == "PERSISTENT" else "UNDETERMINED"
        window["cold_window_signal"]["persistence_runs"] = persistence_runs
        window["signal_evolution"] = {
            "status": evolution_status,
            "runs_seen": len(matches) + 1,
            "trend": trend,
        }
        if evolution_status == "SHIFTING":
            uncertainty = window.get("forecast_uncertainty", "VERY_HIGH")
            current_level = LONG_RANGE_UNCERTAINTY_ORDER.index(uncertainty) if uncertainty in LONG_RANGE_UNCERTAINTY_ORDER else 3
            upgraded_level = min(3, max(2, current_level + 1))
            window["forecast_uncertainty"] = LONG_RANGE_UNCERTAINTY_ORDER[upgraded_level]
            window.setdefault("uncertainty_drivers", []).append("run_to_run_shift")
    return windows


def highest_signal(windows: list[dict], path: tuple[str, ...]) -> str:
    values = []
    for window in windows:
        value: object = window
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value in LONG_RANGE_SIGNAL_ORDER:
            values.append(value)
    return max(values, key=LONG_RANGE_SIGNAL_ORDER.index) if values else "UNDETERMINED"


def long_range_overall(windows: list[dict]) -> dict:
    directions = [
        window.get("temperature_background", {}).get("direction")
        for window in windows
        if window.get("temperature_background", {}).get("direction") in {
            "COLDER_THAN_REFERENCE",
            "NEAR_REFERENCE",
            "WARMER_THAN_REFERENCE",
        }
    ]
    if directions:
        counts = {value: directions.count(value) for value in set(directions)}
        top_count = max(counts.values())
        top_directions = [value for value, count in counts.items() if count == top_count]
        direction = top_directions[0] if len(top_directions) == 1 else "UNDETERMINED"
    else:
        direction = "UNDETERMINED"
    notable = []
    for window in windows:
        signals = []
        if window.get("cold_window_signal", {}).get("signal") in {"WEAK", "MODERATE", "STRONG"}:
            signals.append("COLD_WINDOW")
        if window.get("snow_background", {}).get("signal") in {"WEAK", "MODERATE", "STRONG"}:
            signals.append("SNOW_BACKGROUND")
        if window.get("precipitation_background", {}).get("signal") in {"WEAK", "MODERATE", "STRONG"}:
            signals.append("PRECIPITATION_BACKGROUND")
        if window.get("strong_wind_background", {}).get("signal") in {"WEAK", "MODERATE", "STRONG"}:
            signals.append("STRONG_WIND_BACKGROUND")
        if signals:
            notable.append({
                "start_date": window["start_date"],
                "end_date": window["end_date"],
                "signals": signals,
            })
    uncertainty = max(
        (window.get("forecast_uncertainty") for window in windows if window.get("forecast_uncertainty") in LONG_RANGE_UNCERTAINTY_ORDER),
        key=LONG_RANGE_UNCERTAINTY_ORDER.index,
        default="VERY_HIGH",
    )
    return {
        "temperature": {"direction": direction},
        "cold_air": {"signal": highest_signal(windows, ("cold_window_signal", "signal"))},
        "moisture": {"signal": highest_signal(windows, ("precipitation_background", "signal"))},
        "snow": {"signal": highest_signal(windows, ("snow_background", "signal"))},
        "wind": {"signal": highest_signal(windows, ("strong_wind_background", "signal"))},
        "confidence": "VERY_LOW" if uncertainty in {"HIGH", "VERY_HIGH"} else "LOW",
        "uncertainty": uncertainty,
        "notable_windows": notable,
    }


def run_long_range_reference(
    config: dict,
    point: dict,
    client: ApiClient,
    origin_date: dt.date,
    generated_at: str,
    data_date: str,
    *,
    refresh_history: bool = False,
    cache_dir: Path | None = None,
) -> dict:
    start_date = origin_date + dt.timedelta(days=LONG_RANGE_LEAD_START)
    end_date = origin_date + dt.timedelta(days=LONG_RANGE_LEAD_END)
    try:
        reference_start = dt.date(2025, start_date.month, start_date.day)
        reference_end = dt.date(2025, end_date.month, end_date.day)
    except ValueError as error:
        return {
            "status": "FAILED",
            "reason": f"REFERENCE_DATE_INVALID:{error}",
            "daily": [],
            "record": None,
        }
    record = history_cache_record_or_fetch(
        config,
        client,
        point,
        2025,
        reference_start.isoformat(),
        reference_end.isoformat(),
        refresh_history=refresh_history,
        cache_dir=cache_dir,
        log_label=f"{point['id']}:LONG_REFERENCE 2025",
    )
    if record.get("status") == "PASS":
        log(f"[{point['id']}] LONG_REFERENCE 2025 OK")
    return {
        "status": "PASS" if record.get("status") == "PASS" else "FAILED",
        "daily": record.get("daily", []),
        "record": record,
        "period_start": reference_start.isoformat(),
        "period_end": reference_end.isoformat(),
    }


def run_long_range(
    config: dict,
    client: ApiClient,
    generated_at: str,
    data_date: str,
    now_local: dt.datetime,
    archive_namespace: str | None = None,
    refresh_history: bool = False,
    cache_dir: Path | None = None,
) -> dict:
    points = active_points(config)
    forecast_records: dict[str, dict] = {}
    raw_references: dict[str, dict] = {}
    active_core_ids = []
    for region_id in core_region_ids(config):
        core_id = config["regions"][region_id].get("core_point_id")
        point = points.get(core_id) if core_id else None
        if not point:
            log(f"[{region_id}] LONG_RANGE SKIPPED: PROVISIONAL")
            continue
        active_core_ids.append(core_id)
        record = fetch_point(
            client,
            point=point,
            source="Open-Meteo",
            endpoint=OPEN_METEO_ENDPOINTS["ensemble"],
            model=LONG_RANGE_MODEL,
            params=base_weather_params(
                point,
                models=LONG_RANGE_MODEL_ID,
                forecast_days=LONG_RANGE_REQUESTED_FORECAST_DAYS,
            ),
            variables=LONG_RANGE_VARIABLES,
            # Temperature is the core horizon signal. Other event variables may
            # legitimately end earlier; their per-variable edge availability is
            # audited and their late windows remain UNDETERMINED.
            required_variables=["temperature_2m"],
            grid_limit_km=LONG_RANGE_GRID_QA_LIMIT_KM,
            log_label=f"{core_id}:LONG_RANGE",
            accepted_model_values=(LONG_RANGE_MODEL_ID, LONG_RANGE_MODEL),
            accepted_model_ids=(LONG_RANGE_MODEL_ID,),
            max_forecast_date=point_forecast_end_date(point),
        )
        if record.get("status") == "PASS":
            record.setdefault("qa", {})["grid_scale_class"] = "coarse_ensemble"
            record = trim_long_range_member_edges(record)
            members_valid, member_check = validate_long_range_members(record)
            record.setdefault("qa", {})["long_range_member_check"] = member_check
            if not members_valid:
                record["status"] = "INVALID"
                record["qa"]["valid"] = False
                record["qa"]["final_status"] = "INVALID"
                record["qa"]["reason"] = "LONG_RANGE_MEMBER_SCHEMA_INVALID"
                log(f"[{core_id}] LONG_RANGE MEMBER QA FAIL")
        forecast_records[region_id] = record

    snapshots = load_long_range_snapshots(now_local.date(), namespace=archive_namespace)
    regions = {}
    successful_points = 0
    partial_points = 0
    failed_points = 0
    history_cache_info = history_cache_stats()
    forecast_horizons = []
    member_counts = []
    for region_id in core_region_ids(config):
        region_config = config["regions"][region_id]
        core_id = region_config.get("core_point_id")
        point = points.get(core_id) if core_id else None
        if not point:
            regions[region_id] = {
                "status": "UNAVAILABLE",
                "usable_for_main_chain": False,
                "reason": "NO_VERIFIED_CORE_POINT",
                "windows": [],
                "overall_16_35d": {"temperature": {"direction": "UNDETERMINED"}, "confidence": "VERY_LOW"},
                "qa": {"final_status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
            }
            continue
        record = forecast_records.get(region_id) or {}
        if record.get("status") != "PASS":
            failed_points += 1
            regions[region_id] = {
                "status": "FAILED",
                "usable_for_main_chain": True,
                "point_id": core_id,
                "visit_date": region_config.get("primary_visit_date"),
                "windows": [],
                "overall_16_35d": {"temperature": {"direction": "UNDETERMINED"}, "confidence": "VERY_LOW"},
                "reason": (record.get("qa") or {}).get("reason", "OPEN_METEO_LONG_RANGE_NOT_AVAILABLE"),
                "qa": record.get("qa") or {"final_status": "INVALID", "reason": "OPEN_METEO_LONG_RANGE_NOT_AVAILABLE"},
            }
            continue
        origin_date, daily_by_lead = long_range_daily_member_values(record["hourly"])
        horizon_check = long_range_horizon_check(daily_by_lead)
        record.setdefault("qa", {})["long_range_horizon_check"] = horizon_check
        if horizon_check["status"] == "FAIL":
            record["status"] = "INVALID"
            record["qa"]["valid"] = False
            record["qa"]["final_status"] = "INVALID"
            record["qa"]["reason"] = "LONG_RANGE_HORIZON_INVALID"
            failed_points += 1
            regions[region_id] = {
                "status": "FAILED",
                "usable_for_main_chain": True,
                "point_id": core_id,
                "visit_date": region_config.get("primary_visit_date"),
                "windows": [],
                "overall_16_35d": {"temperature": {"direction": "UNDETERMINED"}, "confidence": "VERY_LOW"},
                "reason": "LONG_RANGE_HORIZON_INVALID",
                "qa": record.get("qa"),
            }
            continue
        successful_points += 1
        last_time = (record.get("hourly") or {}).get("time", [None])[-1]
        forecast_horizons.append(horizon_check["actual_lead_days"])
        member_check = record.get("qa", {}).get("long_range_member_check", {})
        member_counts.extend(member_check.get("actual_member_counts_by_variable", {}).values())
        variable_horizon_partial = bool(member_check.get("edge_truncated_variables"))
        reference = run_long_range_reference(
            config,
            point,
            client,
            origin_date,
            generated_at,
            data_date,
            refresh_history=refresh_history,
            cache_dir=cache_dir,
        )
        if reference.get("record"):
            update_history_cache_stats(history_cache_info, reference["record"])
        raw_references[region_id] = reference.get("record")
        reference_days = {day["date"]: day for day in reference.get("daily", [])}
        windows = build_long_range_windows(
            origin_date,
            daily_by_lead,
            reference_days,
            max_date=point_forecast_end_date(point),
        )
        windows = apply_signal_evolution(region_id, windows, snapshots)
        region_status = "OK" if (
            reference.get("status") == "PASS"
            and horizon_check["status"] == "PASS"
            and not variable_horizon_partial
        ) else "PARTIAL"
        if region_status == "PARTIAL":
            partial_points += 1
        regions[region_id] = {
            "status": region_status,
            "usable_for_main_chain": True,
            "point_id": core_id,
            "visit_date": region_config.get("primary_visit_date"),
            "forecast_origin_date": origin_date.isoformat(),
            "forecast_last_timestamp": last_time,
            "windows": windows,
            "overall_16_35d": long_range_overall(windows),
            "historical_reference": {
                "status": reference.get("status"),
                "period_start": reference.get("period_start"),
                "period_end": reference.get("period_end"),
                "kind": "historical_reference",
            },
            "qa": {
                "forecast": record.get("qa"),
                "request_coordinate": (record.get("request") or {}).get("coordinate"),
                "returned_grid_coordinate": (record.get("response") or {}).get("grid_coordinate"),
                "returned_elevation": (record.get("response") or {}).get("returned_elevation"),
                "historical_reference": (reference.get("record") or {}).get("qa"),
                "grid_scale_class": "coarse_ensemble",
                "variable_horizon_partial": variable_horizon_partial,
                "final_status": "PASS" if region_status == "OK" else "PARTIAL",
            },
        }

    for region_id, region_config in config["regions"].items():
        if region_id in regions:
            continue
        log(f"[{region_id}] LONG_RANGE SKIPPED: PROVISIONAL")
        regions[region_id] = {
            "status": "UNAVAILABLE",
            "usable_for_main_chain": False,
            "reason": "NO_VERIFIED_CORE_POINT",
            "windows": [],
            "overall_16_35d": {"temperature": {"direction": "UNDETERMINED"}, "confidence": "VERY_LOW"},
            "qa": {"final_status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
        }

    if not active_core_ids or failed_points == len(active_core_ids):
        status = "FAILED"
    elif failed_points or partial_points:
        status = "PARTIAL"
    else:
        status = "OK"
    actual_horizon = min(forecast_horizons) if forecast_horizons else None
    actual_members = min(member_counts) if member_counts else None
    horizon_status = (
        "PASS"
        if actual_horizon is not None and actual_horizon >= LONG_RANGE_REQUESTED_FORECAST_DAYS
        else "PARTIAL"
        if actual_horizon and actual_horizon > LONG_RANGE_LEAD_START
        else "FAILED"
    )
    return module_header(
        "long_range_background",
        generated_at,
        data_date,
        status,
        source="Open-Meteo",
        endpoint=OPEN_METEO_ENDPOINTS["ensemble"],
        model=LONG_RANGE_MODEL,
        model_id=LONG_RANGE_MODEL_ID,
        model_documentation=LONG_RANGE_ENDPOINT_DOC,
        model_registry_documentation=LONG_RANGE_MODEL_REGISTRY_DOC,
        ensemble_members=actual_members or LONG_RANGE_ENSEMBLE_MEMBERS,
        expected_ensemble_members=LONG_RANGE_ENSEMBLE_MEMBERS,
        requested_forecast_days=LONG_RANGE_REQUESTED_FORECAST_DAYS,
        forecast_horizon_days=actual_horizon,
        forecast_lead_days=max(0, (actual_horizon or 1) - 1),
        forecast_horizon_status=horizon_status,
        forecast_last_timestamp=max(
            (region.get("forecast_last_timestamp") for region in regions.values() if region.get("forecast_last_timestamp")),
            default=None,
        ),
        native_resolution="0.5° (~50 km)",
        native_time_resolution="3-hourly",
        time_resolution_note="Open-Meteo documents hourly interpolation for ensemble output; the GFS 0.5° product is natively 3-hourly.",
        coverage="global; Xinjiang is within the global product domain",
        aggregation={
            "type": "fixed_3_day_blocks",
            "lead_day_range": f"D{LONG_RANGE_LEAD_START}_D{LONG_RANGE_LEAD_END}",
            "windows": [f"D{start}_D{end}" for start, end in long_range_window_definitions()],
            "hourly_values_in_public_artifact": False,
        },
        interpretation_boundary=config.get(
            "long_range_interpretation_boundary",
            "16-35 day background signal only; not a date-level precise forecast and not a direct phenology lead/lag calculation.",
        ),
        regions=regions,
        excluded_points=excluded_points(config),
        raw_references=raw_references,
        raw_points=forecast_records,
        history_cache={
            "enabled": True,
            "directory": history_cache_relative_path(Path(cache_dir) if cache_dir is not None else HISTORY_CACHE_DIR),
            "refresh_requested": refresh_history,
            **history_cache_info,
        },
        successful_points=successful_points,
        partial_points=partial_points,
        failed_points=failed_points,
        qa={
            "grid_scale_class": "coarse_ensemble",
            "expected_ensemble_members": LONG_RANGE_ENSEMBLE_MEMBERS,
            "actual_ensemble_members": actual_members,
            "actual_horizon_days_including_today": actual_horizon,
            "horizon_status": horizon_status,
            "forecast_lead_days": max(0, (actual_horizon or 1) - 1),
            "last_available_timestamp": max(
                (region.get("forecast_last_timestamp") for region in regions.values() if region.get("forecast_last_timestamp")),
                default=None,
            ),
            "region_statuses": {region_id: region.get("status") for region_id, region in regions.items()},
        },
    )


def select_single_run_target(region_config: dict, now_local: dt.datetime) -> tuple[str, str, str | None]:
    raw_visit_date = region_config.get("primary_visit_date")
    visit_date = dt.date.fromisoformat(raw_visit_date) if raw_visit_date else None
    if visit_date:
        visit_target = dt.datetime.combine(visit_date, dt.time(5, 0), tzinfo=LOCAL_TZ)
        days_ahead = (visit_target - now_local).total_seconds() / 86400
        if 0 <= days_ahead <= 9:
            return (
                visit_target.strftime("%Y-%m-%dT%H:%M"),
                "primary_visit_date_within_10_day_run_horizon",
                visit_date.isoformat(),
            )
    rolling_date = now_local.date() + dt.timedelta(days=3)
    raw_cutoff = region_config.get("forecast_end_date")
    if raw_cutoff:
        rolling_date = min(rolling_date, dt.date.fromisoformat(str(raw_cutoff)))
    rolling = dt.datetime.combine(rolling_date, dt.time(5, 0), tzinfo=LOCAL_TZ)
    policy = "rolling_plus_3_days_before_visit_window" if visit_date else "rolling_plus_3_days_no_visit_date"
    return rolling.strftime("%Y-%m-%dT%H:%M"), policy, visit_date.isoformat() if visit_date else None


def candidate_single_runs(now_utc: dt.datetime, count: int = 8) -> list[dt.datetime]:
    cycle_hour = (now_utc.hour // 6) * 6
    latest_cycle = now_utc.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)
    latest_available_estimate = latest_cycle - dt.timedelta(hours=6)
    return [latest_available_estimate - dt.timedelta(hours=6 * index) for index in range(count)]


def target_values(record: dict, target_time: str) -> dict:
    hourly = record.get("hourly") or {}
    times = hourly.get("time") or []
    try:
        index = times.index(target_time)
    except ValueError:
        return {"status": "INVALID", "reason": "TARGET_TIME_NOT_IN_RUN"}
    values = {}
    for variable in HRES_VARIABLES:
        actual_variable = record.get("solar_variable") if variable == "sunshine_duration" else variable
        if not actual_variable:
            continue
        series = hourly.get(actual_variable) or []
        values[actual_variable] = series[index] if index < len(series) else None
    if any(value is None for value in values.values()):
        return {"status": "INVALID", "reason": "TARGET_VALUE_MISSING", "values": values}
    return {"status": "PASS", "time": target_time, "values": values}


def run_single_runs(config: dict, client: ApiClient, generated_at: str, data_date: str, now_utc: dt.datetime) -> dict:
    points = active_points(config)
    now_local = now_utc.astimezone(LOCAL_TZ)
    regions: dict[str, dict] = {}
    successful_run_entries = []
    candidates = candidate_single_runs(now_utc, 8)
    for region_id in core_region_ids(config):
        region_config = config["regions"][region_id]
        core_id = region_config.get("core_point_id")
        point = points.get(core_id) if core_id else None
        if not point:
            log(f"[{region_id}] SINGLE_RUNS SKIPPED: PROVISIONAL")
            regions[region_id] = {
                "status": "SKIPPED",
                "usable_for_main_chain": False,
                "visit_date": region_config.get("primary_visit_date"),
                "runs": [],
            }
            continue
        target_time, target_policy, visit_date = select_single_run_target(region_config, now_local)
        run_entries = []
        for candidate in candidates:
            run_param = candidate.strftime("%Y-%m-%dT%H:%M")
            record = fetch_point(
                client,
                point=point,
                source="Open-Meteo",
                endpoint=OPEN_METEO_ENDPOINTS["single_runs"],
                model="ECMWF IFS HRES 9 km",
                params=base_weather_params(point, models="ecmwf_ifs", run=run_param, forecast_days=10),
                variables=HRES_VARIABLES,
                required_variables=[value for value in HRES_VARIABLES if value != "sunshine_duration"],
                grid_limit_km=HRES_GRID_QA_LIMIT_KM,
                log_label=f"{core_id}:SINGLE_RUN {run_param}",
                precision_module="single_runs",
                model_run_initialization=iso_utc(candidate),
                max_forecast_date=point_forecast_end_date(point),
            )
            target = target_values(record, target_time) if record.get("status") == "PASS" else {"status": "INVALID", "reason": "RUN_INVALID"}
            entry_status = "PASS" if record.get("status") == "PASS" and target.get("status") == "PASS" else "INVALID"
            if entry_status == "PASS":
                successful_run_entries.append({"region": region_id, "entry": {"init_time": iso_utc(candidate), "target": target}})
            run_entries.append({
                "init_time": iso_utc(candidate),
                "status": entry_status,
                "target_time": target_time,
                "target": target,
                "record": record,
            })
        successful = [entry for entry in run_entries if entry["status"] == "PASS"]
        latest_change = {"status": "UNDETERMINED", "value_c": None, "latest_init_time": None, "previous_init_time": None}
        if len(successful) >= 2:
            latest_value = successful[0]["target"]["values"].get("temperature_2m")
            previous_value = successful[1]["target"]["values"].get("temperature_2m")
            if isinstance(latest_value, (int, float)) and isinstance(previous_value, (int, float)):
                delta = round(float(latest_value) - float(previous_value), 3)
                latest_change = {
                    "status": "UP" if delta > 0 else "DOWN" if delta < 0 else "NO_CHANGE",
                    "value_c": delta,
                    "latest_init_time": successful[0]["init_time"],
                    "previous_init_time": successful[1]["init_time"],
                }
        region_status = "OK" if len(successful) == len(candidates) else "FAILED"
        regions[region_id] = {
            "status": region_status,
            "usable_for_main_chain": True,
            "point_id": core_id,
            "visit_date": visit_date,
            "target_time": f"{target_time}+08:00",
            "target_policy": target_policy,
            "run_count_requested": len(candidates),
            "run_count_available": len(successful),
            "latest_change": latest_change,
            "runs": run_entries,
        }
    status_values = [item["status"] for item in regions.values() if item.get("usable_for_main_chain")]
    return module_header(
        "single_runs",
        generated_at,
        data_date,
        "OK" if status_values and all(value == "OK" for value in status_values) else "FAILED",
        endpoint=OPEN_METEO_ENDPOINTS["single_runs"],
        model="ECMWF IFS HRES 9 km",
        model_id="ecmwf_ifs",
        run_frequency="00, 06, 12, 18 UTC",
        runs_requested=len(candidates),
        runs=[iso_utc(value) for value in candidates],
        note="Run timestamps are UTC initialization times; API availability follows model distribution delay.",
        regions=regions,
        excluded_points=excluded_points(config),
    )


def first_days(record: dict, start_index: int, count: int) -> list[dict]:
    return [
        day
        for day in (record.get("daily") or [])[start_index : start_index + count]
        if day.get("complete")
    ]


def forecast_0_7d(days: list[dict]) -> dict:
    selected = days[:7]
    cold_windows = []
    for day in selected:
        night_min = metric_value(day, "night_min_c")
        if night_min is not None and night_min < 5:
            cold_windows.append({
                "date": day["date"],
                "night_min_c": night_min,
                "below_5c": night_min < 5,
                "below_2c": night_min < 2,
                "below_0c": night_min < 0,
                "precision_class": day.get("precision_class"),
            })
    precipitation = [metric_value(day, "precipitation_mm") for day in selected]
    snowfall = [metric_value(day, "snowfall_cm") for day in selected]
    gusts = [metric_value(day, "wind_gust_max_kmh") for day in selected]
    nights = [metric_value(day, "night_min_c") for day in selected]
    precipitation = [value for value in precipitation if value is not None]
    snowfall = [value for value in snowfall if value is not None]
    gusts = [value for value in gusts if value is not None]
    nights = [value for value in nights if value is not None]
    solar = [day.get("solar_metric") for day in selected if isinstance(day.get("solar_metric"), dict)]
    solar_values = [item["value"] for item in solar if isinstance(item.get("value"), (int, float))]
    solar_variable = solar[0].get("variable") if solar else None
    return {
        "window_days": len(selected),
        "cold_windows": cold_windows,
        "frost_signal": {
            "night_count_below_15c": sum(value < 15 for value in nights),
            "night_count_below_10c": sum(value < 10 for value in nights),
            "night_count_below_5c": sum(value < 5 for value in nights),
            "night_count_below_2c": sum(value < 2 for value in nights),
            "night_count_below_0c": sum(value < 0 for value in nights),
            "minimum_night_c": round(min(nights), 3) if nights else None,
        },
        "snow_signal": {
            "days_with_snowfall": sum(value > 0.1 for value in snowfall),
            "maximum_snowfall_cm": round(max(snowfall), 3) if snowfall else None,
            "precipitation_total_mm": round(sum(precipitation), 3) if precipitation else None,
        },
        "wind_signal": {
            "days_gust_ge_35_kmh": sum(value >= 35 for value in gusts),
            "days_gust_ge_50_kmh": sum(value >= 50 for value in gusts),
            "maximum_gust_kmh": round(max(gusts), 3) if gusts else None,
        },
        "cloud_sun_signal": {
            "cloud_cover_mean_pct": safe_mean([value for value in (metric_value(day, "cloud_cover_mean_pct") for day in selected) if value is not None]),
            "cloud_cover_low_mean_pct": safe_mean([value for value in (metric_value(day, "cloud_cover_low_mean_pct") for day in selected) if value is not None]),
            "solar_metric": {
                "variable": solar_variable,
                "mean_value": round(mean(solar_values), 3) if solar_values else None,
                "unit": solar[0].get("unit") if solar else None,
            },
        },
        "daily": selected,
    }


def leaf_loss_weather_risk(days: list[dict], as_of_date: dt.date) -> dict:
    selected = days[:7]
    strong_wind_dates = [
        day["date"]
        for day in selected
        if (metric_value(day, "wind_gust_max_kmh") is not None and metric_value(day, "wind_gust_max_kmh") >= 50)
    ]
    wet_snow_dates = [
        day["date"]
        for day in selected
        if (
            metric_value(day, "snowfall_cm") is not None
            and metric_value(day, "snowfall_cm") > 0.1
            and metric_value(day, "temperature_min_c") is not None
            and metric_value(day, "temperature_min_c") <= 2
        )
    ]
    rain_snow_dates = [
        day["date"]
        for day in selected
        if (
            metric_value(day, "precipitation_mm") is not None
            and metric_value(day, "precipitation_mm") >= 1
            and metric_value(day, "snowfall_cm") is not None
            and metric_value(day, "snowfall_cm") > 0.1
        )
    ]
    freeze_dates = [
        day["date"]
        for day in selected
        if metric_value(day, "night_min_c") is not None and metric_value(day, "night_min_c") < 0
    ]
    weighted_after_september_20 = as_of_date >= dt.date(as_of_date.year, 9, 20)
    score = (
        (2 * len(strong_wind_dates) if weighted_after_september_20 else 0)
        + 2 * len(wet_snow_dates)
        + len(rain_snow_dates)
        + len(freeze_dates)
    )
    risk = "HIGH" if score >= 4 else "MEDIUM" if score >= 2 else "LOW"
    drivers = []
    if strong_wind_dates:
        drivers.append("gust")
    if wet_snow_dates:
        drivers.append("wet_snow")
    if rain_snow_dates:
        drivers.append("rain_snow")
    if freeze_dates:
        drivers.append("freeze")
    return {
        "weather_event_risk": risk,
        "drivers": drivers,
        "score": score,
        "seasonal_weighting_applied": weighted_after_september_20,
        "events": {
            "strong_wind_gust_ge_50_kmh_dates": strong_wind_dates,
            "wet_snow_dates": wet_snow_dates,
            "rain_snow_dates": rain_snow_dates,
            "freeze_night_dates": freeze_dates,
        },
        "interpretation": "weather event risk only; it does not determine whether leaves fall",
    }


def forecast_8_15d(days: list[dict]) -> dict:
    selected = days[7:15]
    means = [metric_value(day, "temperature_mean_c") for day in selected]
    means = [value for value in means if value is not None]
    precipitation = [metric_value(day, "precipitation_mm") for day in selected]
    gusts = [metric_value(day, "wind_gust_max_kmh") for day in selected]
    snowfall = [metric_value(day, "snowfall_cm") for day in selected]
    precision = {day.get("precision_class") for day in selected}
    if len(means) >= 2:
        change = means[-1] - means[0]
        temperature_trend = "warming" if change >= 1 else "cooling" if change <= -1 else "flat"
    else:
        change = None
        temperature_trend = "undetermined"
    precipitation = [value for value in precipitation if value is not None]
    gusts = [value for value in gusts if value is not None]
    snowfall = [value for value in snowfall if value is not None]
    return {
        "window_days": len(selected),
        "temperature_trend": temperature_trend,
        "temperature_change_first_to_last_c": round(change, 3) if change is not None else None,
        "moisture_trend": {
            "precipitation_total_mm": round(sum(precipitation), 3) if precipitation else None,
            "snowfall_total_cm": round(sum(snowfall), 3) if snowfall else None,
        },
        "wind_snow_trend": {
            "days_gust_ge_50_kmh": sum(value >= 50 for value in gusts),
            "days_with_snowfall": sum(value > 0.1 for value in snowfall),
        },
        "confidence": "LOW" if "trend_only_6h_plus" in precision or "mixed" in precision else "MEDIUM" if selected else "UNDETERMINED",
        "daily": selected,
    }


def agreement_label(condition: bool | None) -> str:
    if condition is None:
        return "UNDETERMINED"
    return "AGREE" if condition else "DISAGREE"


def gfs_crosscheck(hres_record: dict | None, gfs_record: dict | None) -> dict:
    if not hres_record or not gfs_record or hres_record.get("status") != "PASS" or gfs_record.get("status") != "PASS":
        return {
            "temperature_trend_agreement": "UNDETERMINED",
            "cold_window_agreement": "UNDETERMINED",
            "precipitation_agreement": "UNDETERMINED",
            "strong_wind_agreement": "UNDETERMINED",
            "reason": "HRES_OR_GFS_INVALID",
        }
    hres_days = {day["date"]: day for day in first_days(hres_record, 0, 7)}
    gfs_days = {day["date"]: day for day in first_days(gfs_record, 0, 7)}
    common_dates = sorted(set(hres_days) & set(gfs_days))
    if not common_dates:
        return {"temperature_trend_agreement": "UNDETERMINED", "cold_window_agreement": "UNDETERMINED", "precipitation_agreement": "UNDETERMINED", "strong_wind_agreement": "UNDETERMINED", "reason": "NO_COMMON_DATES"}
    temp_diffs = [
        abs(metric_value(hres_days[date], "temperature_mean_c") - metric_value(gfs_days[date], "temperature_mean_c"))
        for date in common_dates
        if metric_value(hres_days[date], "temperature_mean_c") is not None and metric_value(gfs_days[date], "temperature_mean_c") is not None
    ]
    hres_cold = {
        date
        for date in common_dates
        if metric_value(hres_days[date], "night_min_c") is not None and metric_value(hres_days[date], "night_min_c") < 5
    }
    gfs_cold = {
        date
        for date in common_dates
        if metric_value(gfs_days[date], "night_min_c") is not None and metric_value(gfs_days[date], "night_min_c") < 5
    }
    union = hres_cold | gfs_cold
    jaccard = len(hres_cold & gfs_cold) / len(union) if union else 1.0
    hres_precip = sum((metric_value(hres_days[date], "precipitation_mm") or 0) >= 1 for date in common_dates)
    gfs_precip = sum((metric_value(gfs_days[date], "precipitation_mm") or 0) >= 1 for date in common_dates)
    hres_wind = sum((metric_value(hres_days[date], "wind_gust_max_kmh") or 0) >= 50 for date in common_dates)
    gfs_wind = sum((metric_value(gfs_days[date], "wind_gust_max_kmh") or 0) >= 50 for date in common_dates)
    return {
        "common_dates": common_dates,
        "temperature_trend_agreement": agreement_label(max(temp_diffs) <= 2 if temp_diffs else None),
        "temperature_mean_absolute_difference_c": round(mean(temp_diffs), 3) if temp_diffs else None,
        "cold_window_agreement": agreement_label(jaccard >= 0.5),
        "cold_window_jaccard": round(jaccard, 3),
        "precipitation_agreement": agreement_label(abs(hres_precip - gfs_precip) <= 1),
        "precipitation_days": {"hres": hres_precip, "gfs": gfs_precip},
        "strong_wind_agreement": agreement_label(abs(hres_wind - gfs_wind) <= 1),
        "strong_wind_days": {"hres": hres_wind, "gfs": gfs_wind},
        "interpretation": "cross-check only; HRES and GFS are not averaged",
    }


def unavailable_long_range_summary(reason: str) -> dict:
    return {
        "status": "UNAVAILABLE",
        "reason": reason,
    }


def long_range_summary_for_chatgpt(region: dict | None) -> dict:
    """Expose only coarse 16-35 day labels in summary.json."""
    if not region:
        return unavailable_long_range_summary("LONG_RANGE_MODULE_UNAVAILABLE")
    region_status = region.get("status")
    if region_status == "UNAVAILABLE":
        return unavailable_long_range_summary(region.get("reason", "NO_VERIFIED_CORE_POINT"))
    if region_status == "FAILED":
        return {"status": "FAILED", "reason": region.get("reason", "LONG_RANGE_FETCH_FAILED")}
    overall = region.get("overall_16_35d") or {}
    notable_windows = []
    for window in overall.get("notable_windows", []):
        for signal in window.get("signals", []):
            notable_windows.append({
                "start_date": window.get("start_date"),
                "end_date": window.get("end_date"),
                "signal": signal,
            })
    evolution = [
        {
            "horizon_class": window.get("horizon_class"),
            "start_date": window.get("start_date"),
            "end_date": window.get("end_date"),
            "status": (window.get("signal_evolution") or {}).get("status", "INSUFFICIENT_HISTORY"),
            "runs_seen": (window.get("signal_evolution") or {}).get("runs_seen", 0),
            "trend": (window.get("signal_evolution") or {}).get("trend", "UNDETERMINED"),
        }
        for window in region.get("windows", [])
    ]
    return {
        "status": "PASS" if region_status == "OK" else "PARTIAL",
        "temperature_background": (overall.get("temperature") or {}).get("direction", "UNDETERMINED"),
        "cold_air_signal": (overall.get("cold_air") or {}).get("signal", "UNDETERMINED"),
        "precipitation_signal": (overall.get("moisture") or {}).get("signal", "UNDETERMINED"),
        "snow_signal": (overall.get("snow") or {}).get("signal", "UNDETERMINED"),
        "strong_wind_signal": (overall.get("wind") or {}).get("signal", "UNDETERMINED"),
        "uncertainty": overall.get("uncertainty", "VERY_HIGH"),
        "notable_windows": notable_windows,
        "signal_evolution": evolution,
        "interpretation": "16-35 day background signal only; not a date-level forecast or phenology lead/lag calculation",
    }


def flattened_lightweight_window(window: dict | None) -> dict:
    """Flatten the small window contract; never carry daily/hourly arrays here."""
    if not isinstance(window, dict):
        return {"status": "UNAVAILABLE", "metrics": None, "reason": "WINDOW_UNAVAILABLE"}
    output = {
        key: copy.deepcopy(window.get(key))
        for key in (
            "status",
            "start_date",
            "end_date",
            "expected_days",
            "days_available",
            "missing_dates",
            "incomplete_dates",
            "grid_count",
            "reason",
        )
        if key in window
    }
    metrics = window.get("metrics")
    if isinstance(metrics, dict):
        output.update({key: copy.deepcopy(metrics.get(key)) for key in LIGHTWEIGHT_WINDOW_METRIC_KEYS})
        if "temperature_trend" in metrics:
            output["temperature_trend"] = copy.deepcopy(metrics["temperature_trend"])
        if "solar_variable" in metrics:
            output["solar_variable"] = metrics["solar_variable"]
        if "solar_unit" in metrics:
            output["solar_unit"] = metrics["solar_unit"]
    else:
        output["metrics"] = None
    return output


def _month_day_label(value: str | None) -> str | None:
    if not value:
        return None
    parsed = dt.date.fromisoformat(value)
    return f"{parsed.month}/{parsed.day}"


def _forecast_window_availability_note(window: dict, status: str) -> str | None:
    if status == "OK":
        return None
    if status == "INVALID":
        return "窗口无有效数据，不进入主链"
    if status != "PARTIAL":
        return "窗口不可用，不进入主链"
    pending = sorted({
        value
        for key in ("missing_dates", "incomplete_dates")
        for value in (window.get(key) or [])
        if value
    })
    start = window.get("start_date")
    end = window.get("end_date")
    if not pending or not start or not end:
        return "窗口部分可用，可作趋势参考"
    start_date = dt.date.fromisoformat(start)
    end_date = dt.date.fromisoformat(end)
    first_pending = dt.date.fromisoformat(pending[0])
    expected_pending = []
    cursor = first_pending
    while cursor <= end_date:
        expected_pending.append(cursor.isoformat())
        cursor += dt.timedelta(days=1)
    if first_pending > start_date and pending == expected_pending:
        available_end = first_pending - dt.timedelta(days=1)
        available_range = (
            f"{start_date.month}/{start_date.day}–{available_end.day}"
            if start_date.month == available_end.month
            else f"{_month_day_label(start)}–{_month_day_label(available_end.isoformat())}"
        )
        return (
            f"{available_range}预测，"
            f"{_month_day_label(first_pending.isoformat())}待补"
        )
    pending_label = "、".join(_month_day_label(value) or value for value in pending)
    return f"窗口部分可用，可作趋势参考；待补：{pending_label}"


def forecast_window_view(window: dict | None) -> dict:
    """Expose forecast availability per window instead of gating the region globally."""
    output = flattened_lightweight_window(window)
    status = output.get("status", "UNAVAILABLE")
    raw_window = window if isinstance(window, dict) else {}
    has_metrics = isinstance(raw_window.get("metrics"), dict)
    output["usable_for_main_chain"] = status == "OK"
    output["usable_for_trend_reference"] = status in {"OK", "PARTIAL"} and bool(
        output.get("days_available", 0) and has_metrics
    )
    output["availability_note"] = _forecast_window_availability_note(output, status)
    return output


def unavailable_lightweight_windows(forecast_date: dt.date, reason: str) -> dict:
    return {
        key: flattened_lightweight_window(
            lightweight_window_summary([], definition, allow_partial=True)
            | {"status": "UNAVAILABLE", "reason": reason}
        )
        for key, definition in history_forward_windows_for_year(forecast_date, 2026).items()
    }


def unavailable_forecast_windows(forecast_date: dt.date, reason: str) -> dict:
    return {
        key: forecast_window_view(
            lightweight_window_summary([], definition, allow_partial=True)
            | {"status": "UNAVAILABLE", "reason": reason}
        )
        for key, definition in history_forward_windows_for_year(forecast_date, 2026).items()
    }


def light_sampling_from_history_subregion(subregion: dict) -> dict:
    by_year = subregion.get("sampling", {}).get("by_year", {})
    first = next(iter(by_year.values()), {})
    return {
        "status": subregion.get("status", "INVALID"),
        "requested_points": first.get("requested_points", len(subregion.get("point_ids", []))),
        "verified_points": first.get("verified_points", len(subregion.get("verified_point_ids", []))),
        "unique_model_grids": first.get("unique_model_grids", 0),
        "grid_coordinates": copy.deepcopy(first.get("grid_coordinates", [])),
        "grid_cell_ids": copy.deepcopy(first.get("grid_cell_ids", [])),
        "same_unique_grid_set_across_years": subregion.get("sampling", {}).get("same_unique_grid_set_across_years", False),
        "by_year": {
            year: {
                "status": item.get("status", "INVALID"),
                "unique_model_grids": item.get("unique_model_grids", 0),
                "grid_coordinates": copy.deepcopy(item.get("grid_coordinates", [])),
            }
            for year, item in by_year.items()
        },
        "reason": subregion.get("reason"),
    }


def light_sampling_from_records(
    config: dict,
    point_ids: list[str],
    records: dict[str, dict],
    *,
    minimum_verified_unique_grids: int = 1,
) -> dict:
    sampling = grid_sampling_summary(
        config,
        point_ids,
        records,
        minimum_verified_unique_grids=minimum_verified_unique_grids,
    )
    return {
        key: copy.deepcopy(sampling[key])
        for key in (
            "status",
            "requested_points",
            "verified_points",
            "queried_points",
            "valid_points",
            "failed_points",
            "excluded_point_ids",
            "valid_point_ids",
            "unique_model_grids",
            "grid_coordinates",
            "grid_cell_ids",
            "minimum_verified_unique_grids",
            "reason",
            "deduplication",
        )
        if key in sampling
    }


def point_year_lightweight_views(
    config: dict,
    point_id: str,
    hres: dict,
    history_forward: dict,
    forecast_date: dt.date,
) -> tuple[dict, dict]:
    """Build the small four-year view for a single non-Kanas core point."""
    definitions_2026 = history_forward_windows_for_year(forecast_date, 2026)
    years = {}
    forward_point = (history_forward.get("points") or {}).get(point_id) or {}
    for year in HISTORY_FORWARD_YEARS:
        record = (forward_point.get("years") or {}).get(str(year))
        if record and record.get("status") == "PASS":
            definitions = history_forward_windows_for_year(forecast_date, year)
            years[str(year)] = {
                key: flattened_lightweight_window(
                    lightweight_window_summary(record.get("daily", []), definition, allow_partial=False)
                )
                for key, definition in definitions.items()
            }
        else:
            years[str(year)] = unavailable_lightweight_windows(forecast_date, "HISTORY_FORWARD_POINT_INVALID")
    forecast_record = (hres.get("points") or {}).get(point_id)
    if forecast_record and forecast_record.get("status") == "PASS":
        years["2026"] = {
            key: forecast_window_view(
                lightweight_window_summary(forecast_record.get("daily", []), definition, allow_partial=True)
            )
            for key, definition in definitions_2026.items()
        }
    else:
        years["2026"] = unavailable_forecast_windows(forecast_date, "HRES_POINT_INVALID")
    history_sampling = {}
    for year in HISTORY_FORWARD_YEARS:
        record = (forward_point.get("years") or {}).get(str(year))
        history_sampling[str(year)] = light_sampling_from_records(
            config,
            [point_id],
            {point_id: record} if record else {},
            minimum_verified_unique_grids=1,
        )
    forecast_sampling = light_sampling_from_records(
        config,
        [point_id],
        {point_id: forecast_record} if forecast_record else {},
        minimum_verified_unique_grids=1,
    )
    sampling_status = "OK" if forecast_sampling.get("status") == "OK" and all(
        item.get("status") == "OK" for item in history_sampling.values()
    ) else "PARTIAL"
    sampling = {
        "status": sampling_status,
        "forecast_2026": forecast_sampling,
        "historical_2023_2025": {
            "status": "OK" if all(item.get("status") == "OK" for item in history_sampling.values()) else "FAILED",
            "by_year": history_sampling,
        },
    }
    return years, sampling


def build_region_forecast_subregion(
    config: dict,
    region_id: str,
    subregion_id: str,
    hres: dict,
    forecast_date: dt.date,
) -> dict:
    registry_item = region_subregion_registry(config, region_id).get(subregion_id) or {}
    point_ids = region_subregion_point_ids(config, region_id, subregion_id)
    records = {
        point_id: (hres.get("points") or {}).get(point_id)
        for point_id in region_subregion_point_ids(config, region_id, subregion_id, verified_only=True)
        if (hres.get("points") or {}).get(point_id)
    }
    minimum_grids = registry_item.get("minimum_verified_unique_grids", 1)
    sampling = grid_sampling_summary(
        config,
        point_ids,
        records,
        minimum_verified_unique_grids=minimum_grids,
    )
    unique_records = [entry["record"] for entry in deduplicate_grid_records(list(records.values()))]
    years = {"2026": {"status": "OK", "sampling": light_sampling_from_records(
        config,
        point_ids,
        records,
        minimum_verified_unique_grids=minimum_grids,
    )}}
    definitions = history_forward_windows_for_year(forecast_date, 2026)
    for key, definition in definitions.items():
        years["2026"][key] = aggregate_grid_window(unique_records, definition, allow_partial=True)
        if years["2026"][key].get("status") == "INVALID":
            years["2026"]["status"] = "INVALID"
    if not unique_records:
        status = "INVALID"
    elif sampling.get("status") == "OK" and years["2026"]["status"] == "OK":
        status = "OK"
    else:
        status = "PARTIAL"
    return {
        "subregion": subregion_id,
        "name": registry_item.get("name", subregion_id),
        "status": status,
        "usable_for_main_chain": bool(unique_records)
        and years["2026"].get("d0_7", {}).get("status") == "OK",
        "sampling": light_sampling_from_records(
            config,
            point_ids,
            records,
            minimum_verified_unique_grids=minimum_grids,
        ),
        "years": years,
        "reason": None if status == "OK" else sampling.get("reason") or "HRES_SUBREGION_PARTIAL",
    }


def build_kanas_forecast_subregion(
    config: dict,
    subregion_id: str,
    hres: dict,
    forecast_date: dt.date,
) -> dict:
    return build_region_forecast_subregion(config, "kanas", subregion_id, hres, forecast_date)


def build_hemu_forecast_subregion(
    config: dict,
    subregion_id: str,
    hres: dict,
    forecast_date: dt.date,
) -> dict:
    return build_region_forecast_subregion(config, "hemu", subregion_id, hres, forecast_date)


def combine_region_light_years(
    subregions: dict[str, dict],
    history_composite: dict | None,
    forecast_date: dt.date,
    subregion_keys: tuple[str, ...],
    *,
    region_id: str,
) -> dict:
    years = {}
    for year in HISTORY_FORWARD_YEARS:
        source_year = (history_composite or {}).get("years", {}).get(str(year), {})
        definitions = history_forward_windows_for_year(forecast_date, year)
        years[str(year)] = {
            key: flattened_lightweight_window(source_year.get(key))
            if source_year.get(key)
            else flattened_lightweight_window(
                lightweight_window_summary([], definition, allow_partial=True)
                | {"status": "UNAVAILABLE", "reason": "HISTORY_FORWARD_COMPOSITE_UNAVAILABLE"}
            )
            for key, definition in definitions.items()
        }
    definitions_2026 = history_forward_windows_for_year(forecast_date, 2026)
    forecast_year = {"status": "OK"}
    for key, definition in definitions_2026.items():
        items = []
        for subregion_id in subregion_keys:
            item = subregions.get(subregion_id) or {}
            window = (item.get("years", {}).get("2026", {}) or {}).get(key)
            if not window:
                items.append(
                    lightweight_window_summary([], definition, allow_partial=True)
                    | {"reason": "SUBREGION_WINDOW_UNAVAILABLE"}
                )
            else:
                items.append(window)
        forecast_year[key] = equal_mean_subregion_window(items, definition, region_id=region_id)
        if forecast_year[key].get("status") != "OK":
            forecast_year["status"] = "PARTIAL"
    years["2026"] = {
        key: forecast_window_view(forecast_year.get(key))
        for key in definitions_2026
    }
    return years


def combine_kanas_light_years(
    subregions: dict[str, dict],
    history_composite: dict | None,
    forecast_date: dt.date,
) -> dict:
    return combine_region_light_years(
        subregions,
        history_composite,
        forecast_date,
        KANAS_SUBREGION_KEYS,
        region_id="kanas",
    )


def combine_hemu_light_years(
    subregions: dict[str, dict],
    history_composite: dict | None,
    forecast_date: dt.date,
) -> dict:
    return combine_region_light_years(
        subregions,
        history_composite,
        forecast_date,
        HEMU_SUBREGION_KEYS,
        region_id="hemu",
    )


def build_grid_registry(config: dict, hres: dict, history_forward: dict, generated_at: str, data_date: str) -> dict:
    """Persist the point-to-grid mapping without duplicating weather time series."""
    hres_points = hres.get("points") or {}
    forward_points = history_forward.get("points") or {}
    hres_mappings = {
        point_id: point_grid_mapping(record, point_id=point_id)
        for point_id, record in hres_points.items()
    }
    history_mappings = {}
    for point_id, result in forward_points.items():
        history_mappings[point_id] = {
            "point_id": point_id,
            "years": {
                year: point_grid_mapping(record, point_id=point_id, year=int(year))
                for year, record in (result.get("years") or {}).items()
            },
            "same_grid_qa": copy.deepcopy(result.get("same_grid_qa")),
        }
    subregions_by_region = {}
    for region_id, subregion_keys in SUBREGION_KEYS_BY_REGION.items():
        subregions = {}
        for subregion_id in subregion_keys:
            point_ids = region_subregion_point_ids(config, region_id, subregion_id)
            subregions[subregion_id] = {
                "point_ids": point_ids,
                "verified_point_ids": region_subregion_point_ids(
                    config,
                    region_id,
                    subregion_id,
                    verified_only=True,
                ),
                "hres_unique_grid_ids": [
                    entry["grid_cell_id"]
                    for entry in deduplicate_grid_records(
                        [hres_points[point_id] for point_id in point_ids if point_id in hres_points]
                    )
                ],
                "history_unique_grid_ids_by_year": {
                    year: [
                        entry["grid_cell_id"]
                        for entry in deduplicate_grid_records(
                            [
                                (forward_points.get(point_id, {}).get("years") or {}).get(year)
                                for point_id in point_ids
                                if (forward_points.get(point_id, {}).get("years") or {}).get(year)
                            ]
                        )
                    ]
                    for year in (str(value) for value in HISTORY_FORWARD_YEARS)
                },
            }
        if subregions:
            subregions_by_region[region_id] = subregions
    return {
        "schema_version": SCHEMA_VERSION,
        "module": "grid_registry",
        "generated_at": generated_at,
        "data_date": data_date,
        "source": "Open-Meteo",
        "deduplication_key": "returned_grid_coordinate",
        "hres": {"points": hres_mappings},
        "history_forward": {"points": history_mappings},
        "kanas_subregions": subregions_by_region.get("kanas", {}),
        "hemu_subregions": subregions_by_region.get("hemu", {}),
        "subregions_by_region": subregions_by_region,
        "interpretation_boundary": "Mapping and QA metadata only; no weather series or downstream ecological conclusion.",
    }


def build_registered_light_region(
    config: dict,
    region_id: str,
    subregion_keys: tuple[str, ...],
    generated_at: str,
    data_date: str,
    forecast_date: dt.date,
    hres: dict,
    history_forward: dict,
) -> dict:
    region_config = config.get("regions", {}).get(region_id, {})
    history_region = (history_forward.get("regions") or {}).get(region_id) or {}
    subregion_views = {}
    for subregion_id in subregion_keys:
        history_subregion = (history_region.get("subregions") or {}).get(subregion_id)
        forecast_subregion = build_region_forecast_subregion(
            config,
            region_id,
            subregion_id,
            hres,
            forecast_date,
        )
        if history_subregion:
            history_years = {
                str(year): {
                    key: flattened_lightweight_window(
                        (history_subregion.get("years") or {}).get(str(year), {}).get(key)
                    )
                    for key in HISTORY_FORWARD_WINDOW_KEYS
                }
                for year in HISTORY_FORWARD_YEARS
            }
        else:
            history_years = {
                str(year): unavailable_lightweight_windows(
                    forecast_date,
                    "HISTORY_FORWARD_SUBREGION_UNAVAILABLE",
                )
                for year in HISTORY_FORWARD_YEARS
            }
        forecast_year = {
            key: forecast_window_view(
                (forecast_subregion.get("years") or {}).get("2026", {}).get(key)
            )
            for key in HISTORY_FORWARD_WINDOW_KEYS
        }
        forecast_status = forecast_subregion.get("status")
        history_status = (history_subregion or {}).get("status")
        forecast_sampling = forecast_subregion.get("sampling") or {}
        historical_sampling = (
            light_sampling_from_history_subregion(history_subregion)
            if history_subregion
            else {"status": "INVALID", "reason": "HISTORY_FORWARD_SUBREGION_UNAVAILABLE"}
        )
        if forecast_status == "OK" and history_status == "OK":
            subregion_status = "OK"
        elif forecast_subregion.get("usable_for_main_chain") or (history_subregion or {}).get("usable_for_main_chain"):
            subregion_status = "PARTIAL"
        else:
            subregion_status = "INVALID"
        if forecast_sampling.get("status") == "OK" and historical_sampling.get("status") == "OK":
            sampling_status = "OK"
        elif forecast_sampling.get("status") in {"OK", "PARTIAL"} or historical_sampling.get("status") in {"OK", "PARTIAL"}:
            sampling_status = "PARTIAL"
        else:
            sampling_status = "INVALID"
        subregion_views[subregion_id] = {
            "name": forecast_subregion.get("name") or (history_subregion or {}).get("name"),
            "status": subregion_status,
            "usable_for_main_chain": forecast_year["d0_7"].get("usable_for_main_chain", False),
            "sampling": {
                "status": sampling_status,
                "forecast_2026": forecast_sampling,
                "historical_2023_2025": historical_sampling,
            },
            "years": {**history_years, "2026": forecast_year},
            "reason": (
                forecast_subregion.get("reason")
                if forecast_status != "OK"
                else (history_subregion or {}).get("reason")
            ),
        }
    history_composite = history_region.get("composite")
    composite_years = combine_region_light_years(
        subregion_views,
        history_composite,
        forecast_date,
        subregion_keys,
        region_id=region_id,
    )
    statuses = [item.get("status") for item in subregion_views.values()]
    if statuses and all(status == "OK" for status in statuses):
        composite_status = "OK"
    elif any(status in {"OK", "PARTIAL"} for status in statuses):
        composite_status = "PARTIAL"
    else:
        composite_status = "INVALID"
    composite_forecast = composite_years.get("2026", {})
    composite_usable_for_main_chain = composite_forecast.get("d0_7", {}).get(
        "usable_for_main_chain",
        False,
    )
    composite_sampling_status = (
        "OK"
        if subregion_views and all(
            (item.get("sampling") or {}).get("status") == "OK"
            for item in subregion_views.values()
        )
        else "PARTIAL"
        if any(
            (item.get("sampling") or {}).get("status") in {"OK", "PARTIAL"}
            for item in subregion_views.values()
        )
        else "INVALID"
    )
    return {
        "name": region_config.get("name", region_id),
        "usable_for_main_chain": composite_usable_for_main_chain,
        "status": composite_status,
        "subregions": subregion_views,
        "composite": {
            "status": composite_status,
            "usable_for_main_chain": composite_usable_for_main_chain,
            "aggregation": (
                f"equal_mean_of_{region_id}_subregions; "
                f"{', '.join(subregion_keys)} each weight=1/{len(subregion_keys)}; "
                "no point-count weighting"
            ),
            "sampling": {
                "status": composite_sampling_status,
                "subregions": {
                    key: copy.deepcopy(value.get("sampling"))
                    for key, value in subregion_views.items()
                },
            },
            "years": composite_years,
            "missing_or_partial_subregions": [
                key for key, item in subregion_views.items() if item.get("status") != "OK"
            ],
            "reason": (
                None
                if composite_status == "OK"
                else f"{region_id.upper()}_COMPOSITE_REQUIRES_ALL_SUBREGIONS"
            ),
        },
    }


def build_phenology_weather_summary(
    config: dict,
    generated_at: str,
    data_date: str,
    forecast_date: dt.date,
    hres: dict,
    history_forward: dict,
) -> dict:
    """Build the compact ChatGPT-facing weather-only statistics artifact."""
    regions = {}
    for region_id in CORE_REGION_IDS:
        region_config = config.get("regions", {}).get(region_id, {})
        if region_id in SUBREGION_KEYS_BY_REGION and region_subregion_registry(config, region_id):
            regions[region_id] = build_registered_light_region(
                config,
                region_id,
                SUBREGION_KEYS_BY_REGION[region_id],
                generated_at,
                data_date,
                forecast_date,
                hres,
                history_forward,
            )
            continue
        core_id = region_config.get("core_point_id")
        point = active_points(config).get(core_id) if core_id else None
        if not point:
            regions[region_id] = {
                "name": region_config.get("name"),
                "usable_for_main_chain": False,
                "status": "UNAVAILABLE",
                "reason": "NO_VERIFIED_CORE_POINT",
            }
            continue
        years, sampling = point_year_lightweight_views(config, core_id, hres, history_forward, forecast_date)
        regions[region_id] = {
            "name": region_config.get("name"),
            "point_id": core_id,
            "usable_for_main_chain": True,
            "status": "OK" if sampling.get("status") == "OK" else "PARTIAL",
            "sampling": sampling,
            "years": years,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "module": "phenology_weather_summary",
        "generated_at": generated_at,
        "data_date": data_date,
        "forecast_date": forecast_date.isoformat(),
        "source": "Open-Meteo",
        "model_policy": {
            "historical": "ECMWF IFS historical weather / analysis via archive-api; models=ecmwf_ifs",
            "forecast_2026": "ECMWF IFS HRES 9 km via forecast API",
            "timezone": TIMEZONE_NAME,
            "cell_selection": "nearest",
            "elevation": "nan",
            "grid_deduplication": "returned_grid_coordinate; one independent sample per returned model grid",
            "kanas_aggregation": "unique grids equal within each subregion, then three subregions equal in composite",
            "hemu_aggregation": "unique grids equal within valley/backhill, then valley and backhill equal in composite",
        },
        "window_definitions": history_forward_windows_for_year(forecast_date, 2026),
        "weather_only": True,
        "interpretation_boundary": "Weather statistics only; this file contains no downstream ecological or travel conclusion.",
        "regions": regions,
    }


def summary_qa(
    hres_record: dict | None,
    history_region: dict | None,
    ensemble_record: dict | None,
    gfs_record: dict | None,
    single_region: dict | None,
    spatial_region: dict | None,
    long_range_region: dict | None = None,
) -> dict:
    return {
        "hres": hres_record.get("status") if hres_record else "FAILED",
        "history": history_region.get("status", "FAILED") if history_region else "FAILED",
        "ensemble": ensemble_record.get("status") if ensemble_record else "FAILED",
        "gfs": gfs_record.get("status") if gfs_record else "FAILED",
        "single_runs": single_region.get("status") if single_region else "FAILED",
        "spatial_sampling": spatial_region.get("status") if spatial_region else "FAILED",
        "long_range": (
            long_range_region.get("status")
            if long_range_region
            else "FAILED"
        ),
    }


def build_summary(
    config: dict,
    generated_at: str,
    data_date: str,
    now_local: dt.datetime,
    hres: dict,
    history: dict,
    ensemble: dict,
    gfs: dict,
    single_runs: dict,
    spatial: dict,
    long_range: dict | None = None,
) -> dict:
    active = active_points(config)
    history_regions = (history.get("region_summaries") or {}).get("regions", {})
    long_range_regions = (long_range or {}).get("regions") or {}
    regions = {}
    for region_id, region_config in config["regions"].items():
        core_id = region_config.get("core_point_id")
        core_point = active.get(core_id) if core_id else None
        hres_record = (hres.get("points") or {}).get(core_id) if core_id else None
        gfs_record = (gfs.get("points") or {}).get(core_id) if core_id else None
        ensemble_record = (ensemble.get("points") or {}).get(core_id) if core_id else None
        history_region = history_regions.get(region_id)
        single_region = (single_runs.get("regions") or {}).get(region_id)
        spatial_region = (spatial.get("regions") or {}).get(region_id)
        long_range_region = long_range_regions.get(region_id)
        if not core_point:
            regions[region_id] = {
                "visit_date": region_config.get("primary_visit_date"),
                "usable_for_main_chain": False,
                "weather_driver_vs_2025": undetermined_weather_driver("NO_VERIFIED_CORE_POINT"),
                **{
                    f"weather_driver_vs_{year}": undetermined_weather_driver("NO_VERIFIED_CORE_POINT")
                    for year in history_years_for_config(config)
                    if year not in (2025, 2026)
                },
                "forecast_0_7d": {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
                "forecast_8_15d": {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
                "ensemble": {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
                "gfs_crosscheck": {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
                "leaf_loss_weather_risk": {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
                "forecast_16_35d": unavailable_long_range_summary("NO_VERIFIED_CORE_POINT"),
                "qa": summary_qa(None, None, None, None, None, spatial_region, long_range_region),
            }
            continue
        hres_days = (hres_record or {}).get("daily", [])
        forecast_short = forecast_0_7d(hres_days) if hres_record and hres_record.get("status") == "PASS" else {"status": "UNAVAILABLE", "reason": "HRES_INVALID"}
        forecast_long = forecast_8_15d(hres_days) if hres_record and hres_record.get("status") == "PASS" else {"status": "UNAVAILABLE", "reason": "HRES_INVALID"}
        ensemble_summary = {
            "status": ensemble_record.get("status") if ensemble_record else "FAILED",
            "model": ensemble.get("model"),
            "model_id": ensemble.get("model_id"),
            "total_members": ensemble.get("total_members"),
            "distribution": (ensemble_record.get("ensemble") or {}).get("distributions") if ensemble_record else None,
            "qa": ensemble_record.get("qa") if ensemble_record else None,
        }
        regions[region_id] = {
            "visit_date": region_config.get("primary_visit_date"),
            "usable_for_main_chain": True,
            "core_point_id": core_id,
            "weather_driver_vs_2025": (history_region or {}).get("weather_driver_vs_2025") or undetermined_weather_driver("HISTORY_INVALID"),
            **{
                f"weather_driver_vs_{year}": (history_region or {}).get(f"weather_driver_vs_{year}") or undetermined_weather_driver("HISTORY_INVALID")
                for year in history_years_for_config(config)
                if year not in (2025, 2026)
            },
            "forecast_0_7d": forecast_short,
            "forecast_8_15d": forecast_long,
            "ensemble": ensemble_summary,
            "gfs_crosscheck": gfs_crosscheck(hres_record, gfs_record),
            "leaf_loss_weather_risk": leaf_loss_weather_risk(hres_days, now_local.date()) if hres_record and hres_record.get("status") == "PASS" else {"status": "UNAVAILABLE", "reason": "HRES_INVALID"},
            "forecast_16_35d": long_range_summary_for_chatgpt(long_range_region),
            "qa": summary_qa(hres_record, history_region, ensemble_record, gfs_record, single_region, spatial_region, long_range_region),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "data_date": data_date,
        "forecast_date": now_local.date().isoformat(),
        "completed_history_date": data_date,
        "history_years": list(history_years_for_config(config)),
        "visit_dates": {
            region_id: config["regions"][region_id].get("visit_dates", [])
            for region_id in config["regions"]
        },
        "phenology_weather_summary_path": "data/latest/phenology_weather_summary.json",
        "regions": regions,
        "manual_phenology_baseline": config.get("manual_phenology_baseline"),
        "interpretation_boundary": "This file reports weather drivers and weather event risk. It does not produce a final autumn-colour or phenology conclusion.",
    }


EJINA_MODULE_NAMES = ("hres", "history", "ensemble", "gfs", "single_runs", "long_range")


def ejina_module_point_count(module: dict, key: str = "points") -> tuple[int, int]:
    values = module.get(key) or {}
    if not isinstance(values, dict):
        return 0, 0
    records = list(values.values())
    return sum(item.get("status") == "PASS" for item in records if isinstance(item, dict)), len(records)


def ejina_status_value(modules: dict[str, dict], name: str) -> str:
    value = modules.get(name) or {}
    return str(value.get("status", "FAILED"))


def build_ejina_status(
    config: dict,
    generated_at: str,
    data_date: str,
    modules: dict[str, dict],
) -> dict:
    module_statuses = {name: ejina_status_value(modules, name) for name in EJINA_MODULE_NAMES}
    if all(value == "OK" for value in module_statuses.values()):
        pipeline_status = "OK"
    elif any(value in {"OK", "PARTIAL"} for value in module_statuses.values()):
        pipeline_status = "PARTIAL"
    else:
        pipeline_status = "FAILED"
    module_details = {}
    for name in EJINA_MODULE_NAMES:
        value = modules.get(name) or {}
        successful, total = ejina_module_point_count(value)
        if name == "history":
            successful = value.get("successful_fetches", successful)
            total = successful + value.get("failed_fetches", 0)
        elif name == "single_runs":
            regions = value.get("regions") or {}
            successful = sum(region.get("status") == "OK" for region in regions.values())
            total = len(regions)
        elif name == "long_range":
            successful = value.get("successful_points", successful)
            total = len(value.get("regions") or {})
        module_details[name] = {
            "status": module_statuses[name],
            "successful_points": successful,
            "expected_points": total,
            "failed_points": value.get("failed_points", value.get("failed_fetches")),
            "partial_points": value.get("partial_points"),
            "error": value.get("error"),
        }
    points = {}
    for point_id, point in config.get("points", {}).items():
        verified = point.get("status") == "VERIFIED"
        points[point_id] = {
            "name": point.get("name"),
            "region": point.get("region"),
            "status": point.get("status"),
            "usable_for_main_chain": verified,
            "requested_coordinate": {
                "latitude": point.get("latitude"),
                "longitude": point.get("longitude"),
            },
            "forecast_end_date": point.get("forecast_end_date"),
            "coordinate_verification": point.get("coordinate_verification"),
            "reason": None if verified else point.get("reason") or "PROVISIONAL_POINT_EXCLUDED",
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "namespace": "ejina",
        "generated_at": generated_at,
        "data_date": data_date,
        "pipeline_status": pipeline_status,
        "modules": module_statuses,
        "module_details": module_details,
        "points": points,
        "history_start": {
            "2025": f"2025-{config.get('history_start_month_day', '09-01')}",
            "2026": f"2026-{config.get('history_start_month_day', '09-01')}",
        },
        "forecast_end_date": config.get("forecast_end_date"),
        "weather_only_boundary": config.get(
            "weather_only_boundary",
            "This namespace contains weather evidence only; downstream ecological and travel interpretation is external.",
        ),
        "failure_policy": "Invalid or unavailable Open-Meteo data remains explicit; no external weather source fallback is used.",
    }


def ejina_history_summary(history: dict, point_id: str) -> dict:
    comparison = ((history.get("region_summaries") or {}).get("points") or {}).get(point_id)
    if not comparison:
        return {"status": "FAILED", "reason": "HISTORY_COMPARISON_UNAVAILABLE"}
    metrics = comparison.get("metrics") or {}
    return {
        "status": comparison.get("status", "FAILED"),
        "period_start": comparison.get("period_start"),
        "period_end": comparison.get("period_end"),
        "metrics_2025": metrics.get("2025", {}),
        "metrics_2026": metrics.get("2026", {}),
        "delta_2026_minus_2025": comparison.get(
            "delta_2026_minus_2025",
            numeric_deltas(metrics.get("2026", {}), metrics.get("2025", {})),
        ),
        "same_grid_qa": comparison.get("same_grid_qa", {"status": "UNAVAILABLE"}),
    }


def ejina_long_range_summary(region: dict | None) -> dict:
    summary = long_range_summary_for_chatgpt(region)
    summary["interpretation"] = "16-35 day coarse weather background only; no date-level precision or downstream ecological/travel conclusion."
    return summary


def build_ejina_summary(
    config: dict,
    generated_at: str,
    data_date: str,
    now_local: dt.datetime,
    hres: dict,
    history: dict,
    ensemble: dict,
    gfs: dict,
    single_runs: dict,
    long_range: dict,
) -> dict:
    active = active_points(config)
    history_regions = (history.get("region_summaries") or {}).get("regions", {})
    long_range_regions = long_range.get("regions") or {}
    regions = {}
    for region_id, region_config in config.get("regions", {}).items():
        point_id = region_config.get("core_point_id")
        point = active.get(point_id) if point_id else None
        if not point:
            regions[region_id] = {
                "point_id": point_id,
                "name": region_config.get("name"),
                "usable_for_main_chain": False,
                "history_comparison": {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
                "forecast_0_7d": {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
                "forecast_8_15d": {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
                "forecast_16_35d": {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
                "ensemble": {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
                "gfs_crosscheck": {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
                "single_runs": {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
                "qa": {"final_status": "UNAVAILABLE", "reason": "NO_VERIFIED_CORE_POINT"},
            }
            continue
        hres_record = (hres.get("points") or {}).get(point_id)
        gfs_record = (gfs.get("points") or {}).get(point_id)
        ensemble_record = (ensemble.get("points") or {}).get(point_id)
        history_region = history_regions.get(region_id)
        single_region = (single_runs.get("regions") or {}).get(region_id)
        long_range_region = long_range_regions.get(region_id)
        history_point = ((history.get("region_summaries") or {}).get("points") or {}).get(point_id) or {}
        hres_days = (hres_record or {}).get("daily", [])
        forecast_short = forecast_0_7d(hres_days) if hres_record and hres_record.get("status") == "PASS" else {"status": "UNAVAILABLE", "reason": "HRES_INVALID"}
        forecast_middle = forecast_8_15d(hres_days) if hres_record and hres_record.get("status") == "PASS" else {"status": "UNAVAILABLE", "reason": "HRES_INVALID"}
        ensemble_summary = {
            "status": ensemble_record.get("status") if ensemble_record else "FAILED",
            "model": ensemble.get("model"),
            "model_id": ensemble.get("model_id"),
            "total_members": ensemble.get("total_members"),
            "distribution": (ensemble_record.get("ensemble") or {}).get("distributions") if ensemble_record else None,
            "qa": ensemble_record.get("qa") if ensemble_record else None,
        }
        regions[region_id] = {
            "point_id": point_id,
            "name": region_config.get("name"),
            "usable_for_main_chain": True,
            "requested_coordinate": {
                "latitude": point.get("latitude"),
                "longitude": point.get("longitude"),
            },
            "forecast_end_date": point.get("forecast_end_date", config.get("forecast_end_date")),
            "history_comparison": ejina_history_summary(history, point_id),
            "forecast_0_7d": forecast_short,
            "forecast_8_15d": forecast_middle,
            "forecast_16_35d": ejina_long_range_summary(long_range_region),
            "ensemble": ensemble_summary,
            "gfs_crosscheck": gfs_crosscheck(hres_record, gfs_record),
            "single_runs": {
                "status": single_region.get("status") if single_region else "FAILED",
                "target_time": single_region.get("target_time") if single_region else None,
                "latest_change": single_region.get("latest_change") if single_region else None,
                "run_count_requested": single_region.get("run_count_requested") if single_region else None,
                "run_count_available": single_region.get("run_count_available") if single_region else None,
            },
            "qa": {
                "hres": hres_record.get("qa") if hres_record else None,
                "history": history_region.get("status") if history_region else "FAILED",
                "history_same_grid": history_point.get("same_grid_qa"),
                "ensemble": ensemble_record.get("qa") if ensemble_record else None,
                "gfs": gfs_record.get("qa") if gfs_record else None,
                "single_runs": single_region.get("status") if single_region else "FAILED",
                "long_range": long_range_region.get("qa") if long_range_region else None,
            },
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "namespace": "ejina",
        "generated_at": generated_at,
        "data_date": data_date,
        "history_start": {
            "2025": f"2025-{config.get('history_start_month_day', '09-01')}",
            "2026": f"2026-{config.get('history_start_month_day', '09-01')}",
        },
        "forecast_end_date": config.get("forecast_end_date"),
        "forecast_layers": {
            "0_7d": "ECMWF IFS HRES",
            "8_15d": "ECMWF HRES + ECMWF IFS Ensemble + GFS cross-check",
            "16d_plus": "GFS Ensemble coarse background only",
        },
        "regions": regions,
        "interpretation_boundary": config.get(
            "weather_only_boundary",
            "This namespace contains weather evidence only; downstream ecological and travel interpretation is external.",
        ),
    }


def ejina_failure_result(generated_at: str, data_date: str, error: Exception) -> dict:
    reason = f"{type(error).__name__}:{error}"
    modules = {
        name: failed_module(
            name,
            generated_at,
            data_date,
            error,
            artifact_module="long_range_background" if name == "long_range" else name,
        )
        for name in EJINA_MODULE_NAMES
    }
    status = {
        "schema_version": SCHEMA_VERSION,
        "namespace": "ejina",
        "generated_at": generated_at,
        "data_date": data_date,
        "pipeline_status": "FAILED",
        "modules": {name: "FAILED" for name in EJINA_MODULE_NAMES},
        "module_details": {name: {"status": "FAILED", "error": reason} for name in EJINA_MODULE_NAMES},
        "points": {},
        "history_start": {"2025": None, "2026": None},
        "forecast_end_date": None,
        "weather_only_boundary": "Namespace initialization failed before weather data retrieval.",
        "failure_policy": "Invalid or unavailable Open-Meteo data remains explicit; no external weather source fallback is used.",
    }
    summary = {
        "schema_version": SCHEMA_VERSION,
        "namespace": "ejina",
        "generated_at": generated_at,
        "data_date": data_date,
        "history_start": {"2025": None, "2026": None},
        "forecast_end_date": None,
        "forecast_layers": {},
        "regions": {},
        "error": reason,
        "interpretation_boundary": "Namespace unavailable; inspect status and module artifacts.",
    }
    return {"modules": modules, "status": status, "summary": summary, "config": None}


def run_ejina_pipeline(
    client: ApiClient,
    generated_at: str,
    data_date: str,
    now_utc: dt.datetime,
    *,
    refresh_history: bool = False,
) -> dict:
    config = load_ejina_config()
    now_local = now_utc.astimezone(LOCAL_TZ)
    modules: dict[str, dict] = {}
    steps = (
        ("hres", run_hres),
        (
            "history",
            lambda cfg, api, gen, day: run_history(
                cfg,
                api,
                gen,
                day,
                now_local.date() - dt.timedelta(days=1),
                refresh_history=refresh_history,
            ),
        ),
        ("gfs", run_gfs),
        ("ensemble", run_ensemble),
    )
    for name, function in steps:
        log(f"EJINA PHASE: {name.upper()}")
        try:
            modules[name] = function(config, client, generated_at, data_date)
        except Exception as error:
            modules[name] = failed_module(name, generated_at, data_date, error)
    log("EJINA PHASE: SINGLE RUNS")
    try:
        modules["single_runs"] = run_single_runs(config, client, generated_at, data_date, now_utc)
    except Exception as error:
        modules["single_runs"] = failed_module("single_runs", generated_at, data_date, error)
    log("EJINA PHASE: GFS ENSEMBLE LONG RANGE")
    try:
        modules["long_range"] = run_long_range(
            config,
            client,
            generated_at,
            data_date,
            now_local,
            archive_namespace="ejina",
            refresh_history=refresh_history,
        )
    except Exception as error:
        modules["long_range"] = failed_module(
            "long_range",
            generated_at,
            data_date,
            error,
            artifact_module="long_range_background",
        )
    summary = build_ejina_summary(
        config,
        generated_at,
        data_date,
        now_local,
        modules["hres"],
        modules["history"],
        modules["ensemble"],
        modules["gfs"],
        modules["single_runs"],
        modules["long_range"],
    )
    status = build_ejina_status(config, generated_at, data_date, modules)
    return {"config": config, "modules": modules, "status": status, "summary": summary}


def failed_module(
    name: str,
    generated_at: str,
    data_date: str,
    error: Exception,
    *,
    artifact_module: str | None = None,
) -> dict:
    reason = f"{type(error).__name__}:{error}"
    log(f"[{name}] MODULE FAILED: {reason}")
    return module_header(artifact_module or name, generated_at, data_date, "FAILED", error=reason, points={}, regions={})


def compact_record(record: dict) -> dict:
    keep = (
        "point_id",
        "point",
        "status",
        "source",
        "endpoint",
        "model",
        "request",
        "response",
        "qa",
        "solar_variable",
        "daily",
        "ensemble",
        "error",
    )
    return {key: copy.deepcopy(record[key]) for key in keep if key in record}


def compact_module(name: str, value: dict) -> dict:
    compact = copy.deepcopy(value)
    compact["archive_kind"] = "compact_daily_snapshot"
    compact["hourly_values_omitted"] = True
    if name in {"hres", "gfs"}:
        compact["points"] = {point_id: compact_record(record) for point_id, record in value.get("points", {}).items()}
    elif name == "history_comparison":
        compact["points"] = {}
        for point_id, point_result in value.get("points", {}).items():
            item = copy.deepcopy(point_result)
            item["years"] = {year: compact_record(record) for year, record in point_result.get("years", {}).items()}
            compact["points"][point_id] = item
        compact["region_summaries"] = copy.deepcopy(value.get("region_summaries", {}))
    elif name == "history_forward":
        compact["points"] = {}
        for point_id, point_result in value.get("points", {}).items():
            item = copy.deepcopy(point_result)
            item["years"] = {
                year: compact_history_forward_year(record)
                for year, record in point_result.get("years", {}).items()
            }
            compact["points"][point_id] = item
        compact["regions"] = copy.deepcopy(value.get("regions", {}))
    elif name == "ensemble":
        compact["points"] = {}
        for point_id, record in value.get("points", {}).items():
            item = compact_record(record)
            if "ensemble" in record:
                item["ensemble"] = copy.deepcopy(record["ensemble"])
            compact["points"][point_id] = item
    elif name == "single_runs":
        compact["regions"] = {}
        for region_id, region in value.get("regions", {}).items():
            item = copy.deepcopy(region)
            item["runs"] = []
            for run in region.get("runs", []):
                run_item = {key: copy.deepcopy(run[key]) for key in ("init_time", "status", "target_time", "target") if key in run}
                if "record" in run:
                    run_item["record"] = compact_record(run["record"])
                item["runs"].append(run_item)
            compact["regions"][region_id] = item
    elif name == "long_range":
        compact.pop("raw_points", None)
        compact.pop("raw_references", None)
        compact["raw_hourly_included"] = False
    return compact


def public_long_range_artifact(value: dict) -> dict:
    """Remove member-level hourly payloads from the ChatGPT-facing artifact."""
    public = copy.deepcopy(value)
    public.pop("raw_points", None)
    public.pop("raw_references", None)
    public["raw_hourly_included"] = False
    public["raw_snapshot_retention_days"] = RAW_RETENTION_DAYS
    return public


def prune_old_raw_archives(current_archive_date: dt.date) -> None:
    cutoff = current_archive_date - dt.timedelta(days=RAW_RETENTION_DAYS - 1)
    if not ARCHIVE_DIR.exists():
        return
    for child in ARCHIVE_DIR.iterdir():
        if not child.is_dir():
            continue
        try:
            archive_date = dt.date.fromisoformat(child.name)
        except ValueError:
            continue
        if archive_date < cutoff:
            for raw_dir in (child / "raw", child / "ejina" / "raw"):
                if raw_dir.is_dir():
                    # Retention is intentionally limited to generated raw snapshots only.
                    shutil.rmtree(raw_dir)


def write_outputs(
    *,
    now_local: dt.datetime,
    status: dict,
    hres: dict,
    history: dict,
    history_forward: dict,
    ensemble: dict,
    gfs: dict,
    single_runs: dict,
    spatial: dict,
    long_range: dict,
    summary: dict,
    grid_registry: dict | None = None,
    phenology_weather_summary: dict | None = None,
) -> None:
    grid_registry = grid_registry or {}
    phenology_weather_summary = phenology_weather_summary or {}
    artifacts = {
        "status.json": status,
        "hres.json": hres,
        "history_comparison.json": history,
        "history_forward.json": history_forward,
        "ensemble.json": ensemble,
        "gfs.json": gfs,
        "single_runs.json": single_runs,
        "spatial_sampling.json": spatial,
        "long_range.json": public_long_range_artifact(long_range),
        "grid_registry.json": grid_registry,
        "phenology_weather_summary.json": phenology_weather_summary,
        "summary.json": summary,
    }
    for filename, artifact in artifacts.items():
        writer = write_compact_json if filename == "phenology_weather_summary.json" else write_json
        writer(LATEST_DIR / filename, artifact)
    archive_path = ARCHIVE_DIR / now_local.date().isoformat()
    archive_path.mkdir(parents=True, exist_ok=True)
    for filename, artifact in artifacts.items():
        name = filename.removesuffix(".json")
        archive_artifact = compact_module(name, artifact)
        writer = write_compact_json if filename == "phenology_weather_summary.json" else write_json
        writer(archive_path / filename, archive_artifact)
    raw_values = {
        "hres.json.gz": hres,
        "history_comparison.json.gz": history,
        "history_forward.json.gz": history_forward,
        "ensemble.json.gz": ensemble,
        "gfs.json.gz": gfs,
        "single_runs.json.gz": single_runs,
        "spatial_sampling.json.gz": spatial,
        "long_range.json.gz": long_range,
        "grid_registry.json.gz": grid_registry,
    }
    for filename, artifact in raw_values.items():
        write_gzip_json(archive_path / "raw" / filename, artifact)
    prune_old_raw_archives(now_local.date())


def write_ejina_outputs(*, now_local: dt.datetime, result: dict) -> None:
    latest_dir = LATEST_DIR / "ejina"
    archive_dir = ARCHIVE_DIR / now_local.date().isoformat() / "ejina"
    modules = result.get("modules") or {}
    artifacts = {
        "status.json": result["status"],
        "hres.json": modules.get("hres", {}),
        "history_comparison.json": modules.get("history", {}),
        "ensemble.json": modules.get("ensemble", {}),
        "gfs.json": modules.get("gfs", {}),
        "single_runs.json": modules.get("single_runs", {}),
        "long_range.json": public_long_range_artifact(modules.get("long_range", {})),
        "summary.json": result["summary"],
    }
    for filename, artifact in artifacts.items():
        write_json(latest_dir / filename, artifact)
        write_json(archive_dir / filename, compact_module(filename.removesuffix(".json"), artifact))
    raw_values = {
        "hres.json.gz": modules.get("hres", {}),
        "history_comparison.json.gz": modules.get("history", {}),
        "ensemble.json.gz": modules.get("ensemble", {}),
        "gfs.json.gz": modules.get("gfs", {}),
        "single_runs.json.gz": modules.get("single_runs", {}),
        "long_range.json.gz": modules.get("long_range", {}),
    }
    for filename, artifact in raw_values.items():
        write_gzip_json(archive_dir / "raw" / filename, artifact)
    prune_old_raw_archives(now_local.date())


def build_status(
    config: dict,
    generated_at: str,
    data_date: str,
    modules: dict[str, dict],
    namespaces: dict[str, dict] | None = None,
) -> dict:
    module_names = ("hres", "history", "ensemble", "gfs", "single_runs")
    module_values = {name: modules.get(name, {}).get("status", "FAILED") for name in module_names}
    history_forward_status = modules.get("history_forward", {}).get("status", "FAILED")
    long_range_status = modules.get("long_range", {}).get("status", "FAILED")
    light_summary_status = modules.get("phenology_weather_summary", {}).get("status")
    if all(value == "OK" for value in module_values.values()):
        pipeline_status = (
            "OK"
            if long_range_status == "OK"
            and history_forward_status == "OK"
            and light_summary_status in {None, "OK"}
            else "PARTIAL"
        )
    elif modules.get("hres", {}).get("status") == "OK" or modules.get("history", {}).get("status") == "OK":
        pipeline_status = "DEGRADED"
    else:
        pipeline_status = "FAILED"
    points = {}
    for point_id, point in config.get("points", {}).items():
        verified = point.get("status") == "VERIFIED"
        points[point_id] = {
            "name": point.get("name"),
            "region": point.get("region"),
            "status": point.get("status"),
            "usable_for_main_chain": verified,
            "reason": None if verified else point.get("reason") or "PROVISIONAL_POINT_EXCLUDED",
        }
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "data_date": data_date,
        "history_years": list(history_years_for_config(config)),
        "pipeline_status": pipeline_status,
        "modules": module_values | {
            "spatial_sampling": modules.get("spatial_sampling", {}).get("status", "FAILED"),
            "long_range": long_range_status,
            "history_forward": history_forward_status,
        },
        "module_details": {
            name: {
                "status": value.get("status", "FAILED"),
                "successful_points": value.get("successful_points", value.get("successful_fetches")),
                "partial_points": value.get("partial_points"),
                "failed_points": value.get("failed_points", value.get("failed_fetches")),
                "error": value.get("error"),
            }
            for name, value in modules.items()
        },
        "points": points,
        "route_slots": {
            slot_id: {
                "status": slot.get("status"),
                "enabled": slot.get("enabled", False),
                "usable_for_main_chain": False,
                "reason": slot.get("reason") or "ROUTE_NOT_VERIFIED",
            }
            for slot_id, slot in config.get("route_slots", {}).items()
        },
        "manual_phenology_baseline": config.get("manual_phenology_baseline"),
        "failure_policy": "Any request, QA, model, timezone, missing-data, or grid-representativeness failure is recorded as INVALID; no external weather fallback is used.",
    }
    if light_summary_status is not None:
        result["modules"]["phenology_weather_summary"] = light_summary_status
    if namespaces:
        result["namespaces"] = namespaces
    return result


def minimal_failure_status(generated_at: str, reason: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "data_date": None,
        "pipeline_status": "FAILED",
        "modules": {"hres": "FAILED", "history": "FAILED", "history_forward": "FAILED", "ensemble": "FAILED", "gfs": "FAILED", "single_runs": "FAILED", "spatial_sampling": "FAILED", "long_range": "FAILED", "phenology_weather_summary": "FAILED"},
        "module_details": {"pipeline": {"status": "FAILED", "error": reason}},
        "points": {},
        "route_slots": {},
        "failure_policy": "Pipeline initialization failed before data retrieval.",
    }


def run_pipeline(
    now_utc: dt.datetime | None = None,
    *,
    refresh_history: bool = False,
) -> dict:
    now_utc = (now_utc or dt.datetime.now(UTC)).astimezone(UTC)
    now_local = now_utc.astimezone(LOCAL_TZ)
    generated_at = iso_utc(now_utc)
    data_date = (now_local.date() - dt.timedelta(days=1)).isoformat()
    config = load_config()
    client = ApiClient()
    modules: dict[str, dict] = {}

    log("PHASE 1: HRES")
    try:
        modules["hres"] = run_hres(config, client, generated_at, data_date)
    except Exception as error:
        modules["hres"] = failed_module("hres", generated_at, data_date, error)

    log("PHASE 2: HISTORICAL IFS")
    try:
        modules["history"] = run_history(
            config,
            client,
            generated_at,
            data_date,
            now_local.date() - dt.timedelta(days=1),
            refresh_history=refresh_history,
            forward_anchor_date=now_local.date(),
        )
    except Exception as error:
        modules["history"] = failed_module("history", generated_at, data_date, error)

    log("PHASE 3: HISTORICAL FORWARD PATH")
    try:
        modules["history_forward"] = run_history_forward(
            config,
            client,
            generated_at,
            data_date,
            now_local.date(),
            # run_history above refreshes the union needed by the forward
            # paths, so reusing the cache here avoids a second API request in
            # the same pipeline run.
            refresh_history=False,
        )
    except Exception as error:
        modules["history_forward"] = failed_history_forward_module(
            config,
            generated_at,
            data_date,
            now_local.date(),
            error,
        )

    log("PHASE 4: SPATIAL SAMPLING")
    try:
        modules["spatial_sampling"] = run_spatial(config, client, generated_at, data_date, modules["hres"])
    except Exception as error:
        modules["spatial_sampling"] = failed_module("spatial_sampling", generated_at, data_date, error)

    log("PHASE 5: GFS")
    try:
        modules["gfs"] = run_gfs(config, client, generated_at, data_date)
    except Exception as error:
        modules["gfs"] = failed_module("gfs", generated_at, data_date, error)

    log("PHASE 6: ECMWF ENSEMBLE")
    try:
        modules["ensemble"] = run_ensemble(config, client, generated_at, data_date)
    except Exception as error:
        modules["ensemble"] = failed_module("ensemble", generated_at, data_date, error)

    log("PHASE 7: SINGLE RUNS")
    try:
        modules["single_runs"] = run_single_runs(config, client, generated_at, data_date, now_utc)
    except Exception as error:
        modules["single_runs"] = failed_module("single_runs", generated_at, data_date, error)

    log("PHASE 8: GFS ENSEMBLE LONG RANGE")
    try:
        modules["long_range"] = run_long_range(
            config,
            client,
            generated_at,
            data_date,
            now_local,
            # Historical IFS and forward-path phases already warm the cache;
            # the reference layer only fills dates outside that union.
            refresh_history=False,
        )
    except Exception as error:
        modules["long_range"] = failed_module(
            "long_range",
            generated_at,
            data_date,
            error,
            artifact_module="long_range_background",
        )

    log("PHASE 9: SUMMARY")
    try:
        summary = build_summary(
            config,
            generated_at,
            data_date,
            now_local,
            modules["hres"],
            modules["history"],
            modules["ensemble"],
            modules["gfs"],
            modules["single_runs"],
            modules["spatial_sampling"],
            modules["long_range"],
        )
    except Exception as error:
        log(f"[summary] BUILD FAILED: {type(error).__name__}:{error}")
        summary = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "data_date": data_date,
            "phenology_weather_summary_path": "data/latest/phenology_weather_summary.json",
            "regions": {},
            "error": f"{type(error).__name__}:{error}",
            "interpretation_boundary": "Summary unavailable; inspect status.json and module artifacts.",
        }
    modules["summary"] = {"status": "OK" if "error" not in summary else "FAILED"}
    log("PHASE 9A: GRID REGISTRY")
    try:
        grid_registry = build_grid_registry(
            config,
            modules["hres"],
            modules["history_forward"],
            generated_at,
            data_date,
        )
    except Exception as error:
        log(f"[grid_registry] BUILD FAILED: {type(error).__name__}:{error}")
        grid_registry = {
            "schema_version": SCHEMA_VERSION,
            "module": "grid_registry",
            "generated_at": generated_at,
            "data_date": data_date,
            "status": "FAILED",
            "error": f"{type(error).__name__}:{error}",
        }
    log("PHASE 9B: LIGHTWEIGHT WEATHER SUMMARY")
    try:
        phenology_weather_summary = build_phenology_weather_summary(
            config,
            generated_at,
            data_date,
            now_local.date(),
            modules["hres"],
            modules["history_forward"],
        )
        modules["phenology_weather_summary"] = {
            "status": "OK",
            "successful_regions": sum(
                value.get("status") == "OK"
                for value in phenology_weather_summary.get("regions", {}).values()
            ),
            "failed_points": 0,
            "error": None,
        }
    except Exception as error:
        log(f"[phenology_weather_summary] BUILD FAILED: {type(error).__name__}:{error}")
        phenology_weather_summary = {
            "schema_version": SCHEMA_VERSION,
            "module": "phenology_weather_summary",
            "generated_at": generated_at,
            "data_date": data_date,
            "forecast_date": now_local.date().isoformat(),
            "source": "Open-Meteo",
            "weather_only": True,
            "status": "FAILED",
            "error": f"{type(error).__name__}:{error}",
            "regions": {},
        }
        modules["phenology_weather_summary"] = {
            "status": "FAILED",
            "successful_regions": 0,
            "failed_points": 0,
            "error": f"{type(error).__name__}:{error}",
        }
    log("PHASE 10: EJINA WEATHER NAMESPACE")
    try:
        ejina = run_ejina_pipeline(
            client,
            generated_at,
            data_date,
            now_utc,
            refresh_history=refresh_history,
        )
    except Exception as error:
        log(f"[ejina] INITIALIZATION FAILED: {type(error).__name__}:{error}")
        ejina = ejina_failure_result(generated_at, data_date, error)
    write_ejina_outputs(now_local=now_local, result=ejina)
    ejina_status = ejina["status"]
    namespace_status = {
        "status": ejina_status.get("pipeline_status", "FAILED"),
        "modules": ejina_status.get("modules", {}),
        "status_path": "data/latest/ejina/status.json",
        "summary_path": "data/latest/ejina/summary.json",
        "long_range_path": "data/latest/ejina/long_range.json",
    }
    status = build_status(config, generated_at, data_date, modules, {"ejina": namespace_status})
    write_outputs(
        now_local=now_local,
        status=status,
        hres=modules["hres"],
        history=modules["history"],
        history_forward=modules["history_forward"],
        ensemble=modules["ensemble"],
        gfs=modules["gfs"],
        single_runs=modules["single_runs"],
        spatial=modules["spatial_sampling"],
        long_range=modules["long_range"],
        summary=summary,
        grid_registry=grid_registry,
        phenology_weather_summary=phenology_weather_summary,
    )
    log(f"PIPELINE STATUS: {status['pipeline_status']}")
    for name, value in status["modules"].items():
        log(f"MODULE {name}: {value}")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", help="override current time with an ISO-8601 timestamp for reproducible runs")
    parser.add_argument(
        "--refresh-history",
        action="store_true",
        help="revalidate historical cache ranges against Open-Meteo before rebuilding outputs",
    )
    args = parser.parse_args(argv)
    refresh_history = args.refresh_history or os.environ.get("ALTAY_MONITOR_REFRESH_HISTORY", "").lower() in {
        "1",
        "true",
        "yes",
    }
    try:
        run_pipeline(now_from_input(args.now), refresh_history=refresh_history)
        return 0
    except Exception as error:
        reason = f"{type(error).__name__}:{error}"
        generated_at = iso_utc(dt.datetime.now(UTC))
        log(f"PIPELINE INITIALIZATION FAILED: {reason}")
        try:
            write_json(LATEST_DIR / "status.json", minimal_failure_status(generated_at, reason))
        except Exception as write_error:
            log(f"STATUS WRITE FAILED: {type(write_error).__name__}:{write_error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
