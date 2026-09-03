# altay-autumn-monitor

这是一个只使用 Open-Meteo 的、可审计的阿勒泰天气证据管道。它为 ChatGPT 提供固定坐标上的预报、历史 IFS 分析、集合成员、Single Runs、GFS 交叉验证、空间格点去重和天气事件风险数据。它不自动给出“秋色提前/滞后几天”或最终黄度结论。

## ChatGPT Entry Points

以下地址是公开 GitHub Raw URL，无需登录即可读取。日常优先读取 `status.json` 和轻量 `phenology_weather_summary.json`；`summary.json` 保留现有日报字段。

- status.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/status.json>
- summary.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/summary.json>
- hres.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/hres.json>
- history_comparison.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/history_comparison.json>
- history_forward.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/history_forward.json>
- ensemble.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/ensemble.json>
- gfs.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/gfs.json>
- single_runs.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/single_runs.json>
- spatial_sampling.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/spatial_sampling.json>
- long_range.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/long_range.json>
- phenology_weather_summary.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/phenology_weather_summary.json>
- grid_registry.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/grid_registry.json>

额济纳使用独立 `ejina` namespace；日常读取其前两个入口：

- 额济纳 status.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/ejina/status.json>
- 额济纳 summary.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/ejina/summary.json>
- 额济纳 hres.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/ejina/hres.json>
- 额济纳 history_comparison.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/ejina/history_comparison.json>
- 额济纳 ensemble.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/ejina/ensemble.json>
- 额济纳 gfs.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/ejina/gfs.json>
- 额济纳 single_runs.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/ejina/single_runs.json>
- 额济纳 long_range.json: <https://raw.githubusercontent.com/Akira362680164/altay-autumn-monitor/main/data/latest/ejina/long_range.json>

## 职责边界

Codex 负责 GitHub Repository、GitHub Actions、Open-Meteo 请求、原始数据留存、QA、2026 与配置历史参考年的同点天气指标、稳定 JSON Schema、历史归档和机器可读输出。

ChatGPT 负责每天读取 JSON，搜索并人工查看 2026/2025 同地点实拍，结合天气驱动力判断实际物候日差、用户到访日黄度和挂叶风险，并输出每日简报。JSON 中的 `weather_driver_vs_2025` 只代表天气驱动力，不是实际物候结论。

## 唯一数据源和模型

所有数值天气数据均来自 Open-Meteo；程序通过 endpoint 主机白名单阻止其他天气站点或 App 混入。相关官方文档：

- [ECMWF Forecast API](https://open-meteo.com/en/docs/ecmwf-api)：主模型为 ECMWF IFS HRES 9 km，endpoint 为 `https://api.open-meteo.com/v1/ecmwf`。
- [Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api)：endpoint 为 `https://archive-api.open-meteo.com/v1/archive`，固定 `models=ecmwf_ifs`。
- [Ensemble API](https://open-meteo.com/en/docs/ensemble-api)：新疆使用全球 `ecmwf_ifs025_ensemble`，约 25 km、51 个成员。
- [Single Runs API](https://open-meteo.com/en/docs/single-runs-api)：endpoint 为 `https://single-runs-api.open-meteo.com/v1/forecast`，固定 `models=ecmwf_ifs`，比较不同 UTC 初始化 run。
- [GFS API](https://open-meteo.com/en/docs/gfs-api)：固定 GFS Global 0.11°（约 13 km），只作趋势交叉验证。
- [Ensemble API](https://open-meteo.com/en/docs/ensemble-api) 与 [官方 Ensemble OpenAPI 注册表](https://github.com/open-meteo/open-meteo/blob/main/openapi/ensemble.yml)：16–35 天背景层当前使用全球 GFS Ensemble 0.5°，请求模型 ID 为 `ncep_gefs05`。

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
- K5 喀纳斯游客服务中心/换乘中心：`48.692136, 87.029775`（[高德公开 POI](https://ditu.amap.com/place/BV09303042)）
- K7 喀纳斯观鱼台主体：`48.72578, 86.99051`（[Mapcarta / OpenStreetMap 节点](https://mapcarta.com/N2970777553)）
- B1 白哈巴村核心：`48.69583, 86.78382`
- H1 禾木村核心/村庄河谷：`48.56921, 87.43037`（[Mapcarta / OpenStreetMap Kom 节点](https://mapcarta.com/N6118572418)）
- H2 禾木村观景点/后山白桦坡：`48.57648, 87.42650`（[Mapcarta / OpenStreetMap 节点](https://mapcarta.com/N5813997796)）
- H3 禾木桥/禾木河谷：`48.570439, 87.427425`（[Wikimedia Commons 地理标注照片](https://commons.wikimedia.org/wiki/File%3A%E6%96%B0%E7%96%86-%E7%A6%BE%E6%9C%A8%E6%A1%A5%E4%B8%8A%E8%A7%82%E6%99%AF_-_panoramio.jpg)）
- H4 凤凰观景平台/后山森林带：`48.57207, 87.41871`（[Mapcarta / OpenStreetMap 节点](https://mapcarta.com/N10303362686)）
- C1 可可托海镇/额尔齐斯河谷：`47.22061, 89.80911`

喀纳斯注册为三个子区：`sanwan` 三湾河谷（K1/K2/K3）、`lake` 湖区（K4/K5/K6）和 `guanyutai` 观鱼台山地（K7/K8/K9）。K4、K6、K8、K9、B2 白哈巴东坡和 C2 神钟山/峡谷仍保留为 `PROVISIONAL`，会在 `status.json` 中列出但 `usable_for_main_chain=false`。其中 K6 使用公开湖区参考坐标，但尚未核实具体岸线/森林位置；K8/K9 是观鱼台周边山坡候选坐标。当前三湾有 2 个 VERIFIED 独立 HRES 格点，湖区和观鱼台各只有 1 个 VERIFIED 粗格点；后两者按各自实际唯一格点计算，并在 `sampling` 中保留低空间分辨率事实，不会用 PROVISIONAL 点补齐。

禾木已注册为两个子区：`valley` 村庄河谷（H1/H3）和 `backhill` 后山观景区（H2/H4）。四个点均经过公开地图或公开地理标注资料核验并设为 `VERIFIED`；同一返回模式格点只计一个独立样本。子区内部对 unique model grids 等权，`hemu` composite 再对 valley 与 backhill 等权，不按景点数量加权。2026-09-01 用户提供的“整体全绿、尚未进入明显黄叶期”仍只保留为独立 `manual_phenology_baseline`，不参与天气计算。阿禾公路和 G331 只保留 `ROUTE_NOT_VERIFIED` 槽位，未确认用户实际路线前不进入禾木 composite 或其他数值链。

### 喀纳斯分区与返回格点聚合

喀纳斯的天气统计不再把 K1/K2/K3 或新增候选点当成等权独立样本。每个请求保存 requested coordinate、返回模式格点、高程、格点距离、时区和 QA；相同 `returned_grid_coordinate` 只保留一个独立样本，映射关系写在 `grid_registry.json` 以及轻量摘要的 `sampling` 中。

子区第一层对 unique model grids 等权平均；景区 composite 第二层对 `sanwan`、`lake`、`guanyutai` 三个子区等权平均。当前配置的最低独立格点数为：三湾 2、湖区 1、观鱼台 1；这是按已核实的子区尺度和实际返回格点设置的，不把重复格点当成额外样本。任何子区低于自身门槛时，子区为 `PARTIAL`，景区 composite 也不会用其他子区替代或按点数加权。这样 K1/K2 返回同一 HRES 格点时只贡献一次，K3 的另一个格点贡献一次。

### 额济纳独立天气 namespace

额济纳配置位于 `config/ejina_points.json`，不会把额济纳点混入阿勒泰主区域文件。二道桥、四道桥、七道桥均已设为 `VERIFIED`，坐标核验记录和来源链接也保存在配置中：

- EJ1 二道桥：`41.968333, 101.086111`；来源为[北京林业大学研究资料 Table 1](https://j.bjfu.edu.cn/cn/article/pdf/preview/10.12171/j.1000-1522.20210317.pdf)中的 `101°05′10″E, 41°58′06″N`。
- EJ2 四道桥：`42.001200, 101.137400`；来源为[国家冰川冻土沙漠科学数据中心四道桥元数据](https://www.ncdc.ac.cn/portal/metadata?current_page=1&ef=%E5%9B%9B%E9%81%93%E6%A1%A5%E8%B6%85%E7%BA%A7%E7%AB%99)和[ICOS Sidaoqiao metadata](https://meta.icos-cp.eu/resources/stations/ES_CN-Sdq)。
- EJ3 七道桥：`42.009167, 101.231389`；来源为[北京林业大学研究资料 Table 1](https://j.bjfu.edu.cn/cn/article/pdf/preview/10.12171/j.1000-1522.20210317.pdf)中的 `101°13′53″E, 42°00′33″N`。

额济纳 namespace 只输出天气证据：HRES、Historical IFS、ECMWF Ensemble、GFS、Single Runs、GFS Ensemble Long Range 和 QA。它没有旅行日期假设；Single Runs 在没有 visit date 时使用可审计的滚动目标时刻。额济纳历史比较从 `2025-09-01` 与 `2026-09-01` 同点、同 `ecmwf_ifs`、同返回模式格点开始，包含 2026 minus 2025 的天气指标差值，并新增 `<15℃` 夜间阈值。

额济纳未来天气输出统一截断到 `2026-10-07`；`2026-10-08` 及以后在 API 返回后直接丢弃，并在每条记录 QA 中保存 `forecast_cutoff_date`、保留行数和丢弃行数。额济纳采用与现有主链相同的三层结构：0–7 天 HRES，8–15 天 HRES + ECMWF Ensemble + GFS，16 天以后只使用 GFS Ensemble 粗粒度背景层。额济纳数据文件不输出植被、生态或旅游解释，后续解释由 ChatGPT 结合外部实拍完成。

## QA 和失败机制

每条记录都保留 source、endpoint、请求坐标、返回模型格点坐标、返回高程、timezone、UTC offset、retrieval time、模型/run 初始化时间（接口提供时）、格点距离和 QA 结果。QA 包含坐标、格点代表性、时区、模型、elevation 参数和数据完整性检查。

格点距离超过对应模型允许范围会标记 `GRID_REPRESENTATIVENESS_FAIL`，记录变为 `INVALID`，不进入天气差分指标。API 错误、超时、重试后失败、缺字段、缺数据、模型不符、时区不符和数组不完整也都标记 `INVALID`。每个请求最多 3 次、指数退避；重试仍只访问 Open-Meteo，没有天气源 fallback。`sunshine_duration` 若在同一个 Open-Meteo endpoint 被判定为不可用，只切换到同源 `shortwave_radiation`，并在 JSON 中记录实际变量。

即使 QA 失败，管道仍会写出 `status.json` 并把相应模块写成 `FAILED`；初始化级错误也会写最小失败状态。Actions 只有在测试或程序自身崩溃时失败，普通 API/QA 失败会保留产物供排错。

Open-Meteo 实际返回中，HRES 和 Single Runs 可能在数组边缘出现 `null`：程序只删除连续的首尾不完整行，并记录 `original_timestep_count`、保留行数和 `horizon_status=TRUNCATED_EDGE_MISSING`；中间缺失不会被填补，仍为 `INVALID`。普通 Forecast、Historical 和 Ensemble 响应未必提供模型初始化字段，JSON 会保留 `null`，而请求模型、endpoint 和返回格点仍完整记录；Single Runs 的 UTC 初始化时间由请求和记录显式保存。

## 历史差分和指标边界

阿勒泰主 namespace 的 2023、2024、2025、2026 均从 8 月 25 日累计到最近已完成的本地日期，使用同一固定请求坐标、同一 `ecmwf_ifs` 和同一时区。年份由 `config/points.json` 的 `history_years` 控制；额济纳独立 namespace 当前仍按自身配置使用 2025、2026。它只能称为 `ECMWF IFS historical weather / analysis`，不是 `station observation`，因为它不是气象站实测。

每个地点计算：日最低/最高/平均温度、夜最低温、`<15℃`/`<10℃`/`<5℃`/`<2℃`/`<0℃` 寒夜累计、各阈值连续寒夜序列及最大连续长度、昼夜温差、降水、降雪、云量、低云、日照/短波、平均风和最大阵风。

额济纳 namespace 额外从 `09-01` 开始，并增加 `<15℃` 夜间累计；其 `history_comparison.json` 保留同点同模式格点 QA 与 `delta_2026_minus_2025`。阿勒泰主 namespace 仍使用原来的 `08-25` 起点和字段语义，并在保留 `delta_2026_minus_2025` 的同时增加 `delta_2026_minus_2023`、`delta_2026_minus_2024` 和通用 `deltas_2026_minus`。每个年份的 `daily` 与 `metrics` 都完整保留；所有配置历史年份返回格点必须一致，否则该点比较为 `FAILED`，不生成混格点差分。主日报继续使用 `weather_driver_vs_2025`，并可读取新增的 `weather_driver_vs_2023`、`weather_driver_vs_2024`。

`coldness_index` 是本项目的内部相对比较指标，不是官方物候模型，公式为每个完整日累加：

```text
max(0, 10 - daily_mean)
+ 2 * max(0, 5 - night_min)
+ 3 * max(0, 2 - night_min)
+ 4 * max(0, 0 - night_min)
```

`weather_driver_vs_2025.direction` 只允许 `LEADING`、`SYNC`、`LAGGING`、`UNDETERMINED`，表示天气指标相对 2025 的方向，不生成 `actual_phenology_lead_days`。2026-09-01 禾木用户实拍“整体全绿、尚未进入明显黄叶期”仅作为 `manual_phenology_baseline` 元数据，不参与自动天气计算，也不由 Codex 自动识别图片。

### 历史年份同期后续天气路径

`data/latest/history_forward.json` 只在阿勒泰主 namespace 中运行。它以当天 `forecast_date` 为 anchor，调用 Historical Weather API 查询 2023、2024、2025 同一日历节点之后真实发生的天气；2026 仍由现有实时预报链提供，不进入该历史模块。

每个 VERIFIED 核心区域提供 `regions.<region>.years.<year>.d0_7`、`d8_15` 和 `d16_to_10_06` 三个窗口。以 2026-09-02 为例，三个窗口分别是 09-02/09-09、09-10/09-17、09-18/10-06，均包含首尾；日期随每日 anchor 滚动，10-07 及以后永远不请求、不写入输出。窗口同时保留每日温度、降水、降雪、日照和最大阵风，以及窗口统计和前 3 日/后 3 日平均温度变化。

窗口可用性独立判断：`d0_7.status=OK` 且 `usable_for_main_chain=true` 时进入当前主链；`d8_15.status=PARTIAL` 时保留已完成日期并将 `usable_for_trend_reference=true`，例如 `9/10–16预测，9/17待补`；`d16_to_10_06.status=INVALID` 时 `usable_for_main_chain=false`，不参与当前结论。单个窗口缺失不会把整个地区标记为 `usable_for_main_chain=false`；地区级可用性以当前 `d0_7` 是否可用为准。

每个核心点的 `same_grid_qa` 会检查 2023、2024、2025 的请求坐标、返回模式格点、返回高程、格点距离、时区、模型和 API request metadata。三年返回格点完全一致且每年 QA 通过时，`cross_year_comparison_usable=true`；任何年份失败、格点不一致或超过现有历史格点距离限制时，点和区域标记为 `FAILED`，不进入跨年比较。禾木的 valley/backhill 也执行相同三年同格点闸门，只有两个子区均通过后才形成 `hemu` composite。

喀纳斯在该文件中额外提供 `regions.kanas.subregions.<subregion>.years.<year>.<window>` 和 `regions.kanas.composite.years.<year>.<window>`。2023、2024、2025 的历史路径与 2026 的 HRES 预报使用同一注册点集合、同一返回格点去重算法和同一两级聚合规则；不同 API 产品的网格坐标本身不强行要求相同。只有历史参考年之间的 `same_grid_qa` 通过后，历史跨年聚合才可用。

该文件只提供历史天气路径证据，不输出物候、秋色或旅游结论。它与 `history_comparison.json` 并行存在，不改变 HRES、ECMWF Ensemble、GFS、Single Runs、Spatial Sampling 或 Long Range 的逻辑；额济纳 namespace 不生成该文件。

## 空间采样、集合和风雪风险

白哈巴、喀纳斯、禾木、可可托海对每个 VERIFIED 核心点请求核心 + N/S/E/W/NE/NW/SE/SW 约 12 km 的同源 HRES 样本；程序保存每个请求坐标和返回格点坐标，并按返回格点去重。`requested_samples=9` 不等于 9 个独立模式样本；输出 `unique_model_cells`、重复请求数、核心区温度范围和按日期/唯一格点的 `cold_pool_coverage`，用于区分广泛覆盖与单格点现象。

Ensemble 仅对 B1、K1、H1、C1 核心点计算 51 成员的 mean、median、p10、p25、p75、p90、spread，以及夜最低温 `<5℃`、`<2℃`、`<0℃` 的成员支持比例。约 25 km Ensemble 只表达信号稳健性，不能当作村级精确温度，也不与 HRES 简单平均。

## 16–35 Day Long-Range Background

`data/latest/long_range.json` 是新增的长期背景层。当前实际核验的 Open-Meteo GFS Ensemble 配置为：`model_id=ncep_gefs05`、31 个序列（基准/控制序列加 `member01`–`member30`）、全球覆盖、约 0.5°（约 50 km）、原生 3 小时。官方页面当前一方面显示最多 36 天，另一方面参数表仍列 `forecast_days` 为 0–35；本项目用真实接口验证过 `forecast_days=36`，并将返回的本地日期数和非空 lead day 再做 QA。如果接口以后不再接受该请求，模块会写 `FAILED`，不会换用其他天气源。

额济纳的公开长期文件位于 `data/latest/ejina/long_range.json`，同样使用 `ncep_gefs05` 和 31 个序列，并执行 `2026-10-07` 截止闸门；截至日期之后的窗口不会写入公开 forecast 数据。

该层按固定 3 天块聚合为 D16–D18、D19–D21、D22–D24、D25–D27、D28–D30、D31–D33、D34–D35，不在公开长期文件中展示逐小时成员数组。Open-Meteo 可能把集合原生 3 小时序列插值为逐小时数组，因此这些数组只用于内部聚合和审计，不能被解释为逐小时精确预报。

2026-09-02 的真实实跑中，接口返回了 36 个本地日期，但温度非空值只到 D34，D35 窗口被明确写为 `UNAVAILABLE`；降水、降雪和阵风还会有各自更短的非空边界。对应模块状态为 `PARTIAL`，每个变量的 `variable_availability` 会写入 QA。这是接口当前可用边界的记录，不是补值或伪造的 D35 预报。

长期温度方向使用同一点、同一时区和 `ECMWF IFS 9 km historical weather / analysis` 的 `historical_reference`。这是有限的同日期历史参考，不称为 `climatological_normal`，也不是气象站实测。

每个窗口输出集合分布、相对参考方向、集合支持的冷空气/降水/降雪/强风背景信号、粗网格阈值信号和不确定性。`coarse_grid_threshold_signal.usable_for_local_absolute_temperature=false`；0.5° 网格的 `<5℃`、`<2℃`、`<0℃` 只可作背景信号，不能直接证明村级温度或霜冻。`wet_snow_assessment` 仅在粗网格降雪与 `<=2℃` 日最低温重合时标记 `COARSE_POTENTIAL`，不判断当地雪相或积雪量。

GitHub 每日运行会从最近 3–5 次长期摘要中比较同一 `horizon_class`，并标记 `NEW`、`PERSISTENT`、`STRENGTHENING`、`WEAKENING`、`SHIFTING` 或 `DISAPPEARED`。第一次运行没有前序摘要时为 `INSUFFICIENT_HISTORY`。紧凑的长期摘要随每日 archive 保留；包含成员级小时数据的 `raw/long_range.json.gz` 只保留 14 天。

ChatGPT 可以用 16–35 天层提前关注 9 月 15–25 日前后的持续偏冷、冷空气重复、雨雪/湿雪背景、强风背景和集合是否收敛。它不能用这一层给出某日精确最低温或降水量、村级霜冻、实际物候提前/滞后天数，也不能覆盖已经进入 8–15 天窗口的短周期证据。优先级固定为：

```text
0–7天：ECMWF HRES > ECMWF Ensemble > GFS
8–15天：ECMWF HRES趋势 + ECMWF Ensemble > GFS
16–35天：GFS Ensemble background only
```

如果长期背景层与后续进入 8–15 天的 HRES/ECMWF Ensemble 发生变化，以新的短周期模型为准。长期层也不把强风自动解释为掉叶；`leaf_loss_weather_risk` 仍只是天气事件风险，实际挂叶判断由 ChatGPT 结合实拍和成熟度完成。

GFS 只输出 EC/GFS 的温度趋势、寒冷窗口、降水和强风一致性，不参与平均，也不直接产生秋色判断。`leaf_loss_weather_risk` 只表达强阵风、湿雪、雨雪和冻结等天气事件风险；9 月 20 日前强风不额外加权，9 月 20 日后才启用季节权重。它不表示树叶一定掉落，实际挂叶风险由 ChatGPT 结合实拍和成熟度判断。

## 目录和保留策略

```text
.
├── .github/workflows/update-weather.yml
├── config/points.json
├── config/ejina_points.json
├── data/
│   ├── latest/
│   │   ├── status.json
│   │   ├── summary.json
│   │   ├── hres.json
│   │   ├── history_comparison.json
│   │   ├── history_forward.json
│   │   ├── ensemble.json
│   │   ├── gfs.json
│   │   ├── single_runs.json
│   │   ├── spatial_sampling.json
│   │   ├── long_range.json
│   │   ├── grid_registry.json
│   │   ├── phenology_weather_summary.json
│   │   └── ejina/{status,summary,hres,history_comparison,ensemble,gfs,single_runs,long_range}.json
│   └── archive/YYYY-MM-DD/
│       ├── 同名压缩后的每日 JSON（含 history_forward.json 和 phenology_weather_summary.json）
│       ├── raw/*.json.gz
│       └── ejina/{status,summary,hres,history_comparison,ensemble,gfs,single_runs,long_range}.json + raw/*.json.gz
├── schemas/{status,summary,module,history_forward,long_range,grid_registry,phenology_weather_summary,ejina_points,ejina_status,ejina_summary}.schema.json
├── src/pipeline.py
├── tests/test_pipeline.py
├── requirements.txt
└── README.md
```

`latest/` 保存完整数据；每日 archive 保存去掉逐小时数组的可读快照，`archive/YYYY-MM-DD/raw/` 保存压缩后的模块原始快照。原始 gzip 目录保留 14 天，紧凑每日快照长期保留。Schema 版本目前为 `1.1.0`。这是对 v1.0.0 的兼容性新增：已有字段和模块语义保持不变，新增 `long_range_background` 模块及 summary 的 `forecast_16_35d` 字段。破坏性变更必须升级 major version 并同步更新 Schema、测试和 README。

## GitHub Actions 和本地运行

`.github/workflows/update-weather.yml` 支持 `workflow_dispatch`，并在 `00/06/12/18 UTC` 模型时次后第 17 分钟运行，即北京时间每天 `02:17、08:17、14:17、20:17`。17 分钟偏移用于避开整点负载；Open-Meteo 的实际到数时间仍可能因模型处理和服务器同步浮动。它使用 Python 3.12、安装 `requirements.txt`、先运行单元测试，再请求 Open-Meteo，最后在 `permissions: contents: write` 下提交 `data/latest` 和 `data/archive`。

本地运行：

```bash
python3.12 -m pip install -r requirements.txt
python3.12 -m unittest discover -s tests -v
python3.12 src/pipeline.py
```

运行日志会打印类似 `[B1] HRES FETCH OK`、`[B1] GRID QA PASS`、`[B1] HISTORY 2025 OK`、`[H1] HRES FETCH OK` 和 `[K4] SKIPPED: PROVISIONAL`。JSON Schema 文件位于 `schemas/`，机器端应先检查 `status.json`，再按模块状态读取 `summary.json` 或相应明细。

## 推荐的 ChatGPT 每日读取顺序

1. 读取 `status.json`，确认 `pipeline_status` 和 `modules`；任何 `FAILED` 模块都按缺失证据处理。
2. 日常读取 `phenology_weather_summary.json`，按 `regions` 读取 B1、Kanas 三子区/composite、Hemu 两子区/composite、C1 的 2023–2026 窗口统计；该文件不含 hourly/daily 原始数组。
3. 读取 `summary.json`，按 `regions` 的 `visit_date` 映射 10/1 白哈巴、10/2 喀纳斯、10/3 喀纳斯三湾→白哈巴→铁贾公路→契巴罗衣、10/4 禾木、10/5 禾木→阿禾公路→G331→可可托海、10/6 可可托海、10/7 返程。
4. 用 `weather_driver_vs_2025`、`forecast_0_7d`、`forecast_8_15d`、`forecast_16_35d`、Ensemble 分布、Single Runs 和 GFS 交叉验证整理天气证据；长期层只作 16–35 天背景概率层；需要查看完整历史同期后续路径时读取 `history_forward.json`，先检查其 `status`、Kanas/Hemu `subregion_aggregation_status` 和各区域 `same_grid_qa`。
5. 对需要结论的同地点，另行搜索并人工查看 2026/2025 实拍；把实拍判断与天气证据分开写，不能把 JSON 的天气方向改写成自动物候日差。
6. 读取 `grid_registry.json`、`long_range.json`、`hres.json`、`history_comparison.json`、`ensemble.json`、`single_runs.json` 追溯具体点、格点、成员和 run；遇到 `INVALID`、`FAILED`、`PARTIAL` 或 `UNDETERMINED` 时保留不确定性。

当前 v1.1.0 Schema 已覆盖长期背景层。后续如果需要增加图像人工复核结果，建议以独立字段或独立文件追加，并保持 Codex 天气层与 ChatGPT 视觉判断层分离。
