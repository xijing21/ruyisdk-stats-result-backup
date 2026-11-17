#!/usr/bin/env python3
"""RuyiSDK 统计巡检: 数据质量检查 + 增长简报 + LLM 总结 + 飞书推送

增长区块输出定义（本会话确认的方案甲）:
- 周期: 周采集只发周度, 月采集只发月度, both 时两段并列; 由 --period 控制
- 每项三要素: 中文名 | 当前累计值 | 本期 +N | 环比 X% + 刻画词
- 环比 = (本期增量B − 上期增量A) / 上期增量A × 100%, 正值表示增长加速
- 刻画词: ≥+50% ↑ 显著加速 | +10~50% ↑ 增长加快 | ±10% → 基本持平
          −10~−50% ↓ 增长放缓 | ≤−50% ↓ 显著放缓
- A=0 或 A<0 时环比显示 "-", 刻画显示 "(上期+0)" 或 "(上期-N)"
"""
import csv, os, re, sys, json, argparse, hmac, hashlib, base64
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
WEEKLY_CSV  = os.path.join(HERE, "dashboard_weekly.csv")
MONTHLY_CSV = os.path.join(HERE, "dashboard_monthly.csv")

# (csv列名, 展示名, 说明) — 分项顺序与 dashboard csv 列序一致
TOP_FIELDS   = [("installs", "installs", "安装量"),
                ("total_downloads", "total_downloads", "累计下载")]
SUB_FIELDS   = [
    ("pkg",           "pkg",           "软件包下载量"),
    ("pm_total",      "pm_total",      "ruyi 包管理器下载"),
    ("eclipse_total", "eclipse_total", "IDE for Eclipse"),
    ("vscode_total",  "vscode_total",  "IDE for VS Code"),
    ("thirdparty",    "thirdparty",    "第三方包下载"),
    ("humans",        "humans",        "文档/视频资料下载"),
]

# ============ 基础工具 ============

def to_int(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None

def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))

# ============ 环比刻画（方案甲） ============

def huanbi(cur_d, prev_d):
    """返回 (环比字符串, 刻画字符串)"""
    if prev_d is None or prev_d == 0:
        tag = f"(上期+{prev_d})" if prev_d == 0 else "(上期负增长)"
        return "-", tag
    if prev_d < 0:
        return "-", f"(上期{prev_d})"
    if cur_d is None:
        return "-", ""
    pct = (cur_d - prev_d) / prev_d * 100
    if   pct >=  50: kw = "↑ 显著加速"
    elif pct >=  10: kw = "↑ 增长加快"
    elif pct >  -10: kw = "→ 基本持平"
    elif pct >  -50: kw = "↓ 增长放缓"
    else:            kw = "↓ 显著放缓"
    return f"{pct:+.1f}%", kw

# ============ 增长区块 ============

def fmt_field(col, show, desc, cur, prev):
    """单行: 中文名（说明）  累计值  本期 +N  环比 X% 刻画"""
    cum = to_int(cur.get(col))
    d   = to_int(cur.get(col + "_delta"))
    pd  = to_int(prev.get(col + "_delta"))
    if cum is None and d is None:
        return None
    cum_s = str(cum) if cum is not None else "-"
    d_s   = f"+{d}" if d is not None else "-"
    hb, kw = huanbi(d, pd)
    kw_s   = f" {kw}" if kw else ""
    return f"{show}（{desc}） {cum_s}  本期 {d_s}  环比 {hb}{kw_s}"

def growth_block(period, rows):
    """一段【本期增长】; rows 为该 period 的 csv 全部行(按时间升序)"""
    if len(rows) < 2:
        return [f"【本期增长】({period}) 数据不足，跳过", ""]
    cur, prev = rows[-1], rows[-2]
    ts = (cur.get("stat_time") or "").strip() or "?"

    lines = [f"【本期增长】截至 {ts}", ""]

    # 头部两项
    for col, show, desc in TOP_FIELDS:
        s = fmt_field(col, show, desc, cur, prev)
        if s:
            lines.append(s)
    # 分项
    sub = [fmt_field(*f, cur, prev) for f in SUB_FIELDS]
    sub = [s for s in sub if s]
    if sub:
        lines.append(">>> 下载量分项：")
        lines += [f"  {s}" for s in sub]
    lines += [
        "",
        "说明：累计值取自本期采集快照；\"本期\"为本次采集相对上次采集的增量；",
        "环比 = (本期增量 − 上期增量) / 上期增量 × 100%，正值表示增长加速。",
    ]
    return lines

# ============ 数据质量巡检（沿用原有逻辑） ============

def quality_block():
    """最近5期质量检查; 沿用仓库现有实现, 此处为占位封装"""
    # >>>> 接缝点1: 替换为你仓库原文件中的质量检查函数 <<<<
    # 保持输出:
    #   【数据质量】最近5期发现 X 条：
    #     1. [提示] dashboard_monthly.csv 2026/8/31: pkg 本期零增长（接口停滞?）
    #   （更早历史另有 N 条，多为已知情况，已省略）
    pass

# ============ LLM 总结 + 飞书推送（沿用原有逻辑） ============

def ai_polish(raw_text):
    """调用 LLM 将原始事实整理为简短总结; 沿用仓库现有实现"""
    # >>>> 接缝点2: 替换为你仓库原文件中的 LLM 调用 <<<<
    pass

def send_feishu(text):
    """飞书 webhook 推送(含签名); 沿用仓库现有实现"""
    # >>>> 接缝点3: 替换为你仓库原文件中的飞书推送 <<<<
    pass

# ============ 主流程 ============

def build_report(period):
    """period: weekly / monthly / both → 返回完整报告文本"""
    lines = []
    periods = ["weekly", "monthly"] if period == "both" else [period]
    label = "周度" if period == "weekly" else ("月度" if period == "monthly" else "周度+月度")
    lines.append(f"📊 RuyiSDK 统计巡检报告（{label}）")
    lines.append("")

    q = quality_block()          # 列表或 None
    if q:
        lines += list(q) + [""]

    for p in periods:
        rows = load_csv(WEEKLY_CSV if p == "weekly" else MONTHLY_CSV)
        lines += growth_block(p, rows)
        lines.append("")

    lines.append("（以上为程序核算的原始事实，请整理为简短总结）")
    return "\n".join(lines)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["weekly", "monthly", "both", "auto"],
                    default="auto",
                    help="auto=按 csv 最后行时间自查本次更新了哪个周期")
    ap.add_argument("--send", action="store_true", help="调用 LLM 并推送飞书")
    a = ap.parse_args()

    period = a.period
    if period == "auto":
        # 兜底自查: 7 天内有周度新行 → weekly; 31 天内有月度新行 → monthly
        now = datetime.now(timezone(timedelta(hours=8)))
        pick = []
        for p, path in [("weekly", WEEKLY_CSV), ("monthly", MONTHLY_CSV)]:
            rows = load_csv(path)
            if rows:
                ts = rows[-1].get("stat_time", "")
                try:
                    t = datetime.strptime(ts, "%Y/%m/%d %H:%M")
                    t = t.replace(tzinfo=now.tzinfo)
                    if (now - t).days <= (7 if p == "weekly" else 31):
                        pick.append(p)
                except ValueError:
                    pass
        period = "both" if len(pick) == 2 else (pick[0] if pick else "weekly")

    report = build_report(period)
    print(report)

    if a.send:
        final = ai_polish(report) or report
        send_feishu(final)

if __name__ == "__main__":
    main()
