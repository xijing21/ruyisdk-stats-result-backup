# 数据文档说明

## 备份文档说明

按照数据处理的顺序：

- dashboard_stats_backup_20261111.xlsx 是原始的 Excel 记录表
- history_backfill_precvs.xlsx  将 Excel 表进行预处理,删除和清理不计划加入 csv 的数据项
- history_backfill.xlsx
- history_backfill_original.csv  history_backfill.xlsx 转格式
- history_backfill.csv  归档的处理好的数据（用于给 dashboard_weekly.csv 补充历史数据）



## 数据手动处理记录（2026-11 归档）

### 一、做了什么

1. **历史数据合并**
   - 根据原始数据`dashboard_stats_backup_20261111.xlsx`预处理后的`history_backfill.csv`（原始记录）补充到 workflow 生成的 `dashboard_weekly.csv`（周度正式表）中，建立相对完整的周进展数据统计表。
   - 周度数据：从 2025/12/31 起保留全部周数据，最后 4 条（workflow 来源）的 `eclipse_marketplace` 由 0 修正为 19（插件转维护后按 19 计），并连锁更新 `eclipse_total` 与 `total_downloads`。
   - 增加 `dashboard_monthly.csv`（月度正式表）：详见2

2. **月度数据回填**（`dashboard_monthly.csv`，2026/06–2026/10 共 5 行）
   - 数据来源：`web-stats\results-monthly\20260X\api_dashboard_20260XXX.json`
   - 取值规则：`last_updated` → stat_time；`installs.total` → installs；`downloads_by_categories_v1` 下各项按名称对应填入各列。
   - **人工补充项**：`vscode_marketplace` / `openvsx` 无程序接口，按留存的截图数据填写（97/678 → 111/988 → 117/1138 → 128/1538 → 137/1926）。
  
3. **总数和增长量全部清空重算**
   - 所有`*_total` 和 `*_delta` 列 （= 本期值 − 上期值）逐列重新计算，不沿用旧表数据。

### 二、字段对应关系（列 ← 数据源）

| 表列                                | 来源                                                                            |
| ----------------------------------- | ------------------------------------------------------------------------------- |
| stat_time                           | json `last_updated`（保留到秒，`T` 换空格，如 `2026-07-23 09:00:03`）     |
| installs                            | json `installs.total`                                                         |
| pkg                                 | `downloads_by_categories_v1.pkg`                                              |
| pm_mirror / pm_github / pm_pypi     | `pm:mirror` / `pm:github` / `pm:pypi`                                     |
| pm_total                            | 三项之和（脚本算）                                                              |
| ide_eclipse_mirror                  | `ide:eclipse:mirror`                                                          |
| ide_plugin_eclipse_mirror / _github | `ide:plugin:eclipse:mirror` / `ide:plugin:eclipse:github`                   |
| eclipse_marketplace                 | **人工值，转维护后固定 19**                                               |
| eclipse_total                       | 四项之和（含 marketplace 的 19）                                                |
| ide_plugin_vscode_mirror / _github  | `ide:plugin:vscode:mirror` / `ide:plugin:vscode:github`                     |
| vscode_marketplace / openvsx        | **人工值，无接口，按截图/官网季度补充**                                   |
| vscode_total                        | 四项之和                                                                        |
| thirdparty / humans                 | `3rdparty` / `humans`                                                       |
| total_downloads                     | 六分类加和：pkg + pm_total + eclipse_total + vscode_total + thirdparty + humans |

### 三、易错点（本次踩过的坑）

1. **stat_time 用快照时间而非归档时间**：7 月归档目录是 `202607`，但 json `last_updated` 实为 7/23（服务器迁移导致接口服务受影响、数据未采集）。stat_time 必须取 json 内的 `last_updated`，delta 语义按实际快照间隔理解，note 中说明。
2. **eclipse_marketplace 特殊值**：插件已转维护、无程序接口，全表统一按 19 计；weekly 最后 4 行原来填 0，已修正并重算 eclipse_total / total_downloads。
3. **两个 6/28 快照的小差异**：monthly（json 快照）与 weekly（当日采集）在 pkg、pm_mirror、eclipse_total 上有几个单位的出入，属同日不同时点采集，预期现象，不处理。

### 四、文件格式约定

- **编码统一 `UTF-8 with BOM`（utf-8-sig）**：VS Code 正常显示中文、Excel 双击打开不乱码、脚本读写用 `encoding="utf-8-sig"`。三个 csv 编码必须一致。
- **时间格式**：`YYYY-MM-DD HH:MM:SS`（月度取 json last_updated 截到秒；周度沿用 `YYYY/M/D H:MM` 风格，各自内部一致即可）。
- **note 列**：异常数据（接口故障、快照停滞、人工来源）必须写明原因，作为 ai_summary 报告时的豁免依据。

### 五、后续自动化约定（update_csv.py）

- 周度：每周自动跑，取当期 json 各字段 + 计算 delta。
- 月度：每月 1 日跑 `--period monthly`，stat_time 取当期 json `last_updated` 截到秒。
- **写入前内置校验**（assert，失败即不写入）：
  - pm_total / eclipse_total / vscode_total / total_downloads 四个加和；
  - 各 delta = 本期 − 上期；
  - eclipse_marketplace 使用常量 19。
- **人工维护项收敛为两类**：eclipse_marketplace（常量）、vscode_marketplace + openvsx（季度手工补，note 注明来源）。
- 每次修改 csv 一个 commit，配合 note 形成审计链；可选：CI 中跑全表校验脚本。
