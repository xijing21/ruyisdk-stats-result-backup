# Clouldflare web 访问量采集和归档

## 文件清单

```text
仓库根目录/
├── .github/workflows/cf-stats.yml   # ① Workflow
└── cf-stats/
    ├── cf_stats.py                  # ② 统计 + CSV + JSON（纯标准库）
    ├── cf_report.mjs                # ③ 截图 + PDF（需 playwright）
    ├── package.json                 # ④ Node 依赖声明
    └── .gitignore                   # ⑤ 忽略 node_modules
```

## 配置 Secrets

Secrets 需在 *Settings → Secrets and variables → Actions* 配置 3 个：

| Secret | 说明 |
| ----- | ----- |
| `CF_API_TOKEN` | 权限勾选：`Account Analytics:Read`、`Web Analytics:Read`、`Account Settings:Read` |
| `CF_ACCOUNT_ID`  | Cloudflare 控制台右侧「账户 ID」 |
| `CF_DASH_COOKIE` | 浏览器登录 dash.cloudflare.com 后 F12 → Application → Cookies，把所有 Cookie 按 `name=value; name=value` 拼接 |

## 产出物对照（自检用）

每周二跑完后，`cf-stats/results-weekly/20260929/` 应包含：

```text
cf_weekly.csv                                    （上级目录）
ruyisdk@outlook.com_20260929.png                 账户分析整页截图
ruyisdk@outlook.com_20260929.json                账户分析原始数据
ruyisdk.org_20260929.pdf                         站点打印报告
ruyisdk.org_20260929.json                        站点 RUM 原始数据（5 维细分）
support-matrix_20260929.pdf
support-matrix_20260929.json
board-docs_20260929.pdf
board-docs_20260929.json
_meta.json                                       内部衔接文件
```

每月 1 日跑完后，`cf-stats/results-monthly/202608/` 内 8 个数据文件后缀统一为 `_20260901`（= 本月 1 日，即窗口终点），目录名为 `202608`（= 统计月）。

## 首次调试步骤

```bash
# 1. 本地验证 Python 侧（不需要 Cookie，只要两个环境变量）
export CF_API_TOKEN=xxx
export CF_ACCOUNT_ID=xxx
cd cf-stats
python cf_stats.py --period weekly
#    -> 检查 results-weekly/cf_weekly.csv 的 TOTAL 行是否与控制台「过去7天」一致
#    -> 检查 4 个 JSON 内容是否合理
# 2. 本地验证 Node 侧（需要 Cookie）
export CF_DASH_COOKIE='CF_Authorization=xxx; CF_AppSession=yyy; ...'
node cf_report.mjs
#    -> 打开 PNG/PDF 检查是否完整渲染；若 PDF 内容是登录页，说明 Cookie 失效
# 3. 强制预演月报（不等到 1 日）
FORCE_MONTHLY=1 python cf_stats.py --period monthly
# 4. 提交后到 GitHub Actions 手动 workflow_dispatch(period=weekly) 端到端验证
# 5. 验证通过后，定时 cron 自动生效（周二 09:00 / 每月 1 日 09:00，北京时间）
```

## 调试时最可能遇到的 3 个问题及对策

1. **`cf_report.mjs` 里站点 PDF 的 URL 404**：`/analytics/rum/site/<siteTag>` 是按当前 dashboard 路由推断的，若你的账户路由不同，打开浏览器进入某站点 Web Analytics 页面，复制地址栏真实 URL 模板替换 `BASE_URL/analytics/rum/site/` 部分即可。
2. **GraphQL 报某个 dimension 不存在**（如 `clientRequestHTTPHost`）：说明该字段在账户 schema 中不可用，把 `ACC_Q` 中对应字段删掉、并同步删掉 `acc_json` 里对应的 `by_*` 分组即可，其余逻辑不受影响。
3. **CSV 的 TOTAL 行在旧文件上结构不对**：`upsert_csv` 已做自愈（自动剔除旧 TOTAL 行重算）；若曾用别的表头跑过，直接删掉旧 CSV 重跑一次即可重建。

网络」面板里那条 `analytics` 请求的 Cookie 请求标头就是浏览器实际发给 dashboard 的完整登录态，直接整串复制填进 `CF_DASH_COOKIE` 就能用。不过有 4 个注意点：

1. 建议删掉几个「跨 IP 无用甚至有害」的 Cookie
   整串 Cookie 里混着几类东西，不是所有都值得带去 GitHub Actions：

   | Cookie | 作用 | 建议 |
   | ----- | ----- | ----- |
   | `vses2` | **真正的会话凭据**（核心，必须保留） | ✅ 保留 |
   | `CF_VERIFIED_DEVICE_*` | 设备信任标记 | ✅ 保留 |
   | `__cf_logged_in` | 登录状态标志 | ✅ 保留 |
   | `curr-account` | 当前账户记忆 | ✅ 保留（省得脚本落在账户选择页） |
   | `cf_clearance` | 通过人机验证的凭证，**绑定 IP + UA** | ❌ 删掉——GitHub Actions 的 IP 和你浏览器不同，带着它反而可能触发质询 |
   | `__cf_bm` | Bot 管理指纹，**约 30 分钟就过期**   | ❌ 删掉，带了也没用 |
   | `__cflb` / `_cfuvid` / `sparrow_id` / `OptanonConsent` / `cf_v` 等 | 埋点/负载均衡/偏好 | 可留可删，无影响 |
 
    > 所以复制的整串里，手动把 `cf_clearance=...;` 和 `__cf_bm=...;` 这两段删掉再存 Secret，其余原样保留即可。 

1. User-Agent 要和抓包时一致
   `cf_report.mjs` 里 `newContext` 的 `userAgent` 要改成你截图里这个浏览器的 UA（你用的是 Chrome 152 / Windows）：

```javascript
userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36',
```

会话 Cookie 有时和 UA 弱绑定，UA 对不上可能被视为可疑会话。

3. 验证 Cookie 是否有效的小技巧
   填好 Secret 后不用等周二，先本地跑一次：

```bash
CF_DASH_COOKIE='...' CF_ACCOUNT_ID='...' PERIOD=weekly node cf_report.mjs
```

看输出的第一行——如果跳转到了 `/login` 会打印「未登录（Cookie 过期?）」，说明删多了或复制漏了；正常的话会直接开始截图和生成 PDF。另外首次跑通后记下当天日期，观察下次失效是什么时候，你就能知道这个 session 实际寿命，据此定「多久更新一次 Cookie」的节奏。

> 如果涉及Cookie 泄露，则可以通过退出再登录会让旧 session 失效，然后用新会话重新抓 Cookie。