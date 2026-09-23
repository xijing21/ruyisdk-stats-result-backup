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

数据源保留期差异（重要）：
  账户分析 (httpRequestsAdaptiveGroups) : 约 4w4d = 32 天（滚动）
  Web Analytics (rumPageloadEventsAdaptiveGroups) : 更长（界面上可回溯数月）
  当请求起点早于某数据源的保留期时，该数据源的起点被"夹取"(clamp)到
  其可查最早时刻，另一个数据源不受影响、照常全窗口执行。
  JSON / CSV 中的窗口字段一律写入**实际数据窗口**（夹取后），
  同时保留 requested_window_* 记录用户请求的原始窗口，供下游识别残窗。
  某数据源窗口被完全挤出保留期时，该数据源按 0 值降级，不中断整体采集。

用法：
  python cf_stats.py --period weekly                     # CI: 由 workflow 传窗口
  START_DATE=20260915 END_DATE=20260922 python cf_stats.py --period weekly
  START_TS=2026-08-22T17:00:00Z END_DATE=20260825 python cf_stats.py --period weekly
  python cf_stats.py --period weekly                     # 本地裸跑: 最近周二口径
  python cf_stats.py --period monthly                    # 上月整月

环境变量：
  CF_API_TOKEN   Cloudflare API Token
  CF_ACCOUNT_ID  Cloudflare Account ID
  START_DATE     窗口起点 YYYYMMDD (北京时间, 可选)
  END_DATE       窗口终点 YYYYMMDD (北京时间, weekly 必需; monthly 缺省=本月1日)
  START_TS       窗口起点精确时间戳 ISO8601/UTC (可选, 优先级高于 START_DATE,
                 用于边缘补采: 配额线带时分秒, 日期粒度表达不了)
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

# 账户分析保留期: 官方报错口径 4w4d=32天, 预夹取时留 10 分钟余量
ACC_RETENTION_DAYS = 32
CLAMP_MARGIN = timedelta(minutes=10)


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
    """GraphQL 调用失败（HTTP 错误 / API errors / 响应结构缺失）"""


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
        # 4xx/5xx 时把响应体打出来 (之前这种情况只会裸抛, 看不到 Cloudflare 的说明)
        detail = ex.read().decode(errors="replace")
        raise GqlError("GraphQL HTTP %s: %s" % (ex.code, detail[:2000]))

    if data.get("errors"):
        raise GqlError("GraphQL 报错: %s\n[出错查询片段] %s"
                       % (json.dumps(data["errors"], ensure_ascii=False),
                          query.strip()[:300]))

    viewer = data.get("data", {}).get("viewer")
    if not viewer:
        raise GqlError("GraphQL 响应缺少 data.viewer: %s"
                       % json.dumps(data, ensure_ascii=False)[:2000])
    return viewer


def gql(token, query, variables):
    """失败即退出（保持原行为, 供必须成功的调用用）"""
    try:
        return _gql_request(token, query, variables)
    except GqlError as ex:
        sys.exit(str(ex))


def gql_safe(token, query, variables):
    """返回 (viewer, error)；viewer=None 时 error 为失败原因，允许调用方降级"""
    try:
        return _gql_request(token, query, variables), None
    except GqlError as ex:
        return None, str(ex)


def parse_retention_days(err_msg):
    """从 quota 报错消息解析保留期天数。
    例: 'cannot request data older than 4w4d, but ...' -> 32
    解析失败返回 None。"""
    m = re.search(r"cannot request data older than\s+(\d+)w\s*(\d+)?d", err_msg)
    if not m:
        return None
    return int(m.group(1)) * 7 + int(m.group(2) or 0)


def clamp_start(ws, retention_days):
    """把窗口起点夹取到保留期内；返回 (新起点, 是否被夹取)。
    若整个窗口都在保留期之外 (新起点 >= 终点由调用方判断)，返回的起点会越过 ws。"""
    earliest = datetime.now(timezone.utc) \
        - timedelta(days=retention_days) + CLAMP_MARGIN
    return (max(ws, earliest), ws < earliest)


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
    def _to_int(v):
        s = str(v).strip().replace(",", "")     # 容忍 "1,110" 和首尾空格
        try:
            return int(s)
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


# ---------------------------------------------------------------- 数据源抓取 ----

def fetch_account(token, tag, ws, we):
    """账户分析。起点超保留期时夹取；保留期口径变化时按报错动态再夹取。
    返回 (rows_or_None, 实际stamp, 实际s, 实际e, clamped)"""
    acc_ws, clamped = clamp_start(ws, ACC_RETENTION_DAYS)
    if acc_ws >= we:
        sys.stderr.write("警告: 账户分析窗口 [%s ~ %s] 已完全超出保留期(%d天), "
                         "账户数据按 0 值降级\n"
                         % (ws.strftime("%Y%m%d%H%M%S"), we.strftime("%Y%m%d%H%M%S"),
                            ACC_RETENTION_DAYS))
        return None, None, None, None, True

    if clamped:
        print("::警告:: 账户分析保留期约%d天, 窗口起点已从 %s 夹取到 %s"
              % (ACC_RETENTION_DAYS,
                 ws.strftime("%Y%m%d%H%M%S"), acc_ws.strftime("%Y%m%d%H%M%S")))

    for attempt in (1, 2):
        stamp, s, e = _fmt(acc_ws, we)
        viewer, err = gql_safe(token, ACC_Q, {"t": tag, "s": s, "e": e})
        if viewer is not None:
            return (viewer["accounts"][0]["httpRequestsAdaptiveGroups"],
                    stamp, s, e, clamped)
        if attempt == 1 and "quota" in err:
            days = parse_retention_days(err)      # 官方口径变了 -> 按报错实际值再夹
            if days:
                acc_ws, clamped = clamp_start(ws, days)
                sys.stderr.write("警告: 按报错动态夹取账户窗口起点到 %s (保留期=%d天)\n"
                                 % (acc_ws.strftime("%Y%m%d%H%M%S"), days))
                continue
        sys.exit(err)                             # 非配额错误 / 二次失败: 硬失败


def fetch_rum(token, tag, ws, we):
    """三站点 RUM。保留期更长, 先按完整请求窗口试;
    遇 quota 报错时从报错消息解析保留期再夹取重试一次。
    返回 (rows_or_None, 实际stamp, 实际s, 实际e, clamped)"""
    rum_ws = ws
    clamped = False
    for attempt in (1, 2):
        stamp, s, e = _fmt(rum_ws, we)
        viewer, err = gql_safe(token, RUM_Q, {"t": tag, "s": s, "e": e})
        if viewer is not None:
            return (viewer["accounts"][0]["rumPageloadEventsAdaptiveGroups"],
                    stamp, s, e, clamped)
        if attempt == 1 and "quota" in err:
            days = parse_retention_days(err)
            if days:
                rum_ws, clamped = clamp_start(ws, days)
                if rum_ws < we:
                    print("::警告:: Web Analytics 保留期约%d天, RUM 窗口起点已夹取到 %s"
                          % (days, rum_ws.strftime("%Y%m%d%H%M%S")))
                    continue
        sys.exit(err)


# ---------------------------------------------------------------- main ----

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["weekly", "monthly"], required=True)
    args = ap.parse_args()

    token = os.environ.get("CF_API_TOKEN")
    # 去除 BOM 和首尾不可见字符
    token = token.replace("\ufeff", "").strip() if token else token

    tag = os.environ.get("CF_ACCOUNT_ID")
    if not token:
        sys.exit("缺少环境变量 CF_API_TOKEN")
    if not tag:
        sys.exit("缺少环境变量 CF_ACCOUNT_ID")

    # ---- 窗口解析: workflow 通过环境变量传入 (YYYYMMDD, 北京时间) ----
    start_env = os.environ.get("START_DATE", "").strip()
    end_env   = os.environ.get("END_DATE", "").strip()
    start_ts  = os.environ.get("START_TS", "").strip()   # ISO 时间戳, 边缘补采用, 优先级最高
    today = datetime.now(CST)

    def parse_start(fallback):
        """起点: START_TS(精确) > START_DATE(日粒度) > fallback(本地兜底)"""
        if start_ts:
            return datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
        if start_env:
            return datetime.strptime(start_env, "%Y%m%d").replace(tzinfo=CST)
        return fallback

    if args.period == "weekly":
        if end_env:
            we = datetime.strptime(end_env, "%Y%m%d").replace(tzinfo=CST)
        else:
            # 本地裸跑兜底: 最近一个周二 (与 CI 口径一致)
            offset = (today.weekday() - 1) % 7          # 周二=1
            we = (today - timedelta(days=offset)).replace(
                hour=0, minute=0, second=0, microsecond=0)
        ws = parse_start(we - timedelta(days=7))
        stamp, s, e = _fmt(ws, we)
        sub = we.strftime("%Y%m%d")                     # 目录 = 窗口结束日 20260922
        suffix = sub
        base = os.path.join(ROOT, "results-weekly")
    else:
        we = datetime.strptime(end_env, "%Y%m%d").replace(tzinfo=CST) if end_env \
             else datetime(today.year, today.month, 1, tzinfo=CST)   # 缺省=本月1日
        if start_ts:
            ws = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
        elif start_env:
            ws = datetime.strptime(start_env, "%Y%m%d").replace(tzinfo=CST)
        else:
            # 本地裸跑兜底: 上月 1 日
            py, pm = (we.year - 1, 12) if we.month == 1 else (we.year, we.month - 1)
            ws = datetime(py, pm, 1, tzinfo=CST)
        stamp, s, e = _fmt(ws, we)
        sub = ws.strftime("%Y%m")                       # 目录 = 数据所属月 (上月) 202608
        suffix = we.strftime("%Y%m%d")                  # 文件后缀 = 窗口结束日 20260901
        base = os.path.join(ROOT, "results-monthly")

    collect_date = datetime.now(CST).strftime("%Y%m%d")  # 真实采集运行日, 与期数标识分离
    out_dir = os.path.join(base, sub)
    os.makedirs(out_dir, exist_ok=True)
    print("[%s] requested window=%s  ->  %s"
          % (args.period, stamp, os.path.relpath(out_dir, ROOT)))

    # ================= 1) 账户分析 (保留期短, 可能被夹取/降级) =================
    acc, stamp_a, s_a, e_a, acc_clamped = fetch_account(token, tag, ws, we)
    if acc is not None:                                  # acc_clamped 可能仍为 True(完全越界)
        acc_total = {
            "requests": sum(r["count"] for r in acc),
            "visits": sum(r["sum"]["visits"] for r in acc),
            "edge_MB": round(sum(r["sum"]["edgeResponseBytes"] for r in acc) / 1e6, 2),
        }
        acc_window = {"window_local": stamp_a, "window_utc": [s_a, e_a]}
    else:
        acc_total = {"requests": 0, "visits": 0, "edge_MB": 0}
        acc_window = {"window_local": None, "window_utc": [None, None]}
        stamp_a, s_a, e_a = None, None, None             # 账户窗口不可用

    acc_json = {
        "account": ACCOUNT_LABEL,
        "requested_window_local": stamp,                 # 用户请求的原始窗口
        **acc_window,                                    # ★ 实际数据窗口(夹取后)
        "window_clamped": acc_clamped,                   # 下游识别残窗
        "filter": 'requestSource="eyeball"',
        "total": acc_total,
        "by_country": group(acc, lambda r: r["dimensions"].get("clientCountryName") or "(unknown)")
                      if acc is not None else {},
        "by_host": group(acc, lambda r: r["dimensions"].get("clientRequestHTTPHost") or "(unknown)")
                   if acc is not None else {},
    }
    dump_json(os.path.join(out_dir, "%s_%s.json" % (ACCOUNT_LABEL, suffix)), acc_json)

    # ================= 2) 三站点 RUM (保留期长, 独立夹取) =================
    tags = resolve_site_tags(token, tag)
    rum, stamp_r, s_r, e_r, rum_clamped = fetch_rum(token, tag, ws, we)

    per_site = {}
    if rum is not None:
        for r in rum:
            per_site.setdefault(r["dimensions"]["siteTag"], []).append(r)

    # CSV A 列 / window_utc_* 优先用账户实际窗口 (较窄的一半作行口径最保守);
    # 账户完全不可用时退回 RUM 实际窗口
    row_stamp = stamp_a or stamp_r
    row_s     = s_a or s_r
    row_e     = e_a or e_r

    csv_row = [row_stamp, acc_total["requests"], acc_total["visits"]]

    for short, host, _ in SITES:
        st = tags.get(host)
        rows = per_site.get(st, []) if rum is not None else []
        site_json = {
            "host": host,
            "siteTag": st,
            "requested_window_local": stamp,
            "window_local": stamp_r,                     # ★ RUM 实际数据窗口
            "window_utc": [s_r, e_r],
            "window_clamped": rum_clamped,
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

    # ================= 3) CSV 累计表 =================
    csv_row += [collect_date, row_s, row_e]
    csv_name = "cf_weekly.csv" if args.period == "weekly" else "cf_monthly.csv"
    upsert_csv(os.path.join(base, csv_name), csv_row)

    # ================= 4) _meta.json（供 cf_report.mjs 使用）=================
    meta = {
        "period": args.period,
        "requested_window_local": stamp,
        "window_local": row_stamp,                       # 实际数据窗口(账户优先)
        "window_utc": [row_s, row_e],
        "account_window_clamped": acc_clamped,           # 账户数据源是否残窗/降级
        "rum_window_clamped": rum_clamped,               # RUM 数据源是否残窗
        "date_suffix": suffix,
        "sites": {short: {"host": host, "siteTag": tags.get(host)}
                  for short, host, _ in SITES},
    }
    dump_json(os.path.join(out_dir, "_meta.json"), meta)

    print("done.")


if __name__ == "__main__":
    main()
