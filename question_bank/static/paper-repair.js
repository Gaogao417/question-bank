/* Server-owned finite jobs; browsing and local drafts stay independent. */
let paperRepairTimer = null;
let paperRepairStarting = false;
const paperRepairSeen = new Map();
const paperRepairError = (err) =>
  typeof err.detail === "string" ? err.detail : errMessages(err);

function renderPaperRepair(job) {
  const running = job && ["running", "stopping"].includes(job.status);
  const start = $("#repair-paper");
  start.disabled =
    paperRepairStarting || running || !state.paperId || !state.items.length;
  $("#stop-paper-repair").hidden = !running;
  $("#stop-paper-repair").disabled = job?.status === "stopping";
  const message = $("#paper-repair-message");
  const results = $("#paper-repair-results");
  results.replaceChildren();
  if (!job) {
    message.textContent = "自动修复并保存已有疑点，无需逐题接受。";
    return;
  }
  const counts = {};
  for (const item of job.items)
    counts[item.status] = (counts[item.status] || 0) + 1;
  const current = job.items.find((i) => i.status === "running");
  message.textContent = `${job.processed}/${job.total} 题 · 已修复 ${counts.fixed || 0} · 部分修复 ${counts.partial || 0} · 未确定 ${counts.unresolved || 0} · 失败/冲突 ${(counts.failed || 0) + (counts.conflict || 0)}${current ? ` · 正在处理第 ${current.question_number} 题` : ""}。${job.message}`;
  const labels = {
    fixed: "已修复",
    partial: "部分修复",
    unresolved: "未确定",
    failed: "失败",
    conflict: "版本冲突",
    stopped: "未应用",
    interrupted: "已中断",
  };
  for (const item of job.items) {
    if (!labels[item.status]) continue;
    const label =
      item.status === "fixed" ? "已修复，待查看" : labels[item.status];
    const link = el(
      "a",
      { href: `#item=${encodeURIComponent(item.item_id)}` },
      `第 ${item.question_number} 题 · ${label}${item.revision ? ` r${item.revision}` : ""}`,
    );
    const notes = (item.issue_decisions || [])
      .filter((i) => i.state === "open")
      .map((i) => i.note);
    results.append(
      el(
        "li",
        null,
        link,
        el(
          "p",
          null,
          item.error || [item.reason, ...notes].filter(Boolean).join("；"),
        ),
      ),
    );
  }
}

async function refreshPaperRepair() {
  clearTimeout(paperRepairTimer);
  const paper = state.paperId;
  if (!paper) {
    renderPaperRepair(null);
    return;
  }
  try {
    const { job } = await api(
      `/api/papers/${encodeURIComponent(paper)}/auto-repair`,
    );
    if (state.paperId !== paper) return;
    renderPaperRepair(job);
    if (job) {
      const saved = job.items.filter((i) => i.revision);
      const changed = saved.filter(
        (i) => paperRepairSeen.get(i.item_id) !== i.revision,
      );
      for (const item of changed) {
        paperRepairSeen.set(item.item_id, item.revision);
        state.itemCache.delete(item.item_id);
      }
      if (changed.length) {
        await loadItemList();
        if (state.paperId !== paper) return;
        const d = state.detail;
        if (
          d?.is_current &&
          changed.some(
            (i) => i.item_id === d.item_id && i.revision !== d.revision,
          )
        ) {
          if (
            state.dirty ||
            state.editing ||
            savingEdit ||
            assistBusy ||
            lightboxState.job ||
            lightboxState.pending ||
            !$("#source-lightbox").hidden
          ) {
            setMessage(
              "#manage-message",
              "本题已自动修复。当前编辑已保留，保存时将检查版本冲突；可退出编辑后刷新查看。",
              false,
            );
          } else {
            await loadItem(d.item_id);
          }
        }
        recomputePaperStats(paper);
        updatePaperMeta();
      }
    }
    // Also discover jobs started in another tab, without touching current drafts.
    if (state.paperId === paper)
      paperRepairTimer = setTimeout(
        refreshPaperRepair,
        job && ["running", "stopping"].includes(job.status) ? 2000 : 10000,
      );
  } catch (err) {
    if (state.paperId !== paper) return;
    setMessage(
      "#paper-repair-message",
      `进度读取失败：${paperRepairError(err)}；后台任务继续，可刷新重连。`,
      true,
    );
    paperRepairTimer = setTimeout(refreshPaperRepair, 5000);
  }
}

async function startPaperRepair() {
  if (paperRepairStarting || !state.paperId || !state.items.length) return;
  const paper = state.paperId;
  paperRepairStarting = true;
  let started = false;
  $("#repair-paper").disabled = true;
  try {
    // Save the current draft before taking the entire paper's revision snapshot.
    if (
      savingEdit ||
      assistBusy ||
      lightboxState.job ||
      lightboxState.pending
    ) {
      setMessage(
        "#paper-repair-message",
        "请等当前保存、修订或截图完成后再启动。",
        true,
      );
      return;
    }
    if (state.dirty && !(await saveEdits())) return;
    if (state.dirty || state.paperId !== paper) return;
    const key = `qb-paper-repair-request:${paper}`;
    const operation =
      sessionStorage.getItem(key) || `paper-repair-${crypto.randomUUID()}`;
    sessionStorage.setItem(key, operation); // Retry an uncertain network outcome with the same ID.
    await api(`/api/papers/${encodeURIComponent(paper)}/auto-repair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operation_id: operation }),
    });
    sessionStorage.removeItem(key);
    started = true;
  } catch (err) {
    if (err.status)
      sessionStorage.removeItem(`qb-paper-repair-request:${paper}`);
    if (state.paperId === paper)
      setMessage(
        "#paper-repair-message",
        `启动失败：${paperRepairError(err)}`,
        true,
      );
  } finally {
    paperRepairStarting = false;
    if (state.paperId === paper) {
      $("#repair-paper").disabled = false;
      if (started) await refreshPaperRepair();
    }
  }
}

async function stopPaperRepair() {
  const paper = state.paperId;
  if (!paper) return;
  try {
    await api(`/api/papers/${encodeURIComponent(paper)}/auto-repair/stop`, {
      method: "POST",
    });
    if (state.paperId === paper) await refreshPaperRepair();
  } catch (err) {
    if (state.paperId === paper)
      setMessage(
        "#paper-repair-message",
        `停止失败：${paperRepairError(err)}`,
        true,
      );
  }
}
