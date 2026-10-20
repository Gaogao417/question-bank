/* 题库管理（数据库修订闭环）前端。
 *
 * 页面骨架、CSS 与交互沿用现有 Review UI（question-bank-review）：
 * 题目列表 + 阅读栏（题目/题图/答案/解答）+ 疑点 + 人工审核（A/R、←→）
 * + 来源灯箱。按设计文档补齐：三块正文编辑、图片绑定调整、发布/导出。
 * 保存走 base_revision 乐观并发，只有服务端成功回执才更新本地修订号；
 * 冲突时保留未保存修改并弹出比较。
 *
 * 老版查卷与连续审核体验（2026-09 复刻）：
 * - 试卷目录 /api/papers：年份、年级、试卷类型筛选 + 名称检索 + facets，
 *   进度统计基于当前修订（approved/rejected/pending/stale 互斥）。
 * - 切卷只拉当前卷目录，切题按需取详情（缓存 + 邻题预取 + 请求竞态保护）。
 * - 审核成功后自动跳到下一道待审题；全卷通过（Shift+A）部分成功语义，
 *   失败留卷，全部通过后跳筛选结果中的下一份未完成卷。
 */
"use strict";

const state = {
  papers: [],          // 当前筛选结果（/api/papers）
  paperId: null,
  items: [],           // 当前卷题目目录（/api/items?paper_id=）
  itemFilter: "",      // 卷内题号筛选（本地过滤，不请求）
  detail: null,
  viewRevision: null,
  baseRevision: null,
  draft: null,      // {stem, answer, solution, images, issueOps}
  dirty: false,
  editing: false,
  filterToken: 0,
  filtersAbort: null,
  itemLoadToken: 0,
  itemListToken: 0,    // 单卷目录请求序号：过期响应不得覆盖当前卷
  paperToken: 0,       // 切卷流程序号：被更新的切卷请求取代后，旧流程停止定位
  lastFilterKey: "",   // 最近一次成功应用的筛选参数（回退与 dirty 确认用）
  draftEpoch: 0,       // 草稿代际：任何输入/重置/取消都递增，迟到保存回执据此判断草稿是否仍是提交时那份
  itemCache: new Map(),  // item_id → 当前修订详情
  submittingReview: false,
  approvingPaper: false,
};

const $ = (sel) => document.querySelector(sel);
const badge = (text, cls) => {
  const node = document.createElement("span");
  node.className = cls ? `badge ${cls}` : "badge";
  node.textContent = text;
  return node;
};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  attrs = attrs || {};
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(child));
  }
  return node;
}

function typeset(rootNode) {
  if (window.MathJax?.typesetClear && window.MathJax?.typesetPromise) {
    window.MathJax.typesetClear([rootNode]);
    window.MathJax.typesetPromise([rootNode]).catch((error) =>
      console.warn("MathJax typesetting failed", error));
  }
}

async function api(path, options = {}) {
  // 审核工具的读取必须新鲜：浏览器缓存不得回放旧目录/详情（切卷与审核后状态会变）。
  const response = await fetch(path, { cache: "no-store", ...options });
  if (response.ok) return response.json();
  let detail = null;
  try { detail = await response.json(); } catch (_) { /* 非 JSON 错误体 */ }
  const err = new Error(`HTTP ${response.status}`);
  err.status = response.status;
  err.detail = detail && detail.detail ? detail.detail : detail;
  throw err;
}

function errMessages(err) {
  const errors = err.detail && err.detail.errors;
  if (Array.isArray(errors) && errors.length) {
    return errors.map((e) => `${e.code}: ${e.message}`).join("；");
  }
  return err.message || String(err);
}

function opId(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function setMessage(id, text, isError) {
  const node = $(id);
  node.textContent = text || "";
  node.style.color = isError ? "var(--danger, #b3261e)" : "";
}

// ------------------------------------------------------------------
// 提示音（老版操作反馈）：通过两连音、翻页短噪声
// ------------------------------------------------------------------

let audioContext = null;
function ensureAudioContext() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return null;
  if (!audioContext) audioContext = new AudioContextClass();
  if (audioContext.state === "suspended") void audioContext.resume();
  return audioContext;
}

function playApprovalSound() {
  const context = ensureAudioContext();
  if (!context) return;
  const start = context.currentTime;
  [
    { frequency: 660, offset: 0, duration: 0.1 },
    { frequency: 880, offset: 0.105, duration: 0.16 },
  ].forEach(({ frequency, offset, duration }) => {
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(frequency, start + offset);
    gain.gain.setValueAtTime(0.0001, start + offset);
    gain.gain.exponentialRampToValueAtTime(0.13, start + offset + 0.018);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + offset + duration);
    oscillator.connect(gain).connect(context.destination);
    oscillator.start(start + offset);
    oscillator.stop(start + offset + duration);
  });
}

function playPageTurnSound() {
  const context = ensureAudioContext();
  if (!context) return;
  const duration = 0.16;
  const frameCount = Math.floor(context.sampleRate * duration);
  const buffer = context.createBuffer(1, frameCount, context.sampleRate);
  const samples = buffer.getChannelData(0);
  for (let index = 0; index < frameCount; index += 1) {
    const progress = index / frameCount;
    const envelope = Math.sin(Math.PI * progress) * (1 - 0.55 * progress);
    samples[index] = (Math.random() * 2 - 1) * envelope;
  }
  const source = context.createBufferSource();
  const filter = context.createBiquadFilter();
  const gain = context.createGain();
  source.buffer = buffer;
  filter.type = "bandpass";
  filter.frequency.setValueAtTime(1800, context.currentTime);
  filter.frequency.exponentialRampToValueAtTime(650, context.currentTime + duration);
  filter.Q.value = 0.75;
  gain.gain.setValueAtTime(0.075, context.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + duration);
  source.connect(filter).connect(gain).connect(context.destination);
  source.start();
}

// ------------------------------------------------------------------
// 试卷目录：筛选、检索、进度
// ------------------------------------------------------------------

const REVIEW_STATUS_LABELS = {
  approved: "已通过",
  rejected: "待修改",
  stale: "已过期",
  pending: "待审核",
};

function buildFilterParams() {
  const params = new URLSearchParams();
  const grade = $("#filter-grade").value;
  const year = $("#filter-year").value;
  const examType = $("#filter-exam-type").value;
  const query = $("#search-input").value.trim();
  if (grade) params.set("grade", grade);
  if (year) params.set("year", year);
  if (examType) params.set("exam_type", examType);
  if (query) params.set("q", query);
  return params;
}

function restoreFiltersFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const q = params.get("q");
  if (q) $("#search-input").value = q;
  const year = params.get("year");
  if (year) $("#filter-year").value = year;
  const grade = params.get("grade");
  if (grade) $("#filter-grade").value = grade;
  const examType = params.get("exam_type");
  if (examType) $("#filter-exam-type").value = examType;
}

// 把筛选参数写回输入控件（用户拒绝"调整筛选丢弃草稿"时回退用）。
function applyFilterKeyToInputs(key) {
  const params = new URLSearchParams(key);
  $("#search-input").value = params.get("q") || "";
  const yearSelect = $("#filter-year");
  yearSelect.value = params.get("year") || "";
  if (yearSelect.value !== (params.get("year") || "")) {
    // facets 尚未包含该值时补一个临时选项，保证回退后控件状态一致
    yearSelect.append(el("option", { value: params.get("year") }, params.get("year")));
    yearSelect.value = params.get("year");
  }
  $("#filter-grade").value = params.get("grade") || "";
  $("#filter-exam-type").value = params.get("exam_type") || "";
}

function syncFiltersToUrl() {
  const url = new URL(window.location.href);
  const params = buildFilterParams();
  ["grade", "year", "exam_type", "q"].forEach((key) => {
    if (params.has(key)) url.searchParams.set(key, params.get(key));
    else url.searchParams.delete(key);
  });
  window.history.replaceState({}, "", url);
}

function fillFacetOptions(select, values) {
  // facets 的年份是数字、其余是字符串：统一按字符串比较，避免选中值被重置。
  const keepValue = String(select.value);
  const normalized = (values || []).map(String);
  select.replaceChildren();
  select.append(el("option", { value: "" }, "全部"));
  normalized.forEach((value) => select.append(el("option", { value }, value)));
  if (normalized.includes(keepValue)) select.value = keepValue;
}

function applyFacets(facets) {
  // facets 失败或为空不阻断主流程；筛选框保持可用。
  fillFacetOptions($("#filter-year"), facets.years || []);
  fillFacetOptions($("#filter-grade"), facets.grades || []);
  $("#filter-grade").disabled = false;
  $("#filter-year").disabled = false;
  $("#filter-exam-type").disabled = false;
}

function paperLabel(paper) {
  const meta = paper.meta || {};
  const name = paper.title
    ? `${paper.paper_id} · ${paper.title}`
    : paper.paper_id;
  const tail = [meta.year, meta.exam_type, meta.district_label, meta.grade]
    .filter(Boolean).join("·");
  return `试卷｜${name}（${paper.stats.total_received} 题）${tail ? `｜${tail}` : ""}`;
}

function paperMetaLine(paper) {
  const stats = paper.stats;
  const parts = [
    `已入库 ${stats.total_received}`,
    `通过 ${stats.approved}`,
    `待审 ${stats.pending}`,
  ];
  if (stats.rejected) parts.push(`驳回 ${stats.rejected}`);
  if (stats.stale) parts.push(`过期 ${stats.stale}`);
  if (stats.blocked) parts.push(`阻断 ${stats.blocked}`);
  if (stats.published_current) parts.push(`发布 ${stats.published_current}`);
  let line = `${paper.title || paper.paper_id} · ${parts.join(" · ")}`;
  if (paper.expected_state !== "confirmed") {
    line += "（原卷题数未确认，进度只覆盖已入库题）";
  } else if (paper.missing_count) {
    line += `（对照预期缺 ${paper.missing_count} 题）`;
  }
  return line;
}

function recomputePaperStats(paperId) {
  // 目录内单卷统计的本地重算：review-all / 单题审核后不重拉目录也能更新进度。
  const entry = state.papers.find((p) => p.paper_id === paperId);
  if (!entry) return;
  const stats = {
    total_received: state.items.length,
    pending: 0, approved: 0, rejected: 0, stale: 0,
    blocked: 0, published_current: 0,
  };
  for (const item of state.items) {
    stats[item.review_status] += 1;
    if (item.open_blocking_issues) stats.blocked += 1;
    if (item.published_revision) stats.published_current += 1;
  }
  entry.stats = stats;
}

function updatePaperMeta() {
  const entry = state.papers.find((p) => p.paper_id === state.paperId);
  $("#bank-meta").textContent = entry ? paperMetaLine(entry) : "";
}

function renderBankList() {
  const select = $("#bank-select");
  select.replaceChildren();
  if (!state.papers.length) {
    select.append(el("option", { value: "" }, "没有匹配的试卷"));
    $("#bank-meta").textContent = "";
    return;
  }
  state.papers.forEach((paper) => {
    select.append(el("option", {
      value: paper.paper_id,
      selected: paper.paper_id === state.paperId ? "" : null,
    }, paperLabel(paper)));
  });
}

function paperOfItem(itemId) {
  const match = itemId.match(/^(.*)-Q\d+$/);
  return match ? match[1] : null;
}

function confirmDiscardDirty(actionText) {
  if (!state.dirty) return true;
  if (!confirm(`有未保存的修改，${actionText}将丢失。继续？`)) return false;
  state.dirty = false;  // 用户已确认丢弃；后续 loadItem 不再二次确认
  return true;
}

async function applyFilters({ initial = false, quiet = false, preferHash = false } = {}) {
  const params = buildFilterParams();
  const filterKey = params.toString();
  // 筛选变化可能离开当前卷：dirty 时先确认，拒绝则回退到上一次成功的筛选。
  if (!initial && !quiet && state.dirty && filterKey !== state.lastFilterKey) {
    if (!confirm(`调整筛选可能离开当前试卷，未保存的修改将丢失。继续？`)) {
      applyFilterKeyToInputs(state.lastFilterKey);
      return;
    }
    state.dirty = false;  // 已确认丢弃
  }
  const dirtyAtSubmit = state.dirty;
  const token = ++state.filterToken;
  const select = $("#bank-select");
  if (!quiet) select.disabled = true;
  if (state.filtersAbort) state.filtersAbort.abort();
  state.filtersAbort = new AbortController();
  try {
    const data = await api(`/api/papers?${filterKey}`, {
      signal: state.filtersAbort.signal,
    });
    if (token !== state.filterToken) { select.disabled = false; return; }
    // 请求等待期间新建了草稿：应用结果（尤其空结果清视图）前必须再次确认。
    if (!initial && !quiet && state.dirty && !dirtyAtSubmit) {
      if (!confirm("筛选结果将离开当前题目，未保存的修改将丢失。继续？")) {
        applyFilterKeyToInputs(state.lastFilterKey);
        select.disabled = false;
        return;
      }
      state.dirty = false;  // 已确认丢弃
    }
    applyFacets(data.facets || {});
    state.papers = data.papers || [];
    renderBankList();
    syncFiltersToUrl();
    if (!state.papers.length) {
      // 空结果：清掉旧卷视图并失效在途加载，只显示空态；旧题不可再审核。
      state.paperId = null;
      state.items = [];
      state.detail = null;
      state.paperToken += 1;
      state.itemListToken += 1;
      state.itemLoadToken += 1;
      showSkeleton(false);
      $("#review-layout").hidden = true;
      $("#topic-summary").textContent = "尚未选择";
      $("#position-summary").textContent = "—";
      $("#page-status").textContent = filterKey
        ? "没有匹配的试卷，可调整筛选项或检索词。"
        : "数据库暂无试卷：先用 CLI / agent 提交候选包。";
      $("#page-status").hidden = false;
      $("#bank-meta").textContent = "";
      select.disabled = false;
      state.lastFilterKey = filterKey;
      return;
    }
    state.lastFilterKey = filterKey;
    $("#review-layout").hidden = false;
    // 当前卷仍在结果集中则保留（不打断到第 1 题）；否则优先 hash 指向的卷。
    const hashItem = location.hash.match(/^#item=([^&]+)/);
    const hashPaper = hashItem ? paperOfItem(decodeURIComponent(hashItem[1])) : null;
    const keepCurrent = !preferHash && state.paperId
      && state.papers.some((p) => p.paper_id === state.paperId);
    const target = keepCurrent ? state.paperId
      : (hashPaper && state.papers.some((p) => p.paper_id === hashPaper) ? hashPaper
        : state.papers[0].paper_id);
    select.disabled = false;
    if (target !== state.paperId || initial) {
      await selectPaper(target, { initial });
    } else {
      select.value = state.paperId;
      updatePaperMeta();
    }
    $("#page-status").hidden = Boolean(state.detail) || state.items.length > 0;
  } catch (error) {
    select.disabled = false;
    if (error.name === "AbortError") return;
    if (token !== state.filterToken) return;
    $("#page-status").textContent = `试卷目录加载失败：${errMessages(error)}。请刷新页面重试。`;
    $("#page-status").hidden = false;
  }
}

async function selectPaper(paperId, { initial = false } = {}) {
  if (state.paperId === paperId && state.items.length) {
    $("#bank-select").value = paperId;
    updatePaperMeta();
    return;
  }
  if (!initial && !confirmDiscardDirty("切换试卷")) {
    $("#bank-select").value = state.paperId || "";
    return;
  }
  // 切卷流程令牌：更快的新切卷取代本流程后，旧流程不得再定位/加载。
  const token = ++state.paperToken;
  // 切卷即作废旧卷一切在途状态：详情响应（含慢响应）不得再渲染或审核，
  // 旧目录/操作区立即失效，直至新卷目录加载完成。
  state.paperId = paperId;
  state.itemLoadToken += 1;
  state.itemListToken += 1;
  state.detail = null;
  state.items = [];
  renderItemList();  // 同步清掉旧卷题目列表 DOM，失败/慢目录期间不残留旧卷行
  $("#bank-select").value = paperId;
  $("#item-id").textContent = "—";
  $("#item-title").textContent = "正在载入…";
  $("#topic-summary").textContent = paperId;
  $("#position-summary").textContent = "—";
  setReviewControlsDisabled(true);
  showSkeleton(true);
  updatePaperMeta();
  try {
    await loadItemList();
  } finally {
    if (token === state.paperToken) showSkeleton(false);
  }
  if (token !== state.paperToken) return;
  if (!state.items.length) {
    state.detail = null;
    $("#page-status").textContent = "该试卷暂无已入库题目。";
    $("#page-status").hidden = false;
    return;
  }
  // 定位：hash 深链 > 第一道待审题 > 第 1 题。
  const hashMatch = location.hash.match(/^#item=([^&]+)(?:&revision=(\d+))?/);
  const hashItemId = hashMatch ? decodeURIComponent(hashMatch[1]) : null;
  const hashInPaper = hashItemId && state.items.find((i) => i.item_id === hashItemId);
  const target = hashInPaper
    || state.items.find((i) => needsAttention(i))
    || visibleItems()[0];
  if (target) {
    await navigateToItem(target.item_id,
      hashInPaper && hashMatch[2] ? Number(hashMatch[2]) : null);
  }
}

async function navigateToItem(itemId, revision = null) {
  const targetHash = `#item=${itemId}${revision ? `&revision=${revision}` : ""}`;
  if (location.hash === targetHash) await loadItem(itemId, revision);
  else location.hash = targetHash;  // hashchange → route → loadItem
}

// ------------------------------------------------------------------
// 题目目录与列表
// ------------------------------------------------------------------

function needsAttention(item) {
  return item.review_status === "pending" || item.review_status === "stale";
}

function visibleItems() {
  const query = state.itemFilter.trim().toLowerCase();
  if (!query) return state.items;
  return state.items.filter((item) =>
    `${item.question_number} ${item.item_id}`.toLowerCase().includes(query));
}

async function loadItemList() {
  // 目录响应必须仍属于当前卷且是最新请求；否则丢弃（旧响应不得覆盖当前卷）。
  const paperId = state.paperId;
  const token = ++state.itemListToken;
  const data = await api(`/api/items?paper_id=${encodeURIComponent(paperId)}`);
  if (token !== state.itemListToken || state.paperId !== paperId) return;
  state.items = data.items || [];
  renderItemList();
}

function renderItemList() {
  const list = $("#question-list");
  list.replaceChildren();
  const visible = visibleItems();
  $("#item-count").textContent = visible.length === state.items.length
    ? `${state.items.length} 题`
    : `${visible.length} / ${state.items.length} 题`;
  for (const item of visible) {
    const badges = [badge(REVIEW_STATUS_LABELS[item.review_status] || item.review_status,
      item.review_status === "approved" ? "attribution-pending-badge" : null)];
    badges.push(badge(`r${item.revision ?? "—"}`));
    if (item.open_blocking_issues) badges.push(badge(`疑点 ${item.open_blocking_issues}`));
    if (item.published_revision) badges.push(badge(`发布 r${item.published_revision}`));
    const notes = item.notes_summary;
    if (notes) {
      if (notes.missing_explanation) badges.push(badge(
        notes.missing_explanation > 1 ? `缺讲解×${notes.missing_explanation}` : "缺讲解",
        "chip-note-stale"));
      if (notes.missing_solution_count) badges.push(badge(`缺解答×${notes.missing_solution_count}`));
      if (notes.stale_notes) badges.push(badge(`讲义过期×${notes.stale_notes}`));
    }
    list.append(el("button", {
      type: "button",
      class: "question-row",
      "data-item-id": item.item_id,
      onclick: () => { location.hash = `item=${item.item_id}`; },
    },
      el("span", { class: "row-heading" },
        el("span", { class: "row-id" }, `第${item.question_number}题`),
        el("span", { class: "row-title" }, item.item_id)),
      el("span", { class: "row-meta" }, ...badges)));
  }
  renderListActive();
}

function renderListActive() {
  const current = state.detail?.item_id;
  for (const row of document.querySelectorAll("#question-list .question-row")) {
    row.setAttribute("aria-current",
      String(Boolean(current && row.dataset.itemId === current)));
  }
  if (current) {
    const paper = state.papers.find((p) => p.paper_id === state.paperId);
    $("#topic-summary").textContent = paper ? (paper.title || paper.paper_id) : state.paperId;
    $("#position-summary").textContent = `第${state.detail.identity.question_number}题`;
  }
}

// ------------------------------------------------------------------
// 题目加载与渲染（懒加载 + 竞态保护 + 邻题预取）
// ------------------------------------------------------------------

function showSkeleton(show) {
  $("#item-skeleton").hidden = !show;
}

async function fetchDetail(itemId, revision = null) {
  return api(`/api/items/${encodeURIComponent(itemId)}${revision ? `?revision=${revision}` : ""}`);
}

function prefetchNeighbors() {
  const index = state.items.findIndex((i) => i.item_id === state.detail?.item_id);
  if (index < 0) return;
  for (const delta of [1, -1]) {
    const neighbor = state.items[index + delta];
    if (neighbor && !state.itemCache.has(neighbor.item_id)) {
      fetchDetail(neighbor.item_id)
        .then((detail) => state.itemCache.set(neighbor.item_id, detail))
        .catch(() => { /* 预取失败不影响浏览 */ });
    }
  }
}

async function loadItem(itemId, revision = null) {
  if (!confirmDiscardDirty("切换题目")) return false;
  stopAssistRecording(true);
  const token = ++state.itemLoadToken;
  showSkeleton(true);
  $("#load-error").hidden = true;
  let detail;
  if (!revision && state.itemCache.has(itemId)) {
    detail = state.itemCache.get(itemId);
  } else {
    try {
      detail = await fetchDetail(itemId, revision);
    } catch (err) {
      if (token !== state.itemLoadToken) return false;
      showSkeleton(false);
      $("#load-error").hidden = false;
      $("#load-error").textContent = `加载失败：${errMessages(err)}`;
      return false;
    }
    if (!revision) state.itemCache.set(itemId, detail);
  }
  if (token !== state.itemLoadToken) return false;  // 已切到别的题/卷，丢弃旧响应
  showSkeleton(false);
  state.detail = detail;
  state.viewRevision = detail.revision;
  state.baseRevision = detail.revision;
  state.dirty = false;
  state.editing = false;
  state.draft = {
    stem: detail.body.stem,
    answer: detail.body.answer,
    solution: detail.body.solution,
    images: detail.images.map((b) => ({ ...b })),
    issueOps: {},
  };
  state.draftEpoch += 1;  // 新草稿：使在途保存回执的代际比较失效
  resetAssistWizard(detail);
  renderDetail();
  renderListActive();
  prefetchNeighbors();
  return true;
}

function renderDetail() {
  const d = state.detail;
  $("#review-layout").hidden = false;
  $("#page-status").hidden = true;
  $("#load-error").hidden = true;
  $("#item-id").textContent = d.item_id;
  $("#item-title").textContent =
    `第 ${d.identity.question_number} 题 · 修订 r${d.revision}`;
  const badges = $("#item-badges");
  badges.innerHTML = "";
  badges.append(badge(`hash ${d.content_hash.slice(7, 19)}`));
  badges.append(badge(`当前 r${d.current_revision}`));
  if (!d.is_current) badges.append(badge(`正在查看 r${d.revision}（历史）`, "attribution-pending-badge"));
  const reviewStatus = $("#review-status");
  reviewStatus.dataset.status = d.review_state || "";
  reviewStatus.textContent =
    d.review_state === "approved" ? `已批准 r${d.revision}`
    : d.review_state === "rejected" ? `已驳回 r${d.revision}`
    : "待复核";

  const stale = $("#stale-banner");
  stale.innerHTML = "";
  if (!d.is_current) {
    stale.append(el("div", { class: "qbm-banner" },
      `正在查看历史修订 r${d.revision}（内容只读）。`,
      el("button", { type: "button", class: "ghost-button", onclick: () => {
        location.hash = `item=${d.item_id}`;
      } }, "回到当前修订")));
  }

  renderAssistFlow();
  renderBodyBlocks();
  renderImages();
  renderProposals();
  renderPublication();
  renderReviewCard();
  loadNotes(d.item_id);
}

const FIELD_LABELS = { stem: "题目", answer: "答案", solution: "解答" };

async function renderProposals() {
  const section = $("#proposals-section");
  const list = $("#proposal-list");
  const d = state.detail;
  if (!d) return;
  list.innerHTML = "";
  let proposals = [];
  try {
    const data = await api(`/api/items/${encodeURIComponent(d.item_id)}/proposals`);
    proposals = data.proposals || [];
  } catch (err) {
    section.hidden = true;
    return;
  }
  section.hidden = proposals.length === 0;
  if (proposals.length === 0) return;
  const open = proposals.filter((p) => p.status === "open").length;
  $("#proposals-summary").textContent = `${open} 条待确认`;
  const archive = el("details", { class: "proposal-history" },
    el("summary", {}, `已处理建议（${proposals.length - open}）`));
  for (const p of proposals) {
    const rows = [];
    for (const field of ["stem", "answer", "solution"]) {
      if (!(field in (p.patch || {}))) continue;
      const changed = p.patch[field] !== d.body[field];
      rows.push(el("div", { class: "proposal-diff-row" },
        el("span", { class: "proposal-diff-field" }, FIELD_LABELS[field] || field),
        changed
          ? el("details", { class: "proposal-diff" },
              el("summary", {}, changed ? "有差异（点开比较）" : "与当前一致"),
              el("div", { class: "proposal-diff-before" },
                el("strong", {}, "当前："), el("pre", {}, d.body[field])),
              el("div", { class: "proposal-diff-after" },
                el("strong", {}, "建议："), el("pre", {}, p.patch[field])))
          : el("span", { class: "proposal-same" }, "与当前一致")));
    }
    const actions = [];
    if (p.status === "open") {
      const staleBase = p.base_revision !== d.current_revision;
      actions.push(el("span", { class: "proposal-stale-note" },
        staleBase ? `基于 r${p.base_revision}，当前已是 r${d.current_revision}（接受将过期）` : `基于 r${p.base_revision}`));
      if (d.is_current) {
        actions.push(el("button", {
          type: "button", class: "approve-button",
          title: "接受建议并保存为新修订；向导收集的疑点处置随本次保存生效",
          onclick: async () => {
            try {
              if (state.dirty) {
                setMessage("#proposal-message", "请先保存或取消正文修改，再接受 AI 建议。", true);
                return;
              }
              const issueOps = assistIssueOpsForAccept(p.proposal_id);
              await api(`/api/proposals/${encodeURIComponent(p.proposal_id)}/accept`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  operation_id: opId("accept-proposal"),
                  ...(issueOps.length ? { issue_ops: issueOps } : {}),
                }),
              });
              setMessage("#proposal-message",
                `建议已接受并保存为新修订${issueOps.length ? "，对应疑点已处理" : ""}。`);
              state.itemCache.delete(d.item_id);
              await loadItem(d.item_id);
            } catch (err) {
              setMessage("#proposal-message", `接受失败：${errMessages(err)}`, true);
              state.itemCache.delete(d.item_id);
              await loadItem(d.item_id);
            }
          },
        }, "接受并保存"));
        actions.push(el("button", {
          type: "button", class: "ghost-button",
          title: "不采用这条建议",
          onclick: async () => {
            try {
              await api(`/api/proposals/${encodeURIComponent(p.proposal_id)}/reject`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ reason: "UI 驳回" }),
              });
              setMessage("#proposal-message", "建议已驳回。");
              await loadItem(d.item_id);
            } catch (err) {
              setMessage("#proposal-message", `驳回失败：${errMessages(err)}`, true);
            }
          },
        }, "驳回"));
        actions.push(el("button", {
          type: "button", class: "ghost-button",
          title: "回到修改向导补充意见，建议保持待确认",
          onclick: () => {
            assist.phase = "extra";
            renderAssistFlow();
          },
        }, "继续提意见"));
      }
    } else {
      actions.push(el("span", { class: "proposal-stale-note" },
        `${p.status}${p.accepted_revision ? ` · 修订 r${p.accepted_revision}` : ""}`));
    }
    (p.status === "open" ? list : archive).append(el("article", { class: `proposal-card proposal-${p.status}` },
      el("header", { class: "proposal-card-header" },
        el("span", { title: p.proposal_id }, `基于修订 r${p.base_revision}`),
        el("span", { class: "proposal-status" }, ({open:"待确认", accepted:"已采用", rejected:"已驳回", expired:"已过期"})[p.status] || p.status)),
      p.reason ? el("p", { class: "proposal-reason" }, p.reason) : null,
      ...rows,
      el("div", { class: "proposal-actions" }, ...actions)));
  }
  if (proposals.length > open) list.append(archive);
  const msg = $("#proposal-message");
  if (!msg) {
    list.after(el("p", { id: "proposal-message", class: "review-message", role: "status" }, ""));
  }
}

function renderBodyBlocks() {
  const d = state.detail;
  const editing = state.editing && d.is_current;
  for (const field of ["stem", "answer", "solution"]) {
    const view = $(`#${field}`);
    const editor = $(`#editor-${field}`);
    const textarea = $(`#editor-${field}-text`);
    const button = $(`#edit-${field}`);
    view.textContent = state.draft[field];
    typeset(view);
    textarea.value = state.draft[field];
    editor.classList.toggle("open", editing);
    view.style.display = editing ? "none" : "";
    button.hidden = !d.is_current;
    button.textContent = editing ? "收起" : "编辑";
    button.onclick = () => toggleEditor(field);
  }
  renderEvidenceCapsules();
  updateSaveBar();
}

function toggleEditor(field) {
  const editor = $(`#editor-${field}`);
  const opening = !editor.classList.contains("open");
  editor.classList.toggle("open", opening);
  $(`#${field}`).style.display = opening ? "none" : "";
  $(`#edit-${field}`).textContent = opening ? "收起" : "编辑";
  if (!opening) {
    $(`#${field}`).textContent = state.draft[field];
    typeset($(`#${field}`));
    state.editing = state.dirty || Boolean(document.querySelector(".qbm-editor.open"));
    updateSaveBar();
  }
  if (opening) {
    state.editing = true;
    updateSaveBar();
    $(`#editor-${field}-text`).focus();
  }
}

function onBodyInput(field) {
  state.draft[field] = $(`#editor-${field}-text`).value;
  markDirty();
}

function updateSaveBar() {
  const existing = $("#save-bar");
  if (existing) existing.remove();
  if (!state.detail?.is_current || (!state.editing && !state.dirty)) return;
  const bar = el("div", { class: "qbm-editor-actions", id: "save-bar" },
    el("button", {
      type: "button", class: "approve-button", id: "save-btn",
      title: "保存为新修订（快捷键 Ctrl/⌘+S）",
      onclick: saveEdits, disabled: state.dirty ? null : "",
    }, "保存修改"),
    el("button", { type: "button", class: "ghost-button", onclick: cancelEdits }, "取消编辑"),
    el("span", {
      class: "qbm-save-state" + (state.dirty ? " dirty" : ""),
      id: "save-state",
    }, state.dirty ? `未保存修改（基于 r${state.baseRevision}）` : "无修改"));
  $("#save-dock").append(bar);
}

function stripMarkers(key) {
  // 删除图片绑定时同步移除三块正文中的引用标记，避免保存被 IMAGE_MARKER_UNBOUND 拒绝。
  const pattern = /!\[[^\]]*\]\(asset:([A-Za-z0-9_-]+)\)/g;
  for (const field of ["stem", "answer", "solution"]) {
    state.draft[field] = state.draft[field].replace(pattern,
      (marker, markerKey) => markerKey === key ? "" : marker);
  }
}

function enterEdit() {
  // 图片操作与正文编辑一样进入编辑态：显示保存栏，快捷键停用。
  state.dirty = true;
  state.editing = true;
  state.draftEpoch += 1;
  renderDetail();
}

function markDirty() {
  state.dirty = true;
  state.editing = true;
  state.draftEpoch += 1;
  if (!$("#save-bar")) updateSaveBar();
  const status = $("#save-state");
  if (status) {
    status.textContent = `未保存修改（基于 r${state.baseRevision}）`;
    status.classList.add("dirty");
  }
  const saveBtn = $("#save-btn");
  if (saveBtn) saveBtn.disabled = false;
}

function cancelEdits() {
  state.draftEpoch += 1;  // 草稿回到基线：在途保存回执不得再按提交时草稿定稿
  state.draft.stem = state.detail.body.stem;
  state.draft.answer = state.detail.body.answer;
  state.draft.solution = state.detail.body.solution;
  state.draft.images = state.detail.images.map((b) => ({ ...b }));
  state.draft.issueOps = {};
  state.dirty = false;
  state.editing = false;
  renderDetail();
}

// ------------------------------------------------------------------
// 图片绑定
// ------------------------------------------------------------------

function renderUrl(binding) {
  const params = new URLSearchParams();
  if (binding.crop_px) {
    params.set("l", binding.crop_px[0]);
    params.set("t", binding.crop_px[1]);
    params.set("r", binding.crop_px[2]);
    params.set("b", binding.crop_px[3]);
  }
  if (binding.rotate) params.set("rotate", binding.rotate);
  const query = params.toString();
  return `/api/assets/${binding.source_asset_id || binding.asset_id}/render${query ? `?${query}` : ""}`;
}

// Keep the established click/select/paste/delete interaction; persistence is a draft revision.
let selectedImageSlot = null;
let savingImage = false;
function selectImageSlot(role, key = null) {
  selectedImageSlot = { role, key, item: state.detail.item_id, revision: state.baseRevision };
  document.querySelectorAll("[data-image-slot]").forEach(node => {
    const selected = node.dataset.imageSlot === (key || role);
    node.classList.toggle("is-selected", selected);
    node.setAttribute("aria-selected", String(selected));
  });
  setMessage("#review-message", "已选中，按 Ctrl+V / ⌘V 粘贴图片。");
}
function renderImages() {
  selectedImageSlot = null;
  for (const [role, id] of [["stem", "#prompt-preview"], ["solution", "#solution-preview"]]) {
    const well = $(id), section = well.closest(".image-section");
    const bindings = state.draft.images.filter(b => b.role === role);
    const editable = state.detail.is_current;
    const label = role === "stem" ? "题图" : "解答图";
    well.replaceChildren();
    well.classList.toggle("preview-gallery", !!bindings.length || editable);
    section.classList.toggle("has-images", !!bindings.length);
    section.classList.toggle("is-empty", editable && !bindings.length);
    section.classList.toggle("is-editable", editable);
    section.classList.toggle("is-single", role === "stem");
    section.dataset.imageSlot = role;
    section.tabIndex = editable ? 0 : -1;
    section.setAttribute("role", "option");
    section.setAttribute("aria-label", `${label}，点击选中后粘贴`);
    const select = () => editable && selectImageSlot(role, role === "stem" ? bindings[0]?.key : null);
    section.onclick = select;
    section.onkeydown = event => {
      if (event.target !== section) return;
      if (["Enter", " "].includes(event.key)) { event.preventDefault(); select(); }
    };
    if (!bindings.length) {
      well.append(editable ? el("div", {class: "empty-add-hint"},
        el("span", {class: "empty-add-icon"}, "＋"),
        el("span", {}, `点击此处添加${label}`), el("small", {}, "选中后按 Ctrl+V / ⌘V 粘贴")) : el("span", {}, "无图片"));
    }
    bindings.forEach((binding, index) => {
      const card = el("figure", {class: "preview-card", "data-key": binding.key});
      const image = el("img", {src: renderUrl(binding), alt: `${label} ${index + 1}`});
      image.onerror = () => { image.replaceWith(el("span", {}, "预览文件暂不可用")); };
      if (editable) {
        card.dataset.imageSlot = binding.key;
        card.tabIndex = 0;
        card.setAttribute("role", "option");
        card.setAttribute("aria-label", `${label} ${index + 1}，点击选中后粘贴`);
        card.onclick = event => { event.stopPropagation(); selectImageSlot(role, binding.key); };
        card.onkeydown = event => {
          if (event.target !== card) return;
          if (["Enter", " "].includes(event.key)) {
            event.preventDefault(); event.stopPropagation(); selectImageSlot(role, binding.key);
          }
        };
        card.append(el("button", {type: "button", class: "image-delete-button",
          "aria-label": `删除${label} ${index + 1}`, title: `删除${label}`,
          onclick: event => {
            event.stopPropagation();
            state.draft.images = state.draft.images.filter(b => b.key !== binding.key);
            stripMarkers(binding.key); enterEdit();
          }}, "×"));
      }
      card.append(image, el("figcaption", {}, `${label} ${index + 1}`));
      if (editable) card.append(el("span", {class: "slot-paste-hint"}, "点击选中 · Ctrl+V / ⌘V 替换"));
      well.append(card);
    });
  }
}

async function pasteImage(event) {
  const slot = selectedImageSlot;
  if (!slot || !state.detail?.is_current || savingImage) return;
  if (["INPUT", "TEXTAREA"].includes(event.target?.tagName)) return;
  const file = [...(event.clipboardData?.items || [])].find(i => i.type.startsWith("image/"))?.getAsFile();
  if (!file) return;
  event.preventDefault();
  savingImage = true;
  try {
    const info = await uploadBytes(await file.arrayBuffer(), file.type);
    if (state.detail.item_id !== slot.item || state.baseRevision !== slot.revision) {
      throw new Error("题目或修订已切换，请重新选择图片后粘贴");
    }
    const binding = slot.key && state.draft.images.find(b => b.key === slot.key);
    if (slot.key && !binding) throw new Error("图片已删除，请重新选择粘贴位置");
    const attrs = {asset_id: info.asset_id, source_asset_id: info.asset_id, crop_px: null, rotate: 0};
    if (binding) Object.assign(binding, attrs);
    else {
      const key = `${slot.role}-${Date.now().toString(36)}`;
      state.draft.images.push({key, role: slot.role, ...attrs, width: null, ordinal: state.draft.images.length});
      state.draft[slot.role] += `\n\n![新图](asset:${key})`;
    }
    enterEdit();
  } catch (err) { setMessage("#manage-message", `粘贴失败：${errMessages(err)}`, true); }
  finally { savingImage = false; }
}

async function uploadBytes(raw, contentType) {
  return api(`/api/items/${state.detail.item_id}/assets`, {
    method: "POST",
    headers: { "content-type": contentType },
    body: raw,
  });
}

function renderEvidenceCapsules() {
  const wrap = $("#source-capsules");
  wrap.replaceChildren();
  const pages = [...new Map(state.detail.evidence.map(page =>
    [`${page.asset_id}:${page.page_number}`, page])).values()]
    .sort((a, b) => a.page_number - b.page_number);
  if (pages.length) wrap.append(el("button", {
    type: "button", class: "source-capsule",
    "aria-label": `原卷来源页，点击打开 ${pages.length} 张截图`,
    onclick: () => openLightbox("原卷来源页", 0, pages),
  }, `原卷来源页 (${pages.length})`));
}

// ------------------------------------------------------------------
// 修改向导：右栏一次只呈现需要用户回答的一步。
// 疑点逐条回应（怎么改 / 不需要改 / 已线下处理）→ 补充其他意见 →
// 汇总交给 AI（GPT-6.1 Sol）→ 比较后采用。用户回应只是处理意图，
// 采用建议保存或人工保存时才落实疑点处置。
// ------------------------------------------------------------------

const ISSUE_TYPE_LABELS = {
  missing_image: "缺少插图",
  image_mismatch: "插图与原卷不符",
  unbound_marker: "图片标记未绑定",
  transcription: "转写存疑",
};
const TARGET_LABELS = { stem: "题干", answer: "答案", solution: "解答" };

function issueTitle(issue) {
  const target = TARGET_LABELS[issue.target] || "";
  const label = ISSUE_TYPE_LABELS[issue.type];
  if (label) return `${target}${label}`;
  const first = (issue.reason || "").split(/[。；;\n]/)[0].trim();
  if (first) return first.startsWith(target) ? first.slice(0, 28) : `${target}${first.slice(0, 24)}`;
  return `${target || "题目"}存在疑点`;
}

let assistExtraSeq = 0;
const assist = {
  itemId: null,
  phase: "idle",       // issues | extra | summary | idle
  queue: [],           // 未决疑点，逐条呈现
  index: 0,
  responses: {},       // issue_id → {action:"revise"|"skip"|"manual", text}
  drafts: {},          // issue_id → 该步已输入未确认的文本
  extra: [],           // 补充意见 [{id, text}]
  extraDraft: "",
  generated: null,     // {proposal_id, issueIds}：向导生成的建议，采用时落实处置
  statusText: "",
  statusError: false,
};

let assistRecorder = null;
let assistCancelled = false;
let assistBusy = false;

function stopAssistRecording(cancel = false) {
  if (assistRecorder) {
    assistCancelled = cancel;
    if (assistRecorder.state === "recording") assistRecorder.stop();
  }
}

function resetAssistWizard(detail) {
  assist.itemId = detail.item_id;
  assist.queue = detail.issues.filter((i) => i.state === "open");
  assist.index = 0;
  assist.responses = {};
  assist.drafts = {};
  assist.extra = [];
  assist.extraDraft = "";
  assist.generated = null;
  assist.statusText = "";
  assist.statusError = false;
  assist.phase = assist.queue.length ? "issues" : "extra";
}

function setAssistStatus(text, isError) {
  assist.statusText = text || "";
  assist.statusError = Boolean(isError);
  const node = $("#assist-status");
  if (node) {
    node.textContent = assist.statusText;
    node.style.color = isError ? "var(--danger, #b3261e)" : "";
  }
}

function assistRequirements() {
  const items = [];
  for (const issue of assist.queue) {
    const resp = assist.responses[issue.issue_id];
    if (resp && resp.action === "revise") {
      items.push({ tag: `疑点·${issueTitle(issue)}`, text: resp.text, issueId: issue.issue_id });
    }
  }
  for (const extra of assist.extra) items.push({ tag: "补充", text: extra.text, extraId: extra.id });
  return items;
}

// 未走 AI 的处置：skip → dismissed；“manual”在标记时已排入草稿随人工保存。
function assistDispositions() {
  const ops = [];
  for (const issue of assist.queue) {
    const resp = assist.responses[issue.issue_id];
    if (resp && resp.action === "skip") {
      ops.push({ issue_id: issue.issue_id, state: "dismissed", note: "人工：不需要修改" });
    }
  }
  return ops;
}

// 采用向导生成的建议时：revise/manual → resolved，skip → dismissed；只处理仍开放的疑点。
function assistIssueOpsForAccept(proposalId) {
  const d = state.detail;
  if (!assist.generated || !d || assist.generated.proposal_id !== proposalId) return [];
  const ops = [];
  for (const issueId of assist.generated.issueIds) {
    const issue = d.issues.find((i) => i.issue_id === issueId && i.state === "open");
    if (!issue) continue;
    const resp = assist.responses[issueId];
    ops.push(resp && resp.action === "skip"
      ? { issue_id: issueId, state: "dismissed", note: "人工：不需要修改" }
      : {
          issue_id: issueId, state: "resolved",
          note: resp && resp.action === "manual" ? "人工已线下处理" : "AI 修订已采用",
        });
  }
  return ops;
}

async function toggleAssistRecord() {
  if (assistRecorder) { stopAssistRecording(); return; }
  const id = state.detail?.item_id;
  if (!id || assistBusy) return;
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    setAssistStatus("当前浏览器不支持录音，请直接输入修改意见。", true);
    return;
  }
  let stream;
  try {
    assistBusy = true;
    renderAssistFlow();
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (state.detail?.item_id !== id) { stream.getTracks().forEach((t) => t.stop()); return; }
    const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"]
      .find((t) => MediaRecorder.isTypeSupported(t));
    const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    assistRecorder = recorder;
    assistCancelled = false;
    const chunks = [];
    recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    const timer = setTimeout(() => stopAssistRecording(), 180000);
    recorder.onstop = async () => {
      clearTimeout(timer);
      stream.getTracks().forEach((t) => t.stop());
      assistRecorder = null;
      if (assistCancelled) { assistBusy = false; renderAssistFlow(); return; }
      assistBusy = true;
      renderAssistFlow();
      setAssistStatus("正在转写录音…");
      try {
        const result = await api("/api/revision-assist/transcribe", {
          method: "POST",
          headers: { "content-type": recorder.mimeType || mime || "audio/webm" },
          body: new Blob(chunks, { type: recorder.mimeType || mime || "audio/webm" }),
        });
        if (state.detail?.item_id === id) {
          const textarea = $("#assist-flow textarea.assist-text");
          if (textarea) {
            textarea.value = [textarea.value, result.transcript].filter(Boolean).join("\n");
            textarea.dispatchEvent(new Event("input", { bubbles: true }));
          }
          setAssistStatus("转写完成，可编辑后继续。");
        }
      } catch (err) {
        if (state.detail?.item_id === id) setAssistStatus(`转写失败：${errMessages(err)}`, true);
      } finally { assistBusy = false; renderAssistFlow(); }
    };
    recorder.start();
    renderAssistFlow();
    setAssistStatus("正在录音，再次点击结束（最长 3 分钟）。");
  } catch (err) {
    stream?.getTracks().forEach((t) => t.stop());
    setAssistStatus(`无法录音：${err.message}。可直接输入修改意见。`, true);
  } finally {
    if (!assistRecorder) { assistBusy = false; renderAssistFlow(); }
  }
}

function assistHarvestDraft() {
  // 重渲染前保留正在输入的文本：图片编辑等操作会间接触发 renderDetail。
  const textarea = $("#assist-flow textarea.assist-text");
  if (!textarea) return;
  if (assist.phase === "issues" && assist.queue[assist.index]) {
    assist.drafts[assist.queue[assist.index].issue_id] = textarea.value;
  } else if (assist.phase === "extra") {
    assist.extraDraft = textarea.value;
  }
}

function advanceAssist() {
  if (assist.phase === "issues") {
    if (assist.index + 1 < assist.queue.length) assist.index += 1;
    else assist.phase = "extra";
  }
  renderAssistFlow();
}

function assistRecordButton() {
  return el("button", {
    type: "button", class: "ghost-button assist-record-button",
    title: "口述修改意见，停止后自动转写",
    disabled: assistBusy && !assistRecorder ? "" : null,
    onclick: toggleAssistRecord,
  }, assistRecorder ? "■ 停止录音并转写" : "🎙 口述");
}

function renderAssistIssueStep() {
  const issue = assist.queue[assist.index];
  if (!issue) { assist.phase = "extra"; return renderAssistExtraStep(); }
  const draft = assist.drafts[issue.issue_id] || "";
  const textarea = el("textarea", {
    class: "assist-text", rows: 3, maxlength: 8000,
    placeholder: "输入或口述这条应该怎么改…",
    oninput: (event) => {
      assist.drafts[issue.issue_id] = event.target.value;
      const next = $("#assist-next");
      if (next) next.disabled = !event.target.value.trim() || assistBusy;
    },
  });
  textarea.value = draft;
  return el("div", { class: "assist-step", id: "issue-list" },
    el("div", { class: "assist-step-head" },
      el("strong", null, issueTitle(issue)),
      el("span", { class: "assist-progress" }, `疑点 ${assist.index + 1}/${assist.queue.length}`)),
    issue.blocking ? el("span", { class: "badge assist-blocking" }, "阻断") : null,
    el("details", { class: "assist-evidence" },
      el("summary", null, "查看依据"),
      el("p", null, issue.reason || ""),
      el("p", { class: "assist-evidence-meta" },
        `${issue.issue_id} · ${issue.type}${issue.target ? ` → ${issue.target}` : ""}` +
        `${issue.evidence ? ` · 证据 ${issue.evidence}` : ""}`)),
    el("div", { class: "assist-input" },
      textarea,
      el("div", { class: "assist-input-actions" }, assistRecordButton())),
    el("div", { class: "assist-actions" },
      el("button", {
        type: "button", class: "ghost-button",
        title: "确认这条疑点无需修改；随采用 AI 修改或保存处置生效",
        onclick: () => {
          assist.responses[issue.issue_id] = { action: "skip" };
          assist.drafts[issue.issue_id] = "";
          advanceAssist();
        },
      }, "不需要修改"),
      el("button", {
        type: "button", class: "ghost-button assist-manual-button",
        title: "已在正文/图片里手动处理；随下次保存标记已解决",
        onclick: () => {
          assist.responses[issue.issue_id] = { action: "manual" };
          state.draft.issueOps[issue.issue_id] =
            { issue_id: issue.issue_id, state: "resolved", note: "人工已处理" };
          markDirty();
          advanceAssist();
        },
      }, "标记已解决"),
      el("button", {
        type: "button", class: "approve-button", id: "assist-next",
        disabled: !draft.trim() || assistBusy ? "" : null,
        onclick: () => {
          const text = (assist.drafts[issue.issue_id] || "").trim();
          if (!text) return;
          assist.responses[issue.issue_id] = { action: "revise", text };
          assist.drafts[issue.issue_id] = "";
          advanceAssist();
        },
      }, assist.index + 1 < assist.queue.length ? "确认，下一条" : "确认，下一步")));
}

function renderAssistExtraStep() {
  const textarea = el("textarea", {
    class: "assist-text", rows: 3, maxlength: 8000,
    placeholder: "输入或口述本题其他要改的地方…",
    oninput: (event) => {
      assist.extraDraft = event.target.value;
      const btn = $("#assist-add-extra");
      if (btn) btn.disabled = !event.target.value.trim() || assistBusy;
    },
  });
  textarea.value = assist.extraDraft;
  const list = assist.extra.length
    ? el("ul", { class: "assist-extra-list" }, ...assist.extra.map((extra) =>
        el("li", null,
          el("span", null, extra.text),
          el("button", {
            type: "button", class: "assist-remove", title: "删除这条补充意见",
            "aria-label": "删除这条补充意见",
            onclick: () => {
              assist.extra = assist.extra.filter((x) => x.id !== extra.id);
              renderAssistFlow();
            },
          }, "×"))))
    : null;
  const collected = assistRequirements().length
    || assistDispositions().length
    || Object.values(assist.responses).some((r) => r.action === "manual");
  return el("div", { class: "assist-step" },
    el("div", { class: "assist-step-head" },
      el("strong", null, assist.queue.length ? "还有其他要改的地方吗？" : "这道题要怎么改？")),
    el("div", { class: "assist-input" },
      textarea,
      el("div", { class: "assist-input-actions" },
        assistRecordButton(),
        el("button", {
          type: "button", class: "ghost-button", id: "assist-add-extra",
          disabled: !assist.extraDraft.trim() || assistBusy ? "" : null,
          onclick: () => {
            const text = assist.extraDraft.trim();
            if (!text) return;
            assist.extra.push({ id: ++assistExtraSeq, text });
            assist.extraDraft = "";
            renderAssistFlow();
          },
        }, "添加一条"))),
    list,
    el("div", { class: "assist-actions" },
      el("button", {
        type: "button", class: "approve-button",
        onclick: () => {
          assist.phase = collected ? "summary" : "idle";
          renderAssistFlow();
        },
      }, collected ? "下一步" : "完成，暂不修改")));
}

function renderAssistSummaryStep() {
  const requirements = assistRequirements();
  const skips = assistDispositions();
  const manualCount = Object.values(assist.responses)
    .filter((r) => r.action === "manual").length;
  const notes = [];
  if (skips.length) notes.push(`${skips.length} 条疑点按“不需要修改”处理`);
  if (manualCount) notes.push(`${manualCount} 条疑点已标记线下解决`);
  return el("div", { class: "assist-step" },
    el("div", { class: "assist-step-head" },
      el("strong", null, "修改要求汇总"),
      el("span", { class: "assist-progress" }, `${requirements.length} 条`)),
    requirements.length
      ? el("ol", { class: "assist-requirements" }, ...requirements.map((req) =>
          el("li", null,
            el("span", { class: "assist-req-tag" }, req.tag),
            el("span", { class: "assist-req-text" }, req.text),
            el("button", {
              type: "button", class: "assist-remove", title: "删除这条要求",
              "aria-label": "删除这条修改要求",
              onclick: () => {
                if (req.issueId) delete assist.responses[req.issueId];
                else assist.extra = assist.extra.filter((x) => x.id !== req.extraId);
                renderAssistFlow();
              },
            }, "×"))))
      : el("p", { class: "assist-idle-note" }, "没有内容修改要求。"),
    notes.length ? el("p", { class: "assist-hint" }, `${notes.join("；")}。`) : null,
    el("div", { class: "assist-actions" },
      el("button", {
        type: "button", class: "ghost-button",
        onclick: () => { assist.phase = "extra"; renderAssistFlow(); },
      }, "继续补充意见"),
      requirements.length
        ? el("button", {
            type: "button", class: "approve-button", id: "assist-generate",
            title: "GPT-6.1 Sol 生成修订建议，比较后采用",
            disabled: assistBusy ? "" : null,
            onclick: assistGenerateAction,
          }, "交给 AI 修改")
        : el("button", {
            type: "button", class: "approve-button",
            onclick: saveAssistDispositions,
          }, "保存疑点处置")));
}

async function saveAssistDispositions() {
  const d = state.detail;
  if (!d || !d.is_current || savingEdit) return;
  const ops = assistDispositions();
  for (const op of ops) state.draft.issueOps[op.issue_id] = op;
  if (!Object.keys(state.draft.issueOps).length) return;
  state.dirty = true;
  state.draftEpoch += 1;
  await saveEdits();
}

async function assistGenerateAction() {
  const d = state.detail;
  if (!d || assistBusy || assistRecorder) return;
  if (state.dirty) {
    setAssistStatus("先保存正文修改（Ctrl/⌘+S），再交给 AI。", true);
    return;
  }
  const requirements = assistRequirements();
  if (!requirements.length) { setAssistStatus("没有可交给 AI 的修改要求。", true); return; }
  const instruction = "请按以下修改要求逐条修订本题，未提及的内容与全部 asset 图片标记保持不变：\n"
    + requirements.map((r, i) => `${i + 1}. 【${r.tag}】${r.text}`).join("\n");
  const id = d.item_id;
  const issueIds = assist.queue
    .map((i) => i.issue_id)
    .filter((issueId) => assist.responses[issueId]);
  assistBusy = true;
  renderAssistFlow();
  setAssistStatus("GPT-6.1 Sol 正在生成修订建议…");
  try {
    const receipt = await api(`/api/items/${encodeURIComponent(id)}/revision-assist`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ instruction, base_revision: state.baseRevision }),
    });
    assist.generated = { proposal_id: receipt.proposal_id, issueIds };
    if (state.detail?.item_id === id) {
      assist.phase = "idle";
      setAssistStatus("AI 修改已生成，请在下方比较后采用。");
      await renderProposals();
      renderAssistFlow();
    }
  } catch (err) {
    if (state.detail?.item_id === id) setAssistStatus(`生成失败：${errMessages(err)}`, true);
  } finally { assistBusy = false; renderAssistFlow(); }
}

function renderAssistIdleStep() {
  return el("div", { class: "assist-step assist-step-idle" },
    el("p", { class: "assist-idle-note" }, "本题暂无待处理的修改。"),
    el("div", { class: "assist-actions" },
      el("button", {
        type: "button", class: "ghost-button",
        onclick: () => { assist.phase = "extra"; renderAssistFlow(); },
      }, "补充修改意见")));
}

function renderAssistFlow() {
  const wrap = $("#assist-flow");
  if (!wrap) return;
  assistHarvestDraft();
  const d = state.detail;
  wrap.replaceChildren();
  if (!d) return;
  if (!d.is_current) {
    wrap.append(el("p", { class: "assist-idle-note" }, "正在查看历史修订，回到当前修订后才能修改。"));
    return;
  }
  if (assist.itemId !== d.item_id) resetAssistWizard(d);
  let step = null;
  if (assist.phase === "issues") step = renderAssistIssueStep();
  else if (assist.phase === "extra") step = renderAssistExtraStep();
  else if (assist.phase === "summary") step = renderAssistSummaryStep();
  else step = renderAssistIdleStep();
  if (step) wrap.append(step);
  wrap.append(el("p", {
    id: "assist-status", class: "review-message", role: "status",
    style: assist.statusError ? "color: var(--danger, #b3261e);" : null,
  }, assist.statusText || ""));
}

// ------------------------------------------------------------------
// 保存
// ------------------------------------------------------------------

let savingEdit = false;
async function saveEdits() {
  const d = state.detail;
  if (!d || !d.is_current || !state.dirty || savingEdit) return;
  const contextPaper = state.paperId;
  const contextToken = state.itemLoadToken;
  const submittedEpoch = state.draftEpoch;
  savingEdit = true;
  const patch = {
    base_revision: state.baseRevision,
    operation_id: opId("save"),
    stem: state.draft.stem,
    answer: state.draft.answer,
    solution: state.draft.solution,
    images: state.draft.images.map((b) => ({
      key: b.key, asset_id: b.asset_id, role: b.role,
      source_asset_id: b.source_asset_id || null,
      crop_px: b.crop_px, rotate: b.rotate, width: b.width, ordinal: b.ordinal,
    })),
  };
  const issueOps = Object.values(state.draft.issueOps || {});
  if (issueOps.length) patch.issue_ops = issueOps;
  try {
    const receipt = await api(`/api/items/${d.item_id}/content`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    });
    state.itemCache.delete(d.item_id);
    if (stillViewingItem(d.item_id, contextToken)) {
      // 草稿仍是提交时那份（或已取消回到基线）才定稿重载；
      // 提交后又有新输入则保留草稿与 dirty，不吞新修改。
      if (state.draftEpoch === submittedEpoch || !state.dirty) {
        state.dirty = false;
        await loadItem(d.item_id);
        await loadItemList();
        updatePaperMeta();
        setMessage("#manage-message",
          `已保存 r${receipt.revision}${receipt.changed ? "" : "（无变化）"}`, false);
      } else {
        // 提交后又有新输入：保留草稿与 dirty，但把已确认基线推进到服务端新修订，
        // 否则下一次保存会带着过期 base_revision 与自己的保存结果冲突。
        state.baseRevision = receipt.revision;
        d.current_revision = receipt.revision;
        d.revision = receipt.revision;
        d.review_state = null;  // 新修订未审，原批准对新修订不生效
        $("#item-title").textContent =
          `第 ${d.identity.question_number} 题 · 修订 r${d.revision}`;
        const reviewStatusNode = $("#review-status");
        reviewStatusNode.dataset.status = "";
        reviewStatusNode.textContent = "待复核";
        const saveStateNode = $("#save-state");
        if (saveStateNode) {
          saveStateNode.textContent = `未保存修改（基于 r${state.baseRevision}）`;
        }
        setMessage("#manage-message",
          `已保存 r${receipt.revision}（提交后又有新的未保存修改，已保留并将基线推进到 r${receipt.revision}）`, false);
      }
    } else if (state.paperId === contextPaper) {
      // 同卷他题：迟到回执只刷新目录，不把用户拉回旧题。
      await loadItemList();
      updatePaperMeta();
    }
    // 已切卷：不触碰当前的 dirty/草稿——迟到回执不得清除另一题的离开保护。
  } catch (err) {
    if (!stillViewingItem(d.item_id, contextToken)) return;  // 已离开本题：迟到失败不打扰新上下文
    if (err.status === 409 && err.detail && err.detail.conflict) {
      showConflict(err.detail.conflict);
    } else {
      setMessage("#manage-message", `保存失败：${errMessages(err)}`, true);
    }
  } finally {
    savingEdit = false;
  }
}

function showConflict(conflict) {
  const body = $("#conflict-body");
  body.innerHTML = "";
  body.append(el("p", { class: "eyebrow" }, "CONFLICT"),
    el("h2", null, "版本冲突：本题已被他人/AI 更新"),
    el("p", null, `你的基线 r${conflict.base_revision}，服务端当前 r${conflict.current_revision}。`),
    el("p", { class: "transcription-issue-help" },
      `基线后变化的字段：${(conflict.changed_fields || []).join(", ") || "无"}`));
  body.append(el("div", { class: "review-actions" },
    el("button", { type: "button", class: "approve-button", onclick: () => {
      $("#conflict-backdrop").classList.remove("show");
      // 保留本地修改 = 明确以本地编辑覆盖：基线推进到服务端当前修订，
      // 否则再次保存仍带旧 base_revision 永远冲突。
      state.baseRevision = conflict.current_revision;
      if (state.detail) {
        state.detail.current_revision = conflict.current_revision;
        state.detail.review_state = null;
      }
      const saveStateNode = $("#save-state");
      if (saveStateNode) {
        saveStateNode.textContent = `未保存修改（基于 r${state.baseRevision}）`;
      }
      setMessage("#manage-message",
        `已把保存基线更新为 r${conflict.current_revision}；再次保存将以你的编辑覆盖该修订内容`, true);
    } }, "保留我的修改"),
    el("button", { type: "button", class: "reject-button", onclick: async () => {
      $("#conflict-backdrop").classList.remove("show");
      state.dirty = false;
      state.itemCache.delete(conflict.item_id);
      await loadItem(conflict.item_id);
      setMessage("#manage-message", "已加载最新修订；你的修改已丢弃", false);
    } }, "丢弃并加载最新")));
  $("#conflict-backdrop").classList.add("show");
}

// ------------------------------------------------------------------
// 复核（单题 + 审后自动跳题） / 全卷通过 / 发布 / 导出
// ------------------------------------------------------------------

function setReviewControlsDisabled(disabled) {
  $("#approve-item").disabled = disabled || !state.detail?.is_current
    || (state.detail?.approval_blockers || []).length > 0;
  $("#reject-item").disabled = disabled || !state.detail?.is_current;
  $("#approve-paper").disabled = disabled;
}

function renderReviewCard() {
  const d = state.detail;
  if (!d) {
    // 切卷/空态/目录失败期间：审核操作一律禁用（详情为空时不得残留可点状态）。
    $("#approve-item").disabled = true;
    $("#reject-item").disabled = true;
    $("#approve-paper").disabled = true;
    $("#review-blocker").textContent = "";
    return;
  }
  const blockers = d.approval_blockers || [];
  const approve = $("#approve-item");
  approve.disabled = !d.is_current || blockers.length > 0;
  approve.title = blockers.length
    ? `通过前需处理：${blockers.map(blockerTitle).join("；")}` : "快捷键 A";
  $("#reject-item").disabled = !d.is_current;
  // 全卷通过按钮在正常渲染时保持可用（提交期间的禁用由 setReviewControlsDisabled 控制）。
  $("#approve-paper").disabled = !state.paperId || state.items.length === 0;
  // 阻断原因用一句话说明，单独放 #review-blocker，不覆盖审核流程消息。
  $("#review-blocker").textContent = blockers.length
    ? `通过前需处理：${blockers.map(blockerTitle).join("；")}` : "";
}

function blockerTitle(blocker) {
  const match = /存在未决阻断疑点: (.+)/.exec(blocker);
  if (match) {
    const issue = state.detail?.issues.find((i) => i.issue_id === match[1]);
    if (issue) return issueTitle(issue);
  }
  return blocker;
}

function applyDirectoryReview(itemId, decision, revision) {
  // 单题审核成功后就地更新目录项，列表与进度立即可见；不重拉整卷。
  const entry = state.items.find((i) => i.item_id === itemId);
  if (entry) {
    entry.review_status = decision;
    entry.revision = revision ?? entry.revision;
  }
  state.itemCache.delete(itemId);
  renderItemList();
  recomputePaperStats(state.paperId);
  updatePaperMeta();
}

function findNextReviewIndex(currentIndex) {
  // 卷内环形扫描下一道待处理题（待审核/已过期），排除刚处理的本题。
  const items = state.items;
  for (let offset = 1; offset < items.length; offset += 1) {
    const candidate = (currentIndex + offset) % items.length;
    if (needsAttention(items[candidate])) return candidate;
  }
  return -1;
}

// 迟到回执防护：await 期间用户可能已切卷/切题。只有仍停留在发起时的
// 题目（itemLoadToken 未变）才允许改详情视图与自动导航；仍在本卷时只
// 更新目录数据；已切卷则什么都不做。
function stillViewingItem(itemId, token) {
  return state.detail?.item_id === itemId && state.itemLoadToken === token;
}

async function submitReview(decision, note) {
  const d = state.detail;
  if (!d || state.submittingReview) return false;
  const reviewedIndex = state.items.findIndex((i) => i.item_id === d.item_id);
  const contextPaper = state.paperId;
  const contextToken = state.itemLoadToken;
  state.submittingReview = true;
  setReviewControlsDisabled(true);
  setMessage("#review-message", "正在保存审核记录…");
  try {
    const receipt = await api(`/api/items/${d.item_id}/review`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        decision, note,
        base_revision: d.revision, operation_id: opId("review"),
      }),
    });
    const onPaper = state.paperId === contextPaper;
    const onItem = stillViewingItem(d.item_id, contextToken);
    if (onPaper) {
      applyDirectoryReview(d.item_id, decision, receipt.revision);
    }
    if (onItem && onPaper) {
      // 无跳转（本卷再无待审题）时也要立即可见：就地同步详情头部状态。
      d.review_state = decision;
      const reviewStatus = $("#review-status");
      reviewStatus.dataset.status = decision;
      reviewStatus.textContent = decision === "approved"
        ? `已批准 r${receipt.revision}`
        : `已驳回 r${receipt.revision}`;
      const nextReviewIndex = findNextReviewIndex(reviewedIndex);
      if (decision === "approved") playApprovalSound();
      if (nextReviewIndex >= 0) {
        location.hash = `item=${state.items[nextReviewIndex].item_id}`;
        window.setTimeout(playPageTurnSound, decision === "approved" ? 280 : 0);
      }
      const decisionLabel = decision === "approved" ? "通过" : "要求修改";
      const destination = nextReviewIndex >= 0
        ? `已跳到 ${state.items[nextReviewIndex].item_id}。`
        : "本卷已经没有待审核题目。";
      setMessage("#review-message", `${d.item_id} 已保存：${decisionLabel}。${destination}`);
    } else if (onPaper) {
      // 同卷已切到别的题：目录已更新，不强制把用户拉回。
      setMessage("#review-message",
        `${d.item_id} 已保存：${decision === "approved" ? "通过" : "要求修改"}。`);
    }
    return true;
  } catch (err) {
    if (stillViewingItem(d.item_id, contextToken)) {
      setMessage("#review-message", `保存失败：${errMessages(err)}`, true);
      if (decision === "rejected") setMessage("#revision-error", `保存失败：${errMessages(err)}`, true);
    }
    return false;
  } finally {
    state.submittingReview = false;
    renderReviewCard();
  }
}

async function approveItem() {
  await submitReview("approved", "");
}

function rejectItem() {
  const d = state.detail;
  $("#revision-item-label").textContent = `${d.item_id} r${d.revision}`;
  $("#revision-note").value = "";
  $("#revision-error").textContent = "";
  $("#revision-dialog").showModal();
}

async function confirmReject() {
  const d = state.detail;
  const note = $("#revision-note").value.trim();
  if (!note) {
    $("#revision-error").textContent = "请填写需要修改的内容";
    return;
  }
  const ok = await submitReview("rejected", note);
  if (ok) $("#revision-dialog").close();
}

function findNextUnreviewedPaper(currentPaperId) {
  // 当前筛选结果里环形扫描下一份未全通过（approved < total_received）的卷。
  const papers = state.papers;
  const current = papers.findIndex((p) => p.paper_id === currentPaperId);
  if (current < 0) return null;
  for (let offset = 1; offset <= papers.length; offset += 1) {
    const candidate = papers[(current + offset) % papers.length];
    if (candidate.paper_id !== currentPaperId
      && candidate.stats.approved < candidate.stats.total_received) {
      return candidate;
    }
  }
  return null;
}

async function approveWholePaper() {
  if (state.approvingPaper || !state.paperId) return;
  if (state.dirty) {
    setMessage("#bulk-message", "有未保存的修改：请先保存为新修订或取消编辑，再全卷通过。", true);
    return;
  }
  if (!state.items.length) return;
  const currentPaperId = state.paperId;
  state.approvingPaper = true;
  setReviewControlsDisabled(true);
  ensureAudioContext();
  setMessage("#bulk-message", "正在一键通过整卷…");
  setMessage("#review-message", "");
  try {
    const payload = {
      operation_id: opId("reviewall"),
      items: state.items.map((i) => ({ item_id: i.item_id, base_revision: i.revision })),
    };
    const receipt = await api(
      `/api/papers/${encodeURIComponent(currentPaperId)}/review-all`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
    const failed = receipt.failed || [];
    await loadItemList();
    await applyFilters({ quiet: true });  // 刷新目录计数与进度，不打断当前卷
    if (state.paperId !== currentPaperId) return;  // 期间已切卷：停止后续导航
    if (failed.length) {
      setMessage("#bulk-message",
        `${failed.length} 题审核失败：${failed.map((f) => f.item_id).join("、")}。请逐题处理后重试，未跳转。`, true);
      return;
    }
    playApprovalSound();
    const nextPaper = findNextUnreviewedPaper(currentPaperId);
    if (nextPaper) {
      setMessage("#bulk-message", `本卷已全部通过，正在跳转 ${nextPaper.paper_id}…`);
      await selectPaper(nextPaper.paper_id);
      setMessage("#bulk-message", `已跳转 ${nextPaper.paper_id}。`);
    } else {
      const first = state.items[0];
      if (first) location.hash = `item=${first.item_id}`;
      setMessage("#bulk-message", "本卷已全部通过，筛选范围内已无待审试卷。");
    }
  } catch (err) {
    setMessage("#bulk-message", `一键通过失败：${errMessages(err)}`, true);
  } finally {
    state.approvingPaper = false;
    renderReviewCard();
  }
}

function renderPublication() {
  const d = state.detail;
  const status = $("#publication-status");
  if (d.publication) {
    status.dataset.status = "approved";
    status.textContent = `已发布 r${d.publication.revision}`;
  } else {
    status.dataset.status = "";
    status.textContent = "未发布";
  }
  const history = $("#revision-history-wrap");
  history.innerHTML = "";
  const table = el("table", { class: "qbm-history" },
    el("thead", null, el("tr", null,
      ...["修订", "hash", "来源", "时间"].map((h) => el("th", null, h)))),
    el("tbody"));
  for (const rev of [...d.revisions].reverse()) {
    const origin = rev.origin_submission_id
      ? `AI ${rev.origin_submission_id}`
      : rev.origin_operation_id ? `人工 ${rev.origin_operation_id}` : rev.actor_kind;
    table.tBodies[0].append(el("tr", {
      onclick: () => { location.hash = `item=${d.item_id}&revision=${rev.revision}`; },
    },
      el("td", null, `r${rev.revision}${rev.revision === d.current_revision ? "（当前）" : ""}`),
      el("td", null, rev.content_hash.slice(7, 19)),
      el("td", null, origin),
      el("td", null, rev.created_at)));
  }
  history.append(table);
  $("#publish-item").disabled = !d.is_current;
  $("#withdraw-item").disabled = !d.is_current || !d.publication;
  $("#export-item").disabled = !d.is_current;
  $("#export-draft").disabled = !d.is_current;
}

async function publishCurrent() {
  const d = state.detail;
  const contextToken = state.itemLoadToken;
  try {
    const receipt = await api(`/api/items/${d.item_id}/publication`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        revision: d.revision,
        expected_previous: d.publication ? d.publication.publication_id : null,
        operation_id: opId("publish"),
      }),
    });
    setMessage("#manage-message", `已发布 r${receipt.revision}`, false);
    state.itemCache.delete(d.item_id);
    if (stillViewingItem(d.item_id, contextToken)) {
      await loadItem(d.item_id);
      await loadItemList();
    } else if (state.items.some((i) => i.item_id === d.item_id)) {
      await loadItemList();
    }
  } catch (err) {
    setMessage("#manage-message", `发布失败：${errMessages(err)}`, true);
  }
}

async function withdrawCurrent() {
  const d = state.detail;
  const contextToken = state.itemLoadToken;
  if (!d.publication || !confirm(`撤回发布 r${d.publication.revision}？`)) return;
  try {
    await api(`/api/items/${d.item_id}/withdrawal`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        expected_previous: d.publication.publication_id,
        operation_id: opId("withdraw"),
      }),
    });
    setMessage("#manage-message", "已撤回发布", false);
    state.itemCache.delete(d.item_id);
    if (stillViewingItem(d.item_id, contextToken)) {
      await loadItem(d.item_id);
      await loadItemList();
    } else if (state.items.some((i) => i.item_id === d.item_id)) {
      await loadItemList();
    }
  } catch (err) {
    setMessage("#manage-message", `撤回失败：${errMessages(err)}`, true);
  }
}

async function exportCurrent(allowDraft) {
  const d = state.detail;
  try {
    const receipt = await api(`/api/items/${d.item_id}/exports`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ revision: d.revision, allow_draft: allowDraft }),
    });
    setMessage("#manage-message",
      `导出${receipt.status === "ready" ? "就绪" : receipt.status}：${receipt.path}${receipt.replayed ? "（复用既有导出）" : ""}`, false);
  } catch (err) {
    setMessage("#manage-message", `导出失败：${errMessages(err)}`, true);
  }
}

// ------------------------------------------------------------------
// 教学内容（讲解/解答对）：老版 explanations 子系统复刻
// ------------------------------------------------------------------

const notesState = {
  token: 0,
  itemId: null,
  view: null,
};

const noteRecording = {
  noteId: null, recorder: null, chunks: [], startedAt: 0, timer: null, mimeType: "",
};

const NOTE_SOURCE_LABELS = {
  recorded: "录音",
  polished: "录音润色",
  generated: "自动生成",
  manual: "手工",
};

function notesMessage(scope, text, tone = "info") {
  const node = scope
    ? scope.querySelector(".explanation-message") || $("#explanations-message")
    : $("#explanations-message");
  if (!node) return;
  node.textContent = text || "";
  node.dataset.tone = tone;
}

async function loadNotes(itemId) {
  const token = ++notesState.token;
  notesState.itemId = itemId;
  notesState.view = null;
  const body = $("#explanations-body");
  body.replaceChildren(el("p", { class: "explanations-loading" }, "正在读取讲解/解答…"));
  notesMessage(null, "");
  try {
    const view = await api(`/api/items/${encodeURIComponent(itemId)}/notes`);
    if (token !== notesState.token || notesState.itemId !== itemId) return;
    notesState.view = view;
    renderNotes();
  } catch (err) {
    if (token !== notesState.token) return;
    body.replaceChildren(el("p", { class: "explanations-error" },
      `讲解/解答读取失败：${errMessages(err)}`));
  }
}

function noteStatusLabel(note) {
  if (note.source_stale) return "来源已变化";
  if (note.review_status === "approved") return "已批准";
  if (note.review_status === "rejected") return "已驳回";
  const parts = [];
  parts.push(note.explanation_text.trim() ? "有讲解" : "缺讲解");
  parts.push(note.solution_text.trim() ? "有解答" : "缺解答");
  return parts.join(" · ");
}

function subquestionStatusLabel(subq) {
  const notes = subq.notes || [];
  if (!notes.length) return "缺讲解";
  const approved = notes.filter((n) => n.review_status === "approved" && !n.source_stale).length;
  return approved === notes.length && approved > 0
    ? `${notes.length} 对 · 全部已批准`
    : `${notes.length} 对 · ${approved} 已批准`;
}

function renderNotes() {
  const view = notesState.view;
  const body = $("#explanations-body");
  if (!body || !view) return;
  body.replaceChildren();
  const cap = view.capabilities || {};
  const caps = [
    cap.server_ffmpeg ? "服务器 ffmpeg ✓" : "服务器 ffmpeg ✗（无法转写录音）",
    cap.asr_configured ? "ASR 已配置" : "ASR 未配置（DASHSCOPE_API_KEY 缺失）",
    cap.llm_configured ? "润色/生成可用" : "润色/生成未配置",
  ];
  $("#explanations-hint").textContent =
    `讲解与解答按小题配对管理：点“录讲解”口述后自动转写并润色，可编辑、重新润色或补齐；`
    + `批准讲解对后，导出 blueprint 还需题目当前修订已通过人工审核。能力：${caps.join(" · ")}。`;
  const summary = view.summary || {};
  $("#explanations-summary").textContent =
    `${summary.note_count || 0} 对 · 待批 ${Math.max(0, (summary.note_count || 0) - (summary.approved_pairs || 0))}`
    + `${summary.stale_notes ? ` · 过期 ${summary.stale_notes}` : ""}`;
  (view.subquestions || []).forEach((subq) => {
    body.append(renderNoteSubquestion(subq));
  });
  updateRecordingButtons();
  typeset(body);
}

function renderNoteSubquestion(subq) {
  const wrap = el("article", { class: "explanation-subquestion", "data-subquestion-id": subq.id });
  wrap.append(el("div", { class: "explanation-subquestion-head" },
    el("strong", { class: "explanation-subquestion-label" }, subq.label || "整题"),
    el("span", { class: "explanation-subquestion-stem source-text" }, subq.stem || ""),
    badge(subquestionStatusLabel(subq), "chip-explanation-status"),
    subq.extra ? badge("额外小问（题干已变化）", "chip-note-stale") : null,
    el("button", {
      type: "button", class: "ghost-button",
      onclick: (event) => createNoteAction(subq.id, event.target),
    }, "＋ 新建讲解-解答")));
  (subq.notes || []).forEach((note) => wrap.append(renderNoteCard(note, subq)));
  if (!(subq.notes || []).length) {
    wrap.append(el("p", { class: "explanation-empty" },
      "该小问还没有讲解-解答：点“新建讲解-解答”添加，或用顶部“补齐缺失讲解”自动生成。"));
  }
  return wrap;
}

function renderNoteCard(note, subq) {
  const card = el("article", { class: "explanation-approach", "data-note-id": note.note_id });
  const statusChip = note.source_stale
    ? badge(noteStatusLabel(note), "chip-note-stale")
    : note.review_status === "approved" && !note.source_stale
      ? badge(noteStatusLabel(note), "chip-note-approved")
      : badge(noteStatusLabel(note), "chip-explanation-status");
  const actions = el("span", { class: "explanation-approach-actions" });
  const recordButton = el("button", {
    type: "button", class: "explanation-record-button", "data-note-id": note.note_id,
    onclick: () => toggleNoteRecording(note.note_id),
  }, "● 录讲解");
  actions.append(recordButton);
  if (note.transcript || note.audio_asset_id) {
    actions.append(el("button", {
      type: "button", class: "ghost-button",
      onclick: (event) => noteAction(note, "polish", {}, event.target, "正在润色讲解…"),
    }, "润色讲解"));
  }
  if (!note.explanation_text.trim()) {
    actions.append(el("button", {
      type: "button", class: "ghost-button",
      onclick: (event) => noteAction(note, "generate", { kind: "explanation" }, event.target, "正在生成讲解…"),
    }, "生成讲解"));
  }
  if (note.explanation_text.trim() && !note.solution_text.trim()) {
    actions.append(el("button", {
      type: "button", class: "ghost-button",
      onclick: (event) => noteAction(note, "generate", { kind: "solution" }, event.target, "正在生成解答…"),
    }, "生成解答"));
  }
  if (note.explanation_text.trim() && note.solution_text.trim()) {
    actions.append(el("button", {
      type: "button", class: "approve-button",
      onclick: (event) => noteAction(note, "review", { decision: "approved" }, event.target, "正在批准…"),
    }, "批准"));
  }
  if (note.source_stale) {
    actions.append(el("button", {
      type: "button", class: "ghost-button",
      onclick: async (event) => {
        if (!confirm("确认这组讲解-解答仍适配当前题目修订？将建立新来源版本，需要重新批准。")) return;
        await noteAction(note, "patch", { readapt: true }, event.target, "正在确认适配…");
      },
    }, "确认适配当前题目"));
  }
  actions.append(el("button", {
    type: "button", class: "reject-button",
    onclick: async () => {
      if (!confirm("删除这组讲解-解答？录音文件与历史版本保留在数据库中（软删除）。")) return;
      await noteAction(note, "delete", {}, null, "");
    },
  }, "删除"));
  card.append(el("div", { class: "explanation-approach-head" },
    el("strong", { class: "explanation-approach-title" }, note.title || `思路 ${note.note_id.slice(-2)}`),
    badge(`v${note.version}`, "chip-explanation-status"),
    statusChip,
    actions));
  card.append(renderNoteField("讲解", note, "explanation"));
  if (note.transcript) {
    card.append(el("details", { class: "explanation-transcript" },
      el("summary", {}, "录音转写稿"),
      el("pre", {}, note.transcript)));
  }
  if (note.audio_asset_id) {
    card.append(el("audio", {
      class: "explanation-audio", controls: true, preload: "none",
      src: `/api/assets/${encodeURIComponent(note.audio_asset_id)}/audio`,
    }));
  }
  card.append(renderNoteField("解答", note, "solution"));
  card.append(el("p", { class: "explanation-message", role: "status" }, ""));
  return card;
}

function renderNoteField(labelText, note, field) {
  const textKey = `${field}_text`;
  const sourceKey = `${field}_source`;
  const block = el("div", { class: "explanation-field" });
  const head = el("div", { class: "explanation-field-head" },
    el("span", { class: "explanation-field-label" }, labelText),
    note[textKey].trim() ? badge(NOTE_SOURCE_LABELS[note[sourceKey]] || note[sourceKey], "chip-explanation-status") : null);
  const textarea = el("textarea", {
    class: "explanation-textarea", rows: field === "explanation" ? 5 : 4,
    "aria-label": `${labelText}（编辑后失焦保存为新版本）`,
  });
  textarea.value = note[textKey];
  const preview = el("div", { class: "explanation-preview source-text" }, note[textKey]);
  textarea.addEventListener("input", () => { preview.textContent = textarea.value; });
  textarea.addEventListener("blur", () => {
    if (textarea.value.trim() === note[textKey].trim()) return;
    const scope = textarea.closest(".explanation-approach");
    void noteAction(note, "patch", { [textKey]: textarea.value }, null, "保存中…", scope);
  });
  block.append(head, textarea, preview);
  return block;
}

async function noteAction(note, action, extra, button, busyText, scopeOverride) {
  const itemId = notesState.itemId;
  const scope = scopeOverride
    || document.querySelector(`.explanation-approach[data-note-id="${note.note_id}"]`);
  if (busyText) notesMessage(scope, busyText);
  if (button) button.disabled = true;
  try {
    let receipt;
    if (action === "patch") {
      receipt = await api(`/api/notes/${encodeURIComponent(note.note_id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_version: note.version, operation_id: opId("note"), ...extra,
        }),
      });
    } else if (action === "delete") {
      receipt = await api(`/api/notes/${encodeURIComponent(note.note_id)}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_version: note.version, operation_id: opId("note-del") }),
      });
    } else {
      receipt = await api(`/api/notes/${encodeURIComponent(note.note_id)}/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_version: note.version, operation_id: opId(`note-${action}`), ...extra,
        }),
      });
    }
    if (action === "review" && receipt.status === "reviewed") playApprovalSound();
    const done = () => {
      // 重渲染后 scope 节点已被替换：按 note_id 重定位再提示。
      const fresh = document.querySelector(
        `.explanation-approach[data-note-id="${note.note_id}"]`);
      notesMessage(fresh || scope, "已完成。", "ok");
    };
    if (notesState.itemId === itemId && state.detail?.item_id === itemId) {
      await loadNotes(itemId);
      await loadItemList();  // 刷新列表缺讲解/缺解答徽标
    }
    done();
    return receipt;
  } catch (err) {
    const message = `操作失败：${errMessages(err)}`;
    if (notesState.itemId === itemId) await loadNotes(itemId);
    const fresh = document.querySelector(
      `.explanation-approach[data-note-id="${note.note_id}"]`);
    notesMessage(fresh || scope, message, "error");
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

async function createNoteAction(subquestionId, button) {
  const itemId = notesState.itemId;
  if (!itemId) return;
  button.disabled = true;
  try {
    await api(`/api/items/${encodeURIComponent(itemId)}/notes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subquestion_id: subquestionId, operation_id: opId("note-new"),
      }),
    });
    if (notesState.itemId === itemId) await loadNotes(itemId);
  } catch (err) {
    notesMessage(null, `新建失败：${errMessages(err)}`, "error");
  } finally {
    button.disabled = false;
  }
}

async function fillMissingAction(kind) {
  const itemId = notesState.itemId;
  if (!itemId) return;
  notesMessage(null, kind === "explanation" ? "正在补齐缺失讲解…" : "正在补齐缺失解答…");
  try {
    const receipt = await api(`/api/items/${encodeURIComponent(itemId)}/notes/fill`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, operation_id: opId("fill") }),
    });
    const failed = (receipt.results || []).filter((r) => r.status === "failed");
    notesMessage(null,
      receipt.generated
        ? `已生成 ${receipt.generated} 项${failed.length ? `，${failed.length} 项失败` : ""}。`
        : "没有需要补齐的目标（或目标均已有人工内容）。",
      failed.length ? "error" : "ok");
    if (notesState.itemId === itemId) {
      await loadNotes(itemId);
      await loadItemList();
    }
  } catch (err) {
    notesMessage(null, `补齐失败：${errMessages(err)}`, "error");
  }
}

async function exportBlueprintsAction() {
  const paperId = state.paperId;
  if (!paperId) return;
  notesMessage(null, "正在重建 blueprint 批次…");
  try {
    const receipt = await api(
      `/api/papers/${encodeURIComponent(paperId)}/blueprint-export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operation_id: opId("bp-export") }),
      });
    const parts = [];
    if (receipt.candidate_count) {
      parts.push(`已导出 ${receipt.candidate_count} 个 candidate（${receipt.item_count} 题）`);
      parts.push(`本地快照：${receipt.batch_path}`);
      parts.push(receipt.external_status === "delivered"
        ? `已投递 teaching-tools：${receipt.external_path}`
        : receipt.external_status === "not_configured"
          ? "外部 teaching-tools 根未配置（QB_TEACHING_TOOLS_ROOT），仅本地快照"
          : `外部投递失败：${receipt.external_path}`);
      if ((receipt.pending_item_review || []).length) {
        parts.push(`等待题目人工审核：${receipt.pending_item_review.join("、")}`);
      }
    } else if ((receipt.pending_item_review || []).length) {
      parts.push(`讲解对已批准，等待题目人工审核后才能导出：${receipt.pending_item_review.join("、")}`);
    } else {
      parts.push("没有可导出的讲解-解答对（需讲解对已批准且来源匹配，题目修订已批准）。");
    }
    notesMessage(null, parts.join("；"), receipt.candidate_count ? "ok" : "info");
  } catch (err) {
    notesMessage(null, `导出失败：${errMessages(err)}`, "error");
  }
}

// ------------------------------------------------------------------
// 录音（MediaRecorder）：录讲 → 上传转写 → 自动润色（老版链路）
// ------------------------------------------------------------------

function pickRecordingMimeType() {
  if (!window.MediaRecorder) return "";
  for (const type of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return "";
}

function formatRecordingDuration(seconds) {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function updateRecordingButtons() {
  document.querySelectorAll(".explanation-record-button").forEach((button) => {
    if (noteRecording.noteId === button.dataset.noteId) {
      button.classList.add("is-recording");
      const seconds = Math.floor((Date.now() - noteRecording.startedAt) / 1000);
      button.textContent = `■ 停止 ${formatRecordingDuration(seconds)}`;
      button.setAttribute("aria-pressed", "true");
    } else {
      button.classList.remove("is-recording");
      button.textContent = "● 录讲解";
      button.setAttribute("aria-pressed", "false");
      button.disabled = Boolean(noteRecording.noteId);
    }
  });
}

function stopNoteRecording() {
  if (noteRecording.recorder?.state === "recording") {
    noteRecording.recorder.stop();
  }
}

async function toggleNoteRecording(noteId) {
  if (noteRecording.noteId) {
    stopNoteRecording();
    return;
  }
  const itemId = notesState.itemId;
  const scope = document.querySelector(`.explanation-approach[data-note-id="${noteId}"]`);
  if (!itemId) return;
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    notesMessage(scope, "当前浏览器不支持录音（需要 MediaRecorder 与麦克风权限）。", "error");
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mimeType = pickRecordingMimeType();
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    noteRecording.recorder = recorder;
    noteRecording.chunks = [];
    noteRecording.noteId = noteId;
    noteRecording.itemId = itemId;
    noteRecording.mimeType = recorder.mimeType || mimeType || "audio/webm";
    noteRecording.startedAt = Date.now();
    recorder.ondataavailable = (event) => {
      if (event.data?.size) noteRecording.chunks.push(event.data);
    };
    recorder.onstop = () => {
      stream.getTracks().forEach((track) => track.stop());
      const chunks = noteRecording.chunks;
      const meta = {
        itemId: noteRecording.itemId, noteId: noteRecording.noteId,
        mimeType: noteRecording.mimeType, version: noteRecording.version,
      };
      noteRecording.recorder = null;
      noteRecording.noteId = null;
      if (noteRecording.timer) {
        clearInterval(noteRecording.timer);
        noteRecording.timer = null;
      }
      updateRecordingButtons();
      if (chunks.length) {
        void uploadNoteRecording(meta, new Blob(chunks, { type: meta.mimeType }));
      }
    };
    recorder.start();
    noteRecording.timer = setInterval(updateRecordingButtons, 500);
    updateRecordingButtons();
    notesMessage(scope, "录音中…再次点击停止并转写。");
  } catch (error) {
    noteRecording.noteId = null;
    updateRecordingButtons();
    notesMessage(scope, `无法开始录音：${error.message}`, "error");
  }
}

async function uploadNoteRecording(meta, blob) {
  // 录音开始时锁定版本；停止后题/版本已切换则不上传（录音丢弃并提示）。
  const note = findNote(meta.noteId);
  const scope = document.querySelector(`.explanation-approach[data-note-id="${meta.noteId}"]`);
  if (!note || notesState.itemId !== meta.itemId || !state.detail
      || state.detail.item_id !== meta.itemId) {
    notesMessage(scope, "录音期间已切换题目，本次录音未上传。", "error");
    return;
  }
  notesMessage(scope, "录音结束，正在上传并转写…");
  try {
    const receipt = await api(
      `/api/notes/${encodeURIComponent(meta.noteId)}/audio`
      + `?base_version=${note.version}&operation_id=${encodeURIComponent(opId("note-rec"))}`, {
        method: "POST",
        headers: { "Content-Type": meta.mimeType },
        body: blob,
      });
    if (receipt.errors && receipt.errors.length) {
      await loadNotes(meta.itemId);
      const freshErr = document.querySelector(
        `.explanation-approach[data-note-id="${meta.noteId}"]`);
      notesMessage(freshErr || scope,
        `音频已保存，但转写失败：${receipt.errors[0].message}`, "error");
      return;
    }
    // 转写完成 → 自动润色（老版链路）
    notesMessage(scope, "转写完成，正在润色讲解…");
    const fresh = findNote(meta.noteId);
    try {
      await api(`/api/notes/${encodeURIComponent(meta.noteId)}/polish`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_version: receipt.version, operation_id: opId("note-pol"),
        }),
      });
      await loadNotes(meta.itemId);
      const freshOk = document.querySelector(
        `.explanation-approach[data-note-id="${meta.noteId}"]`);
      notesMessage(freshOk, "已根据录音润色讲解，可直接编辑、重新润色或批准。", "ok");
    } catch (err) {
      await loadNotes(meta.itemId);
      notesMessage(
        document.querySelector(`.explanation-approach[data-note-id="${meta.noteId}"]`),
        `录音已保存、转写成功，但润色失败：${errMessages(err)}。可点“润色讲解”重试。`, "error");
    }
  } catch (error) {
    if (notesState.itemId === meta.itemId) await loadNotes(meta.itemId);
    const freshFail = document.querySelector(
      `.explanation-approach[data-note-id="${meta.noteId}"]`);
    notesMessage(freshFail || scope, `录音上传或转写失败：${errMessages(error)}`, "error");
  }
}

function findNote(noteId) {
  for (const subq of notesState.view?.subquestions || []) {
    for (const note of subq.notes || []) {
      if (note.note_id === noteId) return note;
    }
  }
  return null;
}

// ------------------------------------------------------------------
// 灯箱
// ------------------------------------------------------------------

const lightboxState = { pages: [], index: 0 };

function openLightbox(title, index = 0, pages = null) {
  lightboxState.pages = pages || [];
  lightboxState.index = index;
  $("#source-lightbox-title").textContent = title;
  updateLightbox();
  $("#source-lightbox").showModal();
  $("#source-lightbox-close").focus();
}

function updateLightbox() {
  const entry = lightboxState.pages[lightboxState.index];
  if (!entry) return;
  const src = typeof entry === "string"
    ? entry : `/api/assets/${entry.asset_id}/render`;
  $("#source-lightbox-image").src = src;
  $("#source-lightbox-open").href = src;
  $("#source-lightbox-counter").textContent =
    `${lightboxState.index + 1} / ${lightboxState.pages.length}`;
  $("#source-lightbox-caption").textContent = typeof entry === "string"
    ? "" : `原卷第 ${entry.page_number} 页`;
  $("#source-lightbox-prev").disabled = lightboxState.index <= 0;
  $("#source-lightbox-next").disabled = lightboxState.index >= lightboxState.pages.length - 1;
}

// ------------------------------------------------------------------
// 事件与路由
// ------------------------------------------------------------------

function debounce(fn, wait) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), wait);
  };
}

function bindEvents() {
  document.addEventListener("paste", pasteImage);

  $("#bank-select").addEventListener("change", async (event) => {
    if (!event.target.value) return;
    try {
      await selectPaper(event.target.value);
    } catch (err) {
      $("#page-status").textContent = `试卷加载失败：${errMessages(err)}`;
      $("#page-status").hidden = false;
    }
  });
  $("#filter-grade").addEventListener("change", () => applyFilters());
  $("#filter-year").addEventListener("change", () => applyFilters());
  $("#filter-exam-type").addEventListener("change", () => applyFilters());
  $("#search-input").addEventListener("input", debounce(() => applyFilters(), 300));
  $("#item-filter").addEventListener("input", () => {
    state.itemFilter = $("#item-filter").value;
    renderItemList();
  });

  $("#editor-stem-text").addEventListener("input", () => onBodyInput("stem"));
  $("#editor-answer-text").addEventListener("input", () => onBodyInput("answer"));
  $("#editor-solution-text").addEventListener("input", () => onBodyInput("solution"));

  $("#approve-item").addEventListener("click", approveItem);
  $("#reject-item").addEventListener("click", rejectItem);
  $("#approve-paper").addEventListener("click", approveWholePaper);
  $("#revision-form").addEventListener("submit", (event) => {
    event.preventDefault();
    confirmReject();
  });
  $("#cancel-revision").addEventListener("click", () => $("#revision-dialog").close());

  $("#fill-explanations").addEventListener("click", () => fillMissingAction("explanation"));
  $("#fill-solutions").addEventListener("click", () => fillMissingAction("solution"));
  $("#export-blueprints").addEventListener("click", exportBlueprintsAction);

  $("#publish-item").addEventListener("click", publishCurrent);
  $("#withdraw-item").addEventListener("click", withdrawCurrent);
  $("#export-item").addEventListener("click", () => exportCurrent(false));
  $("#export-draft").addEventListener("click", () => exportCurrent(true));

  $("#previous-item").addEventListener("click", () => stepItem(-1));
  $("#next-item").addEventListener("click", () => stepItem(1));

  $("#source-lightbox-prev").addEventListener("click", () => {
    lightboxState.index = (lightboxState.index - 1 + lightboxState.pages.length)
      % lightboxState.pages.length;
    updateLightbox();
  });
  $("#source-lightbox-next").addEventListener("click", () => {
    lightboxState.index = (lightboxState.index + 1) % lightboxState.pages.length;
    updateLightbox();
  });
  $("#source-lightbox-close").addEventListener("click", () => $("#source-lightbox").close());

  window.addEventListener("beforeunload", (event) => {
    if (state.dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  document.addEventListener("keydown", (event) => {
    if ($("#source-lightbox").open) {
      if (event.key === "ArrowLeft") { event.preventDefault(); $("#source-lightbox-prev").click(); }
      if (event.key === "ArrowRight") { event.preventDefault(); $("#source-lightbox-next").click(); }
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (!$("#revision-dialog").open && !$("#conflict-backdrop").classList.contains("show")) saveEdits();
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return;
    if (state.editing || $("#revision-dialog").open) return;
    const target = event.target;
    if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
    const key = event.key.toLowerCase();
    // Shift+A 单独路由到全卷通过，避免和单题 A 撞车。
    if (event.shiftKey && key === "a") {
      event.preventDefault();
      approveWholePaper();
      return;
    }
    if (event.shiftKey) return;
    if (key === "a") {
      if (!$("#approve-item").disabled) approveItem();
    } else if (key === "r") {
      if (!$("#reject-item").disabled) rejectItem();
    } else if (event.key === "ArrowLeft") {
      stepItem(-1);
    } else if (event.key === "ArrowRight") {
      stepItem(1);
    }
  });

  window.addEventListener("hashchange", route);
}

async function stepItem(delta) {
  if (!state.detail) return;
  const visible = visibleItems();
  const index = visible.findIndex((i) => i.item_id === state.detail.item_id);
  const next = visible[index + delta];
  if (next) location.hash = `item=${next.item_id}`;
}

async function route() {
  const match = location.hash.match(/^#item=([^&]+)/);
  if (!match) return;
  const revisionMatch = location.hash.match(/revision=(\d+)/);
  const revision = revisionMatch ? Number(revisionMatch[1]) : null;
  const itemId = decodeURIComponent(match[1]);
  // hash 指向别的卷（深链/前进后退）：当前卷、目录与详情必须始终一致，
  // 不允许只加载异卷详情（全卷通过与审后跳题会作用到错误的卷）。
  const paperId = paperOfItem(itemId);
  if (paperId && paperId !== state.paperId) {
    if (state.papers.some((p) => p.paper_id === paperId)) {
      await selectPaper(paperId);
    } else {
      // 目标卷不在筛选结果：清空筛选纳入它（先保护草稿）。
      if (state.dirty && !confirmDiscardDirty("导航到其他试卷")) {
        if (state.detail) location.hash = `#item=${state.detail.item_id}`;
        return;
      }
      $("#search-input").value = "";
      $("#filter-year").value = "";
      $("#filter-grade").value = "";
      $("#filter-exam-type").value = "";
      await applyFilters({ preferHash: true });
    }
    if (state.paperId === paperId) {
      // selectPaper/applyFilters 已按 hash（含 revision）定位则不再重复加载。
      if (state.detail?.item_id === itemId) return;
      await loadItem(itemId, revision);
      return;
    }
    if (state.detail) {
      location.hash = `#item=${state.detail.item_id}`;  // 切卷被拒绝：恢复 hash 与视图一致
      return;
    }
  }
  await loadItem(itemId, revision);
}

(async function init() {
  bindEvents();
  try {
    restoreFiltersFromUrl();
    const hadHash = /^#item=/.test(location.hash);
    await applyFilters({ initial: true });
    if (hadHash && !state.detail) await route();
  } catch (err) {
    $("#page-status").textContent = `加载失败：${errMessages(err)}`;
    $("#page-status").hidden = false;
  }
})();
