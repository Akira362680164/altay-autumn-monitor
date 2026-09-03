# Historical cache

The pipeline stores compact daily Open-Meteo Historical Weather values here,
separated by namespace, year, and VERIFIED point:

```text
data/cache/history/<namespace>/<year>/<point_id>.json
```

Each file is bound to the requested coordinate, returned model grid,
`models=ecmwf_ifs`, `cell_selection=nearest`, `elevation=nan`,
`timezone=Asia/Shanghai`, model, endpoint, returned elevation, and QA. A
normal run reads the cache and requests only missing contiguous dates. The
selected solar variable is also bound so a sunshine/shortwave fallback cannot
be mixed silently. Use
`python src/pipeline.py --refresh-history` or the Actions `refresh_history`
workflow input for an occasional full revalidation. Identity mismatches are
reported as `INVALID`; no other weather source is used.
