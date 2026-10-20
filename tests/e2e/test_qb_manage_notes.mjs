import "./isolated-postgres.mjs";
/* 教学内容（讲解/解答对）真实浏览器 e2e。
 *
 * 覆盖：小问派生渲染、列表缺讲解/缺解答徽标、新建对、失焦保存、批准门槛、
 * 批准、题目审核后 blueprint 导出（外部未配置状态）、题干修订后来源已变化
 * 徽标与确认适配、软删除、假麦克风录音上传（无 ASR 密钥 → 音频保存+明确失败）。
 * 服务端以清空 DASHSCOPE_API_KEY 启动，模型路径确定失败——真实模型链路另行验证。
 * 运行：node tests/e2e/test_qb_manage_notes.mjs
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
const SHOTS = path.join(REPO, "artifacts", `qb-manage-notes-e2e-${Date.now()}`);
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
  ], {
    cwd: REPO, stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, DASHSCOPE_API_KEY: "" },  // ASR/润色确定失败路径
  });
  liveServers.push(child);
  child.logs = "";
  child.stdout.on("data", (d) => { child.logs += d; });
  child.stderr.on("data", (d) => { child.logs += d; });
  await waitFor(
    () => fetch(`http://127.0.0.1:${port}/healthz`)
      .then((r) => r.ok)
      .catch(() => false),
    { label: "healthz", timeout: 60000 },
  );
  return child;
}

async function main() {
  console.log("e2e root:", ROOT);

  const seeded = spawnSync(PY, [
    path.join("tests", "e2e", "_qb_manage_seed.py"), ROOT, SEED_DIR,
  ], { cwd: REPO, encoding: "utf-8" });
  run_check: {
    check("播种固定候选", seeded.status === 0, seeded.stdout + seeded.stderr);
  }

  const port = await freePort();
  await startServer(port);
  const base = `http://127.0.0.1:${port}`;
  const browser = liveBrowser = await puppeteer.launch({
    headless: true,
    args: [
      "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
      "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
    ],
  });
  const context = await browser.createBrowserContext();
  const page = await context.newPage();
  await page.setViewport({ width: 1500, height: 1000 });
  page.on("dialog", (dialog) => dialog.accept(""));
  page.on("pageerror", (error) => failures.push(`pageerror: ${error}`));
  page.on("response", async (response) => {
    if (response.url().includes("/audio") && response.status() >= 300) {
      const body = await response.text().catch(() => "");
      console.log("AUDIO RESP:", response.status(), body.slice(0, 200));
    }
  });
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const url = request.url();
    if (url.startsWith(base) || url.startsWith("data:")) request.continue();
    else request.abort();
  });
  await page.goto(`${base}/`, { waitUntil: "domcontentloaded", timeout: 60000 });

  const text = (sel) => page.$eval(sel, (n) => n.textContent);
  const noteScope = async (noteId) =>
    (await page.$(`.explanation-approach[data-note-id="${noteId}"]`))
      ? `.explanation-approach[data-note-id="${noteId}"]` : null;
  const scopeMessage = async (noteId) => {
    const sel = await noteScope(noteId);
    return sel ? await page.$eval(`${sel} .explanation-message`, (n) => n.textContent) : "";
  };

  // ---- 1. 卡片渲染与派生小问；能力提示（无密钥） ----
  await waitFor(() => page.$eval("#explanations-body .explanation-subquestion",
    (n) => Boolean(n)), { label: "explanations card" });
  const subqCount = await page.$$eval(".explanation-subquestion", (ns) => ns.length);
  check("派生两个小问（题干含（1）（2））", subqCount === 2, String(subqCount));
  const hint = await text("#explanations-hint");
  check("能力提示区分 ASR 未配置", hint.includes("ASR 未配置"), hint.slice(-60));
  const rowText = await page.$eval("#question-list .question-row", (n) => n.textContent);
  check("列表缺讲解徽标", rowText.includes("缺讲解"), rowText);

  // ---- 2. 手工闭环：新建 → 失焦保存 → 批准门槛 → 批准 ----
  await page.click(".explanation-subquestion[data-subquestion-id='sq1'] .ghost-button");
  const noteId = await waitFor(async () => {
    const ids = await page.$$eval(".explanation-approach", (ns) => ns.map((n) => n.dataset.noteId));
    return ids[0];
  }, { label: "note created" });
  check("新建讲解-解答对", Boolean(noteId), String(noteId));
  // 批准按钮缺正文时不出现
  check("缺正文时不显示批准按钮",
    !(await page.$(`.explanation-approach[data-note-id="${noteId}"] .approve-button`)));
  // 失焦保存讲解
  await page.focus(`.explanation-approach[data-note-id="${noteId}"] .explanation-textarea`);
  await page.type(`.explanation-approach[data-note-id="${noteId}"] .explanation-textarea`,
    "看结构：有对称轴，先配方。");
  await page.click("#item-title");  // 失焦触发保存
  await waitFor(async () => (await scopeMessage(noteId)).includes("已完成"), { label: "explanation saved" });
  check("讲解失焦保存为新版本", true);
  // 解答
  const areas = await page.$$eval(
    `.explanation-approach[data-note-id="${noteId}"] .explanation-textarea`, (ns) => ns.length);
  check("讲解+解答两个编辑区", areas === 2, String(areas));
  await page.evaluate((id) => {
    const areas = document.querySelectorAll(
      `.explanation-approach[data-note-id="${id}"] .explanation-textarea`);
    areas[1].focus();
    areas[1].value = "解：配方得对称轴 $x=2$。";
    areas[1].dispatchEvent(new Event("input", { bubbles: true }));
    areas[1].blur();
  }, noteId);
  // 徽标状态机：只存讲解 → 出现缺解答；补存解答 → 徽标消失、批准按钮出现
  await waitFor(async () => (await page.$eval("#question-list .question-row",
    (n) => n.textContent)).includes("缺解答"), { label: "solution-missing badge appears" });
  check("只存讲解时列表出现缺解答徽标", true);
  // 批准讲解对（等批准按钮出现＝两块正文都已保存）
  await waitFor(() => page.$(`.explanation-approach[data-note-id="${noteId}"] .approve-button`),
    { label: "approve button ready" });
  await page.evaluate((id) => {
    document.querySelector(`.explanation-approach[data-note-id="${id}"] .approve-button`).click();
  }, noteId);
  await waitFor(async () => {
    const chip = await page.$eval(`.explanation-approach[data-note-id="${noteId}"] .chip-note-approved`,
      (n) => n.textContent).catch(() => "");
    return chip.includes("已批准");
  }, { label: "note approved chip" });
  check("讲解对批准徽标", true);
  const badgesAfter = await page.$eval("#question-list .question-row", (n) => n.textContent);
  // 小问覆盖口径：sq1 已覆盖，仅剩 sq2 缺解答 → 计数收敛为 ×1
  check("补存解答后缺解答计数收敛为 ×1", badgesAfter.includes("缺解答×1"), badgesAfter);
  // ---- 3. 题目未审核时导出：pending 提示；题目通过后导出成功 ----
  // 固定顶栏可能遮挡卡片头部按钮：先滚动到视口中央再点击。
  await page.evaluate(() => document.querySelector("#export-blueprints")
    .scrollIntoView({ block: "center" }));
  await page.click("#export-blueprints");
  await sleep(500);
  console.log("export msg early:", await text("#explanations-message"));
  try {
    await waitFor(async () => (await text("#explanations-message")).includes("等待题目人工审核"),
      { label: "pending item review message" });
  } catch (error) {
    console.log("EXPORT MSG DUMP:", await text("#explanations-message"));
    throw error;
  }
  check("题目未审核时导出提示等待题目审核", true);
  // 题目当前修订可能已被前序 e2e 流程动过：直接用 API 检查后按需批准
  const detail = await page.evaluate(async (url) => (await fetch(url, { cache: "no-store" })).json(),
    `${base}/api/items/2026-DEMO-Q021`);
  if (detail.review_state !== "approved") {
    await page.evaluate(async (url, rev) => {
      await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: "approved", note: "", base_revision: rev,
          operation_id: `e2e-note-approve-${Date.now()}` }),
      });
    }, `${base}/api/items/2026-DEMO-Q021/review`, detail.revision);
  }
  await page.reload({ waitUntil: "domcontentloaded", timeout: 60000 });
  await waitFor(() => page.$eval("#explanations-body .explanation-subquestion",
    (n) => Boolean(n)), { label: "card after reload" });
  await page.evaluate(() => document.querySelector("#export-blueprints")
    .scrollIntoView({ block: "center" }));
  await page.click("#export-blueprints");
  await waitFor(async () => (await text("#explanations-message")).includes("已导出 1 个 candidate"),
    { label: "export success" });
  const exportMsg = await text("#explanations-message");
  check("导出 1 个 candidate 且说明外部未配置",
    exportMsg.includes("外部 teaching-tools 根未配置") && exportMsg.includes("本地快照"),
    exportMsg.slice(0, 120));

  // ---- 4. 题干修订 → 来源已变化；确认适配恢复 ----
  await page.evaluate(() => document.querySelector("#edit-stem")
    .scrollIntoView({ block: "center" }));
  await page.click("#edit-stem");
  await page.waitForSelector("#editor-stem.open");
  await page.focus("#editor-stem-text");
  await page.keyboard.press("End");
  await page.type("#editor-stem-text", "\n\n（1）追加一句改变小问内容");
  await page.click("#save-btn");
  await waitFor(() => page.$eval("#manage-message", (n) => n.textContent.includes("已保存")),
    { label: "stem saved" });
  await sleep(600);
  const staleChip = await page.$eval(
    `.explanation-approach[data-note-id="${noteId}"] .chip-note-stale`, (n) => n.textContent)
    .catch(() => "");
  check("题干修订后讲解对标来源已变化", staleChip.includes("来源已变化"), staleChip);
  const listBadge = await page.$eval("#question-list .question-row", (n) => n.textContent);
  check("列表讲义过期徽标", listBadge.includes("讲义过期"), listBadge);
  await page.evaluate((id) => {
    const scope = document.querySelector(`.explanation-approach[data-note-id="${id}"]`);
    [...scope.querySelectorAll("button")].find((b) => b.textContent.includes("确认适配")).click();
  }, noteId);
  await waitFor(async () => {
    const gone = await page.$(
      `.explanation-approach[data-note-id="${noteId}"] .chip-note-stale`);
    return !gone;
  }, { label: "readapt clears stale" });
  check("确认适配后来源恢复且需重新批准", true);

  // ---- 5. 假麦克风录音：无密钥 → 音频保存 + 明确失败 ----
  await page.evaluate((id) => {
    document.querySelector(`.explanation-approach[data-note-id="${id}"] .explanation-record-button`).click();
  }, noteId);
  await waitFor(() => page.$eval(".explanation-record-button.is-recording",
    (n) => n.textContent.includes("停止")), { label: "recording started" });
  check("假麦克风开始录音", true);
  await sleep(1200);
  await page.evaluate((id) => {
    document.querySelector(`.explanation-approach[data-note-id="${id}"] .explanation-record-button`).click();
  }, noteId);
  await waitFor(async () => (await scopeMessage(noteId)).includes("音频已保存"),
    { label: "audio saved with clear asr failure" });
  const recMsg = await scopeMessage(noteId);
  check("无密钥时录音保存+转写失败提示", recMsg.includes("转写失败"), recMsg);
  const hasAudio = await page.$(`.explanation-approach[data-note-id="${noteId}"] audio`);
  check("录音可在页面回放（audio 元素）", Boolean(hasAudio));
  // 数据侧：资产为 audio mime
  const audioOk = await page.evaluate(async (id) => {
    const view = await (await fetch(`/api/items/2026-DEMO-Q021/notes`, { cache: "no-store" })).json();
    for (const subq of view.subquestions) {
      for (const note of subq.notes) {
        if (note.note_id === id && note.audio_asset_id) {
          const head = await fetch(`/api/assets/${note.audio_asset_id}/audio`);
          return head.ok && (head.headers.get("content-type") || "").startsWith("audio/");
        }
      }
    }
    return false;
  }, noteId);
  check("录音资产接口可回放", audioOk);
  await page.screenshot({ path: path.join(SHOTS, "02-recording-saved.png"), fullPage: true });

  // ---- 5b. 权限拒绝：无 fake-ui 标志的独立浏览器（权限默认拒）→ 明确报错 ----
  // 注：--use-fake-ui-for-media-stream 是浏览器级标志，主浏览器所有 context
  // 都会自动授权；权限拒绝路径必须用单独启动的浏览器验证。
  const deniedBrowser = await puppeteer.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
  });
  const deniedContext = await deniedBrowser.createBrowserContext();
  const deniedPage = await deniedContext.newPage();
  await deniedPage.setViewport({ width: 1400, height: 900 });
  deniedPage.on("dialog", (dialog) => dialog.accept(""));
  deniedPage.on("pageerror", (error) => failures.push(`denied pageerror: ${error}`));
  await deniedPage.setRequestInterception(true);
  deniedPage.on("request", (request) => {
    const url = request.url();
    if (url.startsWith(base) || url.startsWith("data:")) request.continue();
    else request.abort();
  });
  await deniedPage.evaluateOnNewDocument(() => {
    window.__deniedAudioPosts = 0;
    const original = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const url = typeof input === "string" ? input : input.url;
      if (String(url).includes("/audio") && String(init?.method || "GET").toUpperCase() === "POST") {
        window.__deniedAudioPosts += 1;
      }
      return original(input, init);
    };
  });
  await deniedPage.goto(`${base}/`, { waitUntil: "domcontentloaded", timeout: 60000 });
  await waitFor(() => deniedPage.$eval("#bank-select", (n) => !n.disabled),
    { label: "denied context loaded" });
  await deniedPage.click("#question-list .question-row");
  await deniedPage.waitForSelector(".explanation-approach");
  const deniedNoteId = await deniedPage.$eval(".explanation-approach",
    (n) => n.dataset.noteId);
  await deniedPage.evaluate((id) => {
    document.querySelector(`.explanation-approach[data-note-id="${id}"] .explanation-record-button`).click();
  }, deniedNoteId);
  const deniedMsg = await waitFor(async () => deniedPage.$eval(
    `.explanation-approach[data-note-id="${deniedNoteId}"] .explanation-message`,
    (n) => n.textContent).catch(() => ""), { label: "permission denied message" });
  check("麦克风权限被拒时明确报错",
    deniedMsg.includes("无法开始录音") || deniedMsg.includes("不支持录音"),
    deniedMsg);
  const deniedAudioPosts = await deniedPage.evaluate(() => window.__deniedAudioPosts || 0);
  check("权限拒绝不产生音频上传", deniedAudioPosts === 0, String(deniedAudioPosts));
  await deniedContext.close();
  await deniedBrowser.close();

  // ---- 6. 软删除 ----
  await page.evaluate((id) => {
    const scope = document.querySelector(`.explanation-approach[data-note-id="${id}"]`);
    [...scope.querySelectorAll("button")].find((b) => b.textContent.trim() === "删除").click();
  }, noteId);
  await waitFor(async () => !(await page.$(`.explanation-approach[data-note-id="${noteId}"]`)),
    { label: "note removed from view" });
  check("软删除后视图移除", true);

  await browser.close();
  for (const server of liveServers) server.kill();
  await sleep(500);
  try { fs.rmSync(TMP_BASE, { recursive: true, force: true }); }
  catch (_) { /* Windows 句柄延迟 */ }

  if (failures.length) {
    console.log(`\n${failures.length} 项失败:`);
    failures.forEach((f) => console.log(` - ${f}`));
    process.exit(1);
  }
  console.log("\n全部通过");
}

process.on("exit", () => {
  try { if (liveBrowser) liveBrowser.close(); } catch (_) { /* ignore */ }
  for (const server of liveServers) { try { server.kill(); } catch (_) { /* ignore */ } }
  try { fs.rmSync(TMP_BASE, { recursive: true, force: true }); } catch (_) { /* ignore */ }
});

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
