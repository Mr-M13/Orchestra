"""Fixed, read-only host probes. Never publish raw subprocess or file output."""
from __future__ import annotations

import copy
import ipaddress
import json
import math
import os
import selectors
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

SERVICES = (
    "orchestra-omcp", "orchestra-mcp-http", "orchestra-github-dispatch",
    "orchestra-tunnel", "orchestra-dashboard", "cloudflared", "tailscaled", "docker",
)
COMMANDS = {
    "services": ("/usr/bin/systemctl", "show", "--no-pager", "--property=Id,LoadState,ActiveState", *(s + ".service" for s in SERVICES)),
    "docker": ("/usr/bin/sudo", "-n", "/usr/bin/docker", "container", "ls", "--all", "--format", "{{.State}}\t{{.Image}}"),
    "tailscale": ("/usr/bin/tailscale", "status", "--json"),
    "public": ("/usr/bin/curl", "-q", "--silent", "--request", "GET", "--output", "/dev/null", "--write-out", "%{http_code}", "--max-time", "2", "--connect-timeout", "1", "--noproxy", "*", "--proto", "=https", "https://mcp.delnavazan.com/healthz"),
}
ALLOWED_COMMANDS = frozenset(COMMANDS.values())
MAX_OUTPUT = 65536
TIMEOUT = 2.5
CACHE_SECONDS = 30


def run_command(argv: tuple[str, ...]) -> str | None:
    """Only exact argv tuples; cap bytes while reading, not after buffering."""
    if argv not in ALLOWED_COMMANDS:
        raise ValueError("probe is not allowlisted")
    try:
        with subprocess.Popen(
            argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd="/", close_fds=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": "/nonexistent"},
        ) as process:
            try:
                deadline = time.monotonic() + TIMEOUT
                data = bytearray()
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            return None
                        chunk = os.read(process.stdout.fileno(), min(4096, MAX_OUTPUT + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > MAX_OUTPUT:
                            return None
                if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                    return None
                return data.decode("utf-8", errors="replace")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
    except (OSError, subprocess.SubprocessError):
        return None


def read_metric(path: str) -> str:
    with open(path, "rb") as source:
        value = source.read(8193)
    if len(value) > 8192:
        raise ValueError("metric too large")
    return value.decode("ascii")


def finite(value: str | float) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid metric")
    return round(number, 2)


def host_metrics() -> dict:
    metrics = {key: None for key in ("load", "cpu_count", "memory", "disk", "uptime_seconds", "temperature_c")}
    try:
        metrics["load"] = [finite(n) for n in os.getloadavg()]
        metrics["cpu_count"] = os.cpu_count()
    except (OSError, ValueError):
        pass
    try:
        memory = {}
        for line in read_metric("/proc/meminfo").splitlines():
            fields = line.split()
            if fields[0] in {"MemTotal:", "MemAvailable:"} and fields[2:] == ["kB"]:
                memory[fields[0][:-1]] = int(fields[1]) * 1024
        total, available = memory["MemTotal"], memory["MemAvailable"]
        if 0 <= available <= total and total > 0:
            metrics["memory"] = {"total_bytes": total, "used_bytes": total - available}
    except (OSError, ValueError, KeyError, IndexError, UnicodeError):
        pass
    try:
        disk = shutil.disk_usage("/")
        metrics["disk"] = {"total_bytes": disk.total, "used_bytes": disk.used}
    except OSError:
        pass
    try:
        metrics["uptime_seconds"] = finite(read_metric("/proc/uptime").split()[0])
    except (OSError, ValueError, IndexError, UnicodeError):
        pass
    # CPU thermal zones only; fixed paths and bounded count, no arbitrary sensors.
    for index in range(16):
        try:
            base = f"/sys/class/thermal/thermal_zone{index}"
            if read_metric(base + "/type").strip() not in {"x86_pkg_temp", "cpu-thermal", "cpu_thermal"}:
                continue
            temperature = finite(read_metric(base + "/temp").strip()) / 1000
            if temperature <= 150:
                metrics["temperature_c"] = round(temperature, 1)
                break
        except (OSError, ValueError, UnicodeError):
            pass
    return metrics


def service_states(raw: str | None) -> dict:
    states = {name: "unavailable" for name in SERVICES}
    for block in (raw or "").strip().split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        name = fields.get("Id", "").removesuffix(".service")
        state = fields.get("ActiveState")
        if name in states and fields.get("LoadState") == "loaded" and state in {"active", "inactive", "failed", "activating", "deactivating", "reloading"}:
            states[name] = state
    return states


def docker_summary(raw: str | None) -> dict:
    if raw is None:
        return {"state": "unavailable"}
    counts = {"containers": 0, "running": 0, "nextcloud_containers": 0, "nextcloud_running": 0}
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or fields[0] not in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}:
            return {"state": "unavailable"}
        state, image = fields
        counts["containers"] += 1
        counts["running"] += int(state == "running")
        # Display counts only: never names, registry URLs, labels, mounts or env.
        image_name = image.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
        if image_name == "nextcloud":
            counts["nextcloud_containers"] += 1
            counts["nextcloud_running"] += int(state == "running")
    return {"state": "available", **counts}


def tailscale_summary(raw: str | None) -> dict:
    result = {"state": "unavailable", "addresses": []}
    try:
        data = json.loads(raw or "")
        if data.get("BackendState") in {"Running", "Stopped", "Starting", "NeedsLogin", "NeedsMachineAuth", "NoState", "InUseOtherUser"}:
            result["state"] = data["BackendState"]
        addresses = data.get("TailscaleIPs", [])
        if isinstance(addresses, list):
            for address in addresses[:8]:
                result["addresses"].append(str(ipaddress.ip_address(address)))
    except (ValueError, TypeError, AttributeError, RecursionError):
        return {"state": "unavailable", "addresses": []}
    return result


def public_summary(raw: str | None) -> dict:
    code = int(raw) if raw and len(raw) == 3 and raw.isascii() and raw.isdigit() else 0
    # Authentication-required responses prove reachability, not application health.
    state = "healthy" if 200 <= code < 300 else "reachable" if 300 <= code < 500 else "unhealthy" if 500 <= code <= 599 else "unavailable"
    return {"host": "mcp.delnavazan.com", "state": state, "http_status": code or None}


class SystemCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = None
        self._collected = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            if self._snapshot is None or time.monotonic() - self._collected >= CACHE_SECONDS:
                self._snapshot = {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "metrics": host_metrics(),
                    "services": service_states(run_command(COMMANDS["services"])),
                    "docker": docker_summary(run_command(COMMANDS["docker"])),
                    "tailscale": tailscale_summary(run_command(COMMANDS["tailscale"])),
                    "public": public_summary(run_command(COMMANDS["public"])),
                }
                self._collected = time.monotonic()
            return copy.deepcopy(self._snapshot)
