/* Independent read-only overview: task polling and task actions stay separate. */
(function () {
  "use strict";
  var panel = document.getElementById("system-overview");
  if (!panel) return;
  var busy = false;
  var button = document.getElementById("refresh-system");
  var status = document.getElementById("system-updated");
  var lastUpdated = "";
  function set(key, value) {
    document.getElementById("system-" + key).textContent = value;
  }
  function bytes(value) { return (value / 1073741824).toFixed(1) + " GiB"; }
  function usage(value) {
    return value ? bytes(value.used_bytes) + " used / " + bytes(value.total_bytes) : "Unavailable";
  }
  function render(data) {
    var m = data.metrics;
    set("load", m.load ? m.load.join(" / ") + " (1 / 5 / 15 min), " + (m.cpu_count || "unknown") + " CPUs" : "Unavailable");
    set("memory", usage(m.memory));
    set("disk", usage(m.disk));
    set("uptime", m.uptime_seconds === null ? "Unavailable" : (m.uptime_seconds / 3600).toFixed(1) + " hours");
    set("temperature", m.temperature_c === null ? "Unavailable" : m.temperature_c + " °C");
    set("services", Object.keys(data.services).map(function (key) { return key + ": " + data.services[key]; }).join("\n"));
    var docker = data.docker;
    set("docker", docker.state === "available" ? docker.running + " / " + docker.containers + " containers running; Nextcloud " + docker.nextcloud_running + " / " + docker.nextcloud_containers + " running" : "Unavailable (no access or Docker absent)");
    set("public", data.public.host + ": " + data.public.state + (data.public.http_status ? " (HTTP " + data.public.http_status + ")" : ""));
    set("tailscale", data.tailscale.state + (data.tailscale.addresses.length ? " · " + data.tailscale.addresses.join(", ") : ""));
    lastUpdated = data.generated_at;
    status.textContent = "Collected " + lastUpdated + " · cached up to 30 seconds";
  }
  function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    button.disabled = true;
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
        button.disabled = false;
      });
  }
  button.addEventListener("click", refresh);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });
  var timer = setInterval(refresh, 30000);
  window.addEventListener("pagehide", function () { clearInterval(timer); });
  window.addEventListener("pageshow", function (event) { if (event.persisted) { timer = setInterval(refresh, 30000); refresh(); } });
  refresh();
})();
