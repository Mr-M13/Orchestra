from __future__ import annotations

import io
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from dashboard import system
from dashboard.app import Dashboard
from dashboard.omcp import OMCPError


TASK_ID = "11111111-1111-1111-1111-111111111111"


def status_payload() -> dict:
    return {
        "health": "Needs attention",
        "system": {
            "uptime": "6h 8m", "load": "0.36 / 0.51 / 0.48",
            "memory": "771 MB used of 3794 MB", "temperature": 41.9,
            "storage": {"online": True, "percent": 6, "text": "411 GB free of 458 GB"},
            "tailscale": True, "dashboardUrl": "http://secret-tailscale:8080/",
        },
        "services": {
            "dashboard": True, "notifications": True, "receiver": True,
            "pixel worker": True, "verifier": True, "cleanup": True,
            "photos evidence": True, "Nextcloud Docker": True,
            "unknown service": False,
        },
        "orchestra": {
            "dashboard": True,
            "services": {
                "Dashboard V1": True, "GitHub dispatch": True,
                "Health timer": True, "OMCP coordinator": True,
                "Secure MCP tunnel": True, "unknown orchestra": False,
            },
        },
        "pixel": {
            "connected": False, "waiting": 0, "queued": 2, "verifying": 0,
            "backedUp": 114, "dbAvailable": True,
            "storagePercent": 6, "storageText": "411 GB free of 458 GB",
            "timers": {
                "pixelbox-v2-receiver.timer": True,
                "pixelbox-v2-pixelworker.timer": True,
                "pixelbox-v2-verifier.timer": True,
                "pixelbox-v2-cleanup.timer": True,
                "pixelbox-photos-evidence.timer": True,
                "unknown.timer": False,
            },
            "recent": [
                {"original_name": "PRIVATE-CANARY.jpg", "status": "BACKED_UP",
                 "event_at": "2026-10-17 04:37:14"},
            ],
        },
        "mbox": {
            "hdd": "Online", "nextcloud": "Running",
            "nextcloudUrl": "https://secret-tailscale.ts.net",
            "storage": {"online": True, "percent": 0, "text": "1734 GB free of 1833 GB"},
        },
    }


class StatusServer:
    def __init__(self, responses, delay=0.0):
        self.responses = list(responses)
        self.requests = []
        self.delay = delay
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                outer.requests.append((self.command, self.path, dict(self.headers)))
                if outer.delay:
                    time.sleep(outer.delay)
                status, body, headers = outer.responses.pop(0)
                data = body if isinstance(body, bytes) else body.encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class SystemCollectorTests(unittest.TestCase):
    def public(self, code=200):
        return lambda: system._public_projection("mcp.delnavazan.com", code)

    def test_uses_only_the_exact_loopback_get_projection(self):
        server = StatusServer([(200, json.dumps(status_payload()), {"Content-Type": "application/json"})])
        self.addCleanup(server.close)
        collector = system.SystemCollector(
            status_port=server.port, public_fetcher=self.public(403),
        )
        result = collector.snapshot()
        self.assertEqual(server.requests[0][0], "GET")
        self.assertEqual(server.requests[0][1], "/api/status")
        self.assertEqual(result["state"], "available")
        self.assertEqual(result["pixelbox"]["system"]["uptime"], "6h 8m")
        self.assertEqual(result["pixelbox"]["system"]["temperature_c"], 41.9)
        self.assertEqual(result["pixelbox"]["system"]["tailscale"], "up")
        self.assertEqual(result["pixelbox"]["photo"]["backed_up"], 114)
        self.assertEqual(result["pixelbox"]["photo"]["queued"], 2)
        self.assertEqual(result["pixelbox"]["photo"]["recent"], [{
            "status": "BACKED_UP", "event_at": "2026-10-17 04:37:14",
        }])
        self.assertEqual(result["mbox"]["hdd"], "Online")
        self.assertEqual(result["mbox"]["nextcloud"], "Running")
        self.assertEqual(result["mbox"]["storage"]["text"], "1734 GB free of 1833 GB")
        self.assertEqual(result["nextcloud"], {"state": "Running", "docker": "up"})
        self.assertEqual(result["pixelbox"]["services"]["Nextcloud Docker"], "up")
        self.assertEqual(result["orchestra_services"]["OMCP coordinator"], "up")
        self.assertEqual(result["public"], {
            "host": "mcp.delnavazan.com", "state": "reachable", "http_status": 403,
        })
        serialized = json.dumps(result, sort_keys=True)
        for forbidden in (
            "dashboardUrl", "nextcloudUrl", "secret-tailscale", "unknown service",
            "unknown orchestra", "unknown.timer", "PRIVATE-CANARY", "original_name",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_redirect_and_non_200_are_not_followed(self):
        for code in (302, 401, 500):
            with self.subTest(code=code):
                server = StatusServer([(code, "not accepted", {"Location": "http://unapproved.test/"})])
                self.addCleanup(server.close)
                collector = system.SystemCollector(
                    status_port=server.port, public_fetcher=self.public(200),
                )
                result = collector.snapshot()
                self.assertEqual(result["state"], "unavailable")
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(result["pixelbox"]["system"]["uptime"], "Unavailable")
                self.assertEqual(result["public"]["state"], "healthy")

    def test_oversized_malformed_and_unavailable_status_fail_closed(self):
        cases = [
            (200, "X" * 128, {"Content-Type": "application/json"}, 64),
            (200, "X" * 128, {"Content-Type": "application/json"}, 128),
            (200, "{broken", {"Content-Type": "application/json"}, 131072),
            (200, "[]", {"Content-Type": "application/json"}, 131072),
        ]
        for code, body, headers, maximum in cases:
            with self.subTest(body=body[:12]):
                server = StatusServer([(code, body, headers)])
                self.addCleanup(server.close)
                collector = system.SystemCollector(
                    status_port=server.port, max_status_bytes=maximum,
                    public_fetcher=self.public(200),
                )
                result = collector.snapshot()
                self.assertEqual(result["state"], "unavailable")
                self.assertEqual(result["source"], "pixelbox-api")
        collector = system.SystemCollector(
            status_fetcher=mock.Mock(side_effect=OSError("SECRET")),
            public_fetcher=mock.Mock(side_effect=OSError("SECRET")),
        )
        result = collector.snapshot()
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["public"]["state"], "unavailable")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_total_deadline_bounds_a_stalled_legacy_endpoint(self):
        server = StatusServer(
            [(200, json.dumps(status_payload()), {"Content-Type": "application/json"})],
            delay=0.3,
        )
        self.addCleanup(server.close)
        collector = system.SystemCollector(
            status_port=server.port, timeout=0.05,
            public_fetcher=self.public(200),
        )
        started = time.monotonic()
        result = collector.snapshot()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["public"]["state"], "healthy")

    def test_tls_context_keeps_verification_enabled(self):
        context = system._tls_context()
        self.assertTrue(context.get_ca_certs())
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, system.ssl.CERT_REQUIRED)

    def test_cache_is_bounded_and_returns_an_independent_copy(self):
        calls = []
        collector = system.SystemCollector(
            status_fetcher=lambda: calls.append(1) or status_payload(),
            public_fetcher=self.public(200),
        )
        first = collector.snapshot()
        first["pixelbox"]["system"]["uptime"] = "tampered"
        second = collector.snapshot()
        self.assertEqual(len(calls), 1)
        self.assertEqual(second["pixelbox"]["system"]["uptime"], "6h 8m")
        collector._collected -= system.CACHE_SECONDS
        collector.snapshot()
        self.assertEqual(len(calls), 2)

    def test_public_health_classification_does_not_retain_a_body(self):
        self.assertEqual(system._public_projection("host", 200)["state"], "healthy")
        self.assertEqual(system._public_projection("host", 403)["state"], "reachable")
        self.assertEqual(system._public_projection("host", 503)["state"], "unhealthy")
        self.assertEqual(system._public_projection("host", 0)["state"], "unavailable")
        self.assertEqual(system._public_projection("host", 200)["host"], "host")

    def test_endpoint_security_and_methods(self):
        collector = mock.Mock()
        collector.snapshot.return_value = {"state": "available", "pixelbox": {}}
        client = mock.Mock()
        app = Dashboard(client, system_collector=collector)
        responses = []
        body = b"".join(app(
            {"PATH_INFO": "/api/system", "REQUEST_METHOD": "GET", "wsgi.input": io.BytesIO()},
            lambda status, headers: responses.append((status, dict(headers))),
        ))
        status, headers = responses[0]
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body), collector.snapshot.return_value)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Content-Security-Policy"], "default-src 'none'")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        for method in ("POST", "PUT", "DELETE", "HEAD"):
            responses = []
            app(
                {"PATH_INFO": "/api/system", "REQUEST_METHOD": method, "wsgi.input": io.BytesIO()},
                lambda status, headers: responses.append((status, dict(headers))),
            )
            self.assertEqual(responses[0][0], "405 Method Not Allowed")
            self.assertEqual(responses[0][1]["Allow"], "GET")
        collector.snapshot.assert_called_once()
        client.call.assert_not_called()

    def test_rendered_panel_uses_the_new_unified_projection_and_no_approval_controls(self):
        app = Dashboard(mock.Mock(), system_collector=mock.Mock())
        panel = app.system_panel()
        for key in (
            "pixelbox", "photo", "photo-timers", "photo-recent", "mbox",
            "nextcloud", "pixelbox-services", "orchestra-services", "public",
        ):
            self.assertIn(f'id="system-{key}"', panel)
        self.assertIn("fixed loopback", panel)
        self.assertNotIn("Approve", panel)

    def test_omcp_offline_still_renders_system_panel_and_hides_exception_text(self):
        client = mock.Mock()
        client.call.side_effect = OMCPError("TOKEN=secret")
        app = Dashboard(client)
        responses = []
        page = b"".join(app(
            {"PATH_INFO": "/", "REQUEST_METHOD": "GET", "wsgi.input": io.BytesIO()},
            lambda status, headers: responses.append((status, dict(headers))),
        )).decode()
        self.assertEqual(responses[0][0], "503 Service Unavailable")
        self.assertIn("system-overview", page)
        self.assertIn("Orchestra is offline", page)
        self.assertNotIn("TOKEN=secret", page)

    def test_no_privileged_or_shell_probe_surface_remains(self):
        with open(system.__file__, encoding="utf-8") as source_file:
            source = source_file.read()
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("/usr/bin/sudo", source)
        self.assertNotIn("container ls", source)
        self.assertFalse(hasattr(system, "COMMANDS"))


if __name__ == "__main__":
    unittest.main()
