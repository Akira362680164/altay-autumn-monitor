# JSON Schema

`status.json` and `summary.json` are the stable ChatGPT-facing contracts. The module artifacts use the common envelope in `module.schema.json`; module-specific fields are intentionally additive so a new QA detail can be added without changing the meaning of existing fields.

Schema version `1.0.0` freezes the field names used by the first public pipeline. A breaking change must increment the version and update the schemas, tests, README, and Raw entry points together. An omitted or `null` numeric value means that the value was unavailable or failed QA; it is never an inferred replacement value.
