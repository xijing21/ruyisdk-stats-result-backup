#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_summary.py — dashboard csv 数据质量巡检 + 增长总结，推送飞书群机器人
环境变量:
  FEISHU_WEBHOOK_URL    必填（不填则仅打印，不报错）
  FEISHU_WEBHOOK_SECRET 可选（机器人开启签名校验时填）
  LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 可选（配置后用 LLM 生成总结，失败自动降级为规则文本）
用法: python ai_summary.py [--period weekly|monthly|both|auto] [--send]
  --period 由 workflow 按 steps.periods.outputs.periods 传入;
  auto 为兜底: 按 csv 最后行时间自查本次更新了哪个周期
"""
import argparse, base64, hmac, hashlib, json, os, sys, time, urllib.request
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path

RECENT_N = 5  # 只详细报告最近 N 期

BASE = Path(__file__).resolve().parent
WEEKLY_CSV  = BASE / "dashboard_weekly.csv"
MONTHLY_CSV = BASE / "dashboard_monthly.csv"

TOTAL_DEFS = {
    "pm_total": ["pm_mirror", "pm_github", "pm_pypi"],
    "eclipse_total": ["ide_eclipse_mirror", "ide_plugin_eclipse_mirror",
                      "ide_plugin_eclipse_github", "eclipse_marketplace"],
    "vscode_total": ["ide_plugin_vscode_mirror", "ide_plugin_vscode_github",
                     "vscode_marketplace", "openvsx"],
}
DELTA_KEYS = ["installs", "pkg", "pm_total", "eclipse_total", "vscode_total",
              "thirdparty", "humans", "total_downloads"]
FROZEN_KEYS = ["pkg", "pm_mirror", "pm_github", "pm_pypi", "installs",
               "ide_plugin_vscode_mirror", "ide_plugin_vscode_github", "openvsx"]

# 展示名: 列名 → (展示标签, 中文名)
DISPLAY = {
    "installs":        ("installs",        "安装量"),
    "total_downloads": ("total_downloads", "累计下载"),
    "pkg":             ("pkg",             "软件包下载量"),
    "pm_total":        ("pm_total",        "ruyi 包管理器下载"),
    "eclipse_total":   ("eclipse_total",   "IDE for Eclipse"),
    "vscode_total":    ("vscode_total",    "IDE for VS Code"),
    "thirdparty":      ("thirdparty",      "第三方包下载"),
    "humans":          ("humans",          "文档/视频资料下载"),
}
TOP_FIELDS = ["installs", "total_downloads"]
SUB_FIELDS = ["pkg", "pm_total", "eclipse_total", "vscode_total", "thirdparty", "humans"]

# ============ 基础 ============

def to_int(v):
    try: return int(float(str(v).strip()))
    except (ValueError, TypeError): return None

def load(path):
    if not path.exists(): return []
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(enc); break
        except UnicodeDecodeError:
            continue
    if text is None:
        sys.exit(f"[ERROR] {path.name} 编码无法识别")
    import io
    return list(csv.DictReader(io.StringIO(text)))

# ============ 数据质量巡检 ============

def check(name, rows):
    """返回 (recent_issues, legacy_severe, legacy_other_count)"""
    recent, legacy_severe = [], []
    legacy_other = 0
    n = len(rows)
    recent_start = max(0, n - RECENT_N)
    prev, hist = None, {k: [] for k in DELTA_KEYS}

    for i, r in enumerate(rows):
        st = r.get("stat_time", "?")
        is_recent = i >= recent_start

        def add(level, msg):
            line = f"[{level}] {name} {st}: {msg}"
            if is_recent:
                recent.append(line)
            elif level == "严重":
                legacy_severe.append(line)
            else:
                nonlocal legacy_other
                legacy_other += 1

        for t, parts in TOTAL_DEFS.items():
            vals = [to_int(r.get(p)) for p in parts]
            tot = to_int(r.get(t))
            if tot is not None and None not in vals and sum(vals) != tot:
                add("严重", f"{t}={tot} ≠ 分项和{sum(vals)}")
        comp = [to_int(r.get("pkg")), to_int(r.get("pm_total")),
                to_int(r.get("eclipse_total")), to_int(r.get("vscode_total")),
                to_int(r.get("thirdparty")), to_int(r.get("humans"))]
        td = to_int(r.get("total_downloads"))
        if td is not None and None not in comp and td != sum(comp):
            add("严重", f"total_downloads={td} ≠ 六分类和{sum(comp)}")

        if i == n - 1 and to_int(r.get("eclipse_marketplace")) not in (None, 19):
            add("警告", f"eclipse_marketplace={r.get('eclipse_marketplace')} ≠ 19（最新行应为冻结值19）")

        for k in DELTA_KEYS:
            d = to_int(r.get(k + "_delta"))
            cur = to_int(r.get(k)); pv = to_int(prev.get(k)) if prev else None
            if prev and cur is not None and pv is not None and d is not None and d != cur - pv:
                add("严重", f"{k}_delta={d} ≠ {cur}-{pv}={cur - pv}")
            if d is not None and d < 0:
                add("警告", f"{k}_delta={d} 负增长")
            if d == 0 and k in FROZEN_KEYS and prev:
                add("提示", f"{k} 本期零增长（接口停滞?）")
            if d is not None:
                h = hist[k]; h.append(d)
                if len(h) >= 4:
                    avg = sum(abs(x) for x in h[-4:-1]) / 3
                    if abs(d) > max(5 * avg, 5000):
                        add("警告", f"{k}_delta={d} 异常跳变（近3期均值|{avg:.0f}|）")

        if prev:
            try:
                d1 = datetime.strptime(prev["stat_time"], "%Y/%m/%d %H:%M")
                d2 = datetime.strptime(st, "%Y/%m/%d %H:%M")
                if "weekly" in name and (d2 - d1).days > 16:
                    add("警告", f"周度间隔异常 {prev['stat_time']} -> {st}（{(d2 - d1).days}天）")
            except ValueError:
                add("提示", "stat_time 格式与历史行不一致")
        prev = r

    return recent, legacy_severe, legacy_other

# ============ 增长区块（ 环比=(B−A)/A + 刻画词） ============

def huanbi(cur_d, prev_d):
    """返回 (环比字符串, 刻画字符串)"""
    if prev_d is None or prev_d == 0:
        return "-", "(上期+0)" if prev_d == 0 else "(上期负增长)"
    if prev_d < 0:
        return "-", f"(上期{prev_d:+d})"
    if cur_d is None:
        return "-", ""
    pct = (cur_d - prev_d) / prev_d * 100
    if   pct >=  50: kw = "↑ 显著加速"
    elif pct >=  10: kw = "↑ 增长加快"
    elif pct >  -10: kw = "→ 基本持平"
    elif pct >  -50: kw = "↓ 增长放缓"
    else:            kw = "↓ 显著放缓"
    return f"{pct:+.1f}%", kw

def fmt_line(col, cur, prev):
    show, desc = DISPLAY[col]
    cum = to_int(cur.get(col))
    d   = to_int(cur.get(col + "_delta"))
    pd  = to_int(prev.get(col + "_delta"))
    if cum is None and d is None:
        return None
    cum_s = str(cum) if cum is not None else "-"
    d_s   = f"{d:+d}" if d is not None else "-"
    hb, kw = huanbi(d, pd)
    kw_s   = f" {kw}" if kw else ""
    return f"{show}（{desc}） {cum_s}  本期 {d_s}  环比 {hb}{kw_s}"

def growth_block(label, rows):
    """一段【本期增长】; rows 为该周期 csv 全部行(时间升序)"""
    if len(rows) < 2:
        return [f"【本期增长】（{label}）数据不足，跳过", ""]
    cur, prev = rows[-1], rows[-2]
    ts = (cur.get("stat_time") or "").strip() or "?"
    lines = [f"【本期增长】（{label}）截至 {ts}", ""]
    for col in TOP_FIELDS:
        s = fmt_line(col, cur, prev)
        if s: lines.append(s)
    sub = [fmt_line(c, cur, prev) for c in SUB_FIELDS]
    sub = [s for s in sub if s]
    if sub:
        lines.append(">>> 下载量分项：")
        lines += [f"  {s}" for s in sub]
    lines += [
        "",
        "说明：累计值取自本期采集快照；\"本期\"为本次采集相对上次采集的增量；",
        "环比 =（本期增量 − 上期增量）/ 上期增量 × 100%，正值表示增长加速。",
    ]
    return lines

# ============ 报告组装 ============

def build_report(period):
    """period: weekly / monthly / both"""
    wrows = load(WEEKLY_CSV)
    mrows = load(MONTHLY_CSV)

    # 质量巡检: 两个 csv 都查（问题不区分周期，统一列出）
    recent, legacy_severe, legacy_other = [], [], 0
    for name, rows in (("dashboard_weekly.csv", wrows),
                       ("dashboard_monthly.csv", mrows)):
        rc, ls, lo = check(name, rows)
        recent += rc; legacy_severe += ls; legacy_other += lo

    label = {"weekly": "周度", "monthly": "月度", "both": "周度+月度"}.get(period, "周度")
    lines = [f"📊 RuyiSDK 统计巡检报告（{label}）", "", f"【数据质量】（仅详列最近{RECENT_N}期）"]
    if recent:
        lines.append(f"最近{RECENT_N}期发现 {len(recent)} 条：")
        lines += [f"  {i+1}. {m}" for i, m in enumerate(recent[:15])]
        if len(recent) > 15:
            lines.append(f"  ...共{len(recent)}条，详见workflow日志")
    else:
        lines.append("✅ 最近5期未发现异常")
    if legacy_severe:
        lines.append(f"⚠️ 历史遗留严重问题 {len(legacy_severe)} 条（加和/增长链错误，建议用 data-repair 处理）：")
        lines += [f"  - {m}" for m in legacy_severe[:3]]
    if legacy_other:
        lines.append(f"（更早历史另有 {legacy_other} 条警告/提示，多为已知历史情况，已省略）")
    lines.append("")

    if period in ("weekly", "both"):
        lines += growth_block("周度", wrows) + [""]
    if period in ("monthly", "both"):
        lines += growth_block("月度", mrows) + [""]

    lines.append("（以上为程序核算的原始事实，请整理为简短总结）")
    return "\n".join(lines)

# ============ LLM 总结 + 飞书推送 ============

def llm_text(facts):
    key = os.environ.get("LLM_API_KEY")
    if not key: return None
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    body = json.dumps({"model": model, "temperature": 0.3, "messages": [
        {"role": "system", "content": "你是数据巡检助手，用简洁中文总结统计数据质量与增长情况，面向团队群通知，300字以内。"},
        {"role": "user", "content": facts}]}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[提示] LLM 总结失败，降级规则文本: {e}")
        return None

def feishu_send(text):
    url = os.environ.get("FEISHU_WEBHOOK_URL")
    if not url:
        print("[提示] 未配置 FEISHU_WEBHOOK_URL，仅打印"); return False
    payload = {"msg_type": "text", "content": {"text": text}}
    secret = os.environ.get("FEISHU_WEBHOOK_SECRET")
    if secret:
        ts = str(int(time.time()))
        sign = base64.b64encode(hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()).decode()
        payload.update({"timestamp": ts, "sign": sign})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            if resp.get("code") not in (0, None):
                print(f"[警告] 飞书返回异常: {resp}"); return False
        return True
    except Exception as e:
        print(f"[警告] 飞书推送失败: {e}"); return False

# ============ 主流程 ============

def auto_period():
    """兜底: 按 csv 最后行时间判断本次更新了哪个周期"""
    now = datetime.now(timezone(timedelta(hours=8)))
    pick = []
    for p, path, days in (("weekly", WEEKLY_CSV, 7), ("monthly", MONTHLY_CSV, 31)):
        rows = load(path)
        if not rows: continue
        try:
            t = datetime.strptime(rows[-1].get("stat_time", ""), "%Y/%m/%d %H:%M")
            if abs((now.replace(tzinfo=None) - t).days) <= days:
                pick.append(p)
        except ValueError:
            pass
    return "both" if len(pick) == 2 else (pick[0] if pick else "weekly")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["weekly", "monthly", "both", "auto"], default="auto")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()

    period = auto_period() if a.period == "auto" else a.period
    print(f"[period] {period}")
    facts = build_report(period)
    print(facts)

    if not a.send:
        return
    final = llm_text(facts) or facts   # LLM 失败降级为规则文本
    ok = feishu_send(final)
    print(f"[feishu] {'✅ 已推送' if ok else '❌ 未推送'}")

if __name__ == "__main__":
    main()
