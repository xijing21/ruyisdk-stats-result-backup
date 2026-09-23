#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RuyiSDK Cloudflare 统计归档
=================================================
核心语义：
  输入窗口 = 需求口径 (requested window)。
  每个数据源在输入窗口内各取其能拿到的最大范围：
    - 账户分析保留期约 32 天 -> 起点早于配额线时自动夹取到配额线
    - Web Analytics 保留期更长(界面观测最早约 2026-03-28) -> 通常全窗可取
  任一数据源窗口完全超出保留期时按 0 值降级，不中断采集。
  JSON/CSV 同时记录 requested window 与各源实际数据窗口，口径不混淆。
  CSV 行标识 = requested window (稳定、幂等、期期相接)。

用法：
  python cf_stats.py --period weekly                # 缺省: 最近周二往前7天
  START_DATE=20260915 END_DATE=20260922 ... --period weekly
  START_TS=2026-08-22T17:00:00Z END_DATE=20260825 ...  # 精确起点(边缘补采)
  python cf_stats.py --period monthly               # 缺省: 上月整月
  START_DATE=20260801 END_DATE=20260901 ... --period monthly   # 补任意月
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
REST    = "https://api.cloudflare.com/client/v4"
CST     = timezone(timedelta(hours=8))
ROOT    = os.path.dirname(os.path.abspath(__file__))

ACCOUNT_LABEL = "ruyisdk@outlook.com"

SITES = [
    ("ruyisdk.org",    "ruyisdk.org",                           "61eaa2a6ba3c48e1881cbbac7d7842b7"),
    ("support-matrix", "support-matrix-frontend-2cl.pages.dev", "a9527d2051924fa892bce3d748662c4e"),
    ("board-docs",     "board-docs-frontend.pages.dev",         "712cd266ae0946899ee783d6f8701fea"),
]

CSV_HEADER = [
    "A_时间窗口", "B_账户请求数", "C_账户访问量",
    "D_ruyisdk_访问量", "E_ruyisdk_页面浏览量",
    "F_support_matrix_访问量", "G_support_matrix_页面浏览量",
    "H_board_docs_访问量", "I_board_docs_页面浏览量",
    "collect_date", "window_utc_start", "window_utc_end",
    "account_window_utc", "rum_window_utc",
]
SUM_COLS = range(1, 9)

# 各数据源保留期(天)。账户=官方口径 4w4d=32;
# RUM=界面观测值(最早约2026-03-28, 约180天), 仅供预夹取,
# 实际值以 quota 报错动态解析为准, 不一致时自动修正。
RETENTION_DAYS = {
    "account": 32,
    "rum":     180,
}
CLAMP_MARGIN = timedelta(minutes=10)   # 起点余量, 避免压线触发报错


# ---------------------------------------------------------------- GraphQL ----

ACC_Q = """
query($t:String!,$s:DateTime!,$e:DateTime!){viewer{accounts(filter:{accountTag:$t}){
 httpRequestsAdaptiveGroups(limit:10000,
   filter:{datetime_geq:$s,datetime_lt:$e,requestSource:"eyeball"}){
   count sum{visits edgeResponseBytes}
   dimensions{clientCountryName clientRequestHTTPHost}}}}}"""

RUM_Q = """
query($t:String!,$s:DateTime!,$e:DateTime!){viewer{accounts(filter:{accountTag:$t}){
 rumPageloadEventsAdaptiveGroups(limit:10000,
   filter:{datetime_geq:$s,datetime_lt:$e}){
   count sum{visits}
   dimensions{siteTag requestHost refererHost countryName requestPath deviceType}}}}}"""


class GqlError(RuntimeError):
    pass


def _gql_request(token, query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL, data=body, method="POST",
        headers={"Authorization": "Bearer %s" % token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        raise GqlError("GraphQL HTTP %s: %s"
                       % (ex.code, ex.read().decode(errors="replace")[:2000]))
    if data.get("errors"):
        raise GqlError("GraphQL 报错: %s\n[出错查询片段] %s"
                       % (json.dumps(data["errors"], ensure_ascii=False),
                          query.strip()[:300]))
    viewer = data.get("data", {}).get("viewer")
    if not viewer:
        raise GqlError("GraphQL 响应缺少 data.viewer: %s"
                       % json.dumps(data, ensure_ascii=False)[:2000])
    return viewer


def gql_safe(token, query, variables):
    try:
        return _gql_request(token, query, variables), None
    except GqlError as ex:
        return None, str(ex)


def parse_retention_days(err_msg):
    """从 quota 报错解析保留期天数: '...older than 4w4d...' -> 32; 失败返回 None"""
    m = re.search(r"cannot request data older than\s+(\d+)w\s*(\d+)?d", err_msg)
    return int(m.group(1)) * 7 + int(m.group(2) or 0) if m else None


def clamp_window(ws, we, retention_days):
    """把窗口夹到该数据源可查范围内 (起点受保留期约束, 终点不越过当前时刻)。
    返回 (s_dt, e_dt, start_clamped, degraded)。
    degraded=True 表示窗口完全出界, 该源应按 0 值降级。"""
    now = datetime.now(timezone.utc)
    earliest = now - timedelta(days=retention_days) + CLAMP_MARGIN
    s = max(ws, earliest)
    e = min(we, now)                      # 未来时段无数据, 夹到当前时刻
    return s, e, ws < earliest, s >= e


def fetch_source(token, tag, query, ws, we, source, label):
    """通用抓取: 预夹取 -> 查询 -> quota 时按报错实际保留期修正重试 -> 完全出界降级。
    返回 (rows|None, stamp, s_utc, e_utc, clamped, degraded)。"""
    retention = RETENTION_DAYS[source]
    s_dt, e_dt, clamped, degraded = clamp_window(ws, we, retention)
    if degraded:
        sys.stderr.write("警告: %s 窗口 [%s ~ %s] 完全超出保留期(约%d天), 按 0 值降级\n"
                         % (label, ws.strftime("%Y%m%d%H%M%S"),
                            we.strftime("%Y%m%d%H%M%S"), retention))
        return None, None, None, None, True, True
    if clamped:
        print("::警告:: %s 保留期约%d天, 窗口起点已夹取: %s -> %s"
              % (label, retention, ws.strftime("%Y%m%d%H%M%S"),
                 s_dt.strftime("%Y%m%d%H%M%S")))

    for attempt in (1, 2):
        stamp, s, e = _fmt(s_dt, e_dt)
        viewer, err = gql_safe(token, query, {"t": tag, "s": s, "e": e})
        if viewer is not None:
            return viewer["accounts"][0][
                "httpRequestsAdaptiveGroups" if source == "account"
                else "rumPageloadEventsAdaptiveGroups"], stamp, s, e, clamped, False
        # 保留期口径与预估值不符 -> 用报错里的实际值重夹取再试一次
        if attempt == 1 and "quota" in err:
            days = parse_retention_days(err)
            if days and days != retention:
                sys.stderr.write("警告: %s 实际保留期为%d天(预估%d天), 按实际值重试\n"
                                 % (label, days, retention))
                RETENTION_DAYS[source] = days
                s_dt, e_dt, clamped, degraded = clamp_window(ws, we, days)
                if degraded:
                    sys.stderr.write("警告: %s 窗口完全超出实际保留期, 按 0 值降级\n" % label)
                    return None, None, None, None, True, True
                continue
        sys.exit(err)   # 非配额错误(token/网络/权限): 真错误, 硬失败


def resolve_site_tags(token, account_id):
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


# ---------------------------------------------------------------- 工具 ----

def _fmt(start, end):
    fl = "%Y-%m-%dT%H:%M:%SZ"
    fs = "%Y%m%d%H%M%S"
    return ("%s-%s" % (start.strftime(fs), end.strftime(fs)),
            start.astimezone(timezone.utc).strftime(fl),
            end.astimezone(timezone.utc).strftime(fl))


def group(rows, key_fn):
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


def upsert_csv(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.reader(f) if r]
        if rows and rows[0] == CSV_HEADER:
            rows = rows[1:]
        rows = [r for r in rows if r and r[0] != "TOTAL"]
    rows = [r for r in rows if r[0] != row[0]]
    rows.append(row)

    totals = ["TOTAL"] + [""] * (len(CSV_HEADER) - 1)
    def _to_int(v):
        try:
            return int(str(v).strip().replace(",", ""))
        except ValueError:
            sys.stderr.write("警告: CSV 出现无法解析的值 %r, 已按 0 计入求和\n" % v)
            return 0
    for c in SUM_COLS:
        totals[c] = str(sum(_to_int(r[c]) for r in rows if len(r) > c))

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
    args = ap.parse_args()

    token = os.environ.get("CF_API_TOKEN")
    token = token.replace("\ufeff", "").strip() if token else token
    tag = os.environ.get("CF_ACCOUNT_ID")
    if not token:
        sys.exit("缺少环境变量 CF_API_TOKEN")
    if not tag:
        sys.exit("缺少环境变量 CF_ACCOUNT_ID")

    start_env = os.environ.get("START_DATE", "").strip()
    end_env   = os.environ.get("END_DATE", "").strip()
    start_ts  = os.environ.get("START_TS", "").strip()
    today = datetime.now(CST)

    def parse_start(fallback):
        if start_ts:
            return datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
        if start_env:
            return datetime.strptime(start_env, "%Y%m%d").replace(tzinfo=CST)
        return fallback

    if args.period == "weekly":
        if end_env:
            we = datetime.strptime(end_env, "%Y%m%d").replace(tzinfo=CST)
        else:
            offset = (today.weekday() - 1) % 7
            we = (today - timedelta(days=offset)).replace(
                hour=0, minute=0, second=0, microsecond=0)
        ws = parse_start(we - timedelta(days=7))
        sub = we.strftime("%Y%m%d")
        suffix = sub
        base = os.path.join(ROOT, "results-weekly")
    else:
        we = datetime.strptime(end_env, "%Y%m%d").replace(tzinfo=CST) if end_env \
             else datetime(today.year, today.month, 1, tzinfo=CST)
        ws = parse_start(datetime((we - timedelta(days=1)).year,
                                  (we - timedelta(days=1)).month, 1, tzinfo=CST))
        sub = ws.strftime("%Y%m")
        suffix = we.strftime("%Y%m%d")
        base = os.path.join(ROOT, "results-monthly")

    if ws >= we:
        sys.exit("窗口无效: 起点(%s)不早于终点(%s)"
                 % (ws.strftime("%Y%m%d%H%M%S"), we.strftime("%Y%m%d%H%M%S")))

    stamp, s_req, e_req = _fmt(ws, we)          # requested window (行标识)
    collect_date = datetime.now(CST).strftime("%Y%m%d")
    out_dir = os.path.join(base, sub)
    os.makedirs(out_dir, exist_ok=True)
    print("[%s] requested window=%s  ->  %s"
          % (args.period, stamp, os.path.relpath(out_dir, ROOT)))

    # ---- 1) 账户分析: 窗口内取最大可得范围, 出界自动降级 ----
    acc, stamp_a, s_a, e_a, acc_clamped, acc_degraded = \
        fetch_source(token, tag, ACC_Q, ws, we, "account", "账户分析")
    acc_total = {
        "requests": sum(r["count"] for r in acc) if acc else 0,
        "visits":   sum(r["sum"]["visits"] for r in acc) if acc else 0,
        "edge_MB":  round(sum(r["sum"]["edgeResponseBytes"] for r in acc) / 1e6, 2)
                    if acc else 0,
    }
    acc_json = {
        "account": ACCOUNT_LABEL,
        "requested_window_local": stamp,
        "window_local": stamp_a,
        "window_utc": [s_a, e_a],
        "window_clamped": acc_clamped,
        "window_degraded": acc_degraded,
        "filter": 'requestSource="eyeball"',
        "total": acc_total,
        "by_country": group(acc, lambda r: r["dimensions"].get("clientCountryName") or "(unknown)")
                      if acc else {},
        "by_host": group(acc, lambda r: r["dimensions"].get("clientRequestHTTPHost") or "(unknown)")
                   if acc else {},
    }
    dump_json(os.path.join(out_dir, "%s_%s.json" % (ACCOUNT_LABEL, suffix)), acc_json)

    # ---- 2) 三站点 RUM: 同样在窗口内取最大可得范围 ----
    tags = resolve_site_tags(token, tag)
    rum, stamp_r, s_r, e_r, rum_clamped, rum_degraded = \
        fetch_source(token, tag, RUM_Q, ws, we, "rum", "Web Analytics")
    per_site = {}
    if rum:
        for r in rum:
            per_site.setdefault(r["dimensions"]["siteTag"], []).append(r)

    csv_row = [stamp, acc_total["requests"], acc_total["visits"]]
    for short, host, _ in SITES:
        st = tags.get(host)
        rows = per_site.get(st, []) if rum else []
        site_json = {
            "host": host,
            "siteTag": st,
            "requested_window_local": stamp,
            "window_local": stamp_r,
            "window_utc": [s_r, e_r],
            "window_clamped": rum_clamped,
            "window_degraded": rum_degraded,
            "total": {
                "visits": sum(r["sum"]["visits"] for r in rows),
                "pageloads": sum(r["count"] for r in rows),
            },
            "by_referrer": group(rows, lambda r: r["dimensions"].get("refererHost") or "(direct)"),
            "by_host": group(rows, lambda r: r["dimensions"].get("requestHost") or "(unknown)"),
            "by_country": group(rows, lambda r: r["dimensions"].get("countryName") or "(unknown)"),
            "by_path": group(rows, lambda r: r["dimensions"].get("requestPath") or "(unknown)"),
            "by_deviceType": group(rows, lambda r: r["dimensions"].get("deviceType") or "(unknown)"),
        }
        dump_json(os.path.join(out_dir, "%s_%s.json" % (short, suffix)), site_json)
        csv_row += [site_json["total"]["visits"], site_json["total"]["pageloads"]]

    # ---- 3) CSV: 行标识=requested window; 末两列记录各源实际数据窗口 ----
    acc_cell = ("%s/%s" % (s_a, e_a)) if s_a else "(超出保留期,按0计)"
    rum_cell = ("%s/%s" % (s_r, e_r)) if s_r else "(超出保留期,按0计)"
    csv_row += [collect_date, s_req, e_req, acc_cell, rum_cell]
    csv_name = "cf_weekly.csv" if args.period == "weekly" else "cf_monthly.csv"
    upsert_csv(os.path.join(base, csv_name), csv_row)

    # ---- 4) _meta.json ----
    meta = {
        "period": args.period,
        "requested_window_local": stamp,
        "window_utc": [s_req, e_req],
        "account_window_utc": [s_a, e_a],
        "rum_window_utc": [s_r, e_r],
        "account_window_clamped": acc_clamped,
        "rum_window_clamped": rum_clamped,
        "date_suffix": suffix,
        "sites": {short: {"host": host, "siteTag": tags.get(host)}
                  for short, host, _ in SITES},
    }
    dump_json(os.path.join(out_dir, "_meta.json"), meta)
    print("done.")


if __name__ == "__main__":
    main()
