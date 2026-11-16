#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_dashboard_csv.py — 将本次采集结果追加到 dashboard_weekly.csv / dashboard_monthly.csv

数据来源:
  - api_dashboard_*.json : installs / downloads_by_categories_v1（官网 dashboard 接口）
  - ruyisdk_stats_*.txt  : VS Code Marketplace 安装量、Open VSX 下载量（接口无此字段，从报告解析）
  - eclipse_marketplace  : 插件转维护、无程序接口，按常量 19 计

规则:
  - stat_time 取 json 的 last_updated（UTC 原值不转时区），格式 YYYY/M/D H:MM
  - 月度: 每月 1 日归档的快照归结为上个月的增长行（同月已有记录则跳过，幂等）
  - 所有 *_total / *_delta 由本脚本计算；写入前断言校验，不一致立即报错退出

用法:
  python update_dashboard_csv.py --period weekly
  python update_dashboard_csv.py --period monthly
  python update_dashboard_csv.py --period monthly --json <api_dashboard.json> --txt <ruyisdk_stats.txt>   # 补采
"""
import argparse, csv, json, re, sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
ECLIPSE_MARKETPLACE = 19  # 插件转维护，市场数据无程序接口，按 19 计（与历史归档口径一致）

COLUMNS = [
    "stat_time","installs","installs_delta","pkg","pkg_delta",
    "pm_mirror","pm_github","pm_pypi","pm_total","pm_total_delta",
    "ide_eclipse_mirror","ide_plugin_eclipse_mirror","ide_plugin_eclipse_github",
    "eclipse_marketplace","eclipse_total","eclipse_total_delta",
    "ide_plugin_vscode_mirror","ide_plugin_vscode_github",
    "vscode_marketplace","openvsx","vscode_total","vscode_total_delta",
    "thirdparty","thirdparty_delta","humans","humans_delta",
    "total_downloads","total_downloads_delta",
    "period_type","is_regular","source","note",
]
# json 字段映射
CAT_MAP = {
    "pkg": "pkg",
    "pm_mirror": "pm:mirror", "pm_github": "pm:github", "pm_pypi": "pm:pypi",
    "ide_eclipse_mirror": "ide:eclipse:mirror",
    "ide_plugin_eclipse_mirror": "ide:plugin:eclipse:mirror",
    "ide_plugin_eclipse_github": "ide:plugin:eclipse:github",
    "ide_plugin_vscode_mirror": "ide:plugin:vscode:mirror",
    "ide_plugin_vscode_github": "ide:plugin:vscode:github",
    "thirdparty": "3rdparty", "humans": "humans",
}
TOTAL_DEFS = {
    "pm_total": ["pm_mirror", "pm_github", "pm_pypi"],
    "eclipse_total": ["ide_eclipse_mirror", "ide_plugin_eclipse_mirror",
                      "ide_plugin_eclipse_github", "eclipse_marketplace"],
    "vscode_total": ["ide_plugin_vscode_mirror", "ide_plugin_vscode_github",
                     "vscode_marketplace", "openvsx"],
    "total_downloads": ["pkg", "pm_total", "eclipse_total", "vscode_total", "thirdparty", "humans"],
}
DELTA_KEYS = ["installs", "pkg", "pm_total", "eclipse_total", "vscode_total",
              "thirdparty", "humans", "total_downloads"]

RE_VSM = re.compile(r"Visual Studio Marketplace\s+\S+\s+安装量:\s*([\d.]+)")
RE_VSX = re.compile(r"Open VSX\s+\S+\s+累计下载量:\s*(\d+)")

def to_int(v):
    if v in (None, "", "QUERY_FAILED"): return None
    try: return int(float(str(v).strip()))
    except ValueError: return None

def read_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def write_rows(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COLUMNS})

def latest_file(directory, pattern):
    files = sorted(Path(directory).glob(pattern))
    return files[-1] if files else None

def parse_json(json_path):
    data = json.loads(json_path.read_text(encoding="utf-8"))
    vals = {"installs": to_int(data.get("installs", {}).get("total"))}
    cats = data.get("downloads_by_categories_v1", {})
    for col, key in CAT_MAP.items():
        vals[col] = to_int(cats.get(key, {}).get("total"))
    lu = data.get("last_updated", "")
    if not lu: sys.exit(f"[ERROR] {json_path} 缺少 last_updated")
    dt = datetime.fromisoformat(lu.replace("Z", "+00:00"))
    return vals, dt

def parse_txt(txt_path):
    """从统计报告解析 VS Code Marketplace / Open VSX；解析失败返回 None"""
    vsm = vsx = None
    if txt_path and Path(txt_path).exists():
        text = Path(txt_path).read_text(encoding="utf-8", errors="ignore")
        m = RE_VSM.search(text)
        if m: vsm = to_int(m.group(1))
        m = RE_VSX.search(text)
        if m: vsx = to_int(m.group(1))
    return vsm, vsx

def calc_deltas(row, prev):
    for k in DELTA_KEYS:
        if prev is not None and to_int(prev.get(k)) is not None and to_int(row.get(k)) is not None:
            row[k + "_delta"] = to_int(row[k]) - to_int(prev[k])
        else:
            row[k + "_delta"] = ""

def validate(row):
    errs = []
    for total, parts in TOTAL_DEFS.items():
        vals = [to_int(row[p]) for p in parts]
        if None in vals or to_int(row[total]) is None: errs.append(f"{total} 分项缺失")
        elif sum(vals) != to_int(row[total]):
            errs.append(f"{total}={row[total]} != 分项和{sum(vals)}")
    for k in DELTA_KEYS:
        d = to_int(row.get(k + "_delta"))
        if d is not None and d < 0:
            errs.append(f"[警告] {k}_delta={d} 为负增长（真实下降时请在 note 说明）")
    return errs

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["weekly", "monthly"], required=True)
    ap.add_argument("--json", default="")
    ap.add_argument("--txt", default="")
    args = ap.parse_args()

    sub = "results-weekly" if args.period == "weekly" else "results-monthly"
    result_dir = max((p for p in (BASE / sub).glob("*") if p.is_dir()), default=None)
    if result_dir is None: sys.exit(f"[ERROR] {sub} 下没有归档目录")

    json_path = Path(args.json) if args.json else latest_file(result_dir, "api_dashboard_*.json")
    txt_path  = Path(args.txt)  if args.txt  else latest_file(result_dir, "ruyisdk_stats_*.txt")
    if json_path is None: sys.exit(f"[ERROR] {result_dir} 下没有 api_dashboard_*.json")

    vals, dt = parse_json(json_path)
    missing = [k for k, v in vals.items() if v is None]
    if missing: sys.exit(f"[ERROR] json 中缺少字段 {missing}，拒绝写入（数据完整性优先）")

    stat_time = dt.strftime("%Y/%m/%d %H:%M")
    notes = []

    # VS Code Marketplace / Open VSX: 优先报告解析，失败沿用上期值
    vsm, vsx = parse_txt(txt_path)
    rows = read_rows(BASE / f"dashboard_{args.period}.csv")
    prev = rows[-1] if rows else None
    if vsm is None and prev and to_int(prev.get("vscode_marketplace")) is not None:
        vsm = to_int(prev["vscode_marketplace"]); notes.append("vscode_marketplace沿用上期值")
    if vsx is None and prev and to_int(prev.get("openvsx")) is not None:
        vsx = to_int(prev["openvsx"]); notes.append("openvsx沿用上期值")
    if vsm is None or vsx is None:
        sys.exit(f"[ERROR] 无法获得 vscode_marketplace/openvsx 数据（报告解析失败且无上期值）")

    # 月度: 幂等——同一月份已有记录则跳过；note 注明归结月份
    if args.period == "monthly":
        y, m = dt.year, dt.month
        if any(r["stat_time"].startswith(f"{y}/{m}/") for r in rows):
            print(f"[SKIP] {args.period} {y}-{m} 已有记录，跳过")
            return
        cy, cm = (y, m - 1) if m > 1 else (y - 1, 12)
        notes.append(f"快照取每月1日归档，增长量归结于{cy}年{cm}月")
    else:
        if any(r["stat_time"] == stat_time for r in rows):
            print(f"[SKIP] stat_time={stat_time} 已存在，跳过")
            return

    row = {c: "" for c in COLUMNS}
    row.update(vals)
    row.update({
        "stat_time": stat_time,
        "eclipse_marketplace": ECLIPSE_MARKETPLACE,
        "vscode_marketplace": vsm, "openvsx": vsx,
        "period_type": args.period, "is_regular": "TRUE", "source": "workflow",
        "note": "；".join(notes),
    })
    for t, parts in TOTAL_DEFS.items():
        row[t] = sum(to_int(row[p]) for p in parts)
    calc_deltas(row, prev)

    errs = validate(row)
    if any(not e.startswith("[警告]") for e in errs):
        sys.exit("[ERROR] 校验失败，未写入:\n" + "\n".join(errs))

    write_rows(BASE / f"dashboard_{args.period}.csv", rows + [row])
    print(f"[OK] {args.period} 行已写入: stat_time={stat_time} total_downloads={row['total_downloads']}")
    for e in errs: print(e)

if __name__ == "__main__":
    main()
