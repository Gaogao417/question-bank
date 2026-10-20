import "./isolated-postgres.mjs";
/* 题库管理 Review UI 真实浏览器 e2e：老版查卷与连续审核体验复刻。
 *
 * 覆盖：试卷目录与 facets、筛选（年份）与检索、审核进度、单题审核后自动跳题、
 * 全卷通过（Shift+A）跳下一卷、阻断疑点部分失败不跳卷、卷内题号筛选、
 * 未保存修改保护。编辑/图片/发布闭环由 test_qb_manage_ui.mjs 覆盖，此处不重复。
 * 外部请求（MathJax CDN）在测试内拦截，保证离线稳定。
 * 运行：node tests/e2e/test_qb_manage_review_flow.mjs
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
const SHOTS = path.join(REPO, "artifacts", `qb-manage-review-e2e-${Date.now()}`);
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

async function newPage(context, base, { delayItemsOf = null } = {}) {
  const page = await context.newPage();
  await page.setViewport({ width: 1500, height: 1000 });
  page.on("dialog", (dialog) => dialog.accept(""));
  await page.setRequestInterception(true);
  page.on("request", async (request) => {
    const url = request.url();
    if (!url.startsWith(base) && !url.startsWith("data:")) { request.abort(); return; }
    if (delayItemsOf && url.includes(`/api/items?paper_id=${encodeURIComponent(delayItemsOf)}`)) {
      await sleep(1200);  // 模拟旧卷目录响应慢（切卷竞态回归）
    }
    request.continue();
  });
  await page.goto(`${base}/`, { waitUntil: "domcontentloaded", timeout: 60000 });
  return page;
}

let page0 = null;
const text = (sel) => page0.$eval(sel, (n) => n.textContent);

async function main() {
  console.log("e2e root:", ROOT);

  const seeded = spawnSync(PY, [
    path.join("tests", "e2e", "_qb_manage_review_seed.py"), ROOT, SEED_DIR,
  ], { cwd: REPO, encoding: "utf-8" });
  run(seeded.status === 0 && seeded.stdout.includes("3 papers / 8 items"),
    "多卷多状态播种", seeded.stdout + seeded.stderr);

  const port = await freePort();
  await startServer(port);
  const base = `http://127.0.0.1:${port}`;
  const browser = liveBrowser = await puppeteer.launch({ headless: true, args: [
    "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
  ] });
  const context = await browser.createBrowserContext();
  page0 = await newPage(context, base);
  page0.on("pageerror", (error) => failures.push(`pageerror: ${error}`));

  // ---- 1. 目录、facets 与进度 ----
  await waitFor(() => page0.$$eval("#bank-select option", (ns) => ns.length >= 3),
    { label: "bank options" });
  const bankOptions = await page0.$$eval("#bank-select option", (ns) => ns.map((n) => n.textContent));
  run(bankOptions.length === 3, "筛选结果含 3 份试卷", bankOptions.join(" | "));
  run(bankOptions.some((t) => t.includes("2014-HUANGPU-YIMO") && t.includes("黄浦") && t.includes("一模")),
    "试卷选项带元信息（黄浦·一模）");
  const yearOptions = await page0.$$eval("#filter-year option", (ns) => ns.map((n) => n.value));
  run(yearOptions.includes("2016") && yearOptions.includes("2014"), "年份 facets 填充", yearOptions.join(","));
  const gradeOptions = await page0.$$eval("#filter-grade option", (ns) => ns.map((n) => n.value));
  run(gradeOptions.includes("九年级"), "年级 facets 填充", gradeOptions.join(","));
  const meta0 = await text("#bank-meta");
  run(meta0.includes("已入库 3") && meta0.includes("通过 1") && meta0.includes("待审 2"),
    "黄浦卷进度：入库 3 / 通过 1 / 待审 2", meta0);
  run(meta0.includes("原卷题数未确认"), "expected unknown 时明示进度只覆盖已入库题", meta0);

  // 初始定位在第一道待审题（Q002）
  await waitFor(() => page0.$eval("#item-id", (n) => n.textContent.includes("2014-HUANGPU-YIMO-Q002")),
    { label: "first pending item Q002" });
  run(true, "初始定位第一道待审题 Q002");

  // ---- 2. 单题审核自动跳题 ----
  await page0.keyboard.press("KeyA");
  await waitFor(() => page0.$eval("#review-message", (n) => n.textContent.includes("已保存：通过")),
    { label: "approve saved" });
  const msg1 = await text("#review-message");
  run(msg1.includes("已跳到 2014-HUANGPU-YIMO-Q003"), "审核通过后自动跳到 Q003", msg1);
  await waitFor(() => page0.$eval("#item-id", (n) => n.textContent.includes("Q003")),
    { label: "jumped to Q003" });
  await page0.keyboard.press("KeyA");
  await waitFor(() => page0.$eval("#review-message", (n) => n.textContent.includes("没有待审核题目")),
    { label: "no more pending" });
  const meta1 = await text("#bank-meta");
  run(meta1.includes("通过 3"), "审核后进度更新（通过 3）", meta1);
  const rows = await page0.$$eval("#question-list .question-row", (ns) => ns.map((n) => n.textContent));
  run(rows.filter((t) => t.includes("已通过")).length === 3, "列表三题全部已通过", rows.join(" | "));
  await page0.screenshot({ path: path.join(SHOTS, "01-paper-approved.png"), fullPage: true });

  // ---- 3. 全卷通过（Shift+A）→ 跳下一份未完成卷 ----
  await page0.keyboard.down("Shift");
  await page0.keyboard.press("KeyA");
  await page0.keyboard.up("Shift");
  await waitFor(() => page0.$eval("#bulk-message", (n) => n.textContent.includes("已跳转 2015-SONGJIANG-ERMO")),
    { label: "jump to next paper" });
  await waitFor(() => page0.$eval("#bank-select", (n) => n.value === "2015-SONGJIANG-ERMO"),
    { label: "select switched" });
  run(true, "全卷通过后跳转 2015-SONGJIANG-ERMO");
  await waitFor(() => page0.$eval("#item-id", (n) => n.textContent.includes("2015-SONGJIANG-ERMO-Q001")),
    { label: "landed on first pending of next paper" });

  // 松江卷全卷通过 → 跳徐汇卷
  await page0.keyboard.down("Shift");
  await page0.keyboard.press("KeyA");
  await page0.keyboard.up("Shift");
  await waitFor(() => page0.$eval("#bulk-message", (n) => n.textContent.includes("已跳转 2016-XUHUI-YIMO")),
    { label: "jump to xuhui" });
  run(true, "松江卷全卷通过后跳转 2016-XUHUI-YIMO");

  // ---- 4. 阻断疑点：全卷通过部分失败不跳卷 ----
  // 徐汇 Q001 正常、Q002 有阻断疑点。先单题通过 Q001 → 跳 Q002；Shift+A 应失败留卷。
  await page0.keyboard.press("KeyA");
  await waitFor(() => page0.$eval("#item-id", (n) => n.textContent.includes("2016-XUHUI-YIMO-Q002")),
    { label: "auto advance to blocked item" });
  await page0.keyboard.down("Shift");
  await page0.keyboard.press("KeyA");
  await page0.keyboard.up("Shift");
  await waitFor(
    () => page0.$eval("#bulk-message", (n) => n.textContent.includes("审核失败")),
    { label: "bulk partial failure message" });
  const bulkFail = await text("#bulk-message");
  run(bulkFail.includes("2016-XUHUI-YIMO-Q002"), "失败清单点名阻断题", bulkFail);
  await sleep(300);
  const paperNow = await page0.$eval("#bank-select", (n) => n.value);
  run(paperNow === "2016-XUHUI-YIMO", "部分失败不跳卷", paperNow);
  const metaC = await text("#bank-meta");
  run(metaC.includes("通过 1") && metaC.includes("阻断 1"), "徐汇卷进度：通过 1 / 阻断 1", metaC);
  await page0.screenshot({ path: path.join(SHOTS, "02-bulk-partial.png"), fullPage: true });

  // ---- 5. 筛选与检索 ----
  await page0.select("#filter-year", "2016");
  await waitFor(() => page0.$$eval("#bank-select option", (ns) => ns.length === 1),
    { label: "year filter applied" });
  const filtered = await page0.$eval("#bank-select option", (n) => n.textContent);
  run(filtered.includes("2016-XUHUI-YIMO"), "年份筛选只剩徐汇卷", filtered);
  run(new URL(page0.url()).searchParams.get("year") === "2016", "筛选写入 URL", page0.url());

  // 卷内题号筛选（本地；item_id 含年份数字，用 Q002 精确到题号）
  await page0.type("#item-filter", "Q002");
  await waitFor(() => page0.$$eval("#question-list .question-row", (ns) => ns.length === 1),
    { label: "item filter" });
  const itemCount = await text("#item-count");
  run(itemCount.includes("1 / 2"), "卷内筛选计数 1 / 2", itemCount);
  await page0.$eval("#item-filter", (n) => { n.value = ""; n.dispatchEvent(new Event("input")); });

  // 检索无结果空态：旧卷视图清空且不可审核（A / Shift+A 不产生审核请求）
  const reviewPosts = [];
  await page0.exposeFunction("noteReviewPost", (url) => reviewPosts.push(url));
  await page0.evaluate(() => {
    const original = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const url = typeof input === "string" ? input : input.url;
      if (String(init?.method || "GET").toUpperCase() === "POST" && String(url).includes("/review")) {
        window.noteReviewPost(String(url));
      }
      return original(input, init);
    };
  });
  await page0.$eval("#search-input", (n) => { n.value = "长宁"; n.dispatchEvent(new Event("input")); });
  await waitFor(() => page0.$eval("#bank-select option", (n) => n.textContent.includes("没有匹配")),
    { label: "no match empty state" });
  const statusText = await text("#page-status");
  run(statusText.includes("没有匹配的试卷"), "空态提示可见", statusText);
  run(await page0.$eval("#review-layout", (n) => n.hidden), "空态隐藏旧题操作区");
  await page0.keyboard.press("KeyA");
  await page0.keyboard.down("Shift");
  await page0.keyboard.press("KeyA");
  await page0.keyboard.up("Shift");
  await sleep(600);
  run(reviewPosts.length === 0, "空态下 A / Shift+A 不产生审核请求", reviewPosts.join(","));
  await page0.$eval("#search-input", (n) => { n.value = ""; n.dispatchEvent(new Event("input")); });
  await waitFor(() => page0.$eval("#review-layout", (n) => !n.hidden && n.textContent.includes("2016-XUHUI-YIMO")),
    { label: "search cleared, view restored" });
  run(true, "恢复筛选后视图返回");

  // ---- 6. 未保存修改保护：编辑态快捷键停用；按钮路径拒绝全卷通过且不吞草稿 ----
  await page0.click("#edit-stem");
  await page0.waitForSelector("#editor-stem.open");
  await page0.focus("#editor-stem-text");
  await page0.keyboard.press("End");
  await page0.type("#editor-stem-text", "（补一句未保存）");
  // 编辑态下 Shift+A 不触发任何审核动作（快捷键停用）
  await page0.keyboard.down("Shift");
  await page0.keyboard.press("KeyA");
  await page0.keyboard.up("Shift");
  await sleep(400);
  run(await page0.$eval("#item-id", (n) => n.textContent.includes("Q002")),
    "编辑态 Shift+A 快捷键停用");
  // 按钮路径：dirty 时明确拒绝全卷通过
  await page0.waitForFunction(() => !document.querySelector("#approve-paper").disabled);
  await page0.evaluate(() => { document.querySelector("#approve-paper").click(); });
  const guardMsg = await text("#bulk-message");
  run(guardMsg.includes("未保存的修改"), "dirty 时全卷通过按钮被拒绝", guardMsg);
  const draftNow = await page0.$eval("#editor-stem-text", (n) => n.value);
  run(draftNow.includes("补一句未保存"), "草稿未丢", draftNow.slice(-30));
  run(await page0.$eval("#stem", (n) => n.style.display === "none"),
    "编辑框保持打开");

  // ---- 7. 切卷竞态：慢的旧卷目录响应不得覆盖当前卷 ----
  const context2 = await browser.createBrowserContext();
  const page2 = await context2.newPage();
  await page2.setViewport({ width: 1500, height: 1000 });
  page2.on("dialog", (dialog) => dialog.accept(""));
  page2.on("pageerror", (error) => failures.push(`page2 error: ${error}`));
  await page2.setRequestInterception(true);
  page2.on("request", async (request) => {
    const url = request.url();
    if (!url.startsWith(base) && !url.startsWith("data:")) { request.abort(); return; }
    if (url.includes("/api/items?paper_id=2014-HUANGPU-YIMO")) {
      await sleep(1200);  // 旧卷（A）目录响应人为延迟
    }
    request.continue();
  });
  const reviewTargets = [];
  await page2.exposeFunction("noteReviewTarget", (url) => reviewTargets.push(url));
  await page2.evaluateOnNewDocument(() => {
    const original = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const url = typeof input === "string" ? input : input.url;
      if (String(init?.method || "GET").toUpperCase() === "POST" && String(url).includes("/review")) {
        window.noteReviewTarget(String(url));
      }
      return original(input, init);
    };
  });
  await page2.goto(`${base}/`, { waitUntil: "domcontentloaded", timeout: 60000 });
  // A 卷目录仍在途时快速切到 B 卷
  await waitFor(() => page2.$eval("#bank-select", (n) => !n.disabled),
    { label: "bank select enabled" });
  await page2.select("#bank-select", "2015-SONGJIANG-ERMO");
  await waitFor(() => page2.$eval("#bank-select", (n) => n.value === "2015-SONGJIANG-ERMO"),
    { label: "switched to B" });
  await waitFor(() => page2.$$eval("#question-list .question-row",
    (ns) => ns.length === 3 && ns.every((n) => n.textContent.includes("2015-SONGJIANG-ERMO"))),
    { label: "B directory rendered" });
  run(true, "切到 B 卷后目录立即是 B 卷题目");
  await sleep(1800);  // 等 A 卷延迟响应落地
  const rowsAfterDelay = await page2.$$eval("#question-list .question-row",
    (ns) => ns.map((n) => n.textContent));
  run(rowsAfterDelay.length === 3
    && rowsAfterDelay.every((t) => t.includes("2015-SONGJIANG-ERMO")),
    "A 卷延迟响应未覆盖 B 卷目录", rowsAfterDelay.join(" | ").slice(0, 80));
  run(await page2.$eval("#item-id", (n) => n.textContent.includes("2015-SONGJIANG-ERMO")),
    "当前详情仍属于 B 卷");
  // 审核目标也必须属于 B 卷
  await page2.keyboard.press("KeyA");
  await sleep(800);
  run(reviewTargets.every((u) => u.includes("2015-SONGJIANG-ERMO")) && reviewTargets.length > 0,
    "审核请求只指向 B 卷题目", reviewTargets.join(","));

  // ---- 8. 切卷窗口竞态：旧题详情慢响应不得渲染/审核；新卷目录失败不留旧卷 ----
  const context3 = await browser.createBrowserContext();
  const page3 = await context3.newPage();
  await page3.setViewport({ width: 1500, height: 1000 });
  page3.on("dialog", (dialog) => dialog.accept(""));
  page3.on("pageerror", (error) => failures.push(`page3 error: ${error}`));
  await page3.setRequestInterception(true);
  page3.on("request", async (request) => {
    const url = request.url();
    if (!url.startsWith(base) && !url.startsWith("data:")) { request.abort(); return; }
    if (url.endsWith("/api/items/2014-HUANGPU-YIMO-Q002")) {
      await sleep(800);  // A 卷待审题详情慢响应：落在切卷等待窗口内
    } else if (url.includes("/api/items?paper_id=2015-SONGJIANG-ERMO")) {
      await sleep(1600);  // B 卷目录慢：拉开窗口
    } else if (url.includes("/api/items?paper_id=2016-XUHUI-YIMO")) {
      request.abort();  // C 卷目录失败（后续失败分支用）
      return;
    }
    request.continue();
  });
  const reviewPosts3 = [];
  await page3.exposeFunction("noteReview3", (url) => reviewPosts3.push(url));
  await page3.evaluateOnNewDocument(() => {
    const original = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const url = typeof input === "string" ? input : input.url;
      if (String(init?.method || "GET").toUpperCase() === "POST" && String(url).includes("/review")) {
        window.noteReview3(String(url));
      }
      return original(input, init);
    };
  });
  await page3.goto(`${base}/`, { waitUntil: "domcontentloaded", timeout: 60000 });
  // A 卷定位第一道待审题 Q002（Q001 播种时已批准），其详情被延迟 800ms
  await waitFor(() => page3.$eval("#bank-select", (n) => !n.disabled && n.value === "2014-HUANGPU-YIMO"),
    { label: "page3 landed on A" });
  await page3.select("#bank-select", "2015-SONGJIANG-ERMO");  // 目录在途时切卷
  await sleep(1000);  // A 题慢详情此刻已返回：必须被丢弃
  run(await page3.$eval("#item-id", (n) => !n.textContent.includes("2014-HUANGPU")),
    "切卷等待窗口内旧题详情未渲染", await page3.$eval("#item-id", (n) => n.textContent));
  run(await page3.$eval("#approve-item", (n) => n.disabled), "窗口内批准按钮禁用");
  await page3.keyboard.press("KeyA");
  await sleep(400);
  run(reviewPosts3.length === 0, "窗口内 A 键不产生审核请求", reviewPosts3.join(","));
  await waitFor(() => page3.$$eval("#question-list .question-row",
    (ns) => ns.length === 3 && ns.every((n) => n.textContent.includes("2015-SONGJIANG-ERMO"))),
    { label: "B directory finally rendered" });
  run(true, "B 卷目录到达后正常渲染");
  // C 卷目录失败：不留旧卷题目，不出现可审核状态
  await page3.select("#bank-select", "2016-XUHUI-YIMO");
  await waitFor(() => page3.$eval("#page-status", (n) => n.textContent.includes("加载失败")),
    { label: "C directory failure surfaced" });
  run(await page3.$$eval("#question-list .question-row", (ns) => ns.length === 0),
    "目录失败后题目列表为空");
  run(await page3.$eval("#approve-item", (n) => n.disabled), "目录失败后批准按钮禁用");
  await page3.keyboard.down("Shift");
  await page3.keyboard.press("KeyA");
  await page3.keyboard.up("Shift");
  await sleep(400);
  run(reviewPosts3.length === 0, "目录失败后 Shift+A 不产生审核请求", reviewPosts3.join(","));

  // ---- 9. 迟到审核回执：在途切卷后不得改写新卷视图/状态/hash ----
  const context4 = await browser.createBrowserContext();
  const page4 = await context4.newPage();
  await page4.setViewport({ width: 1500, height: 1000 });
  page4.on("dialog", (dialog) => dialog.accept(""));
  page4.on("pageerror", (error) => failures.push(`page4 error: ${error}`));
  await page4.setRequestInterception(true);
  page4.on("request", async (request) => {
    const url = request.url();
    if (!url.startsWith(base) && !url.startsWith("data:")) { request.abort(); return; }
    if (request.method() === "POST" && url.includes("/review")
        && url.includes("/api/items/2014-HUANGPU-YIMO-Q")) {
      await sleep(1600);  // A 卷单题审核回执迟到：落在切卷之后
    }
    request.continue();
  });
  await page4.goto(`${base}/`, { waitUntil: "domcontentloaded", timeout: 60000 });
  // 黄浦卷此时已被前序步骤全部通过；当前题任意，审核回执照常延迟
  await waitFor(() => page4.$eval("#item-id", (n) => n.textContent.includes("2014-HUANGPU-YIMO-Q")),
    { label: "page4 on A item" });
  const reviewedAItem = await page4.$eval("#item-id", (n) => n.textContent.trim());
  await page4.keyboard.press("KeyA");  // 审核在途
  await page4.select("#bank-select", "2016-XUHUI-YIMO");  // 立即切到 C 卷
  await waitFor(() => page4.$eval("#item-id", (n) => n.textContent.includes("2016-XUHUI-YIMO")),
    { label: "page4 switched to C" });
  const hashAfterSwitch = await page4.evaluate(() => location.hash);
  await sleep(2500);  // 迟到回执已到达
  run(await page4.$eval("#item-id", (n) => n.textContent.includes("2016-XUHUI-YIMO")),
    "迟到回执未把视图拉回 A 卷",
    await page4.$eval("#item-id", (n) => n.textContent));
  run(await page4.evaluate(() => location.hash) === hashAfterSwitch,
    "hash 未被迟到回执改动", await page4.evaluate(() => location.hash));
  const cStatus = await page4.$eval("#review-status", (n) => n.textContent);
  run(cStatus.includes("待复核"), "C 卷当前题仍为待复核（未被 A 卷回执误标）",
    `${reviewedAItem} → ${cStatus}`);
  const cRows = await page4.$$eval("#question-list .question-row",
    (ns) => ns.map((n) => n.textContent));
  // C 卷 Q002 为阻断待审：迟到回执不得把它改成已通过
  const q002Row = cRows.find((t) => t.includes("Q002"));
  run(q002Row && q002Row.includes("待审核"), "C-Q002 仍为待审核（未被误标）",
    q002Row || cRows.join(" | ").slice(0, 80));

  // ---- 10. 迟到保存回执：不清另一题的未保存标记；同题提交后新输入不丢 ----
  const context5 = await browser.createBrowserContext();
  const page5 = await context5.newPage();
  await page5.setViewport({ width: 1500, height: 1000 });
  page5.on("pageerror", (error) => failures.push(`page5 error: ${error}`));
  const dialogs5 = [];
  let dialogIndex = 0;
  page5.on("dialog", (dialog) => {
    dialogs5.push({ type: dialog.type(), message: dialog.message() });
    // 第 1 个确认（带 A 草稿切卷）接受；第 2 个（迟到回执后离开 C）必须仍会弹出，先拒绝
    if (dialogIndex++ === 0) dialog.accept();
    else dialog.dismiss();
  });
  await page5.setRequestInterception(true);
  page5.on("request", async (request) => {
    const url = request.url();
    if (!url.startsWith(base) && !url.startsWith("data:")) { request.abort(); return; }
    if (request.method() === "PATCH"
        && url.includes("/api/items/2014-HUANGPU-YIMO-Q001/content")) {
      await sleep(1600);  // A 保存回执迟到
    } else if (request.method() === "PATCH"
        && url.includes("/content") && url.includes("2016-XUHUI-YIMO")) {
      await sleep(700);   // C 保存回执稍慢（同题继续输入场景）
    }
    request.continue();
  });
  await page5.goto(`${base}/`, { waitUntil: "domcontentloaded", timeout: 60000 });
  await waitFor(() => page5.$eval("#item-id", (n) => n.textContent.includes("2014-HUANGPU-YIMO-Q")),
    { label: "page5 on A" });
  // A 题提交保存（回执在途），确认切到 C 并新建 C 草稿
  await page5.click("#edit-stem");
  await page5.waitForSelector("#editor-stem.open");
  await page5.focus("#editor-stem-text");
  await page5.keyboard.press("End");
  await page5.type("#editor-stem-text", "（迟保存A）");
  await page5.click("#save-btn");           // PATCH 延迟 1600ms
  await page5.select("#bank-select", "2016-XUHUI-YIMO");  // 触发第 1 个确认（接受）
  await waitFor(() => page5.$eval("#item-id", (n) => n.textContent.includes("2016-XUHUI-YIMO")),
    { label: "page5 switched to C" });
  await page5.click("#edit-stem");
  await page5.waitForSelector("#editor-stem.open");
  await page5.focus("#editor-stem-text");
  await page5.keyboard.press("End");
  await page5.type("#editor-stem-text", "（C草稿）");
  await sleep(2200);  // A 的迟到保存回执已到达
  // 迟到回执不得清除 C 草稿的离开保护：切走必须仍弹确认
  await page5.select("#bank-select", "2014-HUANGPU-YIMO");
  await sleep(600);
  run(dialogs5.length >= 2 && dialogs5[1] && dialogs5[1].message.includes("未保存"),
    "迟到保存回执后离开 C 仍须确认", JSON.stringify(dialogs5));
  run(await page5.$eval("#bank-select", (n) => n.value === "2016-XUHUI-YIMO"),
    "拒绝确认后留在 C 卷", await page5.$eval("#bank-select", (n) => n.value));
  // 同题场景：C 保存回执慢，期间继续输入不得丢失
  const cItem = await page5.$eval("#item-id", (n) => n.textContent.trim());
  await page5.click("#save-btn");           // PATCH 延迟 700ms
  await page5.focus("#editor-stem-text");
  await page5.keyboard.press("End");
  await page5.type("#editor-stem-text", "（提交后新输入）");
  await sleep(1400);  // C 回执已到达
  const stemDraft = await page5.$eval("#editor-stem-text", (n) => n.value);
  run(stemDraft.includes("（提交后新输入）"), "同题提交后新输入未丢失", stemDraft.slice(-40));
  const saveState = await page5.$eval("#save-state", (n) => n.textContent);
  run(saveState.includes("未保存"), "同题迟到回执后仍标记未保存", saveState);
  run(await page5.$eval("#item-id", (n) => n.textContent.trim()) === cItem,
    "视图未被回执拉走", "");
  // 迟到回执推进基线后：再次保存必须成功形成下一修订（不得与自己的保存冲突）
  const baseLine = await page5.$eval("#save-state", (n) => n.textContent);
  run(/基于 r(\d+)/.test(baseLine), "保存基线已推进", baseLine);
  await page5.click("#save-btn");
  await waitFor(() => page5.$eval("#manage-message", (n) =>
    n.textContent.includes("已保存") && !n.textContent.includes("已保留")),
    { label: "second save after late receipt" });
  run(true, "继续输入后的第二次保存成功（基线已推进）",
    await page5.$eval("#manage-message", (n) => n.textContent));
  // 真正的外部并发更新仍须被拒绝：外部 PATCH 抬升修订 → 本地保存 409 → 保留后可覆盖
  await page5.click("#edit-stem");
  await page5.waitForSelector("#editor-stem.open");
  await page5.focus("#editor-stem-text");
  await page5.keyboard.press("End");
  await page5.type("#editor-stem-text", "（本地新编辑）");
  const detailNow = await page5.evaluate(async (item) =>
    (await fetch(`${location.origin}/api/items/${item}`)).json(), cItem);
  const extReceipt = await page5.evaluate(async (item, rev) => {
    const response = await fetch(`${location.origin}/api/items/${item}/content`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        base_revision: rev, operation_id: `e2e-external-${Date.now()}`,
        stem: "外部并发修改（e2e）",
      }),
    });
    return { status: response.status, body: await response.json() };
  }, cItem, detailNow.current_revision);
  run(extReceipt.status === 200, "外部并发保存成功（制造冲突前提）", JSON.stringify(extReceipt.body).slice(0, 80));
  await page5.click("#save-btn");
  await waitFor(() => page5.$eval("#conflict-backdrop", (n) => n.classList.contains("show")),
    { label: "genuine conflict dialog" });
  run(true, "真正外部冲突仍弹 409 冲突框");
  await page5.evaluate(() => {
    [...document.querySelectorAll("#conflict-body button")]
      .find((b) => b.textContent.trim() === "保留我的修改").click();
  });
  await sleep(300);
  await page5.click("#save-btn");
  await waitFor(() => page5.$eval("#manage-message", (n) => n.textContent.includes("已保存")),
    { label: "save after keep-local" });
  run(true, "保留本地后基线推进、再次保存成功",
    await page5.$eval("#manage-message", (n) => n.textContent));
  await page5.reload({ waitUntil: "domcontentloaded" });
  await sleep(1200);
  const stemAfter = await page5.$eval("#stem", (n) => n.textContent);
  run(stemAfter.includes("（本地新编辑）"), "刷新后保留全部本地内容", stemAfter.slice(-40));

  // ---- 11. 筛选迟到空结果：请求期间新建的草稿必须先确认，拒绝则视图与草稿保留 ----
  const context6 = await browser.createBrowserContext();
  const page6 = await context6.newPage();
  await page6.setViewport({ width: 1500, height: 1000 });
  let dialog6 = null;
  page6.on("pageerror", (error) => failures.push(`page6 error: ${error}`));
  page6.on("dialog", (dialog) => { dialog6 = dialog.message(); dialog.dismiss(); });  // 拒绝丢弃
  await page6.setRequestInterception(true);
  page6.on("request", async (request) => {
    const url = request.url();
    if (!url.startsWith(base) && !url.startsWith("data:")) { request.abort(); return; }
    if (url.includes("/api/papers?q=")) {
      await sleep(1500);  // 筛选响应迟到：窗口内先建草稿
    }
    request.continue();
  });
  await page6.goto(`${base}/`, { waitUntil: "domcontentloaded", timeout: 60000 });
  await waitFor(() => page6.$eval("#bank-select", (n) => !n.disabled),
    { label: "page6 loaded" });
  // 先发筛选请求（未 dirty，无提交前确认），再在等待期间新建草稿
  await page6.type("#search-input", "长宁");
  await page6.click("#edit-stem");
  await page6.waitForSelector("#editor-stem.open");
  await page6.focus("#editor-stem-text");
  await page6.keyboard.press("End");
  await page6.type("#editor-stem-text", "（迟到筛选期间的草稿）");
  await sleep(2200);  // 空结果响应已到达并触发确认（已拒绝）
  run(dialog6 !== null && dialog6.includes("未保存"), "迟到空结果前弹出了丢弃确认", String(dialog6));
  run(await page6.$eval("#search-input", (n) => n.value === ""), "拒绝后筛选回退",
    await page6.$eval("#search-input", (n) => n.value));
  run(await page6.$eval("#review-layout", (n) => !n.hidden), "视图与编辑区保留");
  const draft6 = await page6.$eval("#editor-stem-text", (n) => n.value);
  run(draft6.includes("（迟到筛选期间的草稿）"), "草稿仍在编辑框", draft6.slice(-30));
  await page6.click("#save-btn");
  await waitFor(() => page6.$eval("#manage-message", (n) => n.textContent.includes("已保存")),
    { label: "draft saved after declined filter" });
  run(true, "草稿仍可保存", await page6.$eval("#manage-message", (n) => n.textContent));
  await context6.close();

  // ---- 12. 筛选范围外深链：卷/目录/详情必须一致，全卷通过作用于正在看的卷 ----
  const context7 = await browser.createBrowserContext();
  const page7 = await context7.newPage();
  await page7.setViewport({ width: 1500, height: 1000 });
  page7.on("pageerror", (error) => failures.push(`page7 error: ${error}`));
  let dialog7 = null;
  page7.on("dialog", (dialog) => { dialog7 = dialog.message(); dialog.dismiss(); });
  const posts7 = [];
  await page7.exposeFunction("notePost7", (url) => posts7.push(url));
  await page7.evaluateOnNewDocument(() => {
    const original = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const url = typeof input === "string" ? input : input.url;
      if (String(init?.method || "GET").toUpperCase() === "POST") window.notePost7(String(url));
      return original(input, init);
    };
  });
  await page7.setRequestInterception(true);
  page7.on("request", (request) => {
    const url = request.url();
    if (url.startsWith(base) || url.startsWith("data:")) request.continue();
    else request.abort();
  });
  await page7.goto(`${base}/`, { waitUntil: "domcontentloaded", timeout: 60000 });
  await waitFor(() => page7.$eval("#bank-select", (n) => !n.disabled),
    { label: "page7 loaded" });
  await page7.select("#filter-year", "2016");   // 只剩徐汇卷
  await waitFor(() => page7.$$eval("#bank-select option", (ns) => ns.length === 1),
    { label: "year filter applied on page7" });
  await page7.evaluate(() => { location.hash = "#item=2014-HUANGPU-YIMO-Q002"; });
  await waitFor(() => page7.$eval("#item-id", (n) => n.textContent.includes("2014-HUANGPU-YIMO-Q002")),
    { label: "foreign item loaded" });
  run(await page7.$eval("#filter-year", (n) => n.value === ""), "范围外深链清空筛选",
    await page7.$eval("#filter-year", (n) => n.value));
  run(await page7.$eval("#bank-select", (n) => n.value === "2014-HUANGPU-YIMO"),
    "当前卷同步为详情所在卷", await page7.$eval("#bank-select", (n) => n.value));
  const rows7 = await page7.$$eval("#question-list .question-row",
    (ns) => ns.map((n) => n.textContent));
  run(rows7.length === 3 && rows7.every((r) => r.includes("2014-HUANGPU")),
    "目录与详情同卷", rows7.join(" | ").slice(0, 60));
  // Shift+A 必须作用于正在看的黄浦卷
  await page7.keyboard.down("Shift");
  await page7.keyboard.press("KeyA");
  await page7.keyboard.up("Shift");
  await sleep(1200);
  run(posts7.some((u) => u.includes("/api/papers/2014-HUANGPU-YIMO/review-all")),
    "全卷通过作用于正在看的黄浦卷", posts7.join(","));
  run(!posts7.some((u) => u.includes("2016-XUHUI") && u.includes("review-all")),
    "未误操作徐汇卷", posts7.join(","));
  // dirty 时导航到范围外题：拒绝则留在原卷、草稿保留、hash 恢复
  await page7.select("#filter-year", "2016");
  await waitFor(() => page7.$eval("#item-id", (n) => n.textContent.includes("2016-XUHUI")),
    { label: "back to C" });
  await page7.click("#edit-stem");
  await page7.waitForSelector("#editor-stem.open");
  await page7.focus("#editor-stem-text");
  await page7.keyboard.press("End");
  await page7.type("#editor-stem-text", "（拒绝跨卷导航的草稿）");
  await page7.evaluate(() => { location.hash = "#item=2015-SONGJIANG-ERMO-Q001"; });
  await sleep(800);
  run(dialog7 !== null && dialog7.includes("未保存"), "跨卷导航前弹丢弃确认", String(dialog7));
  run(await page7.$eval("#item-id", (n) => n.textContent.includes("2016-XUHUI")),
    "拒绝后留在原卷", await page7.$eval("#item-id", (n) => n.textContent));
  const draft7 = await page7.$eval("#editor-stem-text", (n) => n.value);
  run(draft7.includes("（拒绝跨卷导航的草稿）"), "草稿保留", draft7.slice(-24));
  run((await page7.evaluate(() => location.hash)).includes("2016-XUHUI"),
    "hash 恢复为当前题", await page7.evaluate(() => location.hash));
  await context7.close();

  for (const extra of [context2, context3, context4, context5]) {
    try { await extra.close(); } catch (_) { /* ignore */ }
  }
  await browser.close();
  for (const server of liveServers) server.kill();
  await sleep(500);
  try { fs.rmSync(TMP_BASE, { recursive: true, force: true }); }
  catch (_) { /* Windows 下句柄释放有延迟，留待下次清理 */ }

  if (failures.length) {
    console.log(`\n${failures.length} 项失败:`);
    failures.forEach((f) => console.log(` - ${f}`));
    process.exit(1);
  }
  console.log("\n全部通过");
}

process.on("exit", () => {
  try { if (liveBrowser) liveBrowser.close(); } catch (_) { /* 已关闭 */ }
  for (const server of liveServers) { try { server.kill(); } catch (_) { /* ignore */ } }
  try { fs.rmSync(TMP_BASE, { recursive: true, force: true }); } catch (_) { /* ignore */ }
});

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
