import sys
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


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
        self.assertNotIn("H1", active)
        self.assertEqual(excluded["H1"]["status"], "PROVISIONAL")
        self.assertFalse(excluded["H1"]["usable_for_main_chain"])
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
        self.assertNotIn("H1", pipeline.active_points(config))
        self.assertFalse(pipeline.excluded_points(config)["H1"]["usable_for_main_chain"])
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
