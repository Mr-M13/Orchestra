/* Independent read-only overview: task polling and task actions stay separate. */
(function () {
  "use strict";
  var panel = document.getElementById("system-overview");
  if (!panel) return;
  var busy = false;
  var button = document.getElementById("refresh-system");
  var status = document.getElementById("system-updated");
  var lastUpdated = "";
  function node(key) { return document.getElementById("system-" + key); }
  function set(key, value) { var target = node(key); if (target) target.textContent = value; }
  function label(value) {
    if (value === "up") return "up";
    if (value === "down") return "down";
    if (value === "healthy") return "healthy";
    if (value === "reachable") return "reachable";
    if (value === "unhealthy") return "unhealthy";
    return "unavailable";
  }
  function stateList(values) {
    return Object.keys(values || {}).map(function (key) {
      return key + ": " + label(values[key]);
    }).join("\n") || "Unavailable";
  }
  function storage(value, fallback) {
    if (!value || value.text === "Unavailable") return fallback || "Unavailable";
    var parts = [value.text];
    if (value.percent !== null && value.percent !== undefined) parts.push(value.percent + "% used");
    if (value.online === false) parts.push("offline");
    return parts.join(" · ");
  }
  function count(value) { return value === null || value === undefined ? "unavailable" : value; }
  function render(data) {
    var pixelbox = data.pixelbox || {};
    var system = pixelbox.system || {};
    var photo = pixelbox.photo || {};
    var mbox = data.mbox || {};
    var publicHealth = data.public || {};
    if (data.state !== "available") {
      set("pixelbox", "PixelBox status unavailable (fixed loopback GET /api/status did not respond).");
      set("photo", "Unavailable");
      set("photo-timers", "Unavailable");
      set("photo-recent", "Unavailable");
      set("mbox", "Unavailable");
      set("nextcloud", "Unavailable");
      set("pixelbox-services", "Unavailable");
      set("orchestra-services", "Unavailable");
    } else {
      var temperature = system.temperature_c === null || system.temperature_c === undefined
        ? "temperature unavailable" : system.temperature_c + " °C";
      set("pixelbox", "Health: " + (pixelbox.health || "Unavailable") +
        " · Uptime: " + (system.uptime || "Unavailable") +
        " · Load: " + (system.load || "Unavailable") +
        " · Memory: " + (system.memory || "Unavailable") +
        " · CPU: " + temperature +
        " · Root disk: " + storage(system.storage) +
        " · Tailscale: " + label(system.tailscale));
      set("photo", "Connected: " + label(photo.connected) +
        " · Waiting: " + count(photo.waiting) +
        " · Queued: " + count(photo.queued) +
        " · Verifying: " + count(photo.verifying) +
        " · Backed up: " + count(photo.backed_up) +
        " · Photo DB: " + label(photo.db_available) +
        " · Pixel storage: " + storage(photo.storage));
      set("photo-timers", stateList(photo.timers));
      set("photo-recent", (photo.recent || []).map(function (item) {
        return (item.event_at || "Unknown time") + " · " + (item.status || "Unknown status");
      }).join("\n") || "No recent photo events");
      set("mbox", "HDD: " + (mbox.hdd || "Unavailable") +
        " · Storage: " + storage(mbox.storage) +
        " · Nextcloud: " + (mbox.nextcloud || "Unavailable"));
      set("nextcloud", "State: " + (data.nextcloud && data.nextcloud.state || "Unavailable") +
        " · Docker container: " + label(data.nextcloud && data.nextcloud.docker));
      set("pixelbox-services", stateList(pixelbox.services));
      set("orchestra-services", stateList(data.orchestra_services));
    }
    set("public", (publicHealth.host || "mcp.delnavazan.com") + ": " +
      label(publicHealth.state) +
      (publicHealth.http_status ? " (HTTP " + publicHealth.http_status + ")" : ""));
    lastUpdated = data.generated_at || "";
    status.textContent = "Collected " + (lastUpdated || "unknown time") + " · cached up to 30 seconds";
  }
  function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    if (button) button.disabled = true;
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 12000);
    fetch("/api/system", { method: "GET", credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" }, signal: controller.signal })
      .then(function (response) {
        if (!response.ok || !(response.headers.get("content-type") || "").includes("application/json")) throw new Error("unavailable");
        return response.json();
      }).then(render).catch(function () {
        status.textContent = lastUpdated ? "Overview stale · last collected " + lastUpdated : "System overview unavailable";
      }).finally(function () {
        clearTimeout(timeout);
        busy = false;
        if (button) button.disabled = false;
      });
  }
  if (button) button.addEventListener("click", refresh);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });
  var timer = setInterval(refresh, 30000);
  window.addEventListener("pagehide", function () { clearInterval(timer); });
  window.addEventListener("pageshow", function (event) { if (event.persisted) { timer = setInterval(refresh, 30000); refresh(); } });
  refresh();
})();
