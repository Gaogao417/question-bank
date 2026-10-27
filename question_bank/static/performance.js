/* Local, bounded samples only: no content, identifiers, credentials, or uploads. */
(() => {
  const samples = [];
  window.qbPerformance = {
    record(name, start, outcome = "ok") {
      samples.push({ name, ms: Math.round((performance.now() - start) * 10) / 10, outcome });
      if (samples.length > 500) samples.shift();
    },
    samples: () => samples.map(sample => ({ ...sample })),
    summary() {
      return [...new Set(samples.map(s => s.name))].map(name => {
        const all = samples.filter(s => s.name === name);
        const ok = all.filter(s => s.outcome === "ok").map(s => s.ms).sort((a, b) => a - b);
        const percentile = p => ok.length ? ok[Math.ceil(ok.length * p) - 1] : null;
        return { name, count: all.length, failures: all.filter(s => s.outcome === "error").length,
          canceled: all.filter(s => s.outcome === "canceled").length,
          p50: percentile(.5), p95: percentile(.95), p99: percentile(.99) };
      });
    },
    clear: () => { samples.length = 0; },
  };
})();
