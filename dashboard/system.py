"""Read-only PixelBox projectors with fixed loopback and HTTPS endpoints.

PixelBox and Orchestra deliberately keep separate control planes.  This module
only reads the legacy PixelBox dashboard's precise ``GET /api/status`` endpoint
and the public MCP health URL.  It never invokes Docker, privileged commands,
systemctl, a shell, or the legacy dashboard's POST controls.
"""
from __future__ import annotations

import copy
import http.client
import json
import os
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable


STATUS_HOST = "127.0.0.1"
STATUS_PORT = 8080
STATUS_PATH = "/api/status"
PUBLIC_HOST = "mcp.delnavazan.com"
PUBLIC_PORT = 443
PUBLIC_PATH = "/healthz"
MAX_STATUS_BYTES = 131_072
MAX_PUBLIC_BYTES = 4_096
TIMEOUT = 2.5
CACHE_SECONDS = 30
MAX_RECENT = 10

PIXELBOX_SERVICES = (
    "dashboard", "notifications", "receiver", "pixel worker", "verifier",
    "cleanup", "photos evidence", "Nextcloud Docker",
)
ORCHESTRA_SERVICES = (
    "Dashboard V1", "GitHub dispatch", "OMCP coordinator",
    "Secure MCP tunnel", "Health timer",
)
PHOTO_TIMERS = (
    "pixelbox-v2-receiver.timer", "pixelbox-v2-pixelworker.timer",
    "pixelbox-v2-verifier.timer", "pixelbox-v2-cleanup.timer",
    "pixelbox-photos-evidence.timer",
)


class SystemStatusError(RuntimeError):
    """The fixed PixelBox status endpoint did not return a safe response."""


def _tls_context() -> ssl.SSLContext:
    """Verify the public host with the system trust store when available.

    Some Python builds (notably python.org macOS) do not load the OS bundle in
    ``create_default_context``.  Try fixed, read-only system bundles only when
    the default context has no roots; never disable verification.
    """
    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context
    for path in (
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
    ):
        if not os.path.isfile(path):
            continue
        try:
            context.load_verify_locations(cafile=path)
        except (OSError, ssl.SSLError):
            continue
        if context.get_ca_certs():
            break
    return context


def _text(value: Any, limit: int = 200) -> str:
    if not isinstance(value, str):
        return "Unavailable"
    clean = "".join(character for character in value if character.isprintable()).strip()
    if not clean:
        return "Unavailable"
    return clean[:limit]


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 1_000_000 else None


def _percent(value: Any) -> int | None:
    number = _count(value)
    return min(number, 100) if number is not None else None


def _number(value: Any, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not minimum <= number <= maximum:
        return None
    return round(number, 1)


def _state(value: Any) -> str:
    if value is True:
        return "up"
    if value is False:
        return "down"
    return "unavailable"


def _storage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"online": None, "percent": None, "text": "Unavailable"}
    online = value.get("online") if isinstance(value.get("online"), bool) else None
    return {
        "online": online,
        "percent": _percent(value.get("percent")),
        "text": _text(value.get("text"), 80),
    }


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _recent(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value[:MAX_RECENT]:
        if not isinstance(item, dict):
            continue
        result.append({
            # Deliberately omit original filenames: counts and status/event
            # timestamps are sufficient monitoring data for this surface.
            "status": _text(item.get("status"), 64),
            "event_at": _text(item.get("event_at"), 64),
        })
    return result


def _http_get(
    host: str,
    port: int,
    path: str,
    *,
    https: bool,
    maximum: int,
    timeout: float,
) -> tuple[int, bytes]:
    """One fixed GET with a total deadline and a bounded response body.

    ``http.client`` never follows redirects.  A redirect is therefore returned
    as a status for the caller to reject or classify, rather than silently
    turning into a request to an unapproved target.
    """
    if not host or not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("fixed endpoint is invalid")
    if maximum < 1 or timeout <= 0:
        raise ValueError("response bounds are invalid")
    deadline = time.monotonic() + timeout
    context = _tls_context() if https else None
    connection: http.client.HTTPConnection | http.client.HTTPSConnection
    if https:
        connection = http.client.HTTPSConnection(
            host, port, timeout=timeout, context=context,
        )
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("endpoint deadline expired")
        connection.request(
            "GET", path,
            headers={
                "Accept": "application/json" if not https else "text/plain",
                "User-Agent": "orchestra-dashboard/1",
            },
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("endpoint deadline expired")
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        response = connection.getresponse()
        body = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("endpoint deadline expired")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read(min(8_192, maximum + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > maximum:
                raise SystemStatusError("endpoint response exceeded its bound")
        return response.status, bytes(body)
    finally:
        connection.close()


def _public_projection(host: str, status: int | None) -> dict[str, Any]:
    if status is None:
        state = "unavailable"
    elif 200 <= status < 300:
        state = "healthy"
    elif 300 <= status < 500:
        state = "reachable"
    elif 500 <= status <= 599:
        state = "unhealthy"
    else:
        state = "unavailable"
    return {"host": host, "state": state, "http_status": status}


def _project(raw: dict[str, Any] | None, public: dict[str, Any]) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    system = _mapping(data.get("system"))
    services = _mapping(data.get("services"))
    orchestra = _mapping(data.get("orchestra"))
    orchestra_services = _mapping(orchestra.get("services"))
    pixel = _mapping(data.get("pixel"))
    mbox = _mapping(data.get("mbox"))
    timers = _mapping(pixel.get("timers"))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "pixelbox-api",
        "state": "available" if data else "unavailable",
        "pixelbox": {
            "health": _text(data.get("health"), 80),
            "system": {
                "uptime": _text(system.get("uptime"), 80),
                "load": _text(system.get("load"), 80),
                "memory": _text(system.get("memory"), 80),
                "temperature_c": _number(system.get("temperature"), 0, 200),
                "storage": _storage(system.get("storage")),
                "tailscale": _state(system.get("tailscale")),
            },
            "photo": {
                "connected": _state(pixel.get("connected")),
                "waiting": _count(pixel.get("waiting")),
                "queued": _count(pixel.get("queued")),
                "verifying": _count(pixel.get("verifying")),
                "backed_up": _count(pixel.get("backedUp")),
                "db_available": _state(pixel.get("dbAvailable")),
                "storage": {
                    "percent": _percent(pixel.get("storagePercent")),
                    "text": _text(pixel.get("storageText"), 80),
                },
                "timers": {name: _state(timers.get(name)) for name in PHOTO_TIMERS},
                "recent": _recent(pixel.get("recent")),
            },
            "services": {name: _state(services.get(name)) for name in PIXELBOX_SERVICES},
        },
        "mbox": {
            "hdd": _text(mbox.get("hdd"), 80),
            "storage": _storage(mbox.get("storage")),
            "nextcloud": _text(mbox.get("nextcloud"), 80),
        },
        "nextcloud": {
            "state": _text(mbox.get("nextcloud"), 80),
            "docker": _state(services.get("Nextcloud Docker")),
        },
        "orchestra_services": {
            name: _state(orchestra_services.get(name)) for name in ORCHESTRA_SERVICES
        },
        "public": public,
    }


class SystemCollector:
    """Cache one validated, read-only projection for a short interval."""

    def __init__(
        self,
        *,
        status_host: str = STATUS_HOST,
        status_port: int = STATUS_PORT,
        public_host: str = PUBLIC_HOST,
        timeout: float = TIMEOUT,
        max_status_bytes: int = MAX_STATUS_BYTES,
        status_fetcher: Callable[[], dict[str, Any]] | None = None,
        public_fetcher: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._status_host = status_host
        self._status_port = status_port
        self._public_host = public_host
        self._timeout = timeout
        self._max_status_bytes = max_status_bytes
        self._status_fetcher = status_fetcher or self._fetch_status
        self._public_fetcher = public_fetcher or self._fetch_public
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._collected = 0.0

    def _fetch_status(self) -> dict[str, Any]:
        status, body = _http_get(
            self._status_host, self._status_port, STATUS_PATH,
            https=False, maximum=self._max_status_bytes, timeout=self._timeout,
        )
        if status != 200:
            raise SystemStatusError("legacy status endpoint returned a non-200 response")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise SystemStatusError("legacy status endpoint returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise SystemStatusError("legacy status endpoint returned a non-object")
        return value

    def _fetch_public(self) -> dict[str, Any]:
        try:
            status, _body = _http_get(
                self._public_host, PUBLIC_PORT, PUBLIC_PATH,
                https=True, maximum=MAX_PUBLIC_BYTES, timeout=self._timeout,
            )
        except (OSError, TimeoutError, ValueError, SystemStatusError,
                http.client.HTTPException):
            return _public_projection(self._public_host, None)
        return _public_projection(self._public_host, status)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._snapshot is None or time.monotonic() - self._collected >= CACHE_SECONDS:
                try:
                    raw = self._status_fetcher()
                except Exception:
                    raw = None
                try:
                    public = self._public_fetcher()
                except Exception:
                    public = _public_projection(self._public_host, None)
                self._snapshot = _project(raw, public)
                self._collected = time.monotonic()
            return copy.deepcopy(self._snapshot)
