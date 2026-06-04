# ruyisdk-stats-result-backup

## RuyiSDK 统计数据自动采集与存档

### 项目目的

RuyiSDK 是一个活跃的开源项目，其下载量、安装量等统计数据分散在多个平台（GitHub Releases、PyPI、VS Code Marketplace、Open VSX、Eclipse Marketplace 等）。本项目通过 **GitHub Actions 定期自动化采集** 这些统计数据，并以 **截图 + 文本报告** 的形式存档到 GitHub 仓库中，形成可追溯的历史证据链。

### 核心功能

| 功能                      | 说明                                                                                       |
| ------------------------- | ------------------------------------------------------------------------------------------ |
| **定时截图存档**    | 每周二、每月末自动对 RuyiSDK 各统计页面进行截图，保存为 JPG                                |
| **多源数据汇总**    | 从 ES 镜像站、GitHub Releases、PyPI、VS Code Marketplace、Open VSX 等渠道采集下载/安装数据 |
| **结构化报告输出**  | 生成包含各渠道统计数据的 TXT 报告，便于对比和审计                                          |
| **GitHub 仓库归档** | 所有截图和报告自动提交到本仓库，形成长期历史存档                                           |

### 采集的数据源

- **RuyiSDK 官网 Dashboard** (`ruyisdk.org/dashboard/`)
- **ES 镜像站** — 组件包、文档、IDE、插件等下载量
- **GitHub Releases** — 各仓库 Assets 累计下载量
- **PyPI** — `ruyi` 包累计下载量
- **VS Code Marketplace** — 插件安装量
- **Open VSX Registry** — 扩展下载量
- **Eclipse Marketplace** — 插件安装量

### 目录结构

```
web-stats/
├── screenshot.mjs          # Playwright 截图脚本
├── ruyisdk_stats.sh        # 多源数据统计脚本
├── results-weekly/         # 每周二采集结果（按日期归档）
│   └── 20260603/
│       ├── dashboard_20260603.jpg
│       ├── openvsx_20260603.jpg
│       ├── eclipse_marketplace_20260603.jpg
│       └── ruyisdk_stats_20260603.txt
└── results-monthly/        # 每月末采集结果（按月归档）
```

### 触发方式

- **定时触发**：每周二 UTC 01:00、每月 28-31 日 UTC 16:00
- **手动触发**：通过 GitHub Actions 的 `workflow_dispatch` 手动运行

### 技术栈

- **GitHub Actions** — CI 定时任务调度
- **Playwright** — 无头浏览器截图
- **Bash + curl/jq** — API 数据采集与统计
- **Git** — 结果自动提交归档

### 备注

- ruyisdk_stats_XXXXXX.txt中最后的报告生成时间为 UTC（世界标准时间）时区；一般中国标准时间（CST）是东八区（UTC+8），比 UTC 快 8 个小时。
