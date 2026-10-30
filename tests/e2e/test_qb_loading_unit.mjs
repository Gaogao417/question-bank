import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../question_bank/static/math-renderer.js', import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup(lazy = false) {
  const frames = [];
  const listeners = {};
  const status = { hidden: true, textContent: '' };
  const script = { src: '', dataset: lazy ? { src: '/local-math.js' } : {}, addEventListener: (event, fn) => { listeners[event] = fn; } };
  const window = { MathJax: {} };
  let timeout;
  vm.runInNewContext(source, { window, console,
    document: { getElementById: id => id === 'formula-status' ? status : script },
    requestAnimationFrame: fn => frames.push(fn),
    setTimeout: fn => { timeout = fn; return 1; }, clearTimeout() {},
  });
  return { window, listeners, status, script, frames, timeout: () => timeout() };
}
const lazy = setup(true);
assert.equal(lazy.script.src, '');
lazy.window.QuestionMath.typeset({ isConnected: true });
assert.equal(lazy.script.src, '');
lazy.frames.shift()();
assert.equal(lazy.script.src, '');
lazy.frames.shift()();
assert.equal(lazy.script.src, '/local-math.js');
const ctx = setup();
const old = { isConnected: true };
ctx.window.QuestionMath.typeset(old); // Returns immediately even if CDN never responds.
ctx.timeout();
assert.match(ctx.status.textContent, /仍在加载/);
old.isConnected = false;
const current = { isConnected: true };
ctx.window.QuestionMath.typeset(current);
const rendered = [];
ctx.window.MathJax = { startup: { promise: Promise.resolve() }, typesetClear() {},
  typesetPromise: async nodes => { rendered.push(...nodes); } };
ctx.listeners.load();
await tick();
assert.deepEqual(rendered, [current]);
assert.equal(ctx.status.hidden, true);
const failed = setup();
failed.listeners.error();
assert.match(failed.status.textContent, /加载失败/);
failed.window.QuestionMath.typeset({ isConnected: true });

const metrics = { window: {}, performance: { now: () => 100 } };
vm.runInNewContext(fs.readFileSync(new URL('../../question_bank/static/performance.js', import.meta.url), 'utf8'), metrics);
for (let i = 0; i < 510; i++) metrics.window.qbPerformance.record('catalog', 0);
metrics.window.qbPerformance.record('catalog', 0, 'canceled');
metrics.window.qbPerformance.record('catalog', 0, 'error');
assert.equal(metrics.window.qbPerformance.samples().length, 500);
const summary = metrics.window.qbPerformance.summary()[0];
assert.equal(summary.p95, 100);
assert.equal(summary.failures, 1);
assert.equal(summary.canceled, 1);
metrics.window.qbPerformance.samples()[0].name = 'mutated';
assert.equal(metrics.window.qbPerformance.samples()[0].name, 'catalog');
console.log('PASS: blocked/failed CDN, late formula rendering, detached nodes, bounded metrics');
