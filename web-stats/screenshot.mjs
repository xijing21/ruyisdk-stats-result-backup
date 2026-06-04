import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ========== 时间与日期 ==========
const now = new Date();
const year = now.getUTCFullYear();
const month = now.getUTCMonth() + 1;
const day = now.getUTCDate();
const dateStr = `${year}${String(month).padStart(2, '0')}${String(day).padStart(2, '0')}`;
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
     * 新增：自动处理所有弹窗/横幅/遮罩
     */
    async function takeScreenshot(url, filename, options = {}) {
        try {
            console.log(`📸 开始截图: ${url}`);

            // 根据域名选择加载策略
            const waitUntil = url.includes('marketplace.visualstudio.com')
                ? 'domcontentloaded'      // 微软页面追踪请求多，用 domcontentloaded
                : 'networkidle';          // 其他页面正常用 networkidle

            await page.goto(url, { waitUntil, timeout: 60000 });

            // 如果用了 domcontentloaded，额外等待一下让图片和样式加载
            if (waitUntil === 'domcontentloaded') {
                await page.waitForTimeout(3000);  // 等 3 秒让页面渲染完成
            }

            if (page.url().includes('login') || page.url().includes('signin')) {
                console.log(`⚠️ 页面重定向到登录页，跳过: ${url}`);
                return false;
            }

            // 等待动态内容加载
            await page.waitForTimeout(2000);

            // 彻底移除所有弹窗/横幅/遮罩
            await page.evaluate(() => {
                // 1. 先移除 Shadow DOM 弹窗（Eclipse 赞助弹窗）
                document.querySelectorAll('efsc-featured-story-popup').forEach(el => {
                    console.log('🗑️ 移除 efsc-featured-story-popup');
                    el.remove();
                });

                // 2. 遍历所有元素的 Shadow Root，查找并移除弹窗
                document.querySelectorAll('*').forEach(el => {
                    if (el.shadowRoot) {
                        const popup = el.shadowRoot.querySelector('[aria-label="Popup"], .popup-dismissible');
                        if (popup) {
                            console.log(`🗑️ 移除 Shadow DOM 弹窗: ${el.tagName}`);
                            el.remove();
                        }
                    }
                });

                // 3. 常规选择器移除
                const selectors = [
                    // 精准移除Eclipse Marketplace 截图中的 Popup（基于实际 DOM 结构）
                    '[aria-label="Popup"]',
                    '.popup-dismissible',
                    '[role="region"][aria-label="Popup"]',

                    // 精准移除open-vsx页面的footer（基于实际 DOM 结构）
                    'footer',
                    '[role="contentinfo"]',
                    '[class*="css-69i1ev"]',
                    '[class*="css-k008qs"]',

                    // 其它可能的通用弹窗
                    // Cookie/GDPR 弹窗
                    '#cookie-banner', '.cookie-banner', '#gdpr-consent', '.gdpr-consent',
                    '.cc-window', '.cc-banner', '#onetrust-consent-sdk', '.onetrust-pc-dark-filter',

                    // 通用弹窗和模态框
                    '.modal', '.popup', '[role="dialog"]', '[role="alertdialog"]',
                    '.overlay', '.backdrop', '.mask',

                    // 通知条/横幅（Open VSX 顶部蓝色条等）
                    '[class*="banner"]', '[class*="notification"]', '[class*="alert"]',
                    '[class*="toast"]', '[class*="snackbar"]',

                    // 常见广告 iframe
                    'iframe[src*="ads"]', 'iframe[src*="promo"]',
                ];

                selectors.forEach(sel => {
                    try {
                        document.querySelectorAll(sel).forEach(el => el.remove());
                    } catch (e) { }
                });

                // 第二阶段：移除 fixed 定位的大面积遮罩层
                document.querySelectorAll('*').forEach(el => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    if (style.position === 'fixed' &&
                        rect.width > window.innerWidth * 0.3 &&
                        rect.height > window.innerHeight * 0.3 &&
                        !['BODY', 'HTML', 'MAIN', 'ARTICLE', 'SECTION', 'NAV', 'HEADER'].includes(el.tagName)) {
                        el.remove();
                    }
                });

                // 第三阶段：按 z-index 兜底清理最高层元素
                const allElements = Array.from(document.querySelectorAll('*'));
                const withZIndex = allElements.map(el => {
                    const z = window.getComputedStyle(el).zIndex;
                    return { el, z: z === 'auto' ? 0 : parseInt(z) };
                }).filter(item => item.z > 200);

                withZIndex.sort((a, b) => b.z - a.z);
                withZIndex.slice(0, 5).forEach(item => {
                    const tag = item.el.tagName.toLowerCase();
                    if (!['body', 'html', 'main', 'article', 'section', 'nav', 'header'].includes(tag)) {
                        item.el.remove();
                    }
                });
            });

            // 等待 DOM 更新
            await page.waitForTimeout(1000);

            for (const dir of dirs) {
                const filePath = path.join(dir, filename);
                await page.screenshot({
                    path: filePath,
                    fullPage: options.fullPage === true,
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

    // 1. 不需要登录的页面,默认截取当前视口，如果要截取全页则增加参数：{ fullPage: true }
    await takeScreenshot('https://ruyisdk.org/dashboard/', `dashboard_${dateStr}.jpg`);
    await takeScreenshot('https://open-vsx.org/extension/RuyiSDK/ruyisdk-vscode-extension', `openvsx_${dateStr}.jpg`);
    await takeScreenshot('https://marketplace.visualstudio.com/items?itemName=RuyiSDK.ruyisdk-vscode-extension', `vsm_${dateStr}.jpg`);
    await takeScreenshot('https://marketplace.eclipse.org/content/ruyisdk#metrics', `eclipse_marketplace_${dateStr}.jpg`, { fullPage: true });

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

    await browser.close();
    console.log(`🎉 截图任务完成！结果目录: ${dirs.join(', ')}`);
})();