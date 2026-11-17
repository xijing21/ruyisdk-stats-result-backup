#!/usr/bin/env python3
"""重算 dashboard_{weekly,monthly}.csv 的 *_total 与 *_delta

total 规则(分项齐全才计算; 缺分项保持现值并标注 skipped):
  pm_total        = pm_mirror + pm_github + pm_pypi
  eclipse_total   = ide_eclipse_mirror + ide_plugin_eclipse_mirror
                  + ide_plugin_eclipse_github + eclipse_marketplace
  vscode_total    = ide_plugin_vscode_mirror + ide_plugin_vscode_github
                  + vscode_marketplace + openvsx
  total_downloads = pkg + pm_total + eclipse_total + vscode_total
                  + thirdparty + humans        (不含 installs)
delta 规则: <field>_delta = 本行 − 上一行; 首行或任一侧为空 → 空
只写派生列, 原始分项列永不修改。幂等: 已一致则不写文件。

用法:
  python recalc_dashboard_csv.py --period {weekly,monthly,both} [--dry-run]
"""
import csv, argparse, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))     # .../web-stats
REPO = os.path.dirname(HERE)                          # 仓库根

TOTAL_RULES = {
    "pm_total":        ["pm_mirror", "pm_github", "pm_pypi"],
    "eclipse_total":   ["ide_eclipse_mirror", "ide_plugin_eclipse_mirror",
                        "ide_plugin_eclipse_github", "eclipse_marketplace"],
    "vscode_total":    ["ide_plugin_vscode_mirror", "ide_plugin_vscode_github",
                        "vscode_marketplace", "openvsx"],
    "total_downloads": ["pkg", "pm_total", "eclipse_total",
                        "vscode_total", "thirdparty", "humans"],
}
DELTA_FIELDS = ["installs", "pkg", "pm_total", "eclipse_total", "vscode_total",
                "thirdparty", "humans", "total_downloads"]

def load_rows(path):
    """读取 csv, 自动尝试常见编码; 返回 (fieldnames, rows)"""
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            text = raw.decode(enc); break
        except UnicodeDecodeError:
            continue
    if text is None:
        sys.exit(f"[ERROR] {path} 编码无法识别")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader.fieldnames or []), list(reader)

def to_num(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None

def write_summary(text):
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)

def recalc(path, dry):
    if not os.path.exists(path):
        print(f"::error::文件不存在: {path}"); sys.exit(1)
    fields, rows = load_rows(path)

    idcol = fields[0] if fields else "stat_time"
    changes, skipped = [], []
    prev, out = {}, []

    for i, row in enumerate(rows):
        r = dict(row)
        rid = (r.get(idcol) or f"row{i+2}").strip()
        for tcol, parts in TOTAL_RULES.items():
            if tcol not in r:
                continue
            vals = [to_num(r.get(p)) for p in parts]
            if all(v is not None for v in vals):
                new = str(sum(vals))
            else:
                skipped.append((rid, tcol))
                new = r.get(tcol, "")          # 保持现值
            if new != r.get(tcol, ""):
                changes.append((rid, tcol, r.get(tcol, ""), new))
                r[tcol] = new
        for fld in DELTA_FIELDS:
            dcol = fld + "_delta"
            if dcol not in r:
                continue
            cur = to_num(r.get(fld))
            if cur is None or prev.get(fld) is None:
                new = ""
            else:
                new = str(cur - prev[fld])
            if new != r.get(dcol, ""):
                changes.append((rid, dcol, r.get(dcol, ""), new))
                r[dcol] = new
            if cur is not None:
                prev[fld] = cur
        out.append(r)

    rel = os.path.relpath(path, REPO)
    lines = [f"### Dashboard 重算 — `{rel}`", "",
             f"- 数据行: {len(rows)}; 变更单元格: {len(changes)}"]
    if skipped:
        lines += ["", "skipped(缺分项, 保持现值): " +
                  ", ".join(f"{rid}/{c}" for rid, c in skipped)]
    if changes:
        lines += ["", "| 行 | 列 | 当前值 | 期望值 |", "|---|---|---|---|"]
        lines += [f"| {rid} | {c} | {o or '(空)'} | {n} |" for rid, c, o, n in changes]
    else:
        lines.append("- ✅ 派生列已一致, 无需修复")
    report = "\n".join(lines) + "\n"
    print(report)
    write_summary(report)

    if dry or not changes:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["weekly", "monthly", "both"], required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    periods = ["weekly", "monthly"] if a.period == "both" else [a.period]
    for p in periods:
        recalc(os.path.join(HERE, f"dashboard_{p}.csv"), a.dry_run)

if __name__ == "__main__":
    main()
