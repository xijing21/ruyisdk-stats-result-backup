# RuyiSDK 统计数据采集与归档设计说明
> 更新日期：2026-11-17
> 本文档覆盖 web-stats / cf-stats / data-repair 三部分的设计、口径与运行方式。

## 一、项目概览
本仓库是一套**定时自动采集 → 结构化归档 → 质量巡检 → 团队通知**的统计流水线，对 RuyiSDK 相关的下载量/安装量数据和 Cloudflare 站点流量数据做长期归档，并保证 csv 派生数据的一致性可随时重算。

### 1.1 子系统分工
| 子系统 | 采集对象 | 核心产物 | 主脚本 | 主 Workflow |
|---|---|---|---|---|
| **web-stats** | ruyisdk.org/dashboard、GitHub Releases、PyPI、VS Code Marketplace、Open VSX、Eclipse Marketplace | 截图 JPG + TXT 报告 + `dashboard_{weekly,monthly}.csv` | `ruyisdk_stats.sh` / `update_dashboard_csv.py` / `ai_summary.py` | `web-stats.yml` |
| **cf-stats** | Cloudflare 账户分析 + Web Analytics（ruyisdk.org / support-matrix / board-docs） | 原始 JSON（5 维细分）+ `cf_{weekly,monthly}.csv` | `cf_stats.py` / `recalc_cf_csv.py` | `cf-stats.yml` |
| **data-repair** | 派生数据重算（手动触发） | 修复后的 csv | `recalc_dashboard_csv.py` / `recalc_cf_csv.py` | `data-repair.yml` |
### 1.2 目录结构
```
仓库根/
├── .github/workflows/
│   ├── web-stats.yml              # 主采集：web-stats + cf-stats 转发 + AI 通知
│   ├── cf-stats.yml               # Cloudflare 专用采集（定时 + 手动补采）
│   ├── data-repair.yml            # 派生数据重算（手动，dry-run / apply）
│   └── cf-introspect.yml          # 前期测试用：Cloudflare GraphQL introspect
├── web-stats/
│   ├── screenshot.mjs             # Playwright 截图
│   ├── ruyisdk_stats.sh           # 多源数据统计（输出 txt 报告）
│   ├── update_dashboard_csv.py    # 写入 dashboard_{weekly,monthly}.csv
│   ├── ai_summary.py              # 质量巡检 + 增长简报 + LLM 总结 + 飞书推送
│   ├── recalc_dashboard_csv.py    # 派生列重算（delta/total 校正）
│   ├── dashboard_weekly.csv       # 周度累计表（追加式）
│   ├── dashboard_monthly.csv      # 月度累计表（追加式）
│   └── results-{weekly,monthly}/  # 每期原始归档（截图/JSON/TXT）
└── cf-stats/
    ├── cf_stats.py                # CF GraphQL 采集
    ├── recalc_cf_csv.py           # TOTAL 行 / collect_date 等派生列重算
    └── results-{weekly,monthly}/  # 每期原始 JSON + 截图
        └── cf_{weekly,monthly}.csv
```


## 二、Web-Stats（RuyiSDK 统计采集）
### 2.1 数据源
| 数据源 | 用途 | 接口形式 |
|---|---|---|
| ruyisdk.org/dashboard | installs / downloads_by_categories_v1 | 官网 JSON 接口 |
| GitHub Releases | 各仓库 Assets 累计下载 | REST API |
| PyPI（pepy） | ruyi 包累计下载 | pepy API（需 key） |
| ES 镜像站 | 组件包/文档/IDE 下载 | ES 查询（netrc 鉴权） |
| VS Code Marketplace | 插件安装量 | 网页解析（VSM_COOKIE） |
| Open VSX | 扩展下载量 | 网页解析 |
| Eclipse Marketplace | 插件安装量 | **冻结常量 19**（插件已停维） |
### 2.2 Workflow 流程（web-stats.yml）
```
[schedule 周二/每月1日 UTC 00:22] 或 [workflow_dispatch]
  ↓
Validate cf backfill inputs       ← 填了 cf_start_date/cf_end_date 则走补采分支
  ├─ true  → Trigger cf-stats backfill（转发到专用 cf-stats.yml，本 run 跳过截图/json/csv）
  └─ false → 正常采集：
       Install Playwright → Create ~/.netrc → Run screenshot script
       → Get result directories（识别本次归档到 results-weekly / results-monthly）
       → Determine periods（auto / weekly / monthly / both）
       → Run statistics script（生成 ruyisdk_stats_*.txt）
       → Update dashboard CSVs（按 period 调 update_dashboard_csv.py）
       → AI summary & Feishu notify（调用 ai_summary.py --send）
       → Commit and push results（git add -A，[skip ci]）
```

关键点：
- **cf-stats 补采转发**：手动填 `cf_start_date`/`cf_end_date`（必须成对），本 workflow 不再直接跑 `cf_stats.py`，而是 `gh workflow run "RuyiSDK Cloudflare Web Stats"` 转发，归档由专用 workflow 自行 commit；
- **并发保护**：`concurrency.group: web-stats` + `cancel-in-progress: false`，同日多次 run 串行执行，不会互相覆盖；
- **AI 通知步骤**使用 `continue-on-error`，失败不影响已归档数据。

### 2.3 dashboard csv 列定义
| 列组 | 含义 |
|---|---|
| `stat_time` | **数据自身时间**（来自 json `last_updated`，UTC 原值不转时区，格式 `YYYY/MM/DD HH:MM`） |
| `installs` | 安装量 |
| `pkg` / `pm_mirror` / `pm_github` / `pm_pypi` | 软件包、ruyi 包管理器（镜像/GitHub/PyPI） |
| `ide_eclipse_mirror` / `ide_plugin_eclipse_mirror` / `ide_plugin_eclipse_github` / `eclipse_marketplace` | Eclipse IDE / 插件多源 |
| `ide_plugin_vscode_mirror` / `ide_plugin_vscode_github` / `vscode_marketplace` / `openvsx` | VS Code 插件多源 |
| `thirdparty` / `humans` | 第三方软件包 / 文档视频 |
| `pm_total` / `eclipse_total` / `vscode_total` / `total_downloads` | 派生合计（脚本计算） |
| `*_delta` | 相对上一行的增量（脚本计算） |
| `period_type` / `is_regular` / `source` / `note` | 元数据 |

### 2.4 写入与幂等规则（update_dashboard_csv.py）
- **数据时间口径**：`stat_time` 取 json 的 `last_updated`（数据自身时间），**与采集运行时间无关**；
- **weekly 判重**：`stat_time` **整串精确匹配（含时分秒）**，已存在则 `[SKIP]` 跳过；不同则追加新行。同一天出现两行 = 官侧数据当天变过两次，是正常现象；
- **monthly 判重**：按 `YYYY/MM/` 月前缀匹配，同月已有记录则跳过；note 注明"快照取每月 1 日归档，增长量归结于上个月"；
- **vscode/openvsx 缺数兜底**：txt 报告解析失败时，沿用上期值并在 note 注明"沿用上期值"；
- **校验**：`*_total` 必须等于分项之和，`*_delta` 允许负数（仅警告），任何"严重"级错误（加和不等、字段缺失）拒绝写入；
- **编码**：读 csv 用 `utf-8-sig` → `gb18030` 回退链；写 csv 统一 `utf-8-sig`。

### 2.5 数据质量巡检（ai_summary.py `check()`）
每行 csv 逐条校验，问题分三个等级：
| 等级 | 触发条件 |
|---|---|
| **严重** | `*_total` ≠ 分项和；`total_downloads` ≠ 六分类和；`*_delta` ≠ 当前值 − 上期值 |
| **警告** | `*_delta` 负增长；最新行 `eclipse_marketplace ≠ 19`；`*_delta` 异常跳变（绝对值 > max(近3期均值×5, 5000)）；周度间隔 > 16 天 |
| **提示** | 冻结字段（`pkg`/`pm_*`/`installs`/`ide_plugin_vscode_*`/`openvsx`）本期零增长（接口停滞?）；stat_time 格式与历史不一致 |

报告只详列最近 5 期（`RECENT_N=5`），历史严重问题不静默、历史警告/提示只计数。

### 2.6 增长简报与飞书推送
**输出定义**：每项三要素 `累计值 | 本期增量 | 环比 + 刻画词`。
- **环比公式**：`(本期增量 B − 上期增量 A) / 上期增量 A × 100%`，正值 = 比上期涨得多；
- **刻画词**：
  - `≥ +50%` → `↑ 显著加速`
  - `+10% ~ +50%` → `↑ 增长加快`
  - `−10% ~ +10%` → `→ 基本持平`
  - `−10% ~ −50%` → `↓ 增长放缓`
  - `≤ −50%` → `↓ 显著放缓`
- **特殊值**：上期 A=0 或 A<0 时显示 `环比 -`，附 `(上期+0)` / `(上期-N)` 标注；
- **周期裁剪**：由 `--period weekly|monthly|both|auto` 控制，workflow 把 `steps.periods.outputs.periods` 翻译后传参；`auto` 为兜底（按 csv 最后行时间自查）；
- **输出样例**：
```
【本期增长】（周度）截至 2026/11/16 19:59
installs（安装量）        45977   本期 +470    环比 -12.5% ↓ 增长放缓
total_downloads（累计下载） 393047  本期 +10941  环比 -29.3% ↓ 增长放缓
>>> 下载量分项：
  pkg（软件包下载量）          ... 本期 +528   环比 -18.4% ↓ 增长放缓
  pm_total（ruyi 包管理器下载） ... 本期 +3993  环比 +6.2%  → 基本持平
  ...
说明：累计值取自本期采集快照；"本期"为本次采集相对上次采集的增量；
环比 =（本期增量 − 上期增量）/ 上期增量 × 100%，正值表示增长加速。
```
**飞书推送**：
- 环境变量：`FEISHU_WEBHOOK_URL`（必填）、`FEISHU_WEBHOOK_SECRET`（签名）、`LLM_API_KEY/LLM_BASE_URL/LLM_MODEL`（可选）；
- **LLM 失败自动降级**为规则文本（`llm_text(facts) or facts`），保证无论 LLM 成败都有一条消息进飞书；
- 日志末尾显式打印 `[feishu] ✅ 已推送 / ❌ 未推送`，避免"绿了但没发"。


## 三、CF-Stats（Cloudflare 流量采集）
### 3.1 数据源与保留期
| 数据源 | 保留期 | 说明 |
|---|---|---|
| 账户分析（GraphQL `httpRequestsAdaptiveGroups`） | **约 32 天**（官方 `4w4d`） | 超过永久丢失 |
| Web Analytics（`rumPageloadEventsAdaptiveGroups`） | **约 180 天** | 三个站点 5 维细分 |
**核心约束**：同一历史窗口，账户数据可能已不可得，站点数据完整。**任何"一刀切"时间校验都会导致要么站点数据被白白放弃、要么账户查询报错中断。**
### 3.2 请求窗口 vs 实际数据窗口
- **请求窗口** = 用户/工作流输入的范围（CSV A 列的口径）；
- **实际数据窗口** = 每个源在窗口内能拿到的最大范围（起点早于保留期会被自动 clamp，完全出界降级为 0）；
- JSON 中通过 `requested_window_local` / `window_local` / `window_clamped` / `window_degraded` 四个字段记录；
- CSV 行标识固定用请求窗口 → **同一期补采原地覆盖，幂等**。
### 3.3 触发方式
- **定时**：每周二 UTC 01:00（周报）+ 每月 1 日 UTC 01:00（月报，窗口=上月整月）；
- **手动补采**：`workflow_dispatch`，可选 `period` + `start_date`/`end_date`（成对填写，跨度 ≤ 92 天）；
- **从 web-stats.yml 转发**：填写 `cf_start_date`/`cf_end_date` 时自动转发（按起始日是否为 1 月 1 日自动判定 weekly/monthly）；
- 本地运行：`CF_API_TOKEN`/`CF_ACCOUNT_ID` 必填，支持 `START_DATE`/`END_DATE`（YYYYMMDD）和 `START_TS`（ISO8601，CI 不支持）。
### 3.4 CSV 列定义
| 列 | 含义 | 口径 |
|---|---|---|
| A_时间窗口 | `YYYYMMDDHHMMSS-YYYYMMDDHHMMSS`（北京时间） | **请求窗口** |
| B_账户请求数 / C_账户访问量 | 账户分析 | 实际范围见 `account_window_utc` |
| D~I 列 | 三站点访问量/页面浏览量 | 实际范围见 `rum_window_utc` |
| `collect_date` | 本次采集运行的日期 | 与数据窗口无关 |
| `window_utc_start/end` / `account_window_utc` / `rum_window_utc` | 窗口边界 | 区分"请求的"和"实际拿到的" |


## 四、Data-Repair（派生数据重算）
### 4.1 Workflow 输入（data-repair.yml）
| 输入 | 取值 | 说明 |
|---|---|---|
| `target` | `all` / `cf_weekly` / `cf_monthly` / `dashboard_weekly` / `dashboard_monthly` | 修复对象 |
| `mode` | `dry-run` / `apply` | dry-run 仅预览差异；apply 重算并 commit |
| `note` | 任意字符串 | 写入 commit message |

并发组 `web-stats`，与主采集互斥，避免 push 竞争。

### 4.2 recalc 脚本功能
- **recalc_cf_csv.py**：重算 CF csv 的 TOTAL 求和行（首行）；修补缺失的 `collect_date` 等派生列；窗口列（`window_utc_start/end`、`account_window_utc`、`rum_window_utc`）非派生值，保留不动；
- **recalc_dashboard_csv.py**：重算 dashboard csv 的派生列（`*_total`、`*_delta`），与分项和/前后行对齐；跳过非派生列；
- 两个脚本均使用 `load_rows()`（`utf-8-sig` → `gb18030` → `latin-1` 编码回退链），避免历史数据带 GBK 字节时报 `UnicodeDecodeError`；
- dry-run 输出统一格式：`数据行数 | 变更单元格数`、`skipped 列（含非派生值，保持现值）`、`列|当前值|期望值` 对照表。
### 4.3 典型使用场景
- 主采集脚本升级后字段口径变化 → 跑 `apply` 修复历史；
- 历史行存在加和不等/增长链断裂（AI 巡检的"严重"项）→ 跑 `apply` 修复；
- 先 `dry-run` 预览，再 `apply` 提交。


## 五、统一约定
| 约定 | 内容 |
|---|---|
| **时间口径** | dashboard 的 `stat_time` = 数据自身时间（json `last_updated`，UTC 原值不转时区）；CF 的窗口 = 北京时间 |
| **幂等判重** | dashboard_weekly 用 `stat_time` 整串精确匹配；dashboard_monthly 用月份前缀；CF 用请求窗口 |
| **编码** | 读 csv：`utf-8-sig` → `gb18030` → `latin-1`；写 csv：`utf-8-sig` |
| **delta 计算** | 相对上一行（同一 csv 内按时间顺序），`prev` 缺失时置空 |
| **派生一致性** | `*_total` = 分项和；`total_downloads` = pkg + pm_total + eclipse_total + vscode_total + thirdparty + humans；可通过 data-repair 重算 |
| **并发保护** | `web-stats`、`cf-stats` 各自独立 concurrency group，`cancel-in-progress: false` |
| **commit 规范** | 主采集 `📊 自动采集(period): timestamp`；cf `chore(cf-stats): update YYYYMMDD [skip ci]`；repair `🔧 [data-repair] fix target: note` |
| **失败隔离** | AI 通知 `continue-on-error`；commit 步骤 `if: always()` |


## 六、本地运行
```bash
# web-stats 常规采集
python web-stats/update_dashboard_csv.py --period weekly
python web-stats/update_dashboard_csv.py --period monthly --json <path> --txt <path>   # 补采
# AI 巡检（不推送）
python web-stats/ai_summary.py --period weekly
python web-stats/ai_summary.py --period monthly
python web-stats/ai_summary.py --period both
# AI 巡检（推送飞书）
python web-stats/ai_summary.py --period both --send
# CF 采集
export CF_API_TOKEN='...' CF_ACCOUNT_ID='...'
python cf-stats/cf_stats.py --period weekly
START_DATE=20260915 END_DATE=20260922 python cf-stats/cf_stats.py --period weekly
# 派生数据修复
python cf-stats/recalc_cf_csv.py --dry-run
python web-stats/recalc_dashboard_csv.py --period weekly --dry-run
```

## 七、Secrets 清单
| Secret | 用途 | 所属 Workflow |
|---|---|---|
| `GH_PAT` | GitHub Releases API + 转发 cf-stats workflow | web-stats.yml |
| `PEPY_API_KEY` | PyPI 下载量 | web-stats.yml |
| `ES_NETRC` | ES 镜像站鉴权 | web-stats.yml |
| `VSM_COOKIE` | VS Code Marketplace 网页解析 | web-stats.yml |
| `FEISHU_WEBHOOK_URL` / `FEISHU_WEBHOOK_SECRET` | 飞书机器人 | web-stats.yml |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | LLM 总结 | web-stats.yml |
| `CF_API_TOKEN` / `CF_ACCOUNT_ID` | Cloudflare API | cf-stats.yml |

## 八、常见故障排查
| 现象 | 原因 | 处置 |
|---|---|---|
| 飞书没收到消息但 Actions 绿 | AI 步骤 `continue-on-error` 吞错，或 `ai_summary.py` 中关键函数为空实现 | 看日志末尾 `[feishu]` 行；确认 `feishu_send`/`llm_text` 已实现 |
| `UnicodeDecodeError: 'utf-8' codec can't decode byte 0x…` | csv 混入 GBK 字节（note 列常见） | 用 `load_rows()`（编码回退链）替换 `open(..., encoding='utf-8')` |
| `NameError: name 'io' is not defined` | 用了 `io.StringIO` 但漏 `import io` | 顶部加 `import io` |
| dashboard csv 同一天两行 | 官侧 `last_updated` 当天变化多次，属正常 | 无需处理；如需修补用 data-repair |
| repair 步骤报"字段无法识别" | csv 列结构与脚本预期不一致 | 先 dry-run 看列名对照，再修 `COLUMNS` 或脚本 |
| 手动跑 `ai_summary.py --period` 没反应 | 未传 `--period` 且 csv 最后行时间超出 7/31 天窗口 | 显式传 `--period weekly/monthly/both` |



