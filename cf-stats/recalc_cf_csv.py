#!/usr/bin/env python3
"""重算 cf_{weekly,monthly}.csv 的 TOTAL 行

规则:
- TOTAL 行 = 所有数据行(首列 != TOTAL)的数值列逐列求和
- 某列含无法解析的值 → 该列保持现值并标注 skipped
- 首列(窗口标识)固定为 TOTAL; 其余非数值列(日期/UTC窗口等)置空
- 只写 TOTAL 行, 数据行原样保留(原始数据不可变)
- 幂等: 已一致则不写文件

用法:
  python recalc_cf_csv.py [--dry-run] [csv路径 ...]
  不带路径时, 默认处理同目录下 results-weekly/cf_weekly.csv
  与 results-monthly/cf_monthly.csv
"""
import csv, argparse, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))     
REPO = os.path.dirname(HERE)                        
DEFAULTS = [
    os.path.join(HERE, "results-weekly/cf_weekly.csv"),
    os.path.join(HERE, "results-monthly/cf_monthly.csv"),
]

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

    if not fields:
        print(f"::error::空文件或无表头: {path}"); sys.exit(1)

    key = fields[0]
    data = [r for r in rows if (r.get(key) or "").strip() != "TOTAL"]
    old_total = next((r for r in rows if (r.get(key) or "").strip() == "TOTAL"), None)

    total = {c: "" for c in fields}
    total[key] = "TOTAL"
    skipped = []
    for c in fields[1:]:
        vals = [to_num(r.get(c)) for r in data]
        if vals and all(v is not None for v in vals):
            total[c] = str(sum(vals))
        else:
            skipped.append(c)
            if old_total:
                total[c] = old_total.get(c, "")

    changes = [(c, (old_total or {}).get(c, ""), total[c])
               for c in fields if total[c] != (old_total or {}).get(c, "")]

    rel = os.path.relpath(path, REPO)
    lines = [f"### CF TOTAL 重算 — `{rel}`", "",
             f"- 数据行: {len(data)}; 变更单元格: {len(changes)}"]
    if skipped:
        lines.append(f"- skipped 列(含非数值, 保持现值): {', '.join(skipped)}")
    if changes:
        lines += ["", "| 列 | 当前值 | 期望值 |", "|---|---|---|"]
        lines += [f"| {c} | {o or '(空)'} | {n} |" for c, o, n in changes]
    else:
        lines.append("- ✅ TOTAL 行已一致, 无需修复")
    report = "\n".join(lines) + "\n"
    print(report)
    write_summary(report)

    if dry or not changes:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerow(total)
        w.writerows(data)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*",
                    help="要处理的 csv 路径; 缺省处理 results-weekly + results-monthly")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    for p in (a.paths or DEFAULTS):
        recalc(p, a.dry_run)

if __name__ == "__main__":
    main()
