import "./isolated-postgres.mjs";
/* 题库管理 Review UI 真实浏览器 e2e（阶段 B 闭环，固定候选，不调模型）。
 *
 * 前端沿用现有 Review UI 页面骨架（question-bank-review 的 CSS 与交互）。
 * 覆盖：列表与详情渲染、灯箱、三块正文编辑 dirty/保存 r2、原有图卡粘贴替换/追加、
 * 不开正文编辑直接粘贴图（保存栏出现）、删除绑定同步清除正文标记、
 * 刷新一致、服务重启一致、批准→导出→发布、409 冲突保留本地修改。
 * 外部请求（MathJax CDN）在测试内拦截，保证离线稳定。
 * 运行：node tests/e2e/test_qb_manage_ui.mjs
 */
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const REPO = path.resolve(fileURLToPath(new URL("../..", import.meta.url)));
const PY = process.platform === "win32"
  ? path.join(REPO, ".venv", "Scripts", "python.exe")
  : path.join(REPO, ".venv", "bin", "python");
const TMP_BASE = fs.mkdtempSync(path.join(REPO, ".tmp-qbm-e2e-"));
const ROOT = path.join(TMP_BASE, "root");
const SEED_DIR = path.join(TMP_BASE, "seed");
fs.mkdirSync(ROOT, { recursive: true });
const SHOTS = path.join(REPO, "artifacts", `qb-manage-e2e-${Date.now()}`);
fs.mkdirSync(SHOTS, { recursive: true });

const failures = [];
const liveServers = [];
let liveBrowser;
function check(name, condition, detail = "") {
  if (condition) {
    console.log(`  ok   ${name}`);
  } else {
    failures.push(name + (detail ? ` :: ${detail}` : ""));
    console.log(`  FAIL ${name} ${detail}`);
  }
}
const run = (ok, name, detail) => check(name, ok, detail);

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
    srv.on("error", reject);
  });
}

async function waitFor(fn, { timeout = 15000, interval = 200, label = "condition" } = {}) {
  const deadline = Date.now() + timeout;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const value = await fn();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await sleep(interval);
  }
  throw new Error(`waitFor 超时: ${label}${lastError ? ` (${lastError})` : ""}`);
}

async function startServer(port) {
  const child = spawn(PY, [
    "-m", "question_bank.cli", "serve",
    "--root", ROOT, "--port", String(port),
  ], { cwd: REPO, stdio: ["ignore", "pipe", "pipe"] });
  liveServers.push(child);
  child.logs = "";
  child.stdout.on("data", (d) => { child.logs += d; });
  child.stderr.on("data", (d) => { child.logs += d; });
  await waitFor(
    () => fetch(`http://127.0.0.1:${port}/healthz`)
      .then((r) => r.ok)
      .catch(() => false),
    { label: "healthz", timeout: 30000 },
  );
  return child;
}

async function focusEnd(page, selector) {
  await page.$eval(selector, (node) => {
    node.focus();
    node.setSelectionRange(node.value.length, node.value.length);
  });
}

async function clickButton(page, text) {
  return page.evaluate((label) => {
    const button = [...document.querySelectorAll("button")]
      .find((b) => b.textContent.trim().startsWith(label));
    if (!button || button.disabled) return false;
    button.click();
    return true;
  }, text);
}

async function openFirstItem(page) {
  await page.waitForSelector("#question-list .question-row");
  await page.click("#question-list .question-row");
  await page.waitForFunction(() => document.querySelector("#stem")?.textContent.includes("二次函数"));
}

async function newPage(context, base) {
  const page = await context.newPage();
  await page.setViewport({ width: 1500, height: 1000 });
  page.on("dialog", (dialog) => dialog.accept(""));
  // 拦截外部请求（MathJax CDN）：e2e 不依赖外网，避免 networkidle 挂起。
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const url = request.url();
    if (url.startsWith(base) || url.startsWith("data:")) request.continue();
    else request.abort();
  });
  await page.goto(`${base}/`, { waitUntil: "domcontentloaded", timeout: 60000 });
  return page;
}

async function pasteFixture(page, selector, append = false) {
  if (append) await page.$eval(selector, node => node.click());
  else await page.click(selector);
  const png = fs.readFileSync(path.join(SEED_DIR,"ui-e2e-001","media","figsol.png")).toString("base64");
  await page.evaluate(data => {
    const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
    const clipboardData = new DataTransfer();
    clipboardData.items.add(new File([bytes], "manual.png", {type:"image/png"}));
    document.dispatchEvent(new ClipboardEvent("paste", {clipboardData, bubbles:true, cancelable:true}));
  }, png);
}

async function main() {
  console.log("e2e root:", ROOT);

  // ---- 播种固定候选 ----
  const seeded = spawnSync(PY, [
    path.join("tests", "e2e", "_qb_manage_seed.py"), ROOT, SEED_DIR,
  ], { cwd: REPO, encoding: "utf-8" });
  run(seeded.status === 0 && seeded.stdout.trim().endsWith("r1"),
    "固定候选播种 r1", seeded.stdout + seeded.stderr);

  const port = await freePort();
  let server = await startServer(port);
  const base = `http://127.0.0.1:${port}`;

  const browser = liveBrowser = await puppeteer.launch({ headless: true, args: [
    "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
  ] });
  const context = await browser.createBrowserContext();
  const page = await newPage(context, base);
  page.on("pageerror", (error) => failures.push(`pageerror: ${error}`));

  // ---- 1. 列表与详情渲染（现有 UI 骨架）----
  await page.waitForSelector("#question-list .question-row");
  const listText = await page.$eval("#question-list .question-row", (n) => n.textContent);
  run(listText.includes("第21题"), "列表展示第 21 题");
  await page.click("#question-list .question-row");
  await page.waitForFunction(() => document.querySelector("#stem")?.textContent.includes("二次函数"));
  const stemText = await page.$eval("#stem", (n) => n.textContent);
  run(stemText.includes("二次函数"), "题目正文渲染", stemText.slice(0, 40));
  const answerText = await page.$eval("#answer", (n) => n.textContent);
  run(answerText.includes("x=2"), "答案正文渲染");
  const badges = await page.$eval("#item-badges", (n) => n.textContent);
  run(badges.includes("当前 r1"), "徽章显示当前修订 r1", badges);
  run(await page.$$eval("#prompt-preview .preview-card", (ns) => ns.length === 1),
    "题图绑定卡片渲染");
  run(await page.$$eval("#solution-preview .preview-card", (ns) => ns.length === 1),
    "解答图绑定卡片渲染");
  run(await page.$$eval("#source-capsules .source-capsule", (ns) => ns.length === 1),
    "沿用一个合并来源页胶囊");
  const approveDisabled0 = await page.$eval("#approve-item", (n) => n.disabled);
  run(approveDisabled0 === false, "无阻断疑点时批准按钮可用", String(approveDisabled0));

  // Established source gallery: one capsule, ordered pages, keyboard stays in gallery.
  run(await page.$$("input[type=number]").then(ns => ns.length === 0), "没有新增裁框坐标控件");
  await page.click("#source-capsules .source-capsule");
  await page.waitForSelector("#source-lightbox[open]");
  run(await page.$eval("#source-lightbox-counter", n => n.textContent === "1 / 2"), "来源图廊第一页");
  await page.keyboard.press("ArrowRight");
  run(await page.$eval("#source-lightbox-caption", n => n.textContent.includes("第 5 页")), "方向键翻到来源第5页");
  run(await page.$eval("#source-lightbox-next", n => n.disabled), "图廊末页禁止越界");
  await page.click("#source-lightbox-close");
  await page.screenshot({path:path.join(SHOTS,"01-detail.png"),fullPage:true});

  // ---- 2. 改文：编辑题目 → dirty → 与改图合并保存 r2 ----
  await page.click("#edit-stem");
  await page.waitForSelector("#editor-stem.open");
  await focusEnd(page, "#editor-stem-text");
  await page.keyboard.type("\n\n（e2e 人工修订：补充定义域 $x \\in \\mathbb{R}$）");
  const dirtyText = await page.$eval("#save-state", (n) => n.textContent);
  run(dirtyText.includes("未保存"), "编辑后显示未保存状态", dirtyText);
  run(await page.$eval("#save-btn", (n) => !n.disabled), "编辑后保存按钮启用");

  // Paste replacement uses the same UI clipboard handler as a user's screenshot.
  const originalImage = await page.$eval("#prompt-preview img", n => n.src);
  await pasteFixture(page, "#prompt-preview .preview-card");
  await page.waitForFunction(old => document.querySelector("#prompt-preview img")?.src !== old, {}, originalImage);
  const replacementImage = await page.$eval("#prompt-preview img", n => n.src);

  await page.click("#save-btn");
  await page.waitForFunction(() =>
    document.querySelector("#manage-message")?.textContent.includes("已保存 r2"),
    { timeout: 15000 });
  run(true, "保存形成新修订 r2");
  await sleep(600);
  await page.screenshot({ path: path.join(SHOTS, "02-saved-r2.png"), fullPage: true });

  // ---- 4. 刷新页面：正文/图片/修订一致 ----
  await page.reload({ waitUntil: "domcontentloaded" });
  await openFirstItem(page);
  const stemAfterReload = await page.$eval("#stem", (n) => n.textContent);
  run(stemAfterReload.includes("（e2e 人工修订"), "刷新后正文一致");
  run(await page.$eval("#prompt-preview img", n => n.src) === replacementImage, "刷新后粘贴替换图片一致");
  const title = await page.$eval("#item-title", (n) => n.textContent);
  run(title.includes("r2"), "刷新后当前修订 r2", title);

  // ---- 5. 重启服务：新进程新页面仍一致 ----
  server.kill();
  await sleep(1200);
  server = await startServer(port);
  const freshContext = await browser.createBrowserContext();
  const page2 = await newPage(freshContext, base);
  await page2.bringToFront();  // 后台标签的物理 click 会因无渲染帧而挂起
  await openFirstItem(page2);
  const stemAfterRestart = await page2.$eval("#stem", (n) => n.textContent);
  run(stemAfterRestart.includes("（e2e 人工修订"), "服务重启后正文一致");
  run(await page2.$eval("#prompt-preview img", n => n.src) === replacementImage, "服务重启与新浏览器上下文后图片一致");
  await page2.screenshot({ path: path.join(SHOTS, "03-after-restart.png"), fullPage: true });

  // ---- 6. 复核 → 导出 → 发布（r2）----
  run(await clickButton(page2, "通过"), "点击批准（通过）");
  await page2.waitForFunction(() =>
    document.querySelector("#review-status")?.textContent.includes("已批准"),
    { timeout: 15000 });
  run(await clickButton(page2, "导出当前修订"), "点击导出");
  await page2.waitForFunction(() =>
    document.querySelector("#manage-message")?.textContent.includes("导出就绪"),
    { timeout: 15000 });
  run(true, "导出就绪");
  run(await clickButton(page2, "发布当前修订"), "点击发布");
  await page2.waitForFunction(() =>
    document.querySelector("#manage-message")?.textContent.includes("已发布 r2"));
  await sleep(600);
  const pubStatus = await page2.$eval("#publication-status", (n) => n.textContent);
  run(pubStatus.includes("已发布 r2"), "发布状态可见", pubStatus);
  await page2.screenshot({ path: path.join(SHOTS, "04-published.png"), fullPage: true });
  run(fs.existsSync(path.join(ROOT, "exports", "2026-DEMO-Q021", "r2", "manifest.json")),
    "导出 manifest.json 落盘");

  // ---- 7. 不开正文编辑直接粘贴追加→保存 r3；删绑定→正文标记同步清除→保存 r4 ----
  const page3 = await newPage(context, base);
  await page3.bringToFront();
  await openFirstItem(page3);
  await pasteFixture(page3, "#solution-preview", true);
  await page3.waitForFunction(() => document.querySelectorAll("#solution-preview .preview-card").length === 2);
  await page3.waitForSelector("#save-bar", { timeout: 5000 });
  run(true, "仅粘贴添加图（未开正文编辑）出现保存栏");
  await page3.click("#save-btn");
  await page3.waitForFunction(() =>
    document.querySelector("#manage-message")?.textContent.includes("已保存 r3"),
    { timeout: 15000 });
  run(true, "仅改图保存形成 r3");
  // 删除解答图绑定：正文中的 asset:fig-sol 标记应同步消失
  const solutionBeforeDelete = await page3.$eval("#solution", n => n.textContent);
  await page3.focus('#solution-preview [data-key="fig-sol"] .image-delete-button');
  await page3.keyboard.press("Enter");
  const solutionAfterDelete = await page3.$eval("#editor-solution-text", n => n.value);
  run(solutionAfterDelete === solutionBeforeDelete.replace("![解答图](asset:fig-sol)", ""),
    "精确删除目标标记，保留另一张图片和全部正文");
  await page3.click("#save-btn");
  await page3.waitForFunction(() =>
    document.querySelector("#manage-message")?.textContent.includes("已保存 r4"),
    { timeout: 15000 });
  run(true, "删除绑定后保存形成 r4");
  run(await page3.$eval("#publication-status", n => n.textContent.includes("已发布 r2")), "后续人工编辑不移动旧发布指针");
  await page3.screenshot({ path: path.join(SHOTS, "05-image-only-edit.png"), fullPage: true });

  // ---- 8. 并发冲突：旧基线保存 → 弹窗，本地修改保留 ----
  const answerEditButtons = await page3.$$(".qbm-edit-button");
  await answerEditButtons[1].click(); // 答案编辑
  await page3.waitForSelector("#editor-answer.open");
  await focusEnd(page3, "#editor-answer-text");
  await page3.keyboard.type("\n（第二位审核者的修改）");
  const conflictPage = await newPage(context, base);
  await conflictPage.bringToFront();
  await openFirstItem(conflictPage);
  await conflictPage.click("#edit-stem");
  await conflictPage.waitForSelector("#editor-stem.open");
  await focusEnd(conflictPage, "#editor-stem-text");
  await conflictPage.keyboard.type("\n（第一个保存者的修改）");
  await conflictPage.click("#save-btn");
  await conflictPage.waitForFunction(() =>
    document.querySelector("#manage-message")?.textContent.includes("已保存 r5"),
    { timeout: 15000 });
  run(true, "第一保存者形成 r5");
  // page3 仍基于 r4 保存 → 冲突弹窗
  await page3.bringToFront();
  await page3.click("#save-btn");
  await page3.waitForSelector("#conflict-backdrop.show", { timeout: 15000 });
  const modalText = await page3.$eval("#conflict-body", (n) => n.textContent);
  run(modalText.includes("版本冲突") && modalText.includes("r5"),
    "409 冲突弹窗展示当前修订", modalText.slice(0, 80));
  await page3.evaluate(() => {
    [...document.querySelectorAll("#conflict-body button")]
      .find((b) => b.textContent.includes("保留我的修改")).click();
  });
  const keptDraft = await page3.$eval("#editor-answer-text", (n) => n.value);
  run(keptDraft.includes("（第二位审核者的修改）"), "冲突后本地修改保留");
  await page3.screenshot({ path: path.join(SHOTS, "06-conflict-kept-draft.png"), fullPage: true });

  await browser.close();
  server.kill();

  if (failures.length) {
    console.error(`\nFAILED (${failures.length}):`);
    for (const failure of failures) console.error(" -", failure);
    process.exit(1);
  }
  console.log("screenshots:", SHOTS);
  console.log("\nUI e2e 全部通过");
}

main().catch(async (error) => {
  console.error(error);
  await liveBrowser?.close();
  liveServers.forEach(server => server.kill());
  process.exitCode = 1;
});
