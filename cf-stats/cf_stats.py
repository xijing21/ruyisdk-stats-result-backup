#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RuyiSDK Cloudflare 统计归档（GitHub Actions 版）
=================================================
职责：
  1. 按 period=weekly/monthly 计算统计窗口（GMT+8，左闭右开，时分秒统一 00:00:00）
  2. 调 Cloudflare GraphQL / REST 拉取账户分析 + 三站点 Web Analytics 数据
  3. 追加写入累计 CSV（表头下第一行 = TOTAL 求和行，幂等可重跑）
  4. 输出原始数据 JSON：1 份账户 + 每站点 1 份（5 维细分）
  5. 写 _meta.json 供 cf_report.mjs 使用

窗口口径（相邻期首尾相接、不重叠）：
  weekly  : 上周二 00:00:00 ~ 采集日(周二) 00:00:00   (GMT+8)
  monthly : 上月 1 日 00:00:00 ~ 本月 1 日 00:00:00   (GMT+8)

用法：
  python cf_stats.py --period weekly                # 采集日=今天
  python cf_stats.py --period weekly --date 2026-09-29   # 补采
  python cf_stats.py --period monthly               # 上月整月（需每月 1 日跑）
  FORCE_MONTHLY=1 python cf_stats.py --period monthly    # 任意日期强制出月报

环境变量：
  CF_API_TOKEN   Cloudflare API Token
  CF_ACCOUNT_ID  Cloudflare Account ID
"""
import argparse
import calendar
import csv
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
REST    = "https://api.cloudflare.com/client/v4"
CST     = timezone(timedelta(hours=8))
ROOT    = os.path.dirname(os.path.abspath(__file__))

# 账户显示名（用于文件名），改邮箱时只动这里
ACCOUNT_LABEL = "ruyisdk@outlook.com"

# (文件名简称, 站点域名/正则, 兜底 siteTag)
SITES = [
    ("ruyisdk.org",    "ruyisdk.org",                           "61eaa2a6ba3c48e1881cbbac7d7842b7"),
    ("support-matrix", "support-matrix-frontend-2cl.pages.dev", "a9527d2051924fa892bce3d748662c4e"),
    ("board-docs",     "board-docs-frontend.pages.dev",         "712cd266ae0946899ee783d6f8701fea"),
]

# CSV 列定义（与腾讯文档「web统计数据」A~I 列对齐）
CSV_HEADER = [
    "A_时间窗口", "B_账户请求数", "C_账户访问量",
    "D_ruyisdk_访问量", "E_ruyisdk_页面浏览量",
    "F_support_matrix_访问量", "G_support_matrix_页面浏览量",
    "H_board_docs_访问量", "I_board_docs_页面浏览量",
    "collect_date", "window_utc_start", "window_utc_end",
]
SUM_COLS = range(1, 9)  # B~I 参与求和


# ---------------------------------------------------------------- GraphQL ----

ACC_Q = """
query($t:String!,$s:DateTime!,$e:DateTime!){viewer{accounts(filter:{accountTag:$t}){
 httpRequestsAdaptiveGroups(limit:10000,
   filter:{datetime_geq:$s,datetime_lt:$e,requestSource:"eyeball"}){
   count sum{visits edgeResponseBytes}
   dimensions{countryName clientRequestHTTPHost}}}}}"""

RUM_Q = """
query($t:String!,$s:DateTime!,$e:DateTime!){viewer{accounts(filter:{accountTag:$t}){
 rumPageloadEventsAdaptiveGroups(limit:10000,
   filter:{datetime_geq:$s,datetime_lt:$e}){
   count sum{visits}
   dimensions{siteTag requestHost referrer countryName path deviceType}}}}}"""


def gql(token, query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL, data=body, method="POST",
        headers={"Authorization": "Bearer %s" % token,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data.get("errors"):
        sys.exit("GraphQL 报错: %s" % json.dumps(data["errors"], ensure_ascii=False))
    return data["data"]["viewer"]


def resolve_site_tags(token, account_id):
    """RUM 站点列表 -> 域名映射 siteTag；接口失败时用内置兜底 tag"""
    url = "%s/accounts/%s/rum/site_info/list" % (REST, account_id)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            sites = json.loads(r.read().decode("utf-8")).get("result") or []
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("警告: RUM 站点列表获取失败(%s)，使用内置 siteTag\n" % e)
        return {host: fb for _, host, fb in SITES}

    out = {}
    for _, host, fb in SITES:
        for s in sites:
            pat = s.get("host") or ""
            try:
                if re.search(pat, host):
                    out[host] = s.get("site_tag")
                    break
            except re.error:
                if pat == host:
                    out[host] = s.get("site_tag")
                    break
        out.setdefault(host, fb)
    return out


# ---------------------------------------------------------------- 窗口计算 ----

def week_window(day_str):
    """weekly: 上周二 00:00 ~ 采集日 00:00 (GMT+8)"""
    d = datetime.strptime(day_str, "%Y-%m-%d")
    end = datetime(d.year, d.month, d.day, tzinfo=CST)
    return _fmt(end - timedelta(days=7), end)


def month_window(today):
    """monthly: 上月 1 日 00:00 ~ 本月 1 日 00:00 (GMT+8)
    返回 ((stamp, s, e), (prev_year, prev_month), date_suffix)"""
    py, pm = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    stamp, s, e = _fmt(datetime(py, pm, 1, tzinfo=CST),
                       datetime(today.year, today.month, 1, tzinfo=CST))
    return (stamp, s, e), (py, pm), "%04d%02d01" % (today.year, today.month)


def _fmt(start, end):
    """返回 (A列标签 YYYYMMDDHHMMSS-YYYYMMDDHHMMSS, start_utc, end_utc)"""
    fl = "%Y-%m-%dT%H:%M:%SZ"
    fs = "%Y%m%d%H%M%S"
    return ("%s-%s" % (start.strftime(fs), end.strftime(fs)),
            start.astimezone(timezone.utc).strftime(fl),
            end.astimezone(timezone.utc).strftime(fl))


# ---------------------------------------------------------------- 聚合工具 ----

def group(rows, key_fn):
    """按维度聚合 -> {key: {pageloads, visits}}，按 visits 降序"""
    agg = {}
    for r in rows:
        k = key_fn(r)
        a = agg.setdefault(k, {"pageloads": 0, "visits": 0})
        a["pageloads"] += r["count"]
        a["visits"] += r["sum"]["visits"]
    return dict(sorted(agg.items(), key=lambda x: -x[1]["visits"]))


def dump_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print("  wrote", os.path.relpath(path, ROOT))


# ---------------------------------------------------------------- CSV 写入 ----

def upsert_csv(path, row):
    """追加/覆盖窗口行 + 刷新 TOTAL 求和行（幂等：同 A 列窗口只保留最新一行）"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.reader(f) if r]
        if rows and rows[0] == CSV_HEADER:
            rows = rows[1:]
        # 去掉可能存在的旧 TOTAL 行（结构升级时自愈）
        rows = [r for r in rows if r and r[0] != "TOTAL"]
    rows = [r for r in rows if r[0] != row[0]]
    rows.append(row)

    totals = ["TOTAL"] + [""] * (len(CSV_HEADER) - 1)
    for c in SUM_COLS:
        totals[c] = str(sum(int(r[c]) for r in rows
                            if len(r) > c and r[c].isdigit()))

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        w.writerow(totals)
        for r in rows:
            w.writerow(r)
    print("  wrote", os.path.relpath(path, ROOT), "(rows=%d)" % len(rows))


# ---------------------------------------------------------------- main ----

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["weekly", "monthly"], required=True)
    ap.add_argument("--date", help="采集日 YYYY-MM-DD（仅 weekly；默认今天）")
    args = ap.parse_args()

    token = os.environ.get("CF_API_TOKEN")
    tag = os.environ.get("CF_ACCOUNT_ID")
    if not token:
        sys.exit("缺少环境变量 CF_API_TOKEN")
    if not tag:
        sys.exit("缺少环境变量 CF_ACCOUNT_ID")

    today = datetime.now(CST)

    if args.period == "weekly":
        day = args.date or today.strftime("%Y-%m-%d")
        stamp, s, e = week_window(day)
        sub = day.replace("-", "")                      # 20260929
        suffix = sub                                    # 文件日期后缀 = 窗口终点日
        base = os.path.join(ROOT, "results-weekly")
        collect_date = sub
    else:
        # 每月 1 日执行（cron 保证）；手动调试用 FORCE_MONTHLY=1 放行
        last_day = calendar.monthrange(today.year, today.month)[1]
        if today.day != 1 and not os.environ.get("FORCE_MONTHLY"):
            print("非每月 1 日，跳过月报（如需强制执行请设 FORCE_MONTHLY=1）")
            return
        (stamp, s, e), (py, pm), suffix = month_window(today)
        sub = "%04d%02d" % (py, pm)                     # 202608
        base = os.path.join(ROOT, "results-monthly")
        collect_date = today.strftime("%Y%m%d")

    out_dir = os.path.join(base, sub)
    os.makedirs(out_dir, exist_ok=True)
    print("[%s] window=%s  ->  %s" % (args.period, stamp, os.path.relpath(out_dir, ROOT)))

    v = {"t": tag, "s": s, "e": e}

    # ---- 1) 账户分析原始数据 ----
    acc = gql(token, ACC_Q, v)["accounts"][0]["httpRequestsAdaptiveGroups"]
    acc_json = {
        "account": ACCOUNT_LABEL,
        "window_local": stamp,
        "window_utc": [s, e],
        "filter": 'requestSource="eyeball"',
        "total": {
            "requests": sum(r["count"] for r in acc),
            "visits": sum(r["sum"]["visits"] for r in acc),
            "edge_MB": round(sum(r["sum"]["edgeResponseBytes"] for r in acc) / 1e6, 2),
        },
        "by_country": group(acc, lambda r: r["dimensions"].get("countryName") or "(unknown)"),
        "by_host": group(acc, lambda r: r["dimensions"].get("clientRequestHTTPHost") or "(unknown)"),
    }
    dump_json(os.path.join(out_dir, "%s_%s.json" % (ACCOUNT_LABEL, suffix)), acc_json)

    # ---- 2) 三站点 RUM 原始数据 ----
    tags = resolve_site_tags(token, tag)
    rum = gql(token, RUM_Q, v)["accounts"][0]["rumPageloadEventsAdaptiveGroups"]
    per_site = {}
    for r in rum:
        per_site.setdefault(r["dimensions"]["siteTag"], []).append(r)

    csv_row = [stamp,
               acc_json["total"]["requests"], acc_json["total"]["visits"]]

    for short, host, _ in SITES:
        st = tags.get(host)
        rows = per_site.get(st, [])
        site_json = {
            "host": host,
            "siteTag": st,
            "window_local": stamp,
            "window_utc": [s, e],
            "total": {
                "visits": sum(r["sum"]["visits"] for r in rows),
                "pageloads": sum(r["count"] for r in rows),
            },
            "by_referrer": group(rows, lambda r: r["dimensions"].get("referrer") or "(direct)"),
            "by_host": group(rows, lambda r: r["dimensions"].get("requestHost") or "(unknown)"),
            "by_country": group(rows, lambda r: r["dimensions"].get("countryName") or "(unknown)"),
            "by_path": group(rows, lambda r: r["dimensions"].get("path") or "(unknown)"),
            "by_deviceType": group(rows, lambda r: r["dimensions"].get("deviceType") or "(unknown)"),
        }
        dump_json(os.path.join(out_dir, "%s_%s.json" % (short, suffix)), site_json)
        csv_row += [site_json["total"]["visits"], site_json["total"]["pageloads"]]

    # ---- 3) CSV 累计表 ----
    csv_row += [collect_date, s, e]
    csv_name = "cf_weekly.csv" if args.period == "weekly" else "cf_monthly.csv"
    upsert_csv(os.path.join(base, csv_name), csv_row)

    # ---- 4) _meta.json（供 cf_report.mjs 使用）----
    meta = {
        "period": args.period,
        "window_local": stamp,
        "window_utc": [s, e],
        "date_suffix": suffix,
        "sites": {short: {"host": host, "siteTag": tags.get(host)}
                  for short, host, _ in SITES},
    }
    dump_json(os.path.join(out_dir, "_meta.json"), meta)

    print("done.")


if __name__ == "__main__":
    main()
