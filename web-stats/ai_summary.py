#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_summary.py — dashboard csv 数据质量巡检 + 增长总结，推送飞书群机器人
环境变量:
  FEISHU_WEBHOOK_URL   必填（不填则仅打印，不报错）
  FEISHU_WEBHOOK_SECRET 可选（机器人开启签名校验时填）
  LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 可选（配置后用 LLM 生成总结，失败自动降级为规则文本）
用法: python ai_summary.py [--send]
"""
import argparse, base64, hmac, hashlib, json, os, sys, time, urllib.request
import csv  # noqa: E402
from datetime import datetime
from pathlib import Path

RECENT_N = 5  # 只详细报告最近 N 期

BASE = Path(__file__).resolve().parent
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

def to_int(v):
    try: return int(float(str(v).strip()))
    except (ValueError, TypeError): return None

def load(name):
    p = BASE / name
    if not p.exists(): return []
    raw = p.read_bytes()
    text = None
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(enc); break
        except UnicodeDecodeError:
            continue
    if text is None:
        sys.exit(f"[ERROR] {name} 编码无法识别")
    import io
    return list(csv.DictReader(io.StringIO(text)))



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
                legacy_severe.append(line)   # 历史的严重问题不能静默
            else:
                nonlocal legacy_other
                legacy_other += 1            # 历史的警告/提示只计数

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

        # eclipse_marketplace 只校验最新一行：历史是自然增长(6→19)，≠19 属正常
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

def rule_text(recent, legacy_severe, legacy_other, wrows, mrows):
    lines = ["📊 RuyiSDK 统计巡检报告", "", f"【数据质量】（仅详列最近{RECENT_N}期）"]
    if recent:
        lines.append(f"最近{RECENT_N}期发现 {len(recent)} 条：")
        lines += [f"  {i+1}. {m}" for i, m in enumerate(recent[:15])]
        if len(recent) > 15:
            lines.append(f"  ...共{len(recent)}条，详见workflow日志")
    else:
        lines.append("✅ 最近5期未发现异常")
    if legacy_severe:
        lines.append(f"⚠️ 历史遗留严重问题 {len(legacy_severe)} 条（加和/增长链错误，建议用 fix_csv_totals.py 处理）：")
        lines += [f"  - {m}" for m in legacy_severe[:3]]
    if legacy_other:
        lines.append(f"（更早历史另有 {legacy_other} 条警告/提示，多为已知历史情况，已省略）")
    lines.append("")
    lines.append(growth("周度", wrows, "周度增长"))
    lines.append(growth("月度", mrows, "月度增长"))
    return "\n".join(lines)

def growth(name, rows, period):
    if len(rows) < 2:
        return f"【{period}】数据不足，跳过增长总结"
    cur, prev = rows[-1], rows[-2]
    tdd = to_int(cur.get("total_downloads_delta")) or 0
    prev_d = to_int(prev.get("total_downloads_delta")) or 0
    pct = f"{tdd/prev_d*100:.1f}%" if prev_d else "-"
    parts = []
    for k in DELTA_KEYS:
        d = to_int(cur.get(k + "_delta"))
        if d: parts.append(f"{k}{d:+d}")
    return (f"【{period}】截至 {cur['stat_time']}\n"
            f"  累计下载 {cur.get('total_downloads')}（本期 +{tdd}, 环比 {pct}）\n"
            f"  安装量 {cur.get('installs')}（{to_int(cur.get('installs_delta')) or 0:+d}）\n"
            f"  分项: {'、'.join(parts) if parts else '无增长'}")

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
        print(f"[警告] 飞书发送失败: {e}"); return False

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--send", action="store_true"); a = ap.parse_args()
    w_recent, w_severe, w_other = check("dashboard_weekly.csv", wrows := load("dashboard_weekly.csv"))
    m_recent, m_severe, m_other = check("dashboard_monthly.csv", mrows := load("dashboard_monthly.csv"))
    facts = rule_text(w_recent + m_recent, w_severe + m_severe, w_other + m_other, wrows, mrows)
    facts += "\n\n（以上为程序核算的原始事实，请整理为简短总结）"
    text = llm_text(facts) or facts

    print(text)
    if a.send: feishu_send(text)

if __name__ == "__main__":
    main()
