// cf-stats/cf_report.mjs
// ============================================================
// 职责：
//   1. 账户分析页整页截图 PNG（该页无打印报告按钮，用截图覆盖）
//   2. 三站点 Web Analytics 「打印报告」式 PDF（Chromium page.pdf）
// 前置：cf_stats.py 已运行（读取其产出的 _meta.json）
// 环境变量：
//   CF_DASH_COOKIE  登录 dash.cloudflare.com 后的 Cookie 串
//                   （F12 -> Application -> Cookies，拼成 "k=v; k=v"）
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

const browser = await chromium.launch({
  headless: true,
  args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
});
const ctx = await browser.newContext({
  //userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
  userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36',
  viewport: { width: 1680, height: 1050 },
  locale: 'zh-CN',
  timezoneId: 'Asia/Shanghai',
});
await ctx.addCookies(cookies);

// ---- 1) 账户分析整页截图 ----
try {
  const page = await ctx.newPage();
  await page.goto(`${BASE_URL}/analytics`, {
    waitUntil: 'networkidle', timeout: 120000,
  });
  await page.waitForTimeout(10000);           // 等待图表渲染
  if (!(await isLoggedIn(page))) {
    console.warn('账户分析: 未登录（Cookie 过期?），跳过截图');
  } else {
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
    const png = path.join(outDir, `${ACCOUNT_LABEL}_${suffix}.png`);
    await page.screenshot({ path: png, fullPage: true });
    console.log('account screenshot ok ->', path.basename(png));
  }
  await page.close();
} catch (e) {
  console.warn('account screenshot failed:', e.message);
}

// ---- 2) 三站点「打印报告」式 PDF ----
// range: weekly=P7D, monthly=P30D（可视化近似取证，精确数值以同目录 JSON 为准）
const range = PERIOD === 'monthly' ? 'P30D' : 'P7D';

for (const [short, info] of Object.entries(meta.sites)) {
  const page = await ctx.newPage();
  try {
    const url = `${BASE_URL}/analytics/rum/site/${info.siteTag}?range=${range}&tz=Asia%2FShanghai`;
    await page.goto(url, { waitUntil: 'networkidle', timeout: 120000 });
    await page.waitForTimeout(10000);
    if (!(await isLoggedIn(page))) {
      console.warn(`${short}: 未登录（Cookie 过期?），跳过 PDF`);
      continue;
    }
    // 逐段滚动触发懒加载
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
    console.warn(`pdf failed ${short}:`, e.message);
  } finally {
    await page.close();
  }
}

await browser.close();
console.log('[report] all done.');
