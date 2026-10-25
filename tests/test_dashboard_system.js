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
    state: 'available',
    pixelbox: {
      health: malicious,
      system: {
        uptime: '6h 8m', load: '0.36 / 0.51 / 0.48', memory: '771 MB used of 3794 MB',
        temperature_c: 41.9,
        storage: { online: true, percent: 6, text: '411 GB free of 458 GB' },
        tailscale: 'up'
      },
      photo: {
        connected: 'down', waiting: 0, queued: 2, verifying: 0, backed_up: 114,
        db_available: 'up', storage: { percent: 6, text: '411 GB free of 458 GB' },
        timers: { 'pixelbox-v2-receiver.timer': 'up', [malicious]: malicious },
        recent: [{ status: 'BACKED_UP', event_at: '2026-10-17 04:37:14' }]
      },
      services: { dashboard: 'up', [malicious]: malicious }
    },
    mbox: {
      hdd: 'Online', nextcloud: 'Running',
      storage: { online: true, percent: 0, text: '1734 GB free of 1833 GB' }
    },
    nextcloud: { state: 'Running', docker: 'up' },
    orchestra_services: { 'OMCP coordinator': 'up' },
    public: { host: 'mcp.delnavazan.com', state: 'reachable', http_status: 403 }
  };
}
function response(data) { return { ok: true, headers: { get: () => 'application/json' }, json: async () => data }; }

test('overview renders the unified projection as literal text; GET only, no-store', async () => {
  const calls = [];
  const h = harness(async (url, options) => { calls.push([url, options]); return response(snapshot()); });
  await flush();
  assert.equal(calls[0][0], '/api/system');
  assert.equal(calls[0][1].method, 'GET');
  assert.equal(calls[0][1].cache, 'no-store');
  assert.match(h.nodes['system-pixelbox'].textContent, /6h 8m/);
  assert.match(h.nodes['system-pixelbox'].textContent, /41.9 °C/);
  assert.match(h.nodes['system-pixelbox'].textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(h.nodes['system-photo'].textContent, /Queued: 2/);
  assert.match(h.nodes['system-photo-timers'].textContent, /pixelbox-v2-receiver.timer: up/);
  assert.match(h.nodes['system-photo-recent'].textContent, /2026-10-17 04:37:14 · BACKED_UP/);
  assert.match(h.nodes['system-mbox'].textContent, /1734 GB free of 1833 GB/);
  assert.match(h.nodes['system-nextcloud'].textContent, /Docker container: up/);
  assert.match(h.nodes['system-pixelbox-services'].textContent, /<img src=x onerror=alert\(1\)>: unavailable/);
  assert.match(h.nodes['system-orchestra-services'].textContent, /OMCP coordinator: up/);
  assert.match(h.nodes['system-public'].textContent, /HTTP 403/);
  assert.equal(h.nodes['system-pixelbox'].innerHTML, undefined);
  assert.equal(h.nodes['refresh-system'].disabled, false);
  h.document.hidden = true;
  h.timers[0]();
  await flush();
  assert.equal(calls.length, 1);
});

test('unavailable projection is explicit and does not erase public health', async () => {
  const h = harness(async () => response({
    generated_at: '2026-10-25T12:00:00Z', state: 'unavailable',
    pixelbox: { system: {}, photo: {}, services: {} },
    mbox: {}, nextcloud: {},
    orchestra_services: {},
    public: { host: 'mcp.delnavazan.com', state: 'healthy', http_status: 200 }
  }));
  await flush();
  assert.match(h.nodes['system-pixelbox'].textContent, /PixelBox status unavailable/);
  assert.match(h.nodes['system-public'].textContent, /healthy \(HTTP 200\)/);
});

test('failed refresh retains values and marks them stale; initial failure is unavailable', async () => {
  let count = 0;
  const h = harness(async () => { if (count++ === 0) return response(snapshot()); throw new Error('SECRET'); });
  await flush();
  const before = h.nodes['system-pixelbox'].textContent;
  h.listeners['refresh-systemclick']();
  await flush();
  assert.equal(h.nodes['system-pixelbox'].textContent, before);
  assert.match(h.nodes['system-updated'].textContent, /stale/);
  assert.doesNotMatch(h.nodes['system-updated'].textContent, /SECRET/);
  const failed = harness(async () => { throw new Error('SECRET'); });
  await flush();
  assert.equal(failed.nodes['system-updated'].textContent, 'System overview unavailable');
});
