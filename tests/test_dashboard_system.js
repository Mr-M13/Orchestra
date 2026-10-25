"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../dashboard/static/system.js"), "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness(fetch) {
  const nodes = {};
  const listeners = {};
  const timers = [];
  const document = {
    hidden: false,
    getElementById(id) {
      if (!nodes[id]) nodes[id] = { textContent: "", addEventListener(event, fn) { listeners[id + event] = fn; } };
      return nodes[id];
    },
    addEventListener(event, fn) { listeners[event] = fn; }
  };
  vm.runInNewContext(source, {
    document, window: { addEventListener() {} }, fetch, AbortController,
    setTimeout() { return 1; }, clearTimeout() {},
    setInterval(fn) { timers.push(fn); return 2; }, clearInterval() {}
  });
  return { nodes, listeners, timers, document };
}
const malicious = '<img src=x onerror=alert(1)>';
function snapshot() {
  return {
    generated_at: '2026-10-25T12:00:00Z',
    metrics: { load: [1, 2, 3], cpu_count: 4, memory: { used_bytes: 1073741824, total_bytes: 2147483648 }, disk: null, uptime_seconds: 3600, temperature_c: null },
    services: { docker: 'active', [malicious]: malicious },
    docker: { state: 'available', running: 2, containers: 3, nextcloud_running: 1, nextcloud_containers: 1 },
    public: { host: 'mcp.delnavazan.com', state: 'reachable', http_status: 403 },
    tailscale: { state: 'Running', addresses: ['100.64.0.1'] }
  };
}
function response(data) { return { ok: true, headers: { get: () => 'application/json' }, json: async () => data }; }

test('overview renders metrics and literal untrusted text; GET only, no-store', async () => {
  const calls = [];
  const h = harness(async (url, options) => { calls.push([url, options]); return response(snapshot()); });
  await flush();
  assert.equal(calls[0][0], '/api/system');
  assert.equal(calls[0][1].method, 'GET');
  assert.equal(calls[0][1].cache, 'no-store');
  assert.match(h.nodes['system-services'].textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(h.nodes['system-services'].innerHTML, undefined);
  assert.match(h.nodes['system-memory'].textContent, /1.0 GiB used \/ 2.0 GiB/);
  assert.equal(h.nodes['system-disk'].textContent, 'Unavailable');
  assert.match(h.nodes['system-docker'].textContent, /Nextcloud 1 \/ 1 running/);
  assert.match(h.nodes['system-public'].textContent, /HTTP 403/);
  assert.match(h.nodes['system-tailscale'].textContent, /100.64.0.1/);
  assert.equal(h.nodes['refresh-system'].disabled, false);
  h.document.hidden = true;
  h.timers[0]();
  await flush();
  assert.equal(calls.length, 1);
});

test('failed refresh retains values and marks them stale; initial failure is unavailable', async () => {
  let count = 0;
  const h = harness(async () => { if (count++ === 0) return response(snapshot()); throw new Error('SECRET'); });
  await flush();
  const before = h.nodes['system-load'].textContent;
  h.listeners['refresh-systemclick']();
  await flush();
  assert.equal(h.nodes['system-load'].textContent, before);
  assert.match(h.nodes['system-updated'].textContent, /stale/);
  assert.doesNotMatch(h.nodes['system-updated'].textContent, /SECRET/);
  const failed = harness(async () => { throw new Error('SECRET'); });
  await flush();
  assert.equal(failed.nodes['system-updated'].textContent, 'System overview unavailable');
});
