#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_csv_totals.py — 人工干预数据后，重算 *_total 与 *_total_delta

用法:
  python fix_csv_totals.py --all              # 检查两个csv，显示差异（默认 dry-run）
  python fix_csv_totals.py --all --apply      # 实际写回
  python fix_csv_totals.py dashboard_weekly.csv --apply
"""
import argparse, csv, sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
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

def to_int(v):
    try: return int(float(str(v).strip()))
    except (ValueError, TypeError): return None

def fix(path, apply):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f); cols = reader.fieldnames; rows = list(reader)
    diffs, warns = [], []
    prev = None
    for i, r in enumerate(rows):
        for t, parts in TOTAL_DEFS.items():
            vals = [to_int(r[p]) for p in parts]
            if None in vals:
                warns.append(f"第{i+2}行 {r['stat_time']}: {t} 分项有空值，跳过"); continue
        # 重算 total
            new = sum(vals)
            if to_int(r[t]) != new:
                diffs.append(f"第{i+2}行 {r['stat_time']}: {t} {r[t]} -> {new}")
                r[t] = str(new)
        # 重算 delta
        for k in DELTA_KEYS:
            if prev and to_int(r.get(k)) is not None and to_int(prev.get(k)) is not None:
                new = to_int(r[k]) - to_int(prev[k])
                if to_int(r.get(k + "_delta")) != new:
                    diffs.append(f"第{i+2}行 {r['stat_time']}: {k}_delta {r.get(k+'_delta')} -> {new}")
                r[k + "_delta"] = str(new)
            elif to_int(r.get(k + "_delta")) not in (None,):
                r[k + "_delta"] = ""
            if to_int(r.get(k + "_delta")) is not None and to_int(r[k + "_delta"]) < 0:
                warns.append(f"第{i+2}行 {r['stat_time']}: {k}_delta={r[k+'_delta']} 为负，请确认是否真实下降")
        if to_int(r.get("eclipse_marketplace")) not in (None, 19):
            warns.append(f"第{i+2}行 {r['stat_time']}: eclipse_marketplace={r['eclipse_marketplace']} ≠ 19")
        prev = r
    print(f"== {path.name} ==")
    print("\n".join(diffs) if diffs else "  total/delta 全部一致，无需修正")
    for w in warns: print("  [警告]", w)
    if apply and diffs:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
        print(f"  已写回 {path.name}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.all:
        targets = [BASE / "dashboard_weekly.csv", BASE / "dashboard_monthly.csv"]
    else:
        targets = [Path(p) for p in a.files] or sys.exit("请指定csv路径或 --all")
    for t in targets: fix(t, a.apply)

if __name__ == "__main__":
    main()
