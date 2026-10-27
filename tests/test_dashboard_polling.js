"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  TaskPoller,
  nextDelay,
  shouldApplyResponse,
  formatDuration,
  shortId,
} = require("../dashboard/static/dashboard.js");

const SNAPSHOT = {
  generated_at: "2026-10-24T12:00:00+00:00",
  running: [{ task_id: "1", status: "running" }],
  queued: [],
  recent: [],
};

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function fakeScheduler() {
  let nextId = 1;
  const timers = new Map();
  return {
    setTimeout(fn, delay) {
      const id = nextId++;
      timers.set(id, { fn, delay });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    pending() {
      return Array.from(timers.values());
    },
    async runNext() {
      const first = timers.entries().next();
      if (first.done) return null;
      const [id, timer] = first.value;
      timers.delete(id);
      return timer.fn();
    },
  };
}

function makePoller(fetchTasks, scheduler, extra) {
  return new TaskPoller({
    fetchTasks,
    setTimeoutFn: scheduler.setTimeout.bind(scheduler),
    clearTimeoutFn: scheduler.clearTimeout.bind(scheduler),
    requestTimeoutFn: () => 0,
    clearRequestTimeoutFn: () => {},
    ...extra,
  });
}

test("refresh cadence is ten seconds and failure backoff is bounded", () => {
  assert.equal(nextDelay(0), 10000);
  assert.equal(nextDelay(1), 10000);
  assert.equal(nextDelay(2), 20000);
  assert.equal(nextDelay(3), 40000);
  assert.equal(nextDelay(4), 60000);
  assert.equal(nextDelay(20), 60000);
});

test("older responses cannot overwrite newer state", () => {
  assert.equal(shouldApplyResponse(2, 1), true);
  assert.equal(shouldApplyResponse(1, 2), false);
  assert.equal(shouldApplyResponse(2, 2), false);
});

test("polling does not overlap and schedules the next refresh", async () => {
  const scheduler = fakeScheduler();
  let resolveFetch;
  let calls = 0;
  const snapshots = [];
  const poller = makePoller(() => {
    calls += 1;
    return new Promise((resolve) => { resolveFetch = resolve; });
  }, scheduler, { onSnapshot: (data) => snapshots.push(data) });

  const running = poller.start();
  assert.equal(calls, 1);
  assert.equal(await poller.refresh("manual"), false);
  assert.equal(calls, 1);

  resolveFetch(SNAPSHOT);
  assert.equal(await running, true);
  assert.equal(snapshots.length, 1);
  assert.equal(scheduler.pending().length, 1);
  assert.equal(scheduler.pending()[0].delay, 10000);
});

test("manual refresh fetches immediately and resets the cadence", async () => {
  const scheduler = fakeScheduler();
  let calls = 0;
  const snapshots = [];
  const poller = makePoller(async () => {
    calls += 1;
    return SNAPSHOT;
  }, scheduler, { onSnapshot: (data) => snapshots.push(data) });

  await poller.start();
  assert.equal(calls, 1);
  assert.equal(await poller.refresh("manual"), true);
  assert.equal(calls, 2);
  assert.equal(snapshots.length, 2);
  assert.equal(scheduler.pending().length, 1);
  assert.equal(scheduler.pending()[0].delay, 10000);
});

test("failed refresh preserves prior data and backs off, then recovers", async () => {
  const scheduler = fakeScheduler();
  let calls = 0;
  let fail = true;
  const snapshots = [];
  const errors = [];
  const poller = makePoller(async () => {
    calls += 1;
    if (fail) throw new Error("offline");
    return SNAPSHOT;
  }, scheduler, {
    onSnapshot: (data) => snapshots.push(data),
    onError: (error, failures) => errors.push(failures),
  });

  await poller.start();
  assert.equal(snapshots.length, 0);
  assert.deepEqual(errors, [1]);
  assert.equal(scheduler.pending()[0].delay, 10000);

  await scheduler.runNext();
  assert.deepEqual(errors, [1, 2]);
  assert.equal(scheduler.pending()[0].delay, 20000);

  fail = false;
  await scheduler.runNext();
  assert.equal(snapshots.length, 1);
  assert.equal(errors.length, 2);
  assert.equal(scheduler.pending()[0].delay, 10000);
});

test("a failed refresh after success keeps the last snapshot and reports the error", async () => {
  const scheduler = fakeScheduler();
  let calls = 0;
  const snapshots = [];
  const errors = [];
  const poller = makePoller(async () => {
    calls += 1;
    if (calls > 1) throw new Error("offline");
    return SNAPSHOT;
  }, scheduler, {
    onSnapshot: (data) => snapshots.push(data),
    onError: (error, failures) => errors.push(failures),
  });

  await poller.start();
  assert.equal(snapshots.length, 1);
  await poller.refresh("manual");
  assert.equal(snapshots.length, 1);
  assert.deepEqual(errors, [1]);
});

test("hidden pages pause polling, abort in-flight work and refresh on visibility", async () => {
  const scheduler = fakeScheduler();
  let calls = 0;
  const snapshots = [];
  const poller = makePoller(async () => {
    calls += 1;
    return SNAPSHOT;
  }, scheduler, { onSnapshot: (data) => snapshots.push(data) });

  await poller.start();
  assert.equal(scheduler.pending().length, 1);
  poller.setHidden(true);
  assert.equal(scheduler.pending().length, 0);

  poller.setHidden(false);
  await flush();
  assert.equal(calls, 2);
  assert.equal(snapshots.length, 2);
});

test("visibility changes during an in-flight request refresh immediately after abort", async () => {
  const scheduler = fakeScheduler();
  let calls = 0;
  const snapshots = [];
  const poller = makePoller(() => {
    calls += 1;
    if (calls === 1) return new Promise(() => {});
    return Promise.resolve(SNAPSHOT);
  }, scheduler, { onSnapshot: (data) => snapshots.push(data) });

  const first = poller.start();
  poller.setHidden(true);
  poller.setHidden(false);
  await first;
  await flush();

  assert.equal(calls, 2);
  assert.equal(snapshots.length, 1);
  assert.equal(scheduler.pending()[0].delay, 10000);
});

test("stopping removes the timer and ignores later refreshes", async () => {
  const scheduler = fakeScheduler();
  let calls = 0;
  const poller = makePoller(async () => {
    calls += 1;
    return SNAPSHOT;
  }, scheduler);

  await poller.start();
  assert.equal(scheduler.pending().length, 1);
  poller.stop();
  assert.equal(scheduler.pending().length, 0);
  assert.equal(await poller.refresh("manual"), false);
  assert.equal(calls, 1);
});

test("authentication failure stops polling instead of retrying unauthorised requests", async () => {
  const scheduler = fakeScheduler();
  const authErrors = [];
  const poller = makePoller(async () => {
    const error = new Error("unauthorised");
    error.auth = true;
    throw error;
  }, scheduler, { onAuthError: (error) => authErrors.push(error) });

  await poller.start();
  assert.equal(authErrors.length, 1);
  assert.equal(poller.running, false);
  assert.equal(scheduler.pending().length, 0);
  assert.equal(await poller.refresh("auto"), false);
});

test("display helpers are deterministic", () => {
  assert.equal(shortId("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"), "aaaaaaaa…");
  assert.equal(shortId(""), "—");
  assert.equal(formatDuration(12), "12s");
  assert.equal(formatDuration(125), "2m");
  assert.equal(formatDuration(3725), "1h 2m");
});


test("New Task never offers unproven or response-only workers for coding", () => {
  const { workerIntentAllowed } = require("../dashboard/static/dashboard.js");
  assert.equal(workerIntentAllowed("analysis", "false"), true);
  assert.equal(workerIntentAllowed("code_change", "false"), false);
  assert.equal(workerIntentAllowed("code_change", undefined), false);
  assert.equal(workerIntentAllowed("code_change", "true"), true);
  assert.equal(workerIntentAllowed("deployment", "true"), false);
});
