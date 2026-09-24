# JSON Schema

`status.json` and `summary.json` are the stable ChatGPT-facing contracts. The module artifacts use the common envelope in `module.schema.json`; module-specific fields are intentionally additive so a new QA detail can be added without changing the meaning of existing fields.

Schema version `1.4.0` is an additive update over `1.3.0`; all v1.0.0–v1.3.0 fields keep their meaning. `COMPATIBLE_SCHEMA_VERSIONS` accepts `1.0.0`, `1.1.0`, `1.2.0`, `1.3.0` and `1.4.0` so existing archives stay readable without backfill.

`1.3.0` added the independent `gefs` module and the compact `summary.json.golden_week_brief` view. GEFS is kept separate from ECMWF Ensemble and is used to test GFS deterministic support, event existence and phase spread; the two ensembles are never averaged. The `weather_events` module and `weather_events_cache` schema describe derived weather events sourced only from the existing Historical cache and the current HRES forecast. A breaking change must increment the major version and update the schemas, tests, README, and Raw entry points together. An omitted or `null` numeric value means that the value was unavailable or failed QA; it is never an inferred replacement value.

## Unified weather variable system (1.4.0)

Every forecast model — ECMWF deterministic (HRES), ECMWF ensemble, GFS deterministic and the independent GEFS ensemble — now requests and reports the same vocabulary:

- temperature: `temperature_2m`, `dew_point_2m`, `relative_humidity_2m`;
- precipitation: `precipitation`, `rain`, `snowfall`;
- cloud: `cloud_cover`, `cloud_cover_low`, `cloud_cover_mid`, `cloud_cover_high`;
- wind: `wind_speed_10m` (sustained), `wind_direction_10m` (circular), `wind_gusts_10m` (gust);
- radiation: `sunshine_duration`, falling back to `shortwave_radiation`.

Rules that the schemas and the pipeline enforce:

1. **Open-Meteo is the only numeric source.** No other weather site, app or model may fill a value.
2. **The four model families stay independent.** Values are compared, never averaged across models, and never averaged between ECMWF ensemble and GEFS.
3. **Layered cloud always comes from the API.** `cloud_cover_mid`/`cloud_cover_high` are never derived by subtracting low cloud from the total; the layers overlap and are not additive. When a source returns null arrays for a layer, that layer stays `OPTIONAL_UNAVAILABLE`.
4. **Required / optional boundary.** A required (core) variable that is unavailable makes the module `PARTIAL`/`FAILED`. An optional variable that is unavailable is recorded as `OPTIONAL_UNAVAILABLE` and must not fail the module.
5. **Gust ≠ sustained wind.** `wind_gusts_10m` and `wind_speed_10m` are separate fields in every artifact.
6. **Wind direction is circular.** It is stored hourly and reduced only with a vector mean; ensemble distributions never contain an arithmetic percentile for `wind_direction_10m`.
7. **Unavailable never becomes a number.** A missing value is `null` (or `UNAVAILABLE` in a status field), never `0`.

### `variable_status` vocabulary

Published per model in `status.json.variable_status` and per variable inside the module artifacts:

| Value | Meaning |
|---|---|
| `OK` | returned and fully usable |
| `PARTIAL` | returned with some null entries |
| `REQUIRED_UNAVAILABLE` | a required variable is absent, wrong length or all-null |
| `OPTIONAL_UNAVAILABLE` | an optional variable is absent, wrong length or all-null |
| `MISSING` | the field was not present in the response |
| `ARRAY_LENGTH_MISMATCH` | the array length differs from the time axis |
| `NULL_ARRAY` | the field is present but every entry is null |

`UNAVAILABLE_STATUSES` groups every value except `OK` and `PARTIAL`. `status.json.variable_model_policy` states that the models are independent and that `cross_model_averaging` is `false`.

### New schema definitions

- `summary.schema.json` gains `$defs.deterministicCompact`, `$defs.ensembleCompact`, `$defs.viewingConditions`, `$defs.fogInputs`, `$defs.goldenWeekWindow`, `$defs.goldenWeekLocation` and `$defs.variableStatus`. The golden-week window exposes `ec_det`, `gfs_det`, `ec_ens`, `gefs`, `gfs_support`, `gfs_deterministic_vs_gefs`, `model_consistency` and `viewing_conditions` for the `MORNING`, `AFTERNOON` and `NIGHT` windows; each location also carries `fog_inputs`.
- `status.schema.json` gains `variable_status` and `variable_model_policy`.
- `gefs.schema.json` gains `requested_variables`, `required_variables`, `optional_variables`, `variable_status` and `cloud_layer_status`.
- `module.schema.json` gains the same requested/required/optional split plus `variable_status`, so every module envelope reports availability the same way.

`viewing_conditions` judges the layers separately: low cloud is terrain obstruction, mid cloud flattens direct light, high cloud adds sky texture and sunrise/sunset potential. High cloud is explicitly **not** treated as bad weather. `fog_inputs` publishes raw overnight indicators only — it never publishes a fog probability.

`ejina_points.schema.json` documents the `config/ejina_points.json` registry and keeps its own registry version; it is not a pipeline output artifact.


The main Altay `history_comparison.json` is configured for `history_years=[2023, 2024, 2025, 2026]`. It preserves the v1.0/v1.1 `2025`/`2026` daily and metrics keys, and adds the older years plus `delta_2026_minus_2023`, `delta_2026_minus_2024`, and `deltas_2026_minus`. The `same_grid_qa` object checks every configured year; a returned-grid mismatch makes the comparison `FAILED` and no historical delta is usable. The Ejina namespace keeps its independent `history_years=[2025, 2026]` configuration.

`history_forward.schema.json` defines the Altay-only historical forward-path artifact. It uses `history_years=[2023, 2024, 2025]`, anchors on the current `forecast_date`, and exposes `d0_7`, `d8_15`, and `d16_to_10_06` under each core region and year. The last window is hard-clipped at October 6. The three historical records for each core point must pass the existing 13.5 km Historical grid-distance QA and share one returned grid before `cross_year_comparison_usable` can be true. This module is additive and does not alter the Ejina namespace or the existing forecast modules.

`grid_registry.schema.json` describes the lightweight point-to-returned-grid audit registry. `phenology_weather_summary.schema.json` describes the ChatGPT-facing compact statistics file; it contains no hourly or daily raw series. Full audit data remains in the module artifacts and compressed raw archives.

`gefs.schema.json` describes the independent Open-Meteo GEFS module. `ncep_gefs025` is the near-range global product (about 0.25°, 31 sequences, approximately 10 days) and `ncep_gefs05` is the coarse long-range global product (about 0.5°, 31 sequences, approximately 35 days). The API currently provides 3-hour ensemble fields; a 3-hour output frequency does not mean that a date more than 10 days away has 3-hour forecast precision. The public artifact contains distributions, probabilities, event phase statistics, and deterministic-support checks; member-level raw response data remains in the compressed raw archive/cache.
