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
LATEST_DIR = ROOT / "data" / "latest"
ARCHIVE_DIR = ROOT / "data" / "archive"
TIMEZONE_NAME = "Asia/Shanghai"
LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
UTC = dt.timezone.utc
SCHEMA_VERSION = "1.1.0"
LEGACY_SCHEMA_VERSION = "1.0.0"
RAW_RETENTION_DAYS = 14
HRES_GRID_QA_LIMIT_KM = 14.0
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
CORE_REGION_IDS = ("baihaba", "kanas", "keketuohai")
THRESHOLDS_C = (10.0, 5.0, 2.0, 0.0)

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


def write_gzip_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with gzip.open(temp_path, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    temp_path.replace(path)


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("timezone") != TIMEZONE_NAME:
        raise ValueError(f"config timezone must be {TIMEZONE_NAME}")
    if config.get("schema_version") not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
        raise ValueError("unsupported points schema version")
    return config


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
    payload, completeness = trim_incomplete_edge_rows(payload, selected_required_variables)
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


def history_date_range(completed_date: dt.date, year: int) -> tuple[str, str] | None:
    start = dt.date(year, 8, 25)
    if completed_date < dt.date(2026, 8, 25):
        return None
    try:
        end = dt.date(year, completed_date.month, completed_date.day)
    except ValueError:
        end = dt.date(year, completed_date.month, 28)
    return start.isoformat(), end.isoformat()


def run_history(config: dict, client: ApiClient, generated_at: str, data_date: str, completed_date: dt.date) -> dict:
    points = active_points(config)
    point_results: dict[str, dict] = {}
    all_records = []
    for point_id, point in points.items():
        years: dict[str, dict] = {}
        for year in (2025, 2026):
            date_range = history_date_range(completed_date, year)
            if date_range is None:
                record = invalid_record(
                    point=point,
                    source="Open-Meteo",
                    endpoint=OPEN_METEO_ENDPOINTS["history"],
                    model="ECMWF IFS 9 km historical weather / analysis",
                    request_params={"models": "ecmwf_ifs", "year": year},
                    reason="HISTORY_NOT_STARTED",
                )
            else:
                start_date, end_date = date_range
                record = fetch_point(
                    client,
                    point=point,
                    source="Open-Meteo",
                    endpoint=OPEN_METEO_ENDPOINTS["history"],
                    model="ECMWF IFS 9 km historical weather / analysis",
                    params=base_weather_params(
                        point,
                        models="ecmwf_ifs",
                        start_date=start_date,
                        end_date=end_date,
                    ),
                    variables=HRES_VARIABLES,
                    required_variables=[value for value in HRES_VARIABLES if value != "sunshine_duration"],
                    grid_limit_km=13.5,
                    log_label=f"{point_id}:HISTORY {year}",
                )
                if record.get("status") == "PASS":
                    record["daily"] = daily_metrics(record["hourly"], record.get("solar_variable"))
                    log(f"[{point_id}] HISTORY {year} OK")
            years[str(year)] = record
            all_records.append(record)
        point_results[point_id] = {
            "point_id": point_id,
            "point": {"name": point["name"], "region": point["region"], "status": point["status"]},
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
        model="ECMWF IFS 9 km historical weather / analysis",
        model_parameter="ecmwf_ifs",
        period_start="2025-08-25 / 2026-08-25",
        period_end=data_date,
        note="Historical IFS is reanalysis/analysis, not station observation.",
        points=point_results,
        region_summaries=comparison,
        excluded_points=excluded_points(config),
        successful_fetches=sum(record.get("status") == "PASS" for record in all_records),
        failed_fetches=sum(record.get("status") != "PASS" for record in all_records),
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
    return delta


def driver_direction(current: dict, baseline: dict) -> dict:
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
            "baseline_2025": baseline,
            "delta_2026_minus_2025": numeric_deltas(current, baseline),
            "threshold_count_delta_sum": count_delta,
            "interpretation": "weather_driver_only; requires ChatGPT visual evidence for actual phenology assessment",
        },
    }


def build_history_comparison(config: dict, point_results: dict[str, dict], data_date: str) -> dict:
    comparisons: dict[str, dict] = {}
    for point_id, result in point_results.items():
        years = result["years"]
        daily_2025 = years.get("2025", {}).get("daily", []) if years.get("2025", {}).get("status") == "PASS" else []
        daily_2026 = years.get("2026", {}).get("daily", []) if years.get("2026", {}).get("status") == "PASS" else []
        metrics_2025 = period_metrics(daily_2025)
        metrics_2026 = period_metrics(daily_2026)
        comparisons[point_id] = {
            "point_id": point_id,
            "region": result["point"]["region"],
            "status": "OK" if daily_2025 and daily_2026 else "FAILED",
            "period_start": "08-25",
            "period_end": data_date,
            "daily": {"2025": daily_2025, "2026": daily_2026},
            "metrics": {"2025": metrics_2025, "2026": metrics_2026},
            "weather_driver_vs_2025": driver_direction(metrics_2026, metrics_2025),
        }
    region_summaries: dict[str, dict] = {}
    for region_id in CORE_REGION_IDS:
        region_config = config["regions"][region_id]
        core_id = region_config.get("core_point_id")
        comparison = comparisons.get(core_id) if core_id else None
        if comparison:
            region_summaries[region_id] = {
                "region": region_id,
                "core_point_id": core_id,
                "status": comparison["status"],
                "usable_for_main_chain": True,
                "visit_date": region_config.get("primary_visit_date"),
                "weather_driver_vs_2025": comparison["weather_driver_vs_2025"],
                "metrics": comparison["metrics"],
                "supporting_point_ids": [point_id for point_id, item in comparisons.items() if item["region"] == region_id and point_id != core_id],
            }
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
    for region_id, region_config in config["regions"].items():
        core_id = region_config.get("core_point_id")
        points = active_points(config)
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
        for threshold in (5.0, 2.0, 0.0):
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
    for region_id in CORE_REGION_IDS:
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
) -> list[dict]:
    windows = []
    for start_lead, end_lead in long_range_window_definitions():
        current_values = long_range_member_window_values(daily_by_lead, start_lead, end_lead)
        start_date = origin_date + dt.timedelta(days=start_lead)
        end_date = origin_date + dt.timedelta(days=end_lead)
        if not current_values:
            windows.append({
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "horizon_class": f"D{start_lead}_D{end_lead}",
                "confidence": "VERY_LOW",
                "status": "UNAVAILABLE",
                "reason": "WINDOW_DATA_MISSING",
            })
            continue
        temperature_stats = long_range_temperature_stats(current_values)
        reference_mean, reference_days_available = reference_mean_for_window(
            reference_days, origin_date, start_lead, end_lead
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
            "horizon_class": f"D{start_lead}_D{end_lead}",
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


def load_long_range_snapshots(current_date: dt.date, limit: int = 5) -> list[dict]:
    snapshots = []
    if not ARCHIVE_DIR.exists():
        return snapshots
    for path in ARCHIVE_DIR.glob("*/long_range.json"):
        try:
            archive_date = dt.date.fromisoformat(path.parent.name)
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
    point: dict,
    client: ApiClient,
    origin_date: dt.date,
    generated_at: str,
    data_date: str,
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
    record = fetch_point(
        client,
        point=point,
        source="Open-Meteo",
        endpoint=OPEN_METEO_ENDPOINTS["history"],
        model="ECMWF IFS 9 km historical weather / analysis",
        params=base_weather_params(
            point,
            models="ecmwf_ifs",
            start_date=reference_start.isoformat(),
            end_date=reference_end.isoformat(),
        ),
        variables=["temperature_2m"],
        required_variables=["temperature_2m"],
        grid_limit_km=13.5,
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


def run_long_range(config: dict, client: ApiClient, generated_at: str, data_date: str, now_local: dt.datetime) -> dict:
    points = active_points(config)
    forecast_records: dict[str, dict] = {}
    raw_references: dict[str, dict] = {}
    active_core_ids = []
    for region_id in CORE_REGION_IDS:
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

    snapshots = load_long_range_snapshots(now_local.date())
    regions = {}
    successful_points = 0
    partial_points = 0
    failed_points = 0
    forecast_horizons = []
    member_counts = []
    for region_id in CORE_REGION_IDS:
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
        reference = run_long_range_reference(point, client, origin_date, generated_at, data_date)
        raw_references[region_id] = reference.get("record")
        reference_days = {day["date"]: day for day in reference.get("daily", [])}
        windows = build_long_range_windows(origin_date, daily_by_lead, reference_days)
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
        interpretation_boundary="16-35 day background signal only; not a date-level precise forecast and not a direct phenology lead/lag calculation.",
        regions=regions,
        excluded_points=excluded_points(config),
        raw_references=raw_references,
        raw_points=forecast_records,
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


def select_single_run_target(region_config: dict, now_local: dt.datetime) -> tuple[str, str, str]:
    visit_date = dt.date.fromisoformat(region_config["primary_visit_date"])
    visit_target = dt.datetime.combine(visit_date, dt.time(5, 0), tzinfo=LOCAL_TZ)
    days_ahead = (visit_target - now_local).total_seconds() / 86400
    if 0 <= days_ahead <= 9:
        return visit_target.strftime("%Y-%m-%dT%H:%M"), "primary_visit_date_within_10_day_run_horizon", visit_date.isoformat()
    rolling = dt.datetime.combine(now_local.date() + dt.timedelta(days=3), dt.time(5, 0), tzinfo=LOCAL_TZ)
    return rolling.strftime("%Y-%m-%dT%H:%M"), "rolling_plus_3_days_before_visit_window", visit_date.isoformat()


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
    for region_id in CORE_REGION_IDS:
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
                "weather_driver_vs_2025": {
                    "direction": "UNDETERMINED",
                    "strength": "WEAK",
                    "evidence": {"reason": "NO_VERIFIED_CORE_POINT"},
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
            "weather_driver_vs_2025": (history_region or {}).get("weather_driver_vs_2025") or {
                "direction": "UNDETERMINED",
                "strength": "WEAK",
                "evidence": {"reason": "HISTORY_INVALID"},
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
        "visit_dates": {
            region_id: config["regions"][region_id].get("visit_dates", [])
            for region_id in config["regions"]
        },
        "regions": regions,
        "manual_phenology_baseline": config.get("manual_phenology_baseline"),
        "interpretation_boundary": "This file reports weather drivers and weather event risk. It does not produce a final autumn-colour or phenology conclusion.",
    }


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
        raw_dir = child / "raw"
        if archive_date < cutoff and raw_dir.is_dir():
            # Retention is intentionally limited to generated raw snapshots only.
            shutil.rmtree(raw_dir)


def write_outputs(
    *,
    now_local: dt.datetime,
    status: dict,
    hres: dict,
    history: dict,
    ensemble: dict,
    gfs: dict,
    single_runs: dict,
    spatial: dict,
    long_range: dict,
    summary: dict,
) -> None:
    artifacts = {
        "status.json": status,
        "hres.json": hres,
        "history_comparison.json": history,
        "ensemble.json": ensemble,
        "gfs.json": gfs,
        "single_runs.json": single_runs,
        "spatial_sampling.json": spatial,
        "long_range.json": public_long_range_artifact(long_range),
        "summary.json": summary,
    }
    for filename, artifact in artifacts.items():
        write_json(LATEST_DIR / filename, artifact)
    archive_path = ARCHIVE_DIR / now_local.date().isoformat()
    archive_path.mkdir(parents=True, exist_ok=True)
    for filename, artifact in artifacts.items():
        name = filename.removesuffix(".json")
        write_json(archive_path / filename, compact_module(name, artifact))
    raw_values = {
        "hres.json.gz": hres,
        "history_comparison.json.gz": history,
        "ensemble.json.gz": ensemble,
        "gfs.json.gz": gfs,
        "single_runs.json.gz": single_runs,
        "spatial_sampling.json.gz": spatial,
        "long_range.json.gz": long_range,
    }
    for filename, artifact in raw_values.items():
        write_gzip_json(archive_path / "raw" / filename, artifact)
    prune_old_raw_archives(now_local.date())


def build_status(config: dict, generated_at: str, data_date: str, modules: dict[str, dict]) -> dict:
    module_names = ("hres", "history", "ensemble", "gfs", "single_runs")
    module_values = {name: modules.get(name, {}).get("status", "FAILED") for name in module_names}
    long_range_status = modules.get("long_range", {}).get("status", "FAILED")
    if all(value == "OK" for value in module_values.values()):
        pipeline_status = "OK" if long_range_status == "OK" else "PARTIAL"
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
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "data_date": data_date,
        "pipeline_status": pipeline_status,
        "modules": module_values | {
            "spatial_sampling": modules.get("spatial_sampling", {}).get("status", "FAILED"),
            "long_range": long_range_status,
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


def minimal_failure_status(generated_at: str, reason: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "data_date": None,
        "pipeline_status": "FAILED",
        "modules": {"hres": "FAILED", "history": "FAILED", "ensemble": "FAILED", "gfs": "FAILED", "single_runs": "FAILED", "spatial_sampling": "FAILED", "long_range": "FAILED"},
        "module_details": {"pipeline": {"status": "FAILED", "error": reason}},
        "points": {},
        "route_slots": {},
        "failure_policy": "Pipeline initialization failed before data retrieval.",
    }


def run_pipeline(now_utc: dt.datetime | None = None) -> dict:
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
        modules["history"] = run_history(config, client, generated_at, data_date, now_local.date() - dt.timedelta(days=1))
    except Exception as error:
        modules["history"] = failed_module("history", generated_at, data_date, error)

    log("PHASE 3: SPATIAL SAMPLING")
    try:
        modules["spatial_sampling"] = run_spatial(config, client, generated_at, data_date, modules["hres"])
    except Exception as error:
        modules["spatial_sampling"] = failed_module("spatial_sampling", generated_at, data_date, error)

    log("PHASE 4: GFS")
    try:
        modules["gfs"] = run_gfs(config, client, generated_at, data_date)
    except Exception as error:
        modules["gfs"] = failed_module("gfs", generated_at, data_date, error)

    log("PHASE 5: ECMWF ENSEMBLE")
    try:
        modules["ensemble"] = run_ensemble(config, client, generated_at, data_date)
    except Exception as error:
        modules["ensemble"] = failed_module("ensemble", generated_at, data_date, error)

    log("PHASE 6: SINGLE RUNS")
    try:
        modules["single_runs"] = run_single_runs(config, client, generated_at, data_date, now_utc)
    except Exception as error:
        modules["single_runs"] = failed_module("single_runs", generated_at, data_date, error)

    log("PHASE 7: GFS ENSEMBLE LONG RANGE")
    try:
        modules["long_range"] = run_long_range(config, client, generated_at, data_date, now_local)
    except Exception as error:
        modules["long_range"] = failed_module(
            "long_range",
            generated_at,
            data_date,
            error,
            artifact_module="long_range_background",
        )

    log("PHASE 8: SUMMARY")
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
            "regions": {},
            "error": f"{type(error).__name__}:{error}",
            "interpretation_boundary": "Summary unavailable; inspect status.json and module artifacts.",
        }
    modules["summary"] = {"status": "OK" if "error" not in summary else "FAILED"}
    status = build_status(config, generated_at, data_date, modules)
    write_outputs(
        now_local=now_local,
        status=status,
        hres=modules["hres"],
        history=modules["history"],
        ensemble=modules["ensemble"],
        gfs=modules["gfs"],
        single_runs=modules["single_runs"],
        spatial=modules["spatial_sampling"],
        long_range=modules["long_range"],
        summary=summary,
    )
    log(f"PIPELINE STATUS: {status['pipeline_status']}")
    for name, value in status["modules"].items():
        log(f"MODULE {name}: {value}")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", help="override current time with an ISO-8601 timestamp for reproducible runs")
    args = parser.parse_args(argv)
    try:
        run_pipeline(now_from_input(args.now))
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
