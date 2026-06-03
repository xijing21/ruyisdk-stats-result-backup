import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// 使用 UTC 时间确保一致性
const now = new Date();
const year = now.getUTCFullYear();
const month = now.getUTCMonth() + 1;
const day = now.getUTCDate();
const dateStr = `${year}${String(month).padStart(2, '0')}${String(day).padStart(2, '0')}`;
const monthStr = `${year}${String(month).padStart(2, '0')}`;

// 判断是否是每月最后一天（UTC）
function isLastDayOfMonth(date) {
  const lastDay = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0));
  return date.getUTCDate() === lastDay.getUTCDate();
}

// 判断是否是双周二（UTC）
function isBiweeklyTuesday(date) {
  if (date.getUTCDay() !== 2) return false; // 不是周二
  const startOfYear = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  const dayOfYear = Math.floor((date - startOfYear) / 86400000) + startOfYear.getUTCDay() + 1;
  const weekNumber = Math.ceil(dayOfYear / 7);
  return weekNumber % 2 === 0; // 双周
}

// 确定结果目录
let resultDir;
if (isBiweeklyTuesday(now)) {
  resultDir = path.join(__dirname, 'results-biweekly', dateStr);
} else if (isLastDayOfMonth(now)) {
  resultDir = path.join(__dirname, 'results-monthly', monthStr);
} else {
  resultDir = path.join(__dirname, 'results-manual', dateStr);
}

fs.mkdirSync(resultDir, { recursive: true });

// 将结果目录路径写入文件供统计脚本使用
fs.writeFileSync(path.join(__dirname, 'result_dir.txt'), resultDir);

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    ignoreHTTPSErrors: true
  });
  
  const page = await context.newPage();

  // 通用截图函数
  async function takeScreenshot(url, filename, options = {}) {
    try {
      console.log(`📸 开始截图: ${url}`);
      await page.goto(url, { 
        waitUntil: 'networkidle', 
        timeout: 60000 
      });
      
      // 检查是否重定向到登录页面
      if (page.url().includes('login') || page.url().includes('signin')) {
        console.log(`⚠️ 需要登录，跳过: ${url}`);
        return false;
      }
      
      const filePath = path.join(resultDir, filename);
      await page.screenshot({ 
        path: filePath, 
        fullPage: options.fullPage !== false,
        ...options
      });
      console.log(`✅ 截图已保存: ${filePath}`);
      return true;
    } catch (error) {
      console.error(`❌ 截图失败 ${url}:`, error.message);
      return false;
    }
  }

  // 1. 不需要登录的页面截图
  await takeScreenshot('https://ruyisdk.org/dashboard/', `dashboard_${dateStr}.jpg`);
  await takeScreenshot('https://open-vsx.org/extension/RuyiSDK/ruyisdk-vscode-extension', `openvsx_${dateStr}.jpg`);
  await takeScreenshot('https://marketplace.eclipse.org/content/ruyisdk#metrics', `eclipse_marketplace_${dateStr}.jpg`);

  // 2. API 接口数据下载
  try {
    console.log('📊 开始下载 API 数据...');
    const apiResponse = await fetch('https://api.ruyisdk.cn/fe/dashboard');
    if (apiResponse.ok) {
      const apiData = await apiResponse.json();
      const apiFilePath = path.join(resultDir, `api_dashboard_${dateStr}.json`);
      fs.writeFileSync(apiFilePath, JSON.stringify(apiData, null, 2));
      console.log(`✅ API 数据已保存: ${apiFilePath}`);
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
    
    // 解析 Cookie 字符串（格式：name1=value1; name2=value2）
    const cookies = vsmCookie.split(';').map(cookie => {
      const [name, value] = cookie.trim().split('=');
      return {
        name: name.trim(),
        value: value.trim(),
        domain: '.visualstudio.com',
        path: '/'
      };
    });
    
    await context.addCookies(cookies);
    
    const vsmSuccess = await takeScreenshot(
      'https://marketplace.visualstudio.com/manage/publishers/RuyiSDK',
      `vsm_${dateStr}.jpg`
    );
    
    if (!vsmSuccess) {
      console.log('💡 提示: 请更新 VSM_COOKIE，当前 Cookie 可能已过期');
    }
  } else {
    console.log('⚠️ 未设置 VSM_COOKIE，跳过 Visual Studio Marketplace 截图');
  }

  await browser.close();
  console.log(`🎉 任务完成！结果保存在: ${resultDir}`);
})();
