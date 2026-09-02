# JSON Schema

`status.json` and `summary.json` are the stable ChatGPT-facing contracts. The module artifacts use the common envelope in `module.schema.json`; module-specific fields are intentionally additive so a new QA detail can be added without changing the meaning of existing fields.

Schema version `1.1.0` is an additive update: all v1.0.0 fields retain their meaning, and `summary.json` adds `forecast_16_35d`. The new `long_range_background` module has its own artifact and uses the GFS Ensemble background contract. A breaking change must increment the major version and update the schemas, tests, README, and Raw entry points together. An omitted or `null` numeric value means that the value was unavailable or failed QA; it is never an inferred replacement value.

The main Altay `history_comparison.json` is configured for `history_years=[2023, 2024, 2025, 2026]`. It preserves the v1.0/v1.1 `2025`/`2026` daily and metrics keys, and adds the older years plus `delta_2026_minus_2023`, `delta_2026_minus_2024`, and `deltas_2026_minus`. The `same_grid_qa` object checks every configured year; a returned-grid mismatch makes the comparison `FAILED` and no historical delta is usable. The Ejina namespace keeps its independent `history_years=[2025, 2026]` configuration.
