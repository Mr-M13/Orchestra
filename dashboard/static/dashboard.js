/* Orchestra dashboard task monitor. Server-rendered first, updated in place. */
(function (root, factory) {
  "use strict";
  var api = factory(root);
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", api.init, { once: true });
    } else {
      api.init();
    }
  }
})(typeof window !== "undefined" ? window : globalThis, function (root) {
  "use strict";

  var REFRESH_MS = 10000;
  var MAX_BACKOFF_MS = 60000;
  var REQUEST_TIMEOUT_MS = 8000;
  var STORAGE_KEY = "orchestra.dashboard.sections.v1";
  var SECTION_NAMES = ["running", "queued", "recent"];

  var STATUS_LABELS = {
    starting: "Starting",
    running: "Running",
    cancel_requested: "Cancelling",
    accepted: "Queued",
    approval_required: "Awaiting approval",
    succeeded: "Succeeded",
    failed: "Failed",
    cancelled: "Cancelled",
    rejected: "Rejected",
    interrupted: "Interrupted"
  };

  function nextDelay(failures, intervalMs, maxBackoffMs) {
    var base = intervalMs || REFRESH_MS;
    var cap = maxBackoffMs || MAX_BACKOFF_MS;
    if (!failures || failures < 1) return base;
    return Math.min(cap, base * Math.pow(2, Math.max(0, failures - 1)));
  }

  function shouldApplyResponse(sequence, lastApplied) {
    return Number.isFinite(sequence) && sequence > lastApplied;
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    seconds = Math.floor(seconds);
    if (seconds < 60) return seconds + "s";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m";
    if (seconds < 86400) {
      return Math.floor(seconds / 3600) + "h " + Math.floor((seconds % 3600) / 60) + "m";
    }
    return Math.floor(seconds / 86400) + "d " + Math.floor((seconds % 86400) / 3600) + "h";
  }

  function elapsedSeconds(startedAt) {
    if (typeof startedAt !== "string" || !startedAt) return null;
    var started = Date.parse(startedAt);
    if (!Number.isFinite(started)) return null;
    return Math.max(0, Math.floor((Date.now() - started) / 1000));
  }

  function formatTimestamp(value) {
    if (typeof value !== "string" || !value) return "—";
    return value.replace("T", " ").replace("+00:00", " UTC");
  }

  function shortId(value) {
    var text = String(value || "");
    if (!text) return "—";
    return text.length > 8 ? text.slice(0, 8) + "…" : text;
  }

  function statusLabel(value) {
    var status = String(value || "unknown");
    return STATUS_LABELS[status] || status.replace(/_/g, " ");
  }

  function safeStatus(value) {
    return String(value || "unknown").toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  }

  function TaskPoller(options) {
    options = options || {};
    if (typeof options.fetchTasks !== "function") {
      throw new TypeError("fetchTasks is required");
    }
    this.fetchTasks = options.fetchTasks;
    this.onSnapshot = options.onSnapshot || function () {};
    this.onError = options.onError || function () {};
    this.onAuthError = options.onAuthError || function () {};
    this.onBusyChange = options.onBusyChange || function () {};
    this.intervalMs = options.intervalMs || REFRESH_MS;
    this.maxBackoffMs = options.maxBackoffMs || MAX_BACKOFF_MS;
    this.requestTimeoutMs = options.requestTimeoutMs || REQUEST_TIMEOUT_MS;
    this.setTimeoutFn = options.setTimeoutFn || root.setTimeout.bind(root);
    this.clearTimeoutFn = options.clearTimeoutFn || root.clearTimeout.bind(root);
    this.requestTimeoutFn = options.requestTimeoutFn || root.setTimeout.bind(root);
    this.clearRequestTimeoutFn = options.clearRequestTimeoutFn || root.clearTimeout.bind(root);
    this.running = false;
    this.inFlight = false;
    this.hidden = false;
    this.timer = null;
    this.controller = null;
    this.failures = 0;
    this.sequence = 0;
    this.lastApplied = 0;
    this.pendingRefresh = false;
  }

  TaskPoller.prototype.start = function () {
    if (this.running) return Promise.resolve(false);
    this.running = true;
    this.hidden = typeof document !== "undefined" && Boolean(document.hidden);
    if (this.hidden) return Promise.resolve(false);
    if (this.inFlight) {
      this.pendingRefresh = true;
      return Promise.resolve(false);
    }
    return this.refresh("initial");
  };

  TaskPoller.prototype.stop = function () {
    this.running = false;
    this.pendingRefresh = false;
    this._clearTimer();
    if (this.controller) {
      this.controller.abort();
      this.controller = null;
    }
  };

  TaskPoller.prototype.setHidden = function (hidden) {
    if (this.hidden === Boolean(hidden)) return;
    this.hidden = Boolean(hidden);
    if (this.hidden) {
      this.pendingRefresh = false;
      this._clearTimer();
      if (this.controller) this.controller.abort();
      return;
    }
    if (this.running) {
      this.pendingRefresh = true;
      this.refresh("visible");
    }
  };

  TaskPoller.prototype.refresh = function (trigger) {
    var self = this;
    trigger = trigger || "auto";
    if (!this.running || this.inFlight) return Promise.resolve(false);
    if (this.hidden && trigger !== "manual") return Promise.resolve(false);

    this._clearTimer();
    this.pendingRefresh = false;
    this.inFlight = true;
    this.onBusyChange(true);
    var sequence = ++this.sequence;
    var controller = new root.AbortController();
    this.controller = controller;

    return this._request(controller).then(function (data) {
      if (!self.running) return false;
      if (!shouldApplyResponse(sequence, self.lastApplied)) return false;
      self.lastApplied = sequence;
      self.failures = 0;
      self.onSnapshot(data);
      return true;
    }).catch(function (error) {
      if (error && error.name === "AbortError" && (!self.running || self.hidden)) {
        return false;
      }
      if (error && error.auth) {
        self.running = false;
        self._clearTimer();
        self.onAuthError(error);
        return false;
      }
      self.failures += 1;
      self.onError(error, self.failures);
      return false;
    }).then(function (applied) {
      self.inFlight = false;
      self.controller = null;
      self.onBusyChange(false);
      if (self.running && !self.hidden) {
        if (self.pendingRefresh) {
          self.pendingRefresh = false;
          self.refresh("visible");
        } else {
          var delay = self.failures
            ? nextDelay(self.failures, self.intervalMs, self.maxBackoffMs)
            : self.intervalMs;
          self._schedule(delay);
        }
      }
      return applied;
    });
  };

  TaskPoller.prototype._request = function (controller) {
    var self = this;
    return new Promise(function (resolve, reject) {
      var settled = false;
      var requestTimer = self.requestTimeoutFn(function () {
        fail(new Error("Task refresh timed out"));
        controller.abort();
      }, self.requestTimeoutMs);

      function finish(callback, value) {
        if (settled) return;
        settled = true;
        self.clearRequestTimeoutFn(requestTimer);
        callback(value);
      }

      function fail(error) {
        finish(reject, error);
      }

      if (controller.signal && typeof controller.signal.addEventListener === "function") {
        controller.signal.addEventListener("abort", function () {
          var error = new Error("Task refresh aborted");
          error.name = "AbortError";
          fail(error);
        }, { once: true });
      }

      Promise.resolve(self.fetchTasks({ signal: controller.signal })).then(function (data) {
        finish(resolve, data);
      }, fail);
    });
  };

  TaskPoller.prototype._schedule = function (delay) {
    var self = this;
    this._clearTimer();
    this.timer = this.setTimeoutFn(function () {
      self.timer = null;
      return self.refresh("auto");
    }, delay);
  };

  TaskPoller.prototype._clearTimer = function () {
    if (this.timer !== null) {
      this.clearTimeoutFn(this.timer);
      this.timer = null;
    }
  };

  function loadSectionPreferences() {
    try {
      var value = JSON.parse(root.localStorage.getItem(STORAGE_KEY) || "{}");
      return value && typeof value === "object" ? value : {};
    } catch (_) {
      return {};
    }
  }

  function saveSectionPreferences(preferences) {
    try {
      root.localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
    } catch (_) {
      /* Storage can be unavailable in private or locked-down contexts. */
    }
  }

  function makeElement(tag, className, text) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function buildTaskRow() {
    var row = makeElement("article", "task-row");
    var main = makeElement("div", "task-main");
    var heading = makeElement("div", "task-heading");
    var title = makeElement("a", "task-title");
    var id = makeElement("code", "task-id");
    var meta = makeElement("div", "task-meta");
    heading.appendChild(title);
    heading.appendChild(id);
    main.appendChild(heading);
    main.appendChild(meta);
    var side = makeElement("div", "task-side");
    var state = makeElement("span", "state");
    var elapsed = makeElement("span", "elapsed");
    var details = makeElement("a", "task-details", "Details");
    side.appendChild(state);
    side.appendChild(elapsed);
    side.appendChild(details);
    row.appendChild(main);
    row.appendChild(side);
    return row;
  }

  function updateTaskRow(row, task, section) {
    var taskId = String(task.task_id || "");
    var titleText = String(task.title || "Untitled task");
    var status = String(task.status || "unknown");
    var worker = String(task.worker || "Worker not selected");
    var progress = String(task.progress || "");
    var href = "/tasks/" + encodeURIComponent(taskId);
    var queueState = task.queue_state;
    var failed = section === "recent" && (status === "failed" || status === "interrupted");

    row.className = "task-row" +
      (failed ? " is-failed" : "") +
      (queueState === "approval_pending" ? " is-approval" : "");
    row.dataset.taskId = taskId;
    row.dataset.status = status;

    var title = row.querySelector(".task-title");
    title.href = href;
    title.textContent = titleText;
    title.title = titleText;

    var id = row.querySelector(".task-id");
    id.textContent = shortId(taskId);
    id.title = taskId;

    var meta = row.querySelector(".task-meta");
    meta.replaceChildren();
    var workerItem = makeElement("span");
    workerItem.appendChild(document.createTextNode("Worker "));
    workerItem.appendChild(makeElement("strong", "", worker));
    meta.appendChild(workerItem);

    if (section === "running") {
      var started = task.started_at || task.created_at;
      meta.appendChild(makeElement("span", "", "Started " + formatTimestamp(started)));
    } else if (section === "queued") {
      meta.appendChild(makeElement("span", "", "Submitted " + formatTimestamp(task.created_at)));
      if (queueState === "approval_pending") {
        meta.appendChild(makeElement("span", "queue-state queue-approval", "Awaiting approval"));
      } else {
        meta.appendChild(makeElement("span", "queue-state queue-ready", "Ready to execute"));
      }
    } else {
      var terminalAt = task.terminal_at || task.finished_at || task.created_at;
      meta.appendChild(makeElement("span", "", "Completed " + formatTimestamp(terminalAt)));
    }
    if (progress) {
      meta.appendChild(makeElement("span", "task-progress", progress));
    }

    var state = row.querySelector(".state");
    state.className = "state state-" + safeStatus(status);
    state.textContent = statusLabel(status);

    var elapsed = row.querySelector(".elapsed");
    if (section === "running") {
      var elapsedFrom = task.started_at || task.created_at || "";
      elapsed.hidden = false;
      elapsed.dataset.startedAt = elapsedFrom;
      var seconds = elapsedSeconds(elapsedFrom);
      elapsed.textContent = seconds === null ? "—" : formatDuration(seconds);
    } else {
      elapsed.hidden = true;
      elapsed.textContent = "";
      delete elapsed.dataset.startedAt;
    }

    var details = row.querySelector(".task-details");
    details.href = href;
    details.setAttribute("aria-label", "Details for " + titleText);
  }

  function updateSection(name, tasks) {
    var panel = document.querySelector('[data-section-panel="' + name + '"]');
    if (!panel) return;
    var list = panel.querySelector("[data-task-list]");
    var empty = panel.querySelector("[data-empty]");
    var count = document.querySelector('[data-section-count="' + name + '"]');
    var existing = {};
    var rows = Array.prototype.slice.call(list.children);
    rows.forEach(function (row) {
      if (row.dataset && row.dataset.taskId) existing[row.dataset.taskId] = row;
    });

    var seen = {};
    var cursor = list.firstElementChild;
    tasks.forEach(function (task) {
      var taskId = String(task.task_id || "");
      if (!taskId || seen[taskId]) return;
      seen[taskId] = true;
      var row = existing[taskId] || buildTaskRow();
      var fingerprint = JSON.stringify(task);
      if (row._taskFingerprint !== fingerprint) {
        updateTaskRow(row, task, name);
        row._taskFingerprint = fingerprint;
      }
      if (row !== cursor) list.insertBefore(row, cursor);
      cursor = row.nextElementSibling;
    });
    while (cursor) {
      var next = cursor.nextElementSibling;
      cursor.remove();
      cursor = next;
    }

    var total = Object.keys(seen).length;
    if (count) count.textContent = String(total);
    if (empty) empty.hidden = total > 0;
    list.hidden = total === 0;

    if (name === "queued") {
      var detail = document.querySelector('[data-section-detail="queued"]');
      if (detail) {
        var pending = tasks.filter(function (task) {
          return task.queue_state === "approval_pending";
        }).length;
        detail.textContent = (total - pending) + " ready · " + pending + " awaiting approval";
      }
    } else if (name === "recent") {
      var recentDetail = document.querySelector('[data-section-detail="recent"]');
      var dashboard = document.getElementById("task-dashboard");
      var limit = dashboard ? dashboard.dataset.recentLimit : "";
      if (recentDetail && limit) recentDetail.textContent = "Latest " + limit;
    }
  }

  function installSectionToggles() {
    var preferences = loadSectionPreferences();
    SECTION_NAMES.forEach(function (name) {
      var button = document.querySelector('[data-section-toggle="' + name + '"]');
      var panel = document.querySelector('[data-section-panel="' + name + '"]');
      if (!button || !panel) return;
      if (typeof preferences[name] === "boolean") {
        button.setAttribute("aria-expanded", String(preferences[name]));
        panel.hidden = !preferences[name];
      }
      button.addEventListener("click", function () {
        var expanded = button.getAttribute("aria-expanded") !== "true";
        button.setAttribute("aria-expanded", String(expanded));
        panel.hidden = !expanded;
        preferences[name] = expanded;
        saveSectionPreferences(preferences);
      });
    });
  }

  function fetchTasks(options) {
    return root.fetch("/api/tasks", {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: options.signal
    }).then(function (response) {
      if (response.status === 401 || response.status === 403) {
        var authError = new Error("The dashboard session is no longer authorised");
        authError.auth = true;
        authError.status = response.status;
        throw authError;
      }
      if (!response.ok) {
        var error = new Error("Task refresh failed with status " + response.status);
        error.status = response.status;
        throw error;
      }
      var contentType = response.headers.get("content-type") || "";
      if (contentType.indexOf("application/json") === -1) {
        var responseError = new Error("Task refresh returned an unexpected response");
        responseError.auth = Boolean(response.redirected) && contentType.indexOf("text/html") !== -1;
        throw responseError;
      }
      return response.json();
    }).then(function (data) {
      if (!data || !Array.isArray(data.running) || !Array.isArray(data.queued) ||
          !Array.isArray(data.recent)) {
        throw new Error("Task refresh returned invalid data");
      }
      return data;
    });
  }

  function init() {
    var dashboard = document.getElementById("task-dashboard");
    if (!dashboard || !document.querySelector('[data-section-toggle="running"]')) return;

    installSectionToggles();

    var status = document.getElementById("refresh-status");
    var warning = document.getElementById("refresh-warning");
    var button = document.getElementById("refresh-tasks");
    var authExpired = false;
    var lastSuccessAt = status && status.dataset.generatedAt ? status.dataset.generatedAt : "";

    function showUpdated(timestamp) {
      if (!status) return;
      status.textContent = "Updated " + formatTimestamp(timestamp);
      status.dataset.generatedAt = timestamp || "";
      if (warning) warning.hidden = true;
      authExpired = false;
    }

    function showProblem(message, staleTimestamp) {
      if (status) {
        status.textContent = staleTimestamp
          ? "Last updated " + formatTimestamp(staleTimestamp) + " (stale)"
          : "Not updated";
      }
      if (warning) {
        warning.hidden = false;
        warning.textContent = message;
      }
    }

    var poller = new TaskPoller({
      fetchTasks: fetchTasks,
      onSnapshot: function (data) {
        updateSection("running", data.running);
        updateSection("queued", data.queued);
        updateSection("recent", data.recent);
        if (typeof data.generated_at === "string") lastSuccessAt = data.generated_at;
        showUpdated(lastSuccessAt);
      },
      onError: function (error, failures) {
        showProblem(
          "Live updates are paused. Showing the last successfully retrieved tasks" +
            (lastSuccessAt ? " from " + formatTimestamp(lastSuccessAt) : "") +
            ". Retrying automatically.",
          lastSuccessAt
        );
        if (failures === 1) {
          /* Log one message per outage, not one per retry. */
          if (root.console && typeof root.console.warn === "function") {
            root.console.warn("Orchestra dashboard refresh failed; retrying automatically.", error && error.message);
          }
        }
      },
      onAuthError: function () {
        authExpired = true;
        showProblem(
          "Your dashboard session is no longer authorised. Reload the page to sign in again.",
          lastSuccessAt
        );
        if (button) {
          button.textContent = "Reload";
          button.disabled = false;
        }
      },
      onBusyChange: function (busy) {
        dashboard.setAttribute("aria-busy", busy ? "true" : "false");
        if (!button || authExpired) return;
        button.disabled = busy;
        button.textContent = busy ? "Refreshing…" : "Refresh";
      }
    });

    if (button) {
      button.addEventListener("click", function () {
        if (authExpired) {
          root.location.reload();
          return;
        }
        poller.refresh("manual");
      });
    }

    document.addEventListener("visibilitychange", function () {
      poller.setHidden(document.hidden);
    });

    var elapsedTimer = root.setInterval(function () {
      var nodes = document.querySelectorAll("[data-started-at]");
      nodes.forEach(function (node) {
        var seconds = elapsedSeconds(node.dataset.startedAt);
        node.textContent = seconds === null ? "—" : formatDuration(seconds);
      });
    }, 1000);
    root.addEventListener("pagehide", function (event) {
      if (!event.persisted) {
        poller.stop();
        root.clearInterval(elapsedTimer);
      }
    });
    root.addEventListener("pageshow", function (event) {
      if (event.persisted) poller.start();
    });

    poller.start();
  }

  return {
    TaskPoller: TaskPoller,
    init: init,
    nextDelay: nextDelay,
    shouldApplyResponse: shouldApplyResponse,
    formatDuration: formatDuration,
    elapsedSeconds: elapsedSeconds,
    formatTimestamp: formatTimestamp,
    shortId: shortId,
    statusLabel: statusLabel,
    safeStatus: safeStatus,
    REFRESH_MS: REFRESH_MS,
    MAX_BACKOFF_MS: MAX_BACKOFF_MS
  };
});
