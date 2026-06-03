import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ========== 时间与日期 ==========
const now = new Date();
const year  = now.getUTCFullYear();
const month = now.getUTCMonth() + 1;
const day   = now.getUTCDate();
const dateStr  = `${year}${String(month).padStart(2, '0')}${String(day).padStart(2, '0')}`;
const monthStr = `${year}${String(month).padStart(2, '0')}`;

// ========== 日期判断 ==========
function isLastDayOfMonth(d) {
  const lastDay = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0));
  return d.getUTCDate() === lastDay.getUTCDate();
}

function isTuesday(d) {
  return d.getUTCDay() === 2;
}

// ========== 确定输出目录（可能同时存两个） ==========
const dirs = [];

if (isTuesday(now)) {
  dirs.push(path.join(__dirname, 'results-weekly', dateStr));
}
if (isLastDayOfMonth(now)) {
  dirs.push(path.join(__dirname, 'results-monthly', monthStr));
}
// 手动触发或非约定日期时，仍保存到 results-weekly
if (dirs.length === 0) {
  dirs.push(path.join(__dirname, 'results-weekly', dateStr));
}

dirs.forEach(d => fs.mkdirSync(d, { recursive: true }));

// 写入主目录供统计脚本使用
fs.writeFileSync(path.join(__dirname, 'result_dir.txt'), dirs[0]);

// ========== 截图与采集 ==========
(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    ignoreHTTPSErrors: true,
  });
  const page = await context.newPage();

  /**
   * 通用截图函数：对所有目标目录各存一份
   */
  async function takeScreenshot(url, filename, options = {}) {
    try {
      console.log(`📸 开始截图: ${url}`);
      await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });

      if (page.url().includes('login') || page.url().includes('signin')) {
        console.log(`⚠️ 页面重定向到登录页，跳过: ${url}`);
        return false;
      }

      for (const dir of dirs) {
        const filePath = path.join(dir, filename);
        await page.screenshot({
          path: filePath,
          fullPage: options.fullPage !== false,
          ...options,
        });
        console.log(`✅ 截图已保存: ${filePath}`);
      }
      return true;
    } catch (error) {
      console.error(`❌ 截图失败 ${url}:`, error.message);
      return false;
    }
  }

  // 1. 不需要登录的页面
  await takeScreenshot('https://ruyisdk.org/dashboard/',          `dashboard_${dateStr}.jpg`);
  await takeScreenshot('https://open-vsx.org/extension/RuyiSDK/ruyisdk-vscode-extension', `openvsx_${dateStr}.jpg`);
  await takeScreenshot('https://marketplace.eclipse.org/content/ruyisdk#metrics', `eclipse_marketplace_${dateStr}.jpg`);

  // 2. API 接口数据下载
  try {
    console.log('📊 开始下载 API 数据...');
    const apiResponse = await fetch('https://api.ruyisdk.cn/fe/dashboard');
    if (apiResponse.ok) {
      const apiData = await apiResponse.json();
      for (const dir of dirs) {
        const apiFilePath = path.join(dir, `api_dashboard_${dateStr}.json`);
        fs.writeFileSync(apiFilePath, JSON.stringify(apiData, null, 2));
        console.log(`✅ API 数据已保存: ${apiFilePath}`);
      }
    } else {
      console.error(`❌ API 请求失败: ${apiResponse.status}`);
    }
  } catch (error) {
    console.error('❌ API 数据下载失败:', error.message);
  }

  // 3. 需要登录的 Visual Studio Marketplace
  const vsmCookie = process.env.VSM_COOKIE;
  if (vsmCookie) {
    console.log('🔐 尝试使用 Cookie 登录 Visual Studio Marketplace...');

    // 正确解析 Cookie（值中可能含有 = 号）
    const cookies = vsmCookie.split(';')
      .map(cookie => {
        const trimmed = cookie.trim();
        if (!trimmed) return null;
        const eqIndex = trimmed.indexOf('=');
        if (eqIndex === -1) return null;
        return {
          name:  trimmed.substring(0, eqIndex),
          value: trimmed.substring(eqIndex + 1),
          domain: '.visualstudio.com',
          path: '/',
        };
      })
      .filter(Boolean);

    if (cookies.length > 0) {
      await context.addCookies(cookies);
      const vsmOk = await takeScreenshot(
        'https://marketplace.visualstudio.com/manage/publishers/RuyiSDK',
        `vsm_${dateStr}.jpg`
      );
      if (!vsmOk) {
        console.log('💡 提示: VSM_COOKIE 可能已过期，请更新 GitHub Secrets');
      }
    } else {
      console.log('⚠️ VSM_COOKIE 格式无效，跳过');
    }
  } else {
    console.log('⚠️ 未设置 VSM_COOKIE，跳过 Visual Studio Marketplace 截图');
  }

  await browser.close();
  console.log(`🎉 截图任务完成！结果目录: ${dirs.join(', ')}`);
})();
