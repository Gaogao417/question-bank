/* Formula loading never gates the catalog or plain-text reader. */
(() => {
  const pending = new Set();
  let ready = false;
  let requested = false;
  function requestLibrary() {
    if (requested) return;
    requested = true;
    const script = document.getElementById("mathjax-script");
    if (!script?.dataset?.src) return;
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (!ready) message("公式仍在加载，正文与目录可先使用。");
    }, 8000);
    // Give the reader a painted frame before downloading/executing the large bundle.
    const start = () => { if (!script.src) script.src = script.dataset.src; };
    requestAnimationFrame(() => requestAnimationFrame(start));
    setTimeout(start, 1000); // Background tabs may not receive animation frames.
  }
  let chain = Promise.resolve();
  const status = () => document.getElementById("formula-status");
  function message(text) {
    const node = status();
    if (node) { node.textContent = text; node.hidden = !text; }
  }
  function flush() {
    if (!ready || !pending.size) return;
    // MathJax maintains shared state; serialize typesetting, including after navigation.
    chain = chain.then(async () => {
      const nodes = [...pending].filter(node => node.isConnected);
      pending.clear();
      if (!nodes.length) return;
      window.MathJax.typesetClear(nodes);
      await window.MathJax.typesetPromise(nodes);
      message("");
    }).catch(error => {
      message("公式排版暂不可用，正文与目录可正常使用。");
      console.warn("MathJax typesetting failed", error);
    });
  }
  window.QuestionMath = {
    typeset(node) {
      requestLibrary();
      // Do not retain detached readers while the CDN is unavailable.
      for (const old of pending) if (!old.isConnected) pending.delete(old);
      pending.add(node);
      flush();
    },
  };
  let started = false;
  function loaded() {
    if (started || !window.MathJax?.startup?.promise) return;
    started = true;
    window.MathJax.startup.promise.then(() => {
      ready = true;
      clearTimeout(timer);
      message("");
      flush();
    }).catch(failed);
  }
  function failed() {
    clearTimeout(timer);
    message("公式库加载失败，正文与目录可正常使用；可稍后刷新重试。");
  }
  let timer = setTimeout(() => {
    if (requested && !ready) message("公式仍在加载，正文与目录可先使用。");
  }, 8000);
  const script = document.getElementById("mathjax-script");
  script?.addEventListener("load", loaded);
  script?.addEventListener("error", failed);
  loaded(); // The async script may already have finished before this deferred file.
})();
