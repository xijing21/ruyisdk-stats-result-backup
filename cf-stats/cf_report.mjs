// cf-stats/cf_report.mjs
// ============================================================
// 职责：
//   1. 账户分析页整页截图 PNG（该页无打印报告按钮，用截图覆盖）
//   2. 三站点 Web Analytics 整页截图 PNG
//   3. 三站点 Web Analytics 「打印报告」式 PDF（Chromium page.pdf）
// 时间窗口：
//   全部产物（JSON/CSV/截图/PDF）使用同一统计窗口，来源 _meta.json：
//     weekly  : 上周二 00:00 ~ 采集日 00:00   (GMT+8)
//     monthly : 上月 1 日 00:00 ~ 本月 1 日 00:00 (GMT+8)
//   不再使用 range=P7D 这类相对窗口（相对窗口随打开时刻漂移，
//   会导致截图与数据窗口错位）。
// 前置：cf_stats.py 已运行（读取其产出的 _meta.json）
// 环境变量：
//   CF_DASH_COOKIE  登录 dash.cloudflare.com 后的 Cookie 串
//   CF_ACCOUNT_ID   Cloudflare Account ID
//   PERIOD          weekly | monthly
// ============================================================
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const ACCOUNT_LABEL = 'ruyisdk@outlook.com';
const PERIOD = process.env.PERIOD || 'weekly';
const TZ_PARAM = 'Asia%2FShanghai';

// ---- 定位最新一期统计目录（cf_stats.py 刚生成的）----
const base = path.join(ROOT, PERIOD === 'monthly' ? 'results-monthly' : 'results-weekly');
if (!fs.existsSync(base)) { console.warn('目录不存在:', base); process.exit(1); }
const sub = fs.readdirSync(base)
  .filter(d => /^\d{6,8}$/.test(d))
  .sort()
  .pop();
if (!sub) { console.warn('未找到统计目录，请先运行 cf_stats.py'); process.exit(1); }
const outDir = path.join(base, sub);
const meta = JSON.parse(fs.readFileSync(path.join(outDir, '_meta.json'), 'utf-8'));
const suffix = meta.date_suffix;
console.log(`[report] period=${PERIOD} dir=${sub} suffix=${suffix}`);

// ---- 从 _meta.json 解析绝对时间窗口 ----
// meta.window_local 形如 "20260915000000-20260922000000"（GMT+8）
// 拆出起止时刻，转成仪表盘 URL 用的 ISO 本地时间串。
function parseWindow(stamp) {
  const m = /^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})-(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$/
    .exec(stamp);
  if (!m) throw new Error('无法解析 window_local: ' + stamp);
  const p = (y, mo, d, h, mi, s) =>
    `${y}-${mo}-${d}T${h}:${mi}:${s}`;
  const start = p(m[1], m[2], m[3], m[4], m[5], m[6]);   // GMT+8 起始
  const end   = p(m[7], m[8], m[9], m[10], m[11], m[12]); // GMT+8 结束
  return { start, end };
}

let WIN;
try {
  WIN = parseWindow(meta.window_local);
} catch (e) {
  console.warn('::warning::时间窗口解析失败，截图/PDF 将退回默认视图:', e.message);
  WIN = null;
}

// !! 校准说明（一次性人工核对）：
// 首次运行后打开 CI 日志里打印的 URL，确认仪表盘右上角日期显示为
// 「2026-09-15 ~ 2026-09-22」这类与窗口一致的绝对范围。
// 若仪表盘不接受此格式（日期仍显示“过去 7 天”），在浏览器里手动选一次
// 自定义范围，把地址栏中 range= 的实际格式抄到下面 RANGE_FMT 里再跑。
// 可选格式示例（按仪表盘实际表现三选一）：
//   A: `${s}~${e}`                          (ISO 本地时间, 当前默认)
//   B: `${s}Z~${e}Z`                        (UTC)
//   C: `${s}/${e}`                          (斜杠分隔)
const RANGE_FMT = (s, e) => `${s}~${e}`;

function buildUrl(pathname) {
  if (!WIN) return `${BASE_URL}${pathname}`;
  // + 号在 query 里会被解析成空格，必须用 URLSearchParams 编码
  const qs = new URLSearchParams({
    range: RANGE_FMT(WIN.start, WIN.end),
    tz: 'Asia/Shanghai',
  });
  return `${BASE_URL}${pathname}?${qs.toString()}`;
}

// ---- Cookie 解析 ----
const cookies = (process.env.CF_DASH_COOKIE || '')
  .split(';').map(s => s.trim()).filter(Boolean)
  .map(kv => {
    const i = kv.indexOf('=');
    return { name: kv.slice(0, i), value: kv.slice(i + 1), domain: '.cloudflare.com', path: '/' };
  });
if (!cookies.length) {
  console.warn('CF_DASH_COOKIE 未配置，跳过截图/PDF 导出');
  process.exit(0);
}

const BASE_URL = `https://dash.cloudflare.com/${process.env.CF_ACCOUNT_ID}`;

// 登录态检测：跳到 /login 说明 Cookie 失效
async function isLoggedIn(page) {
  return !page.url().includes('/login');
}

// ---- 通用页面装载：替代 networkidle ----
// Cloudflare 仪表盘是持续发遥测请求的 SPA，networkidle 永远等不到，
// 120s 必超时。改为: DOM 就绪 -> 关键图表元素出现 -> 滚动触发懒加载 -> 静置。
async function loadDashboard(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 120000 });
  console.log('  nav ->', page.url());   // 打印最终 URL, 便于核对 range 参数是否被保留
  if (!(await isLoggedIn(page))) return false;
  // 图表容器/画布/表格任一出现即认为内容已渲染
  await page.waitForSelector("canvas, [class*='chart'], [class*='Chart'], table",
                             { timeout: 60000 }).catch(() => {
    console.warn('  等待图表元素超时（页面可能仍在加载）');
  });
  await page.waitForTimeout(5000);
  // 逐段滚动触发懒加载图表
  await page.evaluate(async () => {
    await new Promise(res => {
      let y = 0;
      const t = setInterval(() => {
        window.scrollBy(0, 800);
        y += 800;
        if (y >= document.body.scrollHeight) { clearInterval(t); res(); }
      }, 300);
    });
    window.scrollTo(0, 0);
  });
  await page.waitForTimeout(3000);
  return true;
}

const browser = await chromium.launch({
  headless: true,
  args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
});
const ctx = await browser.newContext({
  userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36',
  viewport: { width: 1680, height: 1050 },
  locale: 'zh-CN',
  timezoneId: 'Asia/Shanghai',
});
await ctx.addCookies(cookies);

const failures = [];

// ---- 1) 账户分析整页截图（绝对窗口）----
try {
  const page = await ctx.newPage();
  const url = buildUrl('/analytics');
  console.log('[account]', WIN ? `window ${WIN.start} ~ ${WIN.end}` : '(无窗口, 默认视图)');
  if (await loadDashboard(page, url)) {
    const png = path.join(outDir, `${ACCOUNT_LABEL}_${suffix}.png`);
    await page.screenshot({ path: png, fullPage: true });
    console.log('account screenshot ok ->', path.basename(png));
  } else {
    failures.push('account: 未登录（Cookie 过期?）');
    console.warn('账户分析: 未登录（Cookie 过期?），跳过截图');
  }
  await page.close();
} catch (e) {
  failures.push(`account: ${e.message}`);
  console.warn('account screenshot failed:', e.message);
}

// ---- 2) 三站点：Web Analytics 整页截图 PNG + 「打印报告」式 PDF ----
for (const [short, info] of Object.entries(meta.sites)) {
  const page = await ctx.newPage();
  try {
    const url = buildUrl(`/analytics/rum/site/${info.siteTag}`);
    console.log(`[${short}]`, WIN ? `window ${WIN.start} ~ ${WIN.end}` : '(无窗口, 默认视图)');
    if (!(await loadDashboard(page, url))) {
      failures.push(`${short}: 未登录（Cookie 过期?）`);
      console.warn(`${short}: 未登录（Cookie 过期?），跳过`);
      continue;
    }

    // 2a. 整页截图 PNG
    try {
      const png = path.join(outDir, `${short}_webanalytics_${suffix}.png`);
      await page.screenshot({ path: png, fullPage: true });
      console.log(`screenshot ok: ${short} -> ${path.basename(png)}`);
    } catch (e) {
      failures.push(`${short} screenshot: ${e.message}`);
      console.warn(`screenshot failed ${short}:`, e.message);
    }

    // 2b. 打印报告式 PDF（保留原有需求）
    await page.emulateMedia({ media: 'print' });
    const pdf = path.join(outDir, `${short}_${suffix}.pdf`);
    await page.pdf({
      path: pdf,
      format: 'A4',
      landscape: true,
      printBackground: true,                    // 保留彩色图表
      margin: { top: '10mm', bottom: '10mm', left: '8mm', right: '8mm' },
    });
    console.log(`pdf ok: ${short} -> ${path.basename(pdf)}`);
  } catch (e) {
    failures.push(`${short} pdf: ${e.message}`);
    console.warn(`pdf failed ${short}:`, e.message);
  } finally {
    await page.close();
  }
}

await browser.close();

// ---- 汇总：失败项打到 GitHub Summary 可见的位置 ----
if (failures.length) {
  console.warn(`::warning::${failures.length} 项导出失败:\n` +
    failures.map(f => `::warning::  - ${f}`).join('\n'));
  console.log('[report] done with failures:', failures.length);
} else {
  console.log('[report] all done. 4 PNG + 3 PDF 全部成功.');
}
