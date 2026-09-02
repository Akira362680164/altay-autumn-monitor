# altay-autumn-monitor

这是一个只使用 Open-Meteo 的、可审计的阿勒泰天气证据管道。它为 ChatGPT 提供固定坐标上的预报、历史 IFS 分析、集合成员、Single Runs、GFS 交叉验证、空间格点去重和天气事件风险数据。它不自动给出“秋色提前/滞后几天”或最终黄度结论。

## ChatGPT Entry Points

以下地址是公开 GitHub Raw URL，无需登录即可读取。日常优先读取前两个文件：

- status.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/status.json>
- summary.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/summary.json>
- hres.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/hres.json>
- history_comparison.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/history_comparison.json>
- ensemble.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/ensemble.json>
- gfs.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/gfs.json>
- single_runs.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/single_runs.json>
- spatial_sampling.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/spatial_sampling.json>

## 职责边界

Codex 负责 GitHub Repository、GitHub Actions、Open-Meteo 请求、原始数据留存、QA、2026 与 2025 同点天气指标、稳定 JSON Schema、历史归档和机器可读输出。

ChatGPT 负责每天读取 JSON，搜索并人工查看 2026/2025 同地点实拍，结合天气驱动力判断实际物候日差、用户到访日黄度和挂叶风险，并输出每日简报。JSON 中的 `weather_driver_vs_2025` 只代表天气驱动力，不是实际物候结论。

## 唯一数据源和模型

所有数值天气数据均来自 Open-Meteo；程序通过 endpoint 主机白名单阻止其他天气站点或 App 混入。相关官方文档：

- [ECMWF Forecast API](https://open-meteo.com/en/docs/ecmwf-api)：主模型为 ECMWF IFS HRES 9 km，endpoint 为 `https://api.open-meteo.com/v1/ecmwf`。
- [Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api)：endpoint 为 `https://archive-api.open-meteo.com/v1/archive`，固定 `models=ecmwf_ifs`。
- [Ensemble API](https://open-meteo.com/en/docs/ensemble-api)：新疆使用全球 `ecmwf_ifs025_ensemble`，约 25 km、51 个成员。
- [Single Runs API](https://open-meteo.com/en/docs/single-runs-api)：endpoint 为 `https://single-runs-api.open-meteo.com/v1/forecast`，固定 `models=ecmwf_ifs`，比较不同 UTC 初始化 run。
- [GFS API](https://open-meteo.com/en/docs/gfs-api)：固定 GFS Global 0.11°（约 13 km），只作趋势交叉验证。

所有请求统一使用：

- `timezone=Asia/Shanghai`
- `cell_selection=nearest`
- `elevation=nan`
- 固定请求坐标和 WGS84 纬经度

`elevation=nan` 用于禁止 Open-Meteo 的统计高程下推；返回的 `elevation` 是模型网格高程，不能当作用户输入的 DEM 或村级实测海拔。`cell_selection=nearest` 让返回格点选择规则固定且可审计。

主预报的 `precision_class` 按 ECMWF 原生分辨率解释：0–90 小时为 `native_hourly`，90–144 小时为 `coarse_3h_interpolated`，超过 144 小时为 `trend_only_6h_plus`。Open-Meteo 可能把较粗原生时间步插值为逐小时数组，因此逐小时返回值不等于全程原生逐小时预报。GFS 在 120 小时后也使用较粗时间步；Ensemble 按其约 3 小时原生序列理解。

## 坐标注册表和主链闸门

`config/points.json` 是唯一坐标注册表。只有 `status=VERIFIED` 的点能进入正式请求、历史差分和主链摘要；代码中的 `active_points()` 是硬过滤边界。

已启用的 VERIFIED 点：

- K1 喀纳斯神仙湾：`48.65688, 87.03412`
- K2 喀纳斯月亮湾：`48.62947, 87.04162`
- K3 喀纳斯卧龙湾：`48.61991, 87.04873`
- B1 白哈巴村核心：`48.69583, 86.78382`
- C1 可可托海镇/额尔齐斯河谷：`47.22061, 89.80911`

K4 老村、H1/H2 禾木、B2 白哈巴东坡、C2 神钟山/峡谷保留为 `PROVISIONAL`，会在 `status.json` 中列出但 `usable_for_main_chain=false`。由于禾木当前没有 VERIFIED 核心点，H1 不会进入正式天气差分。阿禾公路和 G331 只保留 `ROUTE_NOT_VERIFIED` 槽位，未确认用户实际路线前不进入数值链。

## QA 和失败机制

每条记录都保留 source、endpoint、请求坐标、返回模型格点坐标、返回高程、timezone、UTC offset、retrieval time、模型/run 初始化时间（接口提供时）、格点距离和 QA 结果。QA 包含坐标、格点代表性、时区、模型、elevation 参数和数据完整性检查。

格点距离超过对应模型允许范围会标记 `GRID_REPRESENTATIVENESS_FAIL`，记录变为 `INVALID`，不进入天气差分指标。API 错误、超时、重试后失败、缺字段、缺数据、模型不符、时区不符和数组不完整也都标记 `INVALID`。每个请求最多 3 次、指数退避；重试仍只访问 Open-Meteo，没有天气源 fallback。`sunshine_duration` 若在同一个 Open-Meteo endpoint 被判定为不可用，只切换到同源 `shortwave_radiation`，并在 JSON 中记录实际变量。

即使 QA 失败，管道仍会写出 `status.json` 并把相应模块写成 `FAILED`；初始化级错误也会写最小失败状态。Actions 只有在测试或程序自身崩溃时失败，普通 API/QA 失败会保留产物供排错。

Open-Meteo 实际返回中，HRES 和 Single Runs 可能在数组边缘出现 `null`：程序只删除连续的首尾不完整行，并记录 `original_timestep_count`、保留行数和 `horizon_status=TRUNCATED_EDGE_MISSING`；中间缺失不会被填补，仍为 `INVALID`。普通 Forecast、Historical 和 Ensemble 响应未必提供模型初始化字段，JSON 会保留 `null`，而请求模型、endpoint 和返回格点仍完整记录；Single Runs 的 UTC 初始化时间由请求和记录显式保存。

## 历史差分和指标边界

2025、2026 均从 8 月 25 日累计到最近已完成的本地日期，使用同一固定请求坐标、同一 `ecmwf_ifs` 和同一时区。它只能称为 `ECMWF IFS historical weather / analysis`，不是 `station observation`，因为它不是气象站实测。

每个地点计算：日最低/最高/平均温度、夜最低温、`<10℃`/`<5℃`/`<2℃`/`<0℃` 寒夜累计、各阈值连续寒夜序列及最大连续长度、昼夜温差、降水、降雪、云量、低云、日照/短波、平均风和最大阵风。

`coldness_index` 是本项目的内部相对比较指标，不是官方物候模型，公式为每个完整日累加：

```text
max(0, 10 - daily_mean)
+ 2 * max(0, 5 - night_min)
+ 3 * max(0, 2 - night_min)
+ 4 * max(0, 0 - night_min)
```

`weather_driver_vs_2025.direction` 只允许 `LEADING`、`SYNC`、`LAGGING`、`UNDETERMINED`，表示天气指标相对 2025 的方向，不生成 `actual_phenology_lead_days`。2026-09-01 禾木用户实拍“整体全绿、尚未进入明显黄叶期”仅作为 `manual_phenology_baseline` 元数据，不参与自动天气计算，也不由 Codex 自动识别图片。

## 空间采样、集合和风雪风险

白哈巴、喀纳斯、可可托海对每个 VERIFIED 核心点请求核心 + N/S/E/W/NE/NW/SE/SW 约 12 km 的同源 HRES 样本；禾木在核心点 VERIFIED 前跳过。程序保存每个请求坐标和返回格点坐标，并按返回格点去重。`requested_samples=9` 不等于 9 个独立模式样本；输出 `unique_model_cells`、重复请求数、核心区温度范围和按日期/唯一格点的 `cold_pool_coverage`，用于区分广泛覆盖与单格点现象。

Ensemble 仅对 B1、K1、C1 核心点计算 51 成员的 mean、median、p10、p25、p75、p90、spread，以及夜最低温 `<5℃`、`<2℃`、`<0℃` 的成员支持比例。约 25 km Ensemble 只表达信号稳健性，不能当作村级精确温度，也不与 HRES 简单平均。

GFS 只输出 EC/GFS 的温度趋势、寒冷窗口、降水和强风一致性，不参与平均，也不直接产生秋色判断。`leaf_loss_weather_risk` 只表达强阵风、湿雪、雨雪和冻结等天气事件风险；9 月 20 日前强风不额外加权，9 月 20 日后才启用季节权重。它不表示树叶一定掉落，实际挂叶风险由 ChatGPT 结合实拍和成熟度判断。

## 目录和保留策略

```text
.
├── .github/workflows/update-weather.yml
├── config/points.json
├── data/
│   ├── latest/
│   │   ├── status.json
│   │   ├── summary.json
│   │   ├── hres.json
│   │   ├── history_comparison.json
│   │   ├── ensemble.json
│   │   ├── gfs.json
│   │   ├── single_runs.json
│   │   └── spatial_sampling.json
│   └── archive/YYYY-MM-DD/
│       ├── 同名压缩后的每日 JSON
│       └── raw/*.json.gz
├── schemas/{status,summary,module}.schema.json
├── src/pipeline.py
├── tests/test_pipeline.py
├── requirements.txt
└── README.md
```

`latest/` 保存完整数据；每日 archive 保存去掉逐小时数组的可读快照，`archive/YYYY-MM-DD/raw/` 保存压缩后的模块原始快照。原始 gzip 目录保留 14 天，紧凑每日快照长期保留。Schema 版本目前为 `1.0.0`；兼容性新增字段可以向后兼容地追加，破坏性变更必须升级版本并同步更新 Schema、测试和 README。

## GitHub Actions 和本地运行

`.github/workflows/update-weather.yml` 支持 `workflow_dispatch`，并按 `02:30 UTC` 每日运行，即北京时间 `10:30`。它使用 Python 3.12、安装 `requirements.txt`、先运行单元测试，再请求 Open-Meteo，最后在 `permissions: contents: write` 下提交 `data/latest` 和 `data/archive`。

本地运行：

```bash
python3.12 -m pip install -r requirements.txt
python3.12 -m unittest discover -s tests -v
python3.12 src/pipeline.py
```

运行日志会打印类似 `[B1] HRES FETCH OK`、`[B1] GRID QA PASS`、`[B1] HISTORY 2025 OK` 和 `[hemu] SKIPPED: PROVISIONAL`。JSON Schema 文件位于 `schemas/`，机器端应先检查 `status.json`，再按模块状态读取 `summary.json` 或相应明细。

## 推荐的 ChatGPT 每日读取顺序

1. 读取 `status.json`，确认 `pipeline_status` 和 `modules`；任何 `FAILED` 模块都按缺失证据处理。
2. 读取 `summary.json`，按 `regions` 的 `visit_date` 映射 10/1 白哈巴、10/2 喀纳斯、10/3 喀纳斯三湾→白哈巴→铁贾公路→契巴罗衣、10/4 禾木、10/5 禾木→阿禾公路→G331→可可托海、10/6 可可托海、10/7 返程。
3. 用 `weather_driver_vs_2025`、`forecast_0_7d`、`forecast_8_15d`、Ensemble 分布、Single Runs 和 GFS 交叉验证整理天气证据。
4. 对需要结论的同地点，另行搜索并人工查看 2026/2025 实拍；把实拍判断与天气证据分开写，不能把 JSON 的天气方向改写成自动物候日差。
5. 读取 `hres.json`、`history_comparison.json`、`ensemble.json`、`single_runs.json` 追溯具体点、格点、成员和 run；遇到 `INVALID`、`FAILED` 或 `UNDETERMINED` 时保留不确定性。

当前 v1.0.0 的 Schema 已覆盖本任务要求，不需要 ChatGPT 额外配合调整。后续如果需要增加图像人工复核结果，建议以独立字段或独立文件追加，并保持 Codex 天气层与 ChatGPT 视觉判断层分离。
