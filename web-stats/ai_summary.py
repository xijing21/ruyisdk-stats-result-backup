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
from datetime import datetime
from pathlib import Path

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
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

import csv  # noqa: E402

def check(name, rows, issues):
    prev, hist = None, {k: [] for k in DELTA_KEYS}
    for r in rows:
        st = r.get("stat_time", "?")
        for t, parts in TOTAL_DEFS.items():
            vals = [to_int(r.get(p)) for p in parts]
            tot = to_int(r.get(t))
            if tot is not None and None not in vals and sum(vals) != tot:
                issues.append(f"[严重] {name} {st}: {t}={tot} ≠ 分项和{sum(vals)}")
        comp = [to_int(r.get("pkg")), to_int(r.get("pm_total")), to_int(r.get("eclipse_total")),
                to_int(r.get("vscode_total")), to_int(r.get("thirdparty")), to_int(r.get("humans"))]
        td = to_int(r.get("total_downloads"))
        if td is not None and None not in comp and td != sum(comp):
            issues.append(f"[严重] {name} {st}: total_downloads={td} ≠ 六分类和{sum(comp)}")
        if to_int(r.get("eclipse_marketplace")) not in (None, 19):
            issues.append(f"[警告] {name} {st}: eclipse_marketplace={r.get('eclipse_marketplace')} ≠ 19")
        for k in DELTA_KEYS:
            d = to_int(r.get(k + "_delta")); cur = to_int(r.get(k)); pv = to_int(prev.get(k)) if prev else None
            if prev and cur is not None and pv is not None and d is not None and d != cur - pv:
                issues.append(f"[严重] {name} {st}: {k}_delta={d} ≠ {cur}-{pv}={cur-pv}")
            if d is not None and d < 0:
                issues.append(f"[警告] {name} {st}: {k}_delta={d} 负增长")
            if d == 0 and k in FROZEN_KEYS and prev:
                issues.append(f"[提示] {name} {st}: {k} 本期零增长（接口停滞?）")
            if d is not None:
                h = hist[k]; h.append(d)
                if len(h) >= 4:
                    avg = sum(abs(x) for x in h[-4:-1]) / 3
                    if abs(d) > max(5 * avg, 5000):
                        issues.append(f"[警告] {name} {st}: {k}_delta={d} 异常跳变（近3期均值|{avg:.0f}|）")
        if prev:
            try:
                d1 = datetime.strptime(prev["stat_time"], "%Y/%m/%d %H:%M")
                d2 = datetime.strptime(st, "%Y/%m/%d %H:%M")
                if "weekly" in name and (d2 - d1).days > 16:
                    issues.append(f"[警告] {name}: 周度间隔异常 {prev['stat_time']} -> {st}（{(d2 - d1).days}天）")
            except ValueError:
                issues.append(f"[提示] {name} {st}: stat_time 格式与历史行不一致")
        prev = r
    return rows

def growth_text(name, rows, period):
    if len(rows) < 2: return f"{name}: 数据不足"
    cur, prev = rows[-1], rows[-2]
    td, tdd = to_int(cur.get("total_downloads")), to_int(cur.get("total_downloads_delta"))
    ins, insd = to_int(cur.get("installs")), to_int(cur.get("installs_delta"))
    wow = f"（{(tdd/ (prev_td := to_int(prev.get('total_downloads'))-to_int(prev.get('total_downloads',0))+1e-9))*100:.1f}%）" if False else ""
    lines = [f"• 截至(cur['stat_time'])"]  # 占位,下面重写
    return ""

def growth(name, rows, period):
    if len(rows) < 2:
        return f"【{period}】数据不足，跳过增长总结"
    cur, prev = rows[-1], rows[-2]
    tdd = to_int(cur.get("total_downloads_delta")) or 0
    prev_tdd = to_int(prev.get("total_downloads")) or 0
    pct = f"{tdd/prev_tdd*100:.1f}%" if prev_tdd else "-"
    parts = []
    for k in DELTA_KEYS:
        d = to_int(cur.get(k + "_delta"))
        if d: parts.append(f"{k}{d:+d}")
    return (f"【{period}】截至 {cur['stat_time']}\n"
            f"  累计下载 {cur.get('total_downloads')}（本期 +{tdd}, 环比 {pct}）\n"
            f"  安装量 {cur.get('installs')}（{to_int(cur.get('installs_delta')) or 0:+d}）\n"
            f"  分项: {'、'.join(parts) if parts else '无增长'}")

def rule_text(issues, wrows, mrows):
    lines = ["📊 RuyiSDK 统计巡检报告", ""]
    lines.append("【数据质量】")
    if issues:
        lines.append(f"发现 {len(issues)} 个问题:")
        lines += [f"  {i+1}. {m}" for i, m in enumerate(issues[:20])]
        if len(issues) > 20: lines.append(f"  ...共{len(issues)}条，详见workflow日志")
    else:
        lines.append("✅ 未发现异常，加和与增长链全部一致")
    lines.append("")
    lines.append(growth("周度", wrows, "周度增长"))
    lines.append(growth("月度", mrows, "月度增长"))
    return "\n".join(lines)

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
    issues = []
    wrows = check("dashboard_weekly.csv", load("dashboard_weekly.csv"), issues)
    mrows = check("dashboard_monthly.csv", load("dashboard_monthly.csv"), issues)
    facts = rule_text(issues, wrows, mrows) + "\n\n（以上为程序核算的原始事实，请整理为简短总结）"
    text = llm_text(facts) or facts
    print(text)
    if a.send: feishu_send(text)

if __name__ == "__main__":
    main()
