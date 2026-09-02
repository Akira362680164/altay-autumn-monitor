import sys
import json
import unittest
from datetime import date
from pathlib import Path


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
        point_results = {
            "B1": {
                "point": {"region": "baihaba"},
                "years": {
                    "2025": {"status": "PASS", "daily": [self.make_day("2025-08-25", 8)] * 3},
                    "2026": {"status": "PASS", "daily": [self.make_day("2026-08-25", 3)] * 3},
                },
            }
        }
        comparison = pipeline.build_history_comparison(config, point_results, "2026-08-27")
        self.assertEqual(comparison["points"]["B1"]["metrics"]["2025"]["period_start"], "2025-08-25")
        self.assertEqual(comparison["points"]["B1"]["metrics"]["2026"]["period_start"], "2026-08-25")
        self.assertEqual(comparison["regions"]["baihaba"]["status"], "OK")
        self.assertNotIn("actual_phenology_lead_days", comparison["points"]["B1"])

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
        region_required = set(schema["properties"]["regions"]["additionalProperties"]["required"])
        self.assertIn("weather_driver_vs_2025", region_required)

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


if __name__ == "__main__":
    unittest.main()
