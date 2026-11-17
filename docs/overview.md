# RuyiSDK 统计数据采集与归档

> 更新日期：2026-11-17

## 1. 背景说明

RuyiSDK 是活跃开发中的开源项目，其运营数据分散在多个异构平台：

| 数据类别    | 数据源                                                                                 | 保留期                                                            | 采集方式        |
| ----------- | -------------------------------------------------------------------------------------- | ----------------------------------------------------------------- | --------------- |
| 站点流量    | Cloudflare（账户分析 + Web Analytics，覆盖 ruyisdk.org / support-matrix / board-docs） | 账户分析**~32 天（4w4d）**；Web Analytics **~180 天** | GraphQL API     |
| 下载/安装量 | ES 镜像站、GitHub Releases、PyPI、VS Code Marketplace、Open VSX、Eclipse Marketplace   | **不支持历史查询**，只能“当下快照”                        | API + 截图      |
| 看板快照    | ruyisdk.org/dashboard                                                                  | —                                                                | Playwright 截图 |

**核心矛盾**：这些数据要么会过期消失（CF 账户数据 32 天后永久丢失），要么只能取瞬时值（各下载平台），**没有第二时间点的补救机会**。因此系统的根本目标不是“做报表”，而是**建立一份不可再生的、可追溯的历史证据链**——每个采集周期把“当下能拿到的所有数据”固化到 git 仓库里。

## 2. 设计目标与约束

### 目标

1. **自动周期采集**：每周（周二）、每月（1 日）两条管线各跑一次，产出归档文件 + 累计 CSV；
2. **CF 数据优先保护**：受 32 天保留期约束，CF 采集必须在任何可能失败的步骤之前完成并落库；
3. **历史窗口补采能力**：CF 数据支持手动补采任意 ≤92 天窗口；dashboard 数据不做补采（API 本身不支持，也无意义）；
4. **派生数据可修复**：CSV 中的汇总行/派生列（TOTAL、`*_total`、`*_delta`）提供独立的人工修复入口，与自动采集解耦。

### 硬约束

- **C1 保留期不对称**：账户分析 32 天 vs Web Analytics 180 天 vs 下载平台仅即时——决定补采策略只能给 CF 设计；
- **C2 请求窗口 ≠ 实际数据窗口**：CF 请求起点早于保留线时自动夹取（clamp），完全出界时按 0 值降级；CSV 的 A 列固定用**请求窗口**做行标识以保证幂等；
- **C3 GitHub schedule 为 best-effort**：高峰期（UTC 22:00~02:00）延迟可达数小时，极端情况下任务被静默丢弃；
- **C4 原始数据不可变原则**：脚本/工作流只能追加原始行或重算派生列，永不篡改原始分项数据。

## 3. 系统架构

### 3.1 目录结构

```
ruyisdk-stats-result-backup/
├── .github/workflows/
│   ├── web-stats.yml           # 主采集 workflow（周度+月度全量）
│   ├── cf-stats.yml            # CF 专用 workflow（仅手动补采）
│   ├── data-repair.yml         # 派生数据修复（手动，dry-run / apply 两阶段）
│   ├── cf-introspect.yml       # 前期 GraphQL 探测工具，仅调试用
│   └── ci-stats-screenshot.yml # 旧版截图工作流（已退役，被 web-stats.yml 取代）
├── cf-stats/                   # Cloudflare 管线
│   ├── cf_stats.py             # 核心采集脚本（窗口解析/夹取/降级/幂等写入）
│   ├── recalc_cf_csv.py        # TOTAL 行 / collect_date 等派生列重算
│   ├── results-weekly/         # 周度：每期一个目录（JSON+可选截图）+ cf_weekly.csv
│   └── results-monthly/        # 月度：同上
└── web-stats/                  # 下载量/截图管线/dashboard 
    ├── screenshot.mjs          # Playwright 截图
    ├── ruyisdk_stats.sh        # 多源数据统计（ES/GitHub/PyPI/VSCode/OpenVSX/Eclipse）
    ├── update_dashboard_csv.py # 追加 dashboard csv 行
    ├── ai_summary.py           # 数据质量巡检 + 增长简报 + LLM 总结 + 飞书通知
    ├── recalc_dashboard_csv.py # 派生列（*_total / *_delta）重算
    ├── dashboard_weekly.csv    # 周度累计表
    ├── dashboard_monthly.csv   # 月度累计表
    ├── results-weekly/         # 每期截图+txt 报告
    └── results-monthly/
```

### 3.2 工作流职责分工

| Workflow                                                  | 角色                                                                                                              | 触发                                                                | 产出                                                                                                                             |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| **web-stats.yml**（唯一主采集）                     | **dashboard + Cloudflare 全部采集，单 workflow 打包**                                                       | 定时：周二 00:22 UTC + 每月 1 日 00:22 UTC；手动 dispatch（period） | ① 截图 + txt 报告 + dashboard csv 追加行；② CF 每期 JSON 归档 + cf csv 追加行；③ 一次 commit 全部入库；④ AI 巡检简报推送飞书 |
| **cf-stats.yml**（兜底工具）                        | **仅 CF 补采**：web-stats.yml 未按预期执行且当天未被发现时，手动输入起止周期补采（账户分析 4w+4d 容错期内） | **仅手动 dispatch**（period 必填，start/end 可选）            | 指定窗口的 CF 归档 + csv 追加行，独立 commit                                                                                     |
| **data-repair.yml**（仅人工干预数据时需要手动启动） | 派生数据修复：人工修改/删除 CSV 行后，重算 TOTAL、`*_total`、`*_delta`                                        | 仅手动 dispatch（target / mode=dry-run,apply / note）               | dry-run 差异清单（日志）或 apply 重算 +`[data-repair]` commit                                                                  |
| cf-introspect.yml                                         | GraphQL 探测（调试）                                                                                              | 手动                                                                | 不产出归档                                                                                                                       |
| ci-stats-screenshot.yml                                   | 旧版，已下线被 web-stats.yml 替代                                                                                 | —                                                                  | —                                                                                                                               |

### 3.3 执行流参考

```
                定时(周二/每月1日 00:22 UTC) 或 手动
                              │
              ┌───────────────▼─────────────────┐
              │          web-stats.yml          │
              │  1. CF 采集                      │← 最高优先级; JSON归档 + csv登记(含TOTAL)
              │     (归档+登记一步闭环)            │  一步闭环; 后续任何失败不影响已产出
              │  2. web-stats 采集               │
              │     Playwright 截图 → 多源统计    │  本组步骤可失败, 不影响①
              │     → dashboard csv              │
              │  3. commit (if: always())        │← 数据落库, 关键步骤;
              │     cf-stats/ + web-stats/       │  一次 commit 全部入库
              │  4. AI 通知 (continue-on-error)  │← 锦上添花, 失败不影响已归档数据
              └─────────────────────────────────┘
                              │ 当天未跑/未及时人工发现, 翌日起 4w+4d 容错期内
              ┌───────────────▼─────────────────┐
              │          cf-stats.yml           │
              │  (CF 补采专用, 仅手动)            │← 输入起止 YYYYMMDD; 已删 schedule
              └─────────────────────────────────┘
                              │ 人工改/删 CSV 行后
              ┌───────────────▼─────────────────┐
              │         data-repair.yml         │
              │  dry-run 预览差异                │← 仅重算派生列(TOTAL/*_total/*_delta),
              │  → apply 重算派生列并 commit      │  原始数据不可变; 与主采集共用
              └─────────────────────────────────┘    web-stats 并发组互斥
```

## 4. 关键设计决策

### D1 — CF 数据采集放在最高优先级

web-stats.yml 内部步骤顺序：**CF 采集 → CF TOTAL 重算 → Playwright 截图 → 多源统计 → dashboard csv → AI 通知 → commit（`if: always()`）**。截图/统计等易失败步骤无论结果如何，都不会影响 CF 数据已落库的事实；commit 步骤用 `always()` 兜底保证任何部分成功的数据都被提交。

### D2 — 不做 dashboard 历史补采

所有下载/安装量平台只返回“当前累计值”，过了时间点可能拿不到历史期望时间节点数据（同时也没有特别的必要：记录数据主要是为了观察阶段性增长情况）。

### D3 — CF 补采的窗口语义

`cf_stats.py` 的接口约定：

- `--period {weekly,monthly}` 必填；
- 缺省窗口：weekly = 最近一个周二往前 7 天；monthly = 上月 1 日 ~ 本月 1 日；
- 显式窗口通过 `START_DATE` / `END_DATE`（YYYYMMDD，北京时间）环境变量覆盖；
- **行标识 = 请求窗口**：同一期补采两次原地覆盖，幂等；相邻期首尾相接；

### D4 — 派生数据与原始数据分离

- **原始列**（CF 的 B~I 各期数据行、dashboard 的 `pkg`/`pm_mirror`/`installs` 等分项）：只能由采集产生，人工修正需直接编辑 CSV 并 commit；
- **派生数据**（CF 的 TOTAL 行；dashboard 的 `pm_total`/`eclipse_total`/`vscode_total`/`total_downloads` 及全部 `*_delta`）：可由脚本从原始数据重算，公式已用现有 CSV 交叉验证（如 `total_downloads = pkg + pm_total + eclipse_total + vscode_total + thirdparty + humans`，不含 installs）；
- 派生数据的重算**不在自动管线内做**，由独立的 data-repair 工具负责（§5.4）。

### D5 — 定时任务规避 GitHub 调度高峰

当前 cron `22 0 * * 2` / `22 0 1 * *` 落在 UTC 00:22，处于全球调度拥堵窗口，实测延迟 6~8 小时。**计划改造**（待执行）：迁移到非高峰、非整点的分钟数，例如：

```yaml
- cron: '43 18 * * 2'   # 北京周三 02:43 → 周度窗口不变
- cron: '43 18 1 * *'   # 北京 2 日凌晨 → 月度窗口仍为上月整月
```

并配合外部心跳监控（如 healthchecks.io）在任务被静默丢弃时报警。

### D6 — `stat_time` 是"数据时间”，不是“采集时间" ⚠️ 易遗忘

dashboard csv 的 `stat_time` 取自官网 dashboard json 的 **`last_updated` 字段，即数据自身的时间戳**，与本次采集运行的时钟无关。由此派生两条写入规则（`update_dashboard_csv.py`）：

- **weekly 判重键 = `stat_time` 整串精确匹配（含时分秒）**：官侧没更新 → `last_updated` 不变 → 命中 → SKIP，不产生重复行；官侧当天更新了两次 → `last_updated` 变了 → 追加两行，**这是正常现象，不是 bug**（表示官侧数据当天变过两次）。曾一度考虑“按日期判重+原地覆盖”，已否决——判重必须含时分秒；
- **monthly 判重键 = `YYYY/MM/` 月前缀**：同月已有快照则跳过，note 注明“快照取每月 1 日归档，增长量归结于上个月"；
- 缺数兜底：`vscode_marketplace`/`openvsx` 解析失败时**沿用上一行的值**并在 note 注明，避免断链；
- `eclipse_marketplace` 为**冻结常量 19**（插件已停止维护）。

### D7 — 环比口径统一为增长量环比 ⚠️ 易遗忘

AI 增长简报中的“环比”**不是累计值的环比，而是增长量的环比**：

```
环比 = (本期增长量 B − 上期增长量 A) / 上期增长量 A × 100%
```

正值 = 比上期涨得多，负值 = 比上期涨得少。展示格式为“环比百分比 + 刻画词"：

| 环比值        | 刻画词      |
| ------------- | ----------- |
| ≥ +50%       | ↑ 显著加速 |
| +10% ~ +50%   | ↑ 增长加快 |
| −10% ~ +10%  | → 基本持平 |
| −10% ~ −50% | ↓ 增长放缓 |
| ≤ −50%      | ↓ 显著放缓 |

### D8 — CSV 编码读取必须走回退链 ⚠️ 易遗忘

历史数据中混有 GBK 编码字节（如 note 列的「·」，`0xb7`），硬编码 `encoding="utf-8"` 读取会抛 `UnicodeDecodeError`。**所有读 csv 的脚本统一使用 `load_rows()` 回退链：`utf-8-sig → gb18030 → latin-1`，写回统一 `utf-8-sig`**（写回时顺带归一化编码）。涉及 `update_dashboard_csv.py`、`ai_summary.py`、`recalc_dashboard_csv.py`、`recalc_cf_csv.py`。新增读 csv 的脚本时必须复用该函数，不要直接 `open(..., encoding="utf-8")`。

## 5. 使用手册

### 5.1 自动采集（默认路径，零干预）

周二 / 每月 1 日北京时间早晨：

- `RuyiSDK Stats Collect & Archive`

### 5.2 当天手动采集（替代或补做自动触发）

**场景**：定时任务延迟过久或被丢弃，当天想立即拿到本期数据。
操作：Actions → `RuyiSDK Stats Collect & Archive` → Run workflow →

- period = `weekly`（或 `monthly` / `both`）
- 其余留空
  效果等同定时触发，窗口由脚本按“最近周二”/“上月整月”自动推算。**注意**：周一手动跑只能得到上周那一期（窗口未滚动），新一期要周二才产生。

### 5.3 CF 历史窗口补采

**场景**：某期 CF 数据缺失或异常，且窗口起点仍在 32 天保留期内。
操作：Actions → `RuyiSDK Cloudflare Web Stats` → Run workflow →

- period = `weekly` 或 `monthly`（必填）
- start_date / end_date = `YYYYMMDD`，**同时填或同时留空**
- 月度补采起始日必须为 1 日（输出目录按起始年月命名）
  约束：窗口跨度 ≤92 天；超过保留期的部分由脚本自动夹取/降级，实际数据范围记录在 CSV 的 `account_window_utc` / `rum_window_utc` 列。

### 5.4 派生数据修复（data-repair.yml，已实现）

**场景**：人工清理手动触发的 workflow ，或者自动+手动执行导致数据重复或者数据周期不对，手动删除数据后派生列需要刷新；历史行存在加和不等/增长链断裂（AI 巡检报“严重"）。
操作：Actions → `RuyiSDK Data Repair` → Run workflow →

| 输入                                                 | 取值                                                                                    | 说明                                                                |
| ---------------------------------------------------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| `target`                                           | `all` / `cf_weekly` / `cf_monthly` / `dashboard_weekly` / `dashboard_monthly` | 修复对象                                                            |
| `mode`                                             | `dry-run` / `apply`                                                                 | dry-run 仅在日志输出差异清单，**不写库**；apply 重算并 commit |
| `note`                                             | 任意字符串                                                                              | 写入 commit message                                                 |
| 两阶段流程：先 dry-run 核对差异（输出格式：`数据行数 | 变更单元格数 `、`skipped 列（含非派生值，保持现值）`、`列                           | 当前值                                                              |

### 5.5 AI 巡检与飞书通知（ai_summary.py）

每次主采集结束后自动运行，也可本地手动跑（`--period weekly|monthly|both`，`--send` 推送）。报告三部分：

**① 数据质量巡检**——逐行校验两个 dashboard csv，问题分三级：

| 等级 | 触发条件                                                                                                                                                                            |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 严重 | `*_total` ≠ 分项和；`total_downloads` ≠ 六分类和；`*_delta` ≠ 当前值 − 上期值                                                                                             |
| 警告 | `*_delta` 负增长；`*_delta` 异常跳变（> max(近3期均值×5, 5000)）；周度间隔 >16 天；最新行 `eclipse_marketplace ≠ 19`                                                        |
| 提示 | 冻结字段零增长（接口停滞?）：`pkg`/`pm_mirror`/`pm_github`/`pm_pypi`/`installs`/`ide_plugin_vscode_mirror`/`ide_plugin_vscode_github`/`openvsx`；stat_time 格式异常 |

报告只详列最近 5 期；历史严重问题不静默（提示用 data-repair 修复），历史警告/提示仅计数省略。

**② 增长简报**——按 D7 口径输出每项三要素：

```
installs（安装量）        45977   本期 +470    环比 -12.5% ↓ 增长放缓
total_downloads（累计下载） 393047  本期 +10941  环比 -29.3% ↓ 增长放缓
>>> 下载量分项：
  pkg（软件包下载量）           ...  本期 +528   环比 -18.4% ↓ 增长放缓
  pm_total（ruyi 包管理器下载）  ...  本期 +3993  环比 +6.2%  → 基本持平
  ...
```

周期裁剪由 `--period` 控制：workflow 把 `steps.periods.outputs.periods`（`weekly`/`monthly`/`weekly monthly`）翻译成脚本参数（空格分隔 → `both`），周采集只发周度段、月采集只发月度段。

**③ LLM 总结 + 推送**

配置 `LLM_API_KEY/LLM_BASE_URL/LLM_MODEL` 时用 LLM 把事实整理为简短总结；**LLM 失败自动降级为规则文本**，保证无论成败都有一条消息进飞书。日志末尾显式打印 `[feishu] ✅ 已推送 / ❌ 未推送`。

## 6. 运维注意事项

| 主题                                   | 说明                                                                                                                                                                                                                    |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **定时任务延迟/丢失**            | GitHub schedule 是全球共享队列，best-effort；实测曾延迟 6~8 小时。表现：run 列表里定时 run 迟迟不出现，或整场被静默丢弃（无任何日志）。应对：超时后改手动（§5.2），并按 D5 改造 cron + 加心跳监控                      |
| **同一天重复采集**               | 定时和手动都跑也不会脏数据——dashboard 按 `stat_time` 精确匹配、CF 按请求窗口幂等去重，git diff 为空时 commit 步骤自动跳过；最坏只是多一次 API 调用                                                                  |
| **dashboard 同一天出现两行**     | 官侧 `last_updated` 当天变化多次所致，属正常（见 D6），勿当作 bug 修复                                                                                                                                                |
| **Actions 绿但飞书没消息**       | AI 步骤 `continue-on-error` 会吞错。看该步骤日志末尾的 `[feishu]` 行定位；常见原因：webhook secret 未配、LLM 超时（会降级不应断发）、推送函数异常                                                                   |
| **UnicodeDecodeError (0xb7 等)** | csv 混入 GBK 字节，读文件处未走 `load_rows()` 回退链（D8）。修复：改用 `load_rows()`；同时确认 `import io` 在顶部（曾因漏 import 报 `NameError`）                                                               |
| **派生数据不一致**               | AI 巡检报"严重"或人工发现加和不等 → 跑 data-repair（§5.4），先 dry-run 后 apply                                                                                                                                       |
| **secrets 清单**                 | `CF_API_TOKEN`、`CF_ACCOUNT_ID`（CF）；`ES_NETRC`、`VSM_COOKIE`（截图）；`GH_PAT`、`PEPY_API_KEY`（多源统计）；`FEISHU_WEBHOOK_URL`/`SECRET`、`LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`（AI 通知） |
| **旧 workflow 下线**             | `ci-stats-screenshot.yml` 与 web-stats.yml 功能重叠，会导致同时段双份截图采集，应禁用或删除                                                                                                                           |
| **并发保护**                     | `web-stats` 并发组覆盖主采集与 data-repair（互斥防 push 竞争），`cancel-in-progress: false`；cf-stats 独立成组                                                                                                      |

## 附录 A：关键公式速查

**CF TOTAL 行**：对 `cf_{weekly,monthly}.csv` 全部数据行的 B~I 列逐列求和，其余列置空；`collect_date` 等派生列缺失时由 recalc 补齐。
**Dashboard totals**（分项齐全才计算）：

```
pm_total        = pm_mirror + pm_github + pm_pypi
eclipse_total   = ide_eclipse_mirror + ide_plugin_eclipse_mirror
                + ide_plugin_eclipse_github + eclipse_marketplace
vscode_total    = ide_plugin_vscode_mirror + ide_plugin_vscode_github
                + vscode_marketplace + openvsx
total_downloads = pkg + pm_total + eclipse_total + vscode_total
                + thirdparty + humans        ← 不含 installs
```

**Dashboard deltas**：`<field>_delta = 本行值 − 上一行值`（首行或任一侧为空 → 空）；覆盖 `installs`、`pkg`、`pm_total`、`eclipse_total`、`vscode_total`、`thirdparty`、`humans`、`total_downloads` 共 8 列。
**人工在 csv 中间插入/修改行后，其后所有行的 delta 链失效，必须跑 data-repair 重算**。

## 附录 B：幂等键与时间口径速查 ⚠️ 易遗忘

| 表                         | 行标识（幂等键）                                    | 时间含义                                                      |
| -------------------------- | --------------------------------------------------- | ------------------------------------------------------------- |
| dashboard_weekly.csv       | `stat_time` 整串（含时分秒）精确匹配，命中即 SKIP | **数据时间**（json `last_updated`，UTC 原值不转时区） |
| dashboard_monthly.csv      | `stat_time` 的 `YYYY/MM/` 月前缀，命中即 SKIP   | 同上                                                          |
| cf_weekly / cf_monthly.csv | A 列请求窗口（北京时间），同窗口补采原地覆盖        | **请求窗口**（实际数据范围另记于 `*_window_utc` 列）  |
| CF JSON 归档目录           | 按请求窗口命名，同窗口补采覆盖                      | 同上                                                          |
