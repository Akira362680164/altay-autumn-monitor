import sys
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pipeline


class PipelineUnitTests(unittest.TestCase):
    def make_payload(self, *, model="ecmwf_ifs", timezone="Asia/Shanghai", offset=28800):
        times = ["2026-09-01T00:00", "2026-09-01T01:00"]
        return {
            "latitude": 48.75,
            "longitude": 86.75,
            "elevation": 1664,
            "timezone": timezone,
            "utc_offset_seconds": offset,
            "model": model,
            "hourly": {
                "time": times,
                "temperature_2m": [1, 2],
                "precipitation": [0, 0],
            },
        }

    def make_day(self, day, night_min):
        return {
            "date": day,
            "complete": True,
            "temperature_min_c": night_min,
            "temperature_max_c": 12,
            "temperature_mean_c": 6,
            "night_min_c": night_min,
            "precipitation_mm": 0,
            "snowfall_cm": 0,
            "cloud_cover_mean_pct": 20,
            "cloud_cover_low_mean_pct": 5,
            "wind_speed_mean_kmh": 4,
            "wind_gust_max_kmh": 20,
            "solar_metric": {"variable": "sunshine_duration", "value": 100, "unit": "seconds"},
        }

    def make_long_range_hourly(self, days=36):
        start = datetime(2026, 9, 2)
        times = []
        for day in range(days):
            for hour in (0, 6, 12, 18):
                times.append((start + timedelta(days=day, hours=hour)).strftime("%Y-%m-%dT%H:%M"))
        hourly = {"time": times}
        for variable in pipeline.LONG_RANGE_VARIABLES:
            base_values = []
            for index in range(len(times)):
                day = index // 4
                if variable == "temperature_2m":
                    value = 12 - day * 0.1 + (index % 4) * 0.2
                elif variable == "precipitation":
                    value = 0.1 if day % 3 == 0 else 0
                elif variable == "snowfall":
                    value = 0.02 if day % 5 == 0 else 0
                else:
                    value = 20 + (day % 4)
                base_values.append(value)
            hourly[variable] = base_values
            for member in range(1, pipeline.LONG_RANGE_ENSEMBLE_MEMBERS):
                suffix = f"_member{member:02d}"
                hourly[f"{variable}{suffix}"] = [value + member * 0.01 for value in base_values]
        return hourly

    def test_haversine_distance(self):
        self.assertEqual(pipeline.haversine_km(0, 0, 0, 0), 0)
        self.assertAlmostEqual(pipeline.haversine_km(48.69583, 86.78382, 48.75, 86.75), 6.514, places=2)

    def test_verified_filter_and_provisional_rejection(self):
        config = pipeline.load_config()
        active = pipeline.active_points(config)
        excluded = pipeline.excluded_points(config)
        self.assertIn("B1", active)
        self.assertIn("H1", active)
        self.assertIn("H2", active)
        self.assertIn("H3", active)
        self.assertIn("H4", active)
        self.assertNotIn("K4", active)
        self.assertEqual(excluded["K4"]["status"], "PROVISIONAL")
        self.assertFalse(excluded["K4"]["usable_for_main_chain"])
        self.assertEqual(excluded["AHE_ROAD_G681"]["status"], "ROUTE_NOT_VERIFIED")

    def test_grid_cell_deduplication(self):
        record_a = {"response": {"grid_coordinate": {"latitude": 48.75, "longitude": 86.75}}}
        record_b = {"response": {"grid_coordinate": {"latitude": 48.7500001, "longitude": 86.75}}}
        self.assertEqual(pipeline.record_grid_cell_key(record_a), pipeline.record_grid_cell_key(record_b))

        samples = [
            {"status": "PASS", "grid_cell_key": "48.75,86.75", "daily": [self.make_day("2026-09-01", 4), self.make_day("2026-09-02", 6)]},
            {"status": "PASS", "grid_cell_key": "48.75,86.75", "daily": [self.make_day("2026-09-01", 4), self.make_day("2026-09-02", 6)]},
            {"status": "PASS", "grid_cell_key": "48.80,86.75", "daily": [self.make_day("2026-09-01", 6), self.make_day("2026-09-02", 6)]},
        ]
        summary = pipeline.spatial_region_summary("baihaba", samples)
        self.assertEqual(summary["unique_model_cells"], 2)
        self.assertEqual(summary["duplicate_requested_samples"], 1)
        self.assertEqual(summary["cold_pool_coverage"]["next_7d"]["cold_cells"], 1)
        self.assertEqual(summary["cold_pool_coverage"]["next_7d"]["total_cells"], 2)
        self.assertEqual(summary["cold_pool_coverage"]["next_7d"]["label"], "mixed")

    def test_kanas_subregion_registry_keeps_provisional_points_out(self):
        config = pipeline.load_config()
        registry = pipeline.kanas_subregion_registry(config)
        self.assertEqual(set(registry), {"sanwan", "lake", "guanyutai"})
        self.assertEqual(registry["sanwan"]["point_ids"], ["K1", "K2", "K3"])
        self.assertEqual(registry["lake"]["point_ids"], ["K4", "K5", "K6"])
        self.assertEqual(registry["guanyutai"]["point_ids"], ["K7", "K8", "K9"])
        self.assertEqual(pipeline.kanas_subregion_point_ids(config, "lake", verified_only=True), ["K5"])
        self.assertEqual(pipeline.kanas_subregion_point_ids(config, "guanyutai", verified_only=True), ["K7"])
        self.assertNotIn("K4", pipeline.active_points(config))
        self.assertNotIn("K6", pipeline.history_forward_point_ids(config))
        self.assertNotIn("K8", pipeline.history_forward_point_ids(config))

    def test_hemu_subregion_registry_keeps_routes_out(self):
        config = pipeline.load_config()
        registry = pipeline.hemu_subregion_registry(config)
        self.assertEqual(set(registry), {"valley", "backhill"})
        self.assertEqual(registry["valley"]["point_ids"], ["H1", "H3"])
        self.assertEqual(registry["backhill"]["point_ids"], ["H2", "H4"])
        self.assertEqual(
            pipeline.hemu_subregion_point_ids(config, "valley", verified_only=True),
            ["H1", "H3"],
        )
        self.assertEqual(
            pipeline.hemu_subregion_point_ids(config, "backhill", verified_only=True),
            ["H2", "H4"],
        )
        point_ids = pipeline.history_forward_point_ids(config)
        self.assertTrue({"H1", "H2", "H3", "H4"}.issubset(point_ids))
        self.assertNotIn("AHE_ROAD_G681", point_ids)
        self.assertNotIn("G331", point_ids)

    def test_hemu_composite_uses_equal_subregion_weighting(self):
        definition = {
            "start_date": "2026-09-02",
            "end_date": "2026-09-09",
        }
        subregions = [
            {"status": "OK", "days_available": 8, "metrics": {"temperature_mean_c": 2}},
            {"status": "OK", "days_available": 8, "metrics": {"temperature_mean_c": 10}},
        ]
        composite = pipeline.equal_mean_subregion_window(subregions, definition, region_id="hemu")
        self.assertEqual(composite["status"], "OK")
        self.assertEqual(composite["metrics"]["temperature_mean_c"], 6.0)
        self.assertIn("equal_mean_of_hemu_subregions", composite["aggregation"])

    def test_kanas_unique_grid_sampling_and_equal_grid_mean(self):
        config = pipeline.load_config()

        def record(point_id, grid, value):
            point = config["points"][point_id]
            daily = []
            for offset in range(8):
                day = self.make_day((date(2026, 9, 2) + timedelta(days=offset)).isoformat(), 2)
                day["temperature_mean_c"] = value
                day["temperature_min_c"] = value - 3
                day["temperature_max_c"] = value + 3
                day["night_min_c"] = value - 4
                daily.append(day)
            return {
                "point_id": point_id,
                "status": "PASS",
                "request": {"coordinate": {"latitude": point["latitude"], "longitude": point["longitude"]}},
                "response": {"grid_coordinate": grid, "returned_elevation": 1900, "timezone": "Asia/Shanghai"},
                "qa": {"final_status": "PASS", "grid_distance_km": 2, "grid_distance_limit_km": 14},
                "daily": daily,
            }

        records = {
            "K1": record("K1", {"latitude": 48.75, "longitude": 87.0}, 10),
            "K2": record("K2", {"latitude": 48.7500001, "longitude": 87.0}, 10),
            "K3": record("K3", {"latitude": 48.5, "longitude": 87.0}, 0),
        }
        sampling = pipeline.grid_sampling_summary(config, ["K1", "K2", "K3"], records, minimum_verified_unique_grids=2)
        self.assertEqual(sampling["status"], "OK")
        self.assertEqual(sampling["unique_model_grids"], 2)
        self.assertEqual(sampling["point_to_grid"][0]["requested_coordinate"]["latitude"], 48.65688)
        self.assertEqual(sampling["unique_grid_mappings"][0]["point_ids"], ["K1", "K2"])
        definition = pipeline.history_forward_windows_for_year(date(2026, 9, 2), 2026)["d0_7"]
        aggregate = pipeline.aggregate_grid_window(list(records.values()), definition)
        self.assertEqual(aggregate["status"], "OK")
        self.assertEqual(aggregate["metrics"]["temperature_mean_c"], 5.0)
        self.assertEqual(aggregate["metrics"]["night_min_mean_c"], 1.0)

    def test_kanas_composite_preserves_equal_subregion_temperature_trend(self):
        definition = pipeline.history_forward_windows_for_year(date(2026, 9, 2), 2026)["d0_7"]
        items = [
            {
                "status": "OK",
                "expected_days": 8,
                "days_available": 8,
                "metrics": {
                    "temperature_mean_c": 6,
                    "temperature_trend": {
                        "first_3_days_mean_temperature_c": 8,
                        "last_3_days_mean_temperature_c": 4,
                        "last_3_minus_first_3_mean_temperature_c": -4,
                    },
                },
            },
            {
                "status": "OK",
                "expected_days": 8,
                "days_available": 8,
                "metrics": {
                    "temperature_mean_c": 8,
                    "temperature_trend": {
                        "first_3_days_mean_temperature_c": 7,
                        "last_3_days_mean_temperature_c": 8,
                        "last_3_minus_first_3_mean_temperature_c": 1,
                    },
                },
            },
            {
                "status": "OK",
                "expected_days": 8,
                "days_available": 8,
                "metrics": {
                    "temperature_mean_c": 10,
                    "temperature_trend": {
                        "first_3_days_mean_temperature_c": 9,
                        "last_3_days_mean_temperature_c": 12,
                        "last_3_minus_first_3_mean_temperature_c": 3,
                    },
                },
            },
        ]
        aggregate = pipeline.equal_mean_subregion_window(items, definition)
        self.assertEqual(aggregate["status"], "OK")
        self.assertEqual(aggregate["metrics"]["temperature_mean_c"], 8.0)
        self.assertEqual(aggregate["metrics"]["temperature_trend"]["first_3_days_mean_temperature_c"], 8.0)
        self.assertEqual(aggregate["metrics"]["temperature_trend"]["last_3_days_mean_temperature_c"], 8.0)
        self.assertEqual(aggregate["metrics"]["temperature_trend"]["last_3_minus_first_3_mean_temperature_c"], 0.0)
        self.assertEqual(aggregate["metrics"]["temperature_trend"]["direction"], "NEAR_FLAT")

    def test_lightweight_summary_is_schema_valid_and_contains_no_raw_series(self):
        config = pipeline.load_config()

        def fake_fetch(_client, **kwargs):
            point = kwargs["point"]
            start = date.fromisoformat(kwargs["params"]["start_date"])
            end = date.fromisoformat(kwargs["params"]["end_date"])
            daily = []
            cursor = start
            while cursor <= end:
                daily.append(self.make_day(cursor.isoformat(), 4))
                cursor += timedelta(days=1)
            return {
                "point_id": point["id"],
                "point": point,
                "status": "PASS",
                "source": "Open-Meteo",
                "endpoint": pipeline.OPEN_METEO_ENDPOINTS["history"],
                "model": "ECMWF IFS 9 km historical weather / analysis",
                "request": {"coordinate": {"latitude": point["latitude"], "longitude": point["longitude"]}},
                "response": {"grid_coordinate": {"latitude": 48.75, "longitude": 87.0}, "returned_elevation": 1900, "timezone": "Asia/Shanghai"},
                "qa": {"final_status": "PASS", "grid_distance_km": 2, "grid_distance_limit_km": pipeline.HISTORY_GRID_QA_LIMIT_KM},
                "solar_variable": "sunshine_duration",
                "daily": daily,
            }

        with patch.object(pipeline, "fetch_point", side_effect=fake_fetch):
            forward = pipeline.run_history_forward(
                config,
                object(),
                "2026-09-02T00:00:00Z",
                "2026-09-01",
                date(2026, 9, 2),
            )
        hres_points = {}
        for point_id in pipeline.history_forward_point_ids(config):
            point = config["points"][point_id]
            hres_points[point_id] = {
                "point_id": point_id,
                "status": "PASS",
                "request": {"coordinate": {"latitude": point["latitude"], "longitude": point["longitude"]}},
                "response": {"grid_coordinate": {"latitude": 48.75, "longitude": 87.0}, "returned_elevation": 1900, "timezone": "Asia/Shanghai"},
                "qa": {"final_status": "PASS", "grid_distance_km": 2, "grid_distance_limit_km": pipeline.HRES_GRID_QA_LIMIT_KM},
                "daily": [self.make_day((date(2026, 9, 2) + timedelta(days=offset)).isoformat(), 4) for offset in range(15)],
            }
        light = pipeline.build_phenology_weather_summary(
            config,
            "2026-09-02T00:00:00Z",
            "2026-09-01",
            date(2026, 9, 2),
            {"points": hres_points},
            forward,
        )
        with (ROOT / "schemas" / "phenology_weather_summary.schema.json").open(encoding="utf-8") as handle:
            schema = json.load(handle)
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(light)), [])
        self.assertEqual(light["regions"]["kanas"]["composite"]["years"]["2023"]["d0_7"]["start_date"], "2023-09-02")
        self.assertEqual(light["regions"]["kanas"]["composite"]["years"]["2023"]["d0_7"]["end_date"], "2023-09-09")
        self.assertEqual(light["regions"]["kanas"]["composite"]["years"]["2026"]["d0_7"]["status"], "OK")
        self.assertEqual(light["regions"]["kanas"]["composite"]["years"]["2026"]["d8_15"]["status"], "PARTIAL")
        self.assertIsNotNone(light["regions"]["kanas"]["composite"]["years"]["2026"]["d8_15"]["temperature_mean_c"])
        self.assertEqual(set(light["regions"]["hemu"]["subregions"]), {"valley", "backhill"})
        self.assertEqual(
            light["regions"]["hemu"]["composite"]["years"]["2023"]["d0_7"]["start_date"],
            "2023-09-02",
        )
        self.assertEqual(
            light["regions"]["hemu"]["composite"]["years"]["2026"]["d0_7"]["status"],
            "OK",
        )
        serialized = json.dumps(light, ensure_ascii=False).lower()
        self.assertNotIn('"hourly"', serialized)
        self.assertNotIn('"daily"', serialized)
        for forbidden in ("actual_phenology_lead_days", "yellow_leaf_percentage", "旅游建议"):
            self.assertNotIn(forbidden, serialized)

    def test_edge_missing_rows_are_trimmed_and_audited(self):
        payload = self.make_payload()
        payload["hourly"]["time"] = ["a", "b", "c"]
        payload["hourly"]["temperature_2m"] = [None, 1, None]
        payload["hourly"]["precipitation"] = [None, 0, None]
        trimmed, audit = pipeline.trim_incomplete_edge_rows(payload, ["temperature_2m", "precipitation"])
        self.assertEqual(trimmed["hourly"]["time"], ["b"])
        self.assertEqual(audit["leading_missing_rows"], 1)
        self.assertEqual(audit["trailing_missing_rows"], 1)
        self.assertEqual(audit["horizon_status"], "TRUNCATED_EDGE_MISSING")

    def test_model_timezone_and_grid_qa_failures(self):
        point = {"latitude": 48.75, "longitude": 86.75}
        bad_model = pipeline.validate_payload(
            self.make_payload(model="wrong_model"), point, "ecmwf_ifs", ["temperature_2m", "precipitation"], 1
        )
        self.assertFalse(bad_model["valid"])
        self.assertIn("MODEL_MISMATCH", bad_model["reason"])

        bad_timezone = pipeline.validate_payload(
            self.make_payload(timezone="UTC", offset=0), point, "ecmwf_ifs", ["temperature_2m", "precipitation"], 1
        )
        self.assertFalse(bad_timezone["valid"])
        self.assertIn("TIMEZONE_MISMATCH", bad_timezone["reason"])

        far_grid = self.make_payload()
        far_grid["latitude"] = 0
        far_grid["longitude"] = 0
        far_grid_result = pipeline.validate_payload(
            far_grid, point, "ecmwf_ifs", ["temperature_2m", "precipitation"], 1
        )
        self.assertFalse(far_grid_result["valid"])
        self.assertIn("GRID_REPRESENTATIVENESS_FAIL", far_grid_result["reason"])

    def test_missing_api_data_is_invalid(self):
        payload = self.make_payload()
        del payload["hourly"]["precipitation"]
        result = pipeline.validate_payload(
            payload, {"latitude": 48.75, "longitude": 86.75}, "ecmwf_ifs", ["temperature_2m", "precipitation"], 1
        )
        self.assertFalse(result["valid"])
        self.assertIn("MISSING_DATA:precipitation", result["reason"])

    def test_threshold_and_consecutive_cold_night_metrics(self):
        days = [
            self.make_day("2026-08-25", 4),
            self.make_day("2026-08-26", 3),
            self.make_day("2026-08-27", 6),
            self.make_day("2026-08-29", 1),
        ]
        metrics = pipeline.period_metrics(days)
        self.assertEqual(metrics["threshold_nights"]["below_5_c"], 3)
        below_5 = metrics["consecutive_cold_nights"]["below_5_c"]
        self.assertEqual(below_5["max_consecutive"], 2)
        self.assertEqual(below_5["sequences"], [
            {"start_date": "2026-08-25", "end_date": "2026-08-26", "nights": 2},
            {"start_date": "2026-08-29", "end_date": "2026-08-29", "nights": 1},
        ])

    def test_history_year_matching_and_weather_only_boundary(self):
        config = pipeline.load_config()
        requested_coordinate = {"latitude": 48.69583, "longitude": 86.78382}
        returned_grid = {"latitude": 48.75, "longitude": 86.75}
        point_results = {
            "B1": {
                "point": {"region": "baihaba"},
                "years": {
                    str(year): {
                        "status": "PASS",
                        "daily": [self.make_day(f"{year}-08-25", 8 if year < 2026 else 3)] * 3,
                        "request": {"coordinate": requested_coordinate},
                        "response": {"grid_coordinate": returned_grid},
                    }
                    for year in (2023, 2024, 2025, 2026)
                },
            }
        }
        comparison = pipeline.build_history_comparison(config, point_results, "2026-08-27")
        self.assertEqual(set(comparison["points"]["B1"]["metrics"]), {"2023", "2024", "2025", "2026"})
        self.assertEqual(comparison["points"]["B1"]["metrics"]["2025"]["period_start"], "2025-08-25")
        self.assertEqual(comparison["points"]["B1"]["metrics"]["2026"]["period_start"], "2026-08-25")
        self.assertEqual(comparison["points"]["B1"]["same_grid_qa"]["checked_years"], ["2023", "2024", "2025", "2026"])
        self.assertEqual(comparison["points"]["B1"]["same_grid_qa"]["final_status"], "PASS")
        self.assertIn("delta_2026_minus_2023", comparison["points"]["B1"])
        self.assertIn("delta_2026_minus_2024", comparison["points"]["B1"])
        self.assertIn("weather_driver_vs_2023", comparison["points"]["B1"])
        self.assertIn("weather_driver_vs_2024", comparison["points"]["B1"])
        self.assertEqual(comparison["regions"]["baihaba"]["status"], "OK")
        self.assertNotIn("actual_phenology_lead_days", comparison["points"]["B1"])

    def test_history_grid_mismatch_fails_all_year_comparison(self):
        config = pipeline.load_config()
        point_results = {
            "B1": {
                "point": {"region": "baihaba"},
                "years": {
                    str(year): {
                        "status": "PASS",
                        "daily": [self.make_day(f"{year}-08-25", 4)] * 3,
                        "request": {"coordinate": {"latitude": 48.69583, "longitude": 86.78382}},
                        "response": {
                            "grid_coordinate": {"latitude": 48.75, "longitude": 86.75}
                            if year != 2024
                            else {"latitude": 48.80, "longitude": 86.75}
                        },
                    }
                    for year in (2023, 2024, 2025, 2026)
                },
            }
        }
        comparison = pipeline.build_history_comparison(config, point_results, "2026-08-27")
        point = comparison["points"]["B1"]
        self.assertEqual(point["status"], "FAILED")
        self.assertEqual(point["same_grid_qa"]["status"], "FAIL")
        self.assertEqual(point["same_grid_qa"]["final_status"], "FAILED")
        self.assertEqual(point["same_grid_qa"]["reason"], "HISTORICAL_GRID_MISMATCH")
        self.assertEqual(point["delta_2026_minus_2023"], {})

    def test_history_year_configuration_is_namespace_specific(self):
        self.assertEqual(pipeline.history_years_for_config(pipeline.load_config()), (2023, 2024, 2025, 2026))
        self.assertEqual(pipeline.history_years_for_config(pipeline.load_ejina_config()), (2025, 2026))

    def test_run_history_requests_all_configured_years(self):
        config = pipeline.load_config()
        requests = []

        def fake_fetch(_client, **kwargs):
            requests.append(kwargs["params"])
            point = kwargs["point"]
            day = kwargs["params"]["start_date"]
            hourly = {
                "time": [f"{day}T{hour:02d}:00" for hour in range(24)],
                "temperature_2m": [5] * 24,
                "precipitation": [0] * 24,
                "snowfall": [0] * 24,
                "cloud_cover": [20] * 24,
                "cloud_cover_low": [5] * 24,
                "wind_speed_10m": [4] * 24,
                "wind_gusts_10m": [20] * 24,
            }
            return {
                "point_id": point["id"],
                "point": point,
                "status": "PASS",
                "hourly": hourly,
                "request": {"coordinate": {"latitude": point["latitude"], "longitude": point["longitude"]}},
                "response": {"grid_coordinate": {"latitude": 48.75, "longitude": 86.75}},
                "solar_variable": None,
            }

        with patch.object(pipeline, "fetch_point", side_effect=fake_fetch):
            history = pipeline.run_history(
                config,
                object(),
                "2026-08-28T00:00:00Z",
                "2026-08-27",
                date(2026, 8, 27),
            )

        self.assertEqual(history["history_years"], [2023, 2024, 2025, 2026])
        self.assertEqual(len(requests), len(pipeline.active_points(config)) * 4)
        self.assertEqual(sorted({item["start_date"][:4] for item in requests}), ["2023", "2024", "2025", "2026"])
        self.assertTrue(all(item["models"] == "ecmwf_ifs" for item in requests))
        self.assertTrue(all(item["cell_selection"] == "nearest" for item in requests))
        self.assertTrue(all(item["elevation"] == "nan" for item in requests))
        self.assertTrue(all(item["timezone"] == "Asia/Shanghai" for item in requests))

    def make_history_forward_record(self, point, params, *, grid=None):
        start = date.fromisoformat(params["start_date"])
        end = date.fromisoformat(params["end_date"])
        daily = []
        cursor = start
        while cursor <= end:
            daily.append(self.make_day(cursor.isoformat(), 4))
            cursor += timedelta(days=1)
        returned_grid = grid or {
            "latitude": round(point["latitude"], 6),
            "longitude": round(point["longitude"], 6),
        }
        return {
            "point_id": point["id"],
            "point": point,
            "status": "PASS",
            "source": "Open-Meteo",
            "endpoint": pipeline.OPEN_METEO_ENDPOINTS["history"],
            "model": "ECMWF IFS 9 km historical weather / analysis",
            "request": {
                "coordinate": {"latitude": point["latitude"], "longitude": point["longitude"]},
                "parameters": params,
            },
            "response": {
                "grid_coordinate": returned_grid,
                "returned_elevation": 1000,
                "timezone": "Asia/Shanghai",
            },
            "qa": {
                "final_status": "PASS",
                "grid_distance_km": 0,
                "grid_distance_limit_km": pipeline.HISTORY_GRID_QA_LIMIT_KM,
            },
            "solar_variable": "sunshine_duration",
            "daily": daily,
        }

    def test_history_forward_window_boundaries_roll_and_cutoff(self):
        definitions = pipeline.history_forward_window_definitions(date(2026, 9, 2))
        self.assertEqual(
            [(item["window"], item["start_date"], item["end_date"]) for item in definitions],
            [
                ("d0_7", "2026-09-02", "2026-09-09"),
                ("d8_15", "2026-09-10", "2026-09-17"),
                ("d16_to_10_06", "2026-09-18", "2026-10-06"),
            ],
        )
        translated = pipeline.history_forward_windows_for_year(date(2026, 9, 2), 2023)
        self.assertEqual(translated["d0_7"]["start_date"], "2023-09-02")
        self.assertEqual(translated["d16_to_10_06"]["end_date"], "2023-10-06")
        late = pipeline.history_forward_window_definitions(date(2026, 10, 1))
        self.assertEqual(late[0]["end_date"], "2026-10-06")
        self.assertEqual(late[1]["status"], "UNAVAILABLE")
        self.assertEqual(late[2]["status"], "UNAVAILABLE")
        for item in late:
            for field in ("requested_start_date", "requested_end_date", "start_date", "end_date"):
                if item.get(field):
                    self.assertLessEqual(item[field], "2026-10-06")

    def test_history_forward_fetches_three_years_kanas_subregion_points_and_clips_cutoff(self):
        config = pipeline.load_config()
        requests = []

        def fake_fetch(_client, **kwargs):
            requests.append(kwargs["params"])
            return self.make_history_forward_record(kwargs["point"], kwargs["params"])

        with patch.object(pipeline, "fetch_point", side_effect=fake_fetch):
            result = pipeline.run_history_forward(
                config,
                object(),
                "2026-09-02T00:00:00Z",
                "2026-09-01",
                date(2026, 9, 2),
            )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["history_years"], [2023, 2024, 2025])
        self.assertEqual(result["forecast_date"], "2026-09-02")
        expected_points = pipeline.history_forward_point_ids(config)
        self.assertEqual(result["expected_fetches"], len(expected_points) * 3)
        self.assertEqual(result["successful_fetches"], len(expected_points) * 3)
        self.assertEqual(len(requests), len(expected_points) * 3)
        self.assertEqual(set(result["points"]), set(expected_points))
        for params in requests:
            self.assertEqual(params["models"], "ecmwf_ifs")
            self.assertEqual(params["cell_selection"], "nearest")
            self.assertEqual(params["elevation"], "nan")
            self.assertEqual(params["timezone"], "Asia/Shanghai")
            self.assertEqual(params["end_date"][5:], "10-06")
        for region_id in ("baihaba", "kanas", "hemu", "keketuohai"):
            region = result["regions"][region_id]
            self.assertEqual(region["status"], "OK")
            self.assertTrue(region["cross_year_comparison_usable"])
            for year in ("2023", "2024", "2025"):
                self.assertEqual(region["years"][year]["d0_7"]["start_date"], f"{year}-09-02")
                self.assertEqual(region["years"][year]["d0_7"]["end_date"], f"{year}-09-09")
                self.assertEqual(region["years"][year]["d8_15"]["start_date"], f"{year}-09-10")
                self.assertEqual(region["years"][year]["d8_15"]["end_date"], f"{year}-09-17")
                self.assertEqual(region["years"][year]["d16_to_10_06"]["end_date"], f"{year}-10-06")
                self.assertTrue(all(day["date"] <= f"{year}-10-06" for day in region["years"][year]["d16_to_10_06"]["daily"]))
            self.assertEqual(region["same_grid_qa"]["checked_years"], ["2023", "2024", "2025"])
            self.assertEqual(region["same_grid_qa"]["final_status"], "PASS")
        self.assertEqual(result["regions"]["kanas"]["subregions"]["sanwan"]["status"], "OK")
        self.assertEqual(result["regions"]["kanas"]["subregions"]["lake"]["status"], "OK")
        self.assertEqual(result["regions"]["kanas"]["subregions"]["guanyutai"]["status"], "OK")
        self.assertEqual(result["regions"]["kanas"]["composite"]["status"], "OK")
        for subregion_id in ("sanwan", "lake", "guanyutai"):
            sampling = result["regions"]["kanas"]["subregions"][subregion_id]["sampling"]
            self.assertTrue(sampling["same_unique_grid_set_across_years"])
            self.assertEqual(
                set(sampling["by_year"]),
                {"2023", "2024", "2025"},
            )
        hemu = result["regions"]["hemu"]
        self.assertEqual(hemu["status"], "OK")
        self.assertTrue(hemu["usable_for_main_chain"])
        self.assertEqual(set(hemu["subregions"]), {"valley", "backhill"})
        self.assertEqual(hemu["composite"]["status"], "OK")
        self.assertEqual(set(hemu["subregions"]["valley"]["point_ids"]), {"H1", "H3"})
        self.assertEqual(set(hemu["subregions"]["backhill"]["point_ids"]), {"H2", "H4"})
        self.assertIn("H1", result["points"])

    def test_history_forward_same_grid_failure_blocks_cross_year_comparison(self):
        config = pipeline.load_config()
        def fake_fetch(_client, **kwargs):
            year = int(kwargs["params"]["start_date"][:4])
            grid = {"latitude": 48.75, "longitude": 86.75} if year != 2024 else {"latitude": 48.80, "longitude": 86.75}
            return self.make_history_forward_record(kwargs["point"], kwargs["params"], grid=grid)

        with patch.object(pipeline, "fetch_point", side_effect=fake_fetch):
            result = pipeline.run_history_forward(
                config,
                object(),
                "2026-09-02T00:00:00Z",
                "2026-09-01",
                date(2026, 9, 2),
            )
        self.assertEqual(result["status"], "FAILED")
        point = result["points"]["B1"]
        self.assertEqual(point["same_grid_qa"]["final_status"], "FAILED")
        self.assertFalse(point["cross_year_comparison_usable"])
        self.assertEqual(point["same_grid_qa"]["pairwise"]["2023_vs_2024"], "FAIL")
        self.assertEqual(result["regions"]["baihaba"]["status"], "FAILED")
        self.assertEqual(result["regions"]["hemu"]["status"], "FAILED")
        self.assertEqual(result["regions"]["hemu"]["composite"]["status"], "INVALID")
        self.assertFalse(result["regions"]["hemu"]["composite"]["cross_year_comparison_usable"])

    def test_history_forward_status_failure_does_not_hide_short_chain(self):
        config = pipeline.load_config()
        modules = {name: {"status": "OK"} for name in ("hres", "history", "ensemble", "gfs", "single_runs", "spatial_sampling")}
        modules["history_forward"] = {"status": "FAILED", "error": "TEST"}
        modules["long_range"] = {"status": "OK"}
        status = pipeline.build_status(config, "2026-09-02T00:00:00Z", "2026-09-01", modules)
        self.assertEqual(status["pipeline_status"], "PARTIAL")
        self.assertEqual(status["modules"]["history_forward"], "FAILED")
        self.assertEqual(status["modules"]["hres"], "OK")

    def test_history_forward_schema_and_weather_only_boundary(self):
        config = pipeline.load_config()
        record = self.make_history_forward_record(pipeline.active_points(config)["B1"], {
            "models": "ecmwf_ifs",
            "cell_selection": "nearest",
            "elevation": "nan",
            "timezone": "Asia/Shanghai",
            "start_date": "2023-09-02",
            "end_date": "2023-10-06",
        })
        windows = pipeline.history_forward_windows_for_year(date(2026, 9, 2), 2023)
        for key, definition in windows.items():
            record[key] = pipeline.history_forward_window_summary(record["daily"], definition)
        qa = pipeline.history_forward_same_grid_qa({str(year): record for year in (2023, 2024, 2025)})
        result = pipeline.module_header(
            "history_forward",
            "2026-09-02T00:00:00Z",
            "2026-09-01",
            "OK",
            forecast_date="2026-09-02",
            anchor_date="2026-09-02",
            cutoff_date="2026-10-06",
            history_years=[2023, 2024, 2025],
            window_definitions=pipeline.history_forward_window_definitions(date(2026, 9, 2)),
            points={"B1": {"point_id": "B1", "status": "OK", "same_grid_qa": qa, "years": {str(year): record for year in (2023, 2024, 2025)}}},
            regions={"hemu": {"region": "hemu", "status": "UNAVAILABLE", "usable_for_main_chain": False, "cross_year_comparison_usable": False, "same_grid_qa": None, "years": {}}},
            excluded_points={},
        )
        with (ROOT / "schemas" / "history_forward.schema.json").open(encoding="utf-8") as handle:
            schema = json.load(handle)
        errors = list(Draft202012Validator(schema).iter_errors(result))
        self.assertEqual(errors, [])
        text = json.dumps(result, ensure_ascii=False).lower()
        for forbidden in ("phenology", "autumn", "黄叶", "物候", "旅游", "worth_going"):
            self.assertNotIn(forbidden, text)

    def test_history_forward_is_written_to_latest_and_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history_forward = {"module": "history_forward", "status": "OK", "points": {}, "regions": {}}
            with patch.object(pipeline, "LATEST_DIR", root / "latest"), patch.object(pipeline, "ARCHIVE_DIR", root / "archive"):
                pipeline.write_outputs(
                    now_local=datetime(2026, 9, 2, 10, tzinfo=pipeline.LOCAL_TZ),
                    status={},
                    hres={},
                    history={},
                    history_forward=history_forward,
                    ensemble={},
                    gfs={},
                    single_runs={},
                    spatial={},
                    long_range={},
                    summary={},
                    grid_registry={"module": "grid_registry"},
                    phenology_weather_summary={"module": "phenology_weather_summary"},
                )
            self.assertTrue((root / "latest" / "history_forward.json").is_file())
            self.assertTrue((root / "archive" / "2026-09-02" / "history_forward.json").is_file())
            self.assertTrue((root / "archive" / "2026-09-02" / "raw" / "history_forward.json.gz").is_file())
            self.assertTrue((root / "latest" / "grid_registry.json").is_file())
            self.assertTrue((root / "latest" / "phenology_weather_summary.json").is_file())
            self.assertTrue((root / "archive" / "2026-09-02" / "phenology_weather_summary.json").is_file())

    def test_failed_module_is_explicit_in_status(self):
        config = pipeline.load_config()
        modules = {name: {"status": "FAILED"} for name in ("hres", "history", "ensemble", "gfs", "single_runs")}
        status = pipeline.build_status(config, "2026-09-02T00:00:00Z", "2026-09-01", modules)
        self.assertEqual(status["pipeline_status"], "FAILED")
        self.assertEqual(status["modules"]["ensemble"], "FAILED")

    def test_stable_summary_schema_contract(self):
        schema_path = ROOT / "schemas" / "summary.schema.json"
        with schema_path.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        self.assertEqual(schema["properties"]["schema_version"]["const"], pipeline.SCHEMA_VERSION)
        required = set(schema["required"])
        self.assertTrue({"data_date", "regions", "interpretation_boundary"}.issubset(required))
        self.assertIn("history_years", schema["properties"])
        region_required = set(schema["properties"]["regions"]["additionalProperties"]["required"])
        self.assertIn("weather_driver_vs_2025", region_required)
        self.assertIn("weather_driver_vs_2023", schema["properties"]["regions"]["additionalProperties"]["properties"])
        self.assertIn("weather_driver_vs_2024", schema["properties"]["regions"]["additionalProperties"]["properties"])

        compact_schema_path = ROOT / "schemas" / "phenology_weather_summary.schema.json"
        with compact_schema_path.open(encoding="utf-8") as handle:
            compact_schema = json.load(handle)
        self.assertIn("hemu_aggregation", compact_schema["properties"]["model_policy"].get("properties", {}))

    def test_zero_values_are_not_treated_as_missing_in_risk_checks(self):
        wet_snow_day = self.make_day("2026-09-01", 0)
        wet_snow_day["snowfall_cm"] = 0.2
        wet_snow_day["precipitation_mm"] = 1.0
        risk = pipeline.leaf_loss_weather_risk([wet_snow_day], date(2026, 9, 1))
        self.assertIn("wet_snow", risk["drivers"])
        self.assertIn("rain_snow", risk["drivers"])

        hres_record = {"status": "PASS", "daily": [self.make_day("2026-09-01", 0)]}
        gfs_record = {"status": "PASS", "daily": [self.make_day("2026-09-01", 0)]}
        crosscheck = pipeline.gfs_crosscheck(hres_record, gfs_record)
        self.assertEqual(crosscheck["cold_window_agreement"], "AGREE")

    def test_long_range_current_model_contract(self):
        self.assertEqual(pipeline.LONG_RANGE_MODEL_ID, "ncep_gefs05")
        self.assertEqual(pipeline.LONG_RANGE_ENSEMBLE_MEMBERS, 31)
        self.assertEqual(pipeline.LONG_RANGE_REQUESTED_FORECAST_DAYS, 36)
        self.assertEqual(
            pipeline.OPEN_METEO_ENDPOINTS["ensemble"],
            "https://ensemble-api.open-meteo.com/v1/ensemble",
        )

    def test_long_range_member_count_and_array_consistency(self):
        hourly = self.make_long_range_hourly()
        valid, check = pipeline.validate_long_range_members({"hourly": hourly})
        self.assertTrue(valid)
        self.assertEqual(check["actual_member_counts_by_variable"]["temperature_2m"], 31)

        del hourly["snowfall_member30"]
        valid, check = pipeline.validate_long_range_members({"hourly": hourly})
        self.assertFalse(valid)
        self.assertIn("snowfall:expected_31_got_30", check["missing_or_wrong_count"])

        hourly = self.make_long_range_hourly()
        hourly["wind_gusts_10m_member01"] = hourly["wind_gusts_10m_member01"][:-1]
        valid, check = pipeline.validate_long_range_members({"hourly": hourly})
        self.assertFalse(valid)
        self.assertIn("wind_gusts_10m_member01", check["array_length_mismatch"])

        hourly = self.make_long_range_hourly()
        for key in list(hourly):
            if key != "time":
                hourly[key][-1] = None
        valid, check = pipeline.validate_long_range_members({"hourly": hourly})
        self.assertTrue(valid)
        self.assertIn("temperature_2m", check["edge_truncated_variables"])
        self.assertEqual(check["variable_availability"]["temperature_2m"]["last_complete_index"], len(hourly["time"]) - 2)

    def test_long_range_horizon_and_three_day_windows(self):
        hourly = self.make_long_range_hourly()
        origin, daily_by_lead = pipeline.long_range_daily_member_values(hourly)
        self.assertEqual(origin, date(2026, 9, 2))
        horizon = pipeline.long_range_horizon_check(daily_by_lead)
        self.assertEqual(horizon["status"], "PASS")
        self.assertEqual(horizon["actual_lead_days"], 36)
        self.assertEqual(pipeline.long_range_window_definitions()[-1], (34, 35))
        windows = pipeline.build_long_range_windows(origin, daily_by_lead, {})
        self.assertEqual(len(windows), 7)
        self.assertEqual(windows[0]["horizon_class"], "D16_D18")
        self.assertEqual(windows[-1]["horizon_class"], "D34_D35")
        self.assertNotIn("hourly", windows[0])
        partial_windows = pipeline.build_long_range_windows(origin, {lead: values for lead, values in daily_by_lead.items() if lead <= 34}, {})
        partial_windows = pipeline.apply_signal_evolution("baihaba", partial_windows, [])
        self.assertEqual(partial_windows[-1]["status"], "UNAVAILABLE")
        self.assertEqual(partial_windows[-1]["signal_evolution"]["status"], "INSUFFICIENT_HISTORY")
        partial_horizon = pipeline.long_range_horizon_check({lead: values for lead, values in daily_by_lead.items() if lead <= 34})
        self.assertEqual(partial_horizon["status"], "PARTIAL")
        self.assertEqual(partial_horizon["missing_lead_days"], [35])

    def test_long_range_optional_variable_edge_missing_is_undetermined(self):
        hourly = self.make_long_range_hourly()
        for key in [key for key in hourly if key.startswith("precipitation")]:
            hourly[key][-4:] = [None] * 4
        origin, daily_by_lead = pipeline.long_range_daily_member_values(hourly)
        windows = pipeline.build_long_range_windows(origin, daily_by_lead, {})
        last_window = windows[-1]
        self.assertEqual(last_window["status"], "OK")
        self.assertEqual(last_window["precipitation_background"]["signal"], "UNDETERMINED")
        self.assertIsNone(last_window["precipitation_background"]["member_support"])
        self.assertIn(last_window["forecast_uncertainty"], ("HIGH", "VERY_HIGH"))

    def test_long_range_percentiles_thresholds_and_coarse_qa(self):
        self.assertEqual(pipeline.percentile([1, 2, 3, 4], 0.5), 2.5)
        current = {f"member{index:02d}": {
            "temperature_mean_c": 4 + index * 0.1,
            "temperature_min_c": 3,
            "precipitation_mm": 1,
            "snowfall_cm": 0.2,
            "wind_gust_max_kmh": 55,
        } for index in range(31)}
        previous = {key: {**value, "temperature_mean_c": value["temperature_mean_c"] + 3} for key, value in current.items()}
        signal, diagnostics = pipeline.long_range_cold_window_signal(current, previous, "2026-09-18", "2026-09-20")
        self.assertEqual(signal["signal"], "STRONG")
        self.assertEqual(diagnostics["member_count"], 31)
        stats = pipeline.long_range_temperature_stats(current)
        self.assertEqual(stats["median"], 5.5)
        self.assertIn("interquartile_spread", stats)

        point = {"latitude": 48.69583, "longitude": 86.78382}
        coarse = self.make_payload()
        coarse["latitude"] = 48.5
        coarse["longitude"] = 87.0
        coarse["model"] = pipeline.LONG_RANGE_MODEL
        result = pipeline.validate_payload(
            coarse,
            point,
            pipeline.LONG_RANGE_MODEL,
            ["temperature_2m", "precipitation"],
            pipeline.LONG_RANGE_GRID_QA_LIMIT_KM,
        )
        self.assertTrue(result["valid"])
        self.assertEqual(pipeline.LONG_RANGE_GRID_QA_LIMIT_KM, 35.0)

    def test_long_range_uncertainty_and_run_persistence(self):
        uncertainty, drivers = pipeline.long_range_uncertainty({"spread": 12}, [0.5, 0.2], 30)
        self.assertEqual(uncertainty, "VERY_HIGH")
        self.assertIn("longer_lead_time", drivers)

        windows = [{
            "horizon_class": "D16_D18",
            "start_date": "2026-09-18",
            "end_date": "2026-09-20",
            "cold_window_signal": {"signal": "MODERATE", "persistence_runs": 0},
            "forecast_uncertainty": "MODERATE",
            "uncertainty_drivers": [],
        }]
        snapshots = [
            {"generated_at": "2026-09-01T00:00:00Z", "regions": {"baihaba": {"windows": [{"horizon_class": "D16_D18", "start_date": "2026-09-18", "cold_window_signal": {"signal": "MODERATE"}}]}}},
            {"generated_at": "2026-08-31T00:00:00Z", "regions": {"baihaba": {"windows": [{"horizon_class": "D16_D18", "start_date": "2026-09-18", "cold_window_signal": {"signal": "MODERATE"}}]}}},
            {"generated_at": "2026-08-30T00:00:00Z", "regions": {"baihaba": {"windows": [{"horizon_class": "D16_D18", "start_date": "2026-09-18", "cold_window_signal": {"signal": "MODERATE"}}]}}},
        ]
        updated = pipeline.apply_signal_evolution("baihaba", windows, snapshots)
        self.assertEqual(updated[0]["signal_evolution"]["status"], "PERSISTENT")
        self.assertEqual(updated[0]["signal_evolution"]["runs_seen"], 4)
        self.assertEqual(updated[0]["cold_window_signal"]["persistence_runs"], 4)

    def test_long_range_provisional_and_summary_boundaries(self):
        config = pipeline.load_config()
        self.assertIn("H1", pipeline.active_points(config))
        self.assertIn("H4", pipeline.active_points(config))
        self.assertFalse(pipeline.excluded_points(config)["K4"]["usable_for_main_chain"])
        unavailable = pipeline.long_range_summary_for_chatgpt({
            "status": "UNAVAILABLE",
            "reason": "NO_VERIFIED_CORE_POINT",
        })
        self.assertEqual(unavailable, {
            "status": "UNAVAILABLE",
            "reason": "NO_VERIFIED_CORE_POINT",
        })

    def test_long_range_failure_keeps_short_chain_available(self):
        config = pipeline.load_config()
        modules = {
            name: {"status": "OK"}
            for name in ("hres", "history", "ensemble", "gfs", "single_runs", "spatial_sampling")
        }
        modules["long_range"] = {"status": "FAILED", "error": "OPEN_METEO_LONG_RANGE_NOT_AVAILABLE"}
        status = pipeline.build_status(config, "2026-09-02T00:00:00Z", "2026-09-01", modules)
        self.assertEqual(status["pipeline_status"], "PARTIAL")
        self.assertEqual(status["modules"]["hres"], "OK")
        self.assertEqual(status["modules"]["long_range"], "FAILED")

    def test_long_range_public_artifact_omits_raw_members(self):
        artifact = pipeline.public_long_range_artifact({
            "module": "long_range_background",
            "raw_points": {"B1": {"hourly": {"time": ["x"]}}},
            "raw_references": {"baihaba": {"hourly": {"time": ["x"]}}},
            "regions": {},
        })
        self.assertNotIn("raw_points", artifact)
        self.assertNotIn("raw_references", artifact)
        self.assertFalse(artifact["raw_hourly_included"])

    def test_schema_is_additive_v110_and_long_range_contract_exists(self):
        with (ROOT / "schemas" / "summary.schema.json").open(encoding="utf-8") as handle:
            summary_schema = json.load(handle)
        with (ROOT / "schemas" / "long_range.schema.json").open(encoding="utf-8") as handle:
            long_range_schema = json.load(handle)
        self.assertEqual(summary_schema["properties"]["schema_version"]["const"], "1.1.0")
        region_required = set(summary_schema["properties"]["regions"]["additionalProperties"]["required"])
        self.assertTrue({"forecast_0_7d", "forecast_8_15d", "forecast_16_35d"}.issubset(region_required))
        self.assertEqual(long_range_schema["properties"]["model_id"]["const"], "ncep_gefs05")
        self.assertEqual(long_range_schema["properties"]["expected_ensemble_members"]["const"], 31)

    def test_long_range_model_id_mismatch_is_rejected(self):
        payload = self.make_payload(model=pipeline.LONG_RANGE_MODEL)
        payload["model_id"] = "wrong_model_id"
        result = pipeline.validate_payload(
            payload,
            {"latitude": 48.75, "longitude": 86.75},
            pipeline.LONG_RANGE_MODEL,
            ["temperature_2m", "precipitation"],
            1,
            accepted_model_values=(pipeline.LONG_RANGE_MODEL_ID, pipeline.LONG_RANGE_MODEL),
            accepted_model_ids=(pipeline.LONG_RANGE_MODEL_ID,),
        )
        self.assertFalse(result["valid"])
        self.assertIn("MODEL_ID_MISMATCH", result["reason"])

    def test_ejina_registry_has_three_verified_points_and_weather_only_namespace(self):
        config = pipeline.load_ejina_config()
        self.assertEqual(config["namespace"], "ejina")
        self.assertEqual(pipeline.core_region_ids(config), ("erdaoqiao", "sidaoqiao", "qidaoqiao"))
        active = pipeline.active_points(config)
        self.assertEqual(set(active), {"EJ1", "EJ2", "EJ3"})
        self.assertEqual(active["EJ1"]["status"], "VERIFIED")
        self.assertAlmostEqual(active["EJ1"]["latitude"], 41.968333, places=6)
        self.assertAlmostEqual(active["EJ1"]["longitude"], 101.086111, places=6)
        self.assertAlmostEqual(active["EJ2"]["latitude"], 42.0012, places=6)
        self.assertAlmostEqual(active["EJ2"]["longitude"], 101.1374, places=6)
        self.assertAlmostEqual(active["EJ3"]["latitude"], 42.009167, places=6)
        self.assertAlmostEqual(active["EJ3"]["longitude"], 101.231389, places=6)
        self.assertEqual(config["history_start_month_day"], "09-01")
        self.assertEqual(config["forecast_end_date"], "2026-10-07")
        self.assertEqual(set(pipeline.THRESHOLDS_C), {15.0, 10.0, 5.0, 2.0, 0.0})

    def test_ejina_history_start_and_forecast_cutoff(self):
        self.assertEqual(
            pipeline.history_date_range(date(2026, 9, 1), 2025, "09-01"),
            ("2025-09-01", "2025-09-01"),
        )
        self.assertIsNone(pipeline.history_date_range(date(2026, 8, 31), 2026, "09-01"))
        payload = self.make_payload()
        payload["hourly"]["time"] = [
            "2026-10-07T23:00",
            "2026-10-08T00:00",
        ]
        payload["hourly"]["temperature_2m"] = [1, 2]
        payload["hourly"]["precipitation"] = [0, 0]
        trimmed, audit = pipeline.trim_to_forecast_cutoff(payload, date(2026, 10, 7))
        self.assertEqual(trimmed["hourly"]["time"], ["2026-10-07T23:00"])
        self.assertEqual(audit["rows_dropped_after_cutoff"], 1)
        self.assertEqual(audit["forecast_cutoff_date"], "2026-10-07")

    def test_ejina_long_range_window_cutoff_never_emits_later_date(self):
        hourly = self.make_long_range_hourly()
        origin, daily_by_lead = pipeline.long_range_daily_member_values(hourly)
        windows = pipeline.build_long_range_windows(
            origin,
            daily_by_lead,
            {},
            max_date=date(2026, 9, 25),
        )
        self.assertTrue(windows)
        self.assertTrue(all(item["end_date"] <= "2026-09-25" for item in windows))
        self.assertTrue(all(item["start_date"] <= "2026-10-07" for item in windows))

    def test_ejina_single_run_target_works_without_visit_date(self):
        target, policy, visit_date = pipeline.select_single_run_target(
            {"primary_visit_date": None, "forecast_end_date": "2026-10-07"},
            datetime(2026, 9, 2, 0, tzinfo=pipeline.LOCAL_TZ),
        )
        self.assertEqual(target, "2026-09-05T05:00")
        self.assertEqual(policy, "rolling_plus_3_days_no_visit_date")
        self.assertIsNone(visit_date)

    def test_ejina_summary_contains_weather_deltas_without_downstream_conclusions(self):
        config = pipeline.load_ejina_config()
        days_2025 = [self.make_day("2025-09-01", 8)]
        days_2026 = [self.make_day("2026-09-01", 3)]
        metrics_2025 = pipeline.period_metrics(days_2025)
        metrics_2026 = pipeline.period_metrics(days_2026)
        hres_days = [self.make_day(f"2026-09-{day:02d}", 4) for day in range(2, 17)]
        point_record = {"status": "PASS", "daily": hres_days, "qa": {"final_status": "PASS"}}
        modules = {
            "hres": {"points": {point_id: point_record for point_id in ("EJ1", "EJ2", "EJ3")}},
            "history": {
                "region_summaries": {
                    "points": {
                        "EJ1": {
                            "status": "OK",
                            "period_start": "09-01",
                            "period_end": "2026-09-01",
                            "metrics": {"2025": metrics_2025, "2026": metrics_2026},
                            "delta_2026_minus_2025": pipeline.numeric_deltas(metrics_2026, metrics_2025),
                            "same_grid_qa": {"status": "PASS"},
                        }
                    },
                    "regions": {},
                }
            },
            "ensemble": {"model": "ECMWF IFS 0.25° Ensemble", "model_id": "ecmwf_ifs025_ensemble", "total_members": 51, "points": {point_id: point_record for point_id in ("EJ1", "EJ2", "EJ3")}},
            "gfs": {"points": {point_id: point_record for point_id in ("EJ1", "EJ2", "EJ3")}},
            "single_runs": {"regions": {region_id: {"status": "OK", "latest_change": {"status": "NO_CHANGE"}, "run_count_requested": 8, "run_count_available": 8} for region_id in pipeline.core_region_ids(config)}},
            "long_range": {"regions": {region_id: {"status": "UNAVAILABLE", "reason": "TEST"} for region_id in pipeline.core_region_ids(config)}},
        }
        summary = pipeline.build_ejina_summary(
            config,
            "2026-09-02T00:00:00Z",
            "2026-09-01",
            datetime(2026, 9, 2, tzinfo=pipeline.LOCAL_TZ),
            modules["hres"],
            modules["history"],
            modules["ensemble"],
            modules["gfs"],
            modules["single_runs"],
            modules["long_range"],
        )
        self.assertIn("delta_2026_minus_2025", summary["regions"]["erdaoqiao"]["history_comparison"])
        text = json.dumps(summary, ensure_ascii=False).lower()
        for forbidden in ("actual_phenology_lead_days", "best_viewing_period", "worth_going", "tourism_recommendation"):
            self.assertNotIn(forbidden, text)

    def test_ejina_namespace_status_is_independent_from_main_status(self):
        config = pipeline.load_config()
        modules = {name: {"status": "OK"} for name in ("hres", "history", "ensemble", "gfs", "single_runs", "spatial_sampling")}
        modules["long_range"] = {"status": "PARTIAL"}
        status = pipeline.build_status(
            config,
            "2026-09-02T00:00:00Z",
            "2026-09-01",
            modules,
            {"ejina": {"status": "FAILED", "modules": {"hres": "FAILED"}}},
        )
        self.assertEqual(status["modules"]["hres"], "OK")
        self.assertEqual(status["namespaces"]["ejina"]["status"], "FAILED")

    def test_ejina_long_range_snapshot_loader_reads_nested_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "2026-09-01" / "ejina"
            archive.mkdir(parents=True)
            snapshot = {
                "status": "PARTIAL",
                "generated_at": "2026-09-01T00:00:00Z",
                "regions": {},
            }
            (archive / "long_range.json").write_text(json.dumps(snapshot), encoding="utf-8")
            with patch.object(pipeline, "ARCHIVE_DIR", Path(directory)):
                loaded = pipeline.load_long_range_snapshots(date(2026, 9, 2), namespace="ejina")
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["status"], "PARTIAL")


if __name__ == "__main__":
    unittest.main()
