from __future__ import annotations

import io
import json
import re
import socket
import tempfile
import threading
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from dashboard.app import Dashboard
from dashboard import omcp as dashboard_omcp
from dashboard.omcp import OMCPClient, OMCPError


NOW = datetime.now(timezone.utc).isoformat()
TASK_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
QUEUED_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
APPROVAL_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
RECENT_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


class FakeOMCP:
    def __init__(self, offline: bool = False) -> None:
        self.offline = offline
        self.calls = []
        self.approvals = []
        self.task = {
            "task_id": TASK_ID, "title": "<script>alert(1)</script>", "worker": "Ina",
            "repository_profile": "Delnavazan", "status": "running",
            "created_at": NOW, "started_at": NOW,
            "consequence_class": "routine_development",
            "authorization": {"mode": "standing", "authorization_id": "routine-1"},
            "execution_capabilities": {
                "network": False, "external_credentials": False,
                "git_metadata": "read_only",
            },
            "progress": "Execution in progress",
        }
        self.snapshot = {
            "generated_at": NOW,
            "running": [self.task],
            "queued": [
                {
                    "task_id": QUEUED_ID, "title": "Queued task", "worker": "Ina",
                    "status": "accepted", "created_at": NOW, "queue_state": "ready",
                    "progress": "Ready to execute",
                },
                {
                    "task_id": APPROVAL_ID, "title": "Needs review", "worker": "Ina",
                    "status": "approval_required", "created_at": NOW,
                    "queue_state": "approval_pending", "progress": "Awaiting approval",
                },
            ],
            "recent": [
                {
                    "task_id": RECENT_ID, "title": "Finished task", "worker": "Ina",
                    "status": "succeeded", "created_at": NOW,
                    "terminal_at": NOW, "progress": "Completed",
                },
            ],
            "counts": {
                "running": 1, "queued": 2, "ready": 1, "approval_pending": 1,
                "recent": 1, "recent_limit": 20,
            },
        }

        self.workers = [
            {
                "worker": "Ina", "available": True, "current_task_id": None,
                "idle": True, "runnable": True, "reason": "no_queued_task",
                "proven_healthy": False,
                "health_reason": "last_attempt_credit_exhausted",
                "last_attempt": {
                    "task_id": RECENT_ID, "status": "failed", "exit_code": 1,
                    "failure_category": "credit_exhausted", "finished_at": NOW,
                },
                "next_action": "Add credits to Ina's workspace, then run a separately authorised small verification task.",
            },
            {
                "worker": "codex", "available": True, "current_task_id": None,
                "idle": True, "runnable": True, "reason": "no_queued_task",
                "proven_healthy": True, "health_reason": "verified_success",
                "last_attempt": {
                    "task_id": TASK_ID, "status": "succeeded", "exit_code": 0,
                    "failure_category": None, "finished_at": NOW,
                },
                "next_action": None,
            },
        ]

    def call(self, tool, arguments=None):
        self.calls.append((tool, arguments or {}))
        if self.offline:
            from dashboard.omcp import OMCPError
            raise OMCPError("offline")
        if tool == "bridge_status":
            return {"status": "ok", "repository_profiles": ["Delnavazan"]}
        if tool == "worker_availability":
            return {"workers": self.workers}
        if tool == "task_snapshot":
            return self.snapshot
        if tool == "list_tasks":
            return {"tasks": [self.task]}
        if tool == "list_approvals":
            return {"approvals": self.approvals}
        if tool == "task_status":
            return self.task
        if tool == "task_result":
            return {**self.task, "result": "<b>untrusted</b>"}
        if tool == "submit_task":
            return {**self.task, "status": "approval_required"}
        if tool == "cancel_task":
            return {**self.task, "status": "cancel_requested"}
        raise AssertionError(tool)


def request(app, path="/", method="GET", form="", content_length=None, body=None):
    environ = {}
    setup_testing_defaults(environ)
    environ.update({
        "PATH_INFO": path, "REQUEST_METHOD": method,
        "CONTENT_LENGTH": str(len(form)) if content_length is None else content_length,
        "wsgi.input": body or io.BytesIO(form.encode()),
    })
    seen = {}
    response = b"".join(app(environ, lambda status, headers: seen.update(
        status=status, headers=dict(headers),
    ))).decode()
    return seen, response


class DashboardTests(unittest.TestCase):
    def test_home_escapes_untrusted_task_text_and_has_responsive_viewport(self):
        _, body = request(Dashboard(FakeOMCP()))
        self.assertIn('name="viewport"', body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("<script>alert(1)</script>", body)

    def test_worker_rows_show_credit_exhaustion_without_hiding_codex_success(self):
        _, body = request(Dashboard(FakeOMCP()))
        self.assertIn("last attempt blocked: workspace credits exhausted", body)
        self.assertIn("state-unavailable", body)
        codex_start = body.index("<h3>codex</h3>")
        codex_row = body[codex_start:body.index("</article>", codex_start)]
        self.assertIn("state-available", codex_row)

    def test_worker_rows_do_not_advertise_a_failed_last_attempt_as_available(self):
        client = FakeOMCP()
        client.workers[0]["last_attempt"].update({
            "failure_category": "malformed_completion",
        })
        client.workers[0]["health_reason"] = "last_attempt_failed"
        client.workers[0]["next_action"] = None
        _, body = request(Dashboard(client))
        ina_start = body.index("<h3>Ina</h3>")
        ina_row = body[ina_start:body.index("</article>", ina_start)]
        self.assertIn("state-unavailable", ina_row)
        self.assertIn("last attempt failed: malformed_completion", ina_row)

    def test_task_detail_renders_credit_failure_and_next_action(self):
        client = FakeOMCP()
        client.task.update({
            "status": "failed", "exit_code": 1,
            "failure": {
                "type": "CreditExhausted", "category": "credit_exhausted",
                "stage": "provider", "provider": "openai",
                "next_action": "Add credits to Ina's workspace, then run a separately authorised small verification task.",
            },
        })
        _, body = request(Dashboard(client), "/tasks/" + TASK_ID)
        self.assertIn("Failure", body)
        self.assertIn("credit_exhausted", body)
        self.assertIn("Add credits to Ina", body)

    def test_home_renders_three_sections_in_order_with_independent_state(self):
        _, body = request(Dashboard(FakeOMCP()))
        self.assertLess(body.index("Running Tasks"), body.index("Queued Tasks"))
        self.assertLess(body.index("Queued Tasks"), body.index("Recent Tasks"))
        running = body[body.index('data-section="running"'):body.index('data-section="queued"')]
        queued = body[body.index('data-section="queued"'):body.index('data-section="recent"')]
        recent = body[body.index('data-section="recent"'):]
        self.assertIn('aria-expanded="true"', running)
        self.assertIn('aria-expanded="true"', queued)
        self.assertIn('aria-expanded="false"', recent)
        self.assertIn("recent-panel", recent)
        self.assertIn(" hidden", recent)

    def test_home_shows_counts_complete_rows_and_approval_distinction(self):
        _, body = request(Dashboard(FakeOMCP()))
        self.assertIn('data-section-count="running">1</span>', body)
        self.assertIn('data-section-count="queued">2</span>', body)
        self.assertIn('data-section-count="recent">1</span>', body)
        self.assertIn("1 ready · 1 awaiting approval", body)
        self.assertIn("Ready to execute", body)
        self.assertIn("Awaiting approval", body)
        self.assertIn("waiting for approval", body)
        self.assertIn("Execution in progress", body)
        self.assertIn("Finished task", body)
        self.assertIn('data-task-id="' + TASK_ID + '"', body)
        self.assertIn('data-task-id="' + QUEUED_ID + '"', body)
        self.assertIn('data-task-id="' + APPROVAL_ID + '"', body)
        self.assertIn('data-task-id="' + RECENT_ID + '"', body)

    def test_short_task_id_keeps_full_id_accessible(self):
        _, body = request(Dashboard(FakeOMCP()))
        self.assertIn("aaaaaaaa…", body)
        self.assertIn(f'title="{TASK_ID}"', body)
        self.assertIn(f'href="/tasks/{TASK_ID}"', body)

    def test_api_tasks_returns_snapshot_and_uses_bounded_recent_limit(self):
        client = FakeOMCP()
        seen, body = request(Dashboard(client), "/api/tasks")
        self.assertTrue(seen["status"].startswith("200"))
        self.assertIn("application/json", seen["headers"]["Content-Type"])
        self.assertEqual(seen["headers"]["Cache-Control"], "no-store")
        data = json.loads(body)
        self.assertEqual(data["counts"]["running"], 1)
        self.assertIn(("task_snapshot", {"recent_limit": 20}), client.calls)
        self.assertNotIn("list_tasks", [name for name, _ in client.calls])

    def test_api_tasks_rejects_write_methods(self):
        client = FakeOMCP()
        seen, body = request(Dashboard(client), "/api/tasks", "POST")
        self.assertTrue(seen["status"].startswith("405"))
        self.assertEqual(seen["headers"]["Allow"], "GET")
        self.assertEqual(json.loads(body), {"error": "method_not_allowed"})
        self.assertEqual(client.calls, [])

    def test_api_failure_returns_json_without_task_data(self):
        client = FakeOMCP(offline=True)
        seen, body = request(Dashboard(client), "/api/tasks")
        self.assertTrue(seen["status"].startswith("503"))
        self.assertEqual(json.loads(body), {"error": "orchestra_unavailable"})
        self.assertNotIn(TASK_ID, body)

    def test_static_javascript_is_served_with_its_own_content_type(self):
        seen, body = request(Dashboard(FakeOMCP()), "/static/dashboard.js")
        self.assertTrue(seen["status"].startswith("200"))
        self.assertIn("text/javascript", seen["headers"]["Content-Type"])
        self.assertIn("TaskPoller", body)

    def test_section_headers_are_native_buttons_with_aria_state(self):
        _, body = request(Dashboard(FakeOMCP()))
        for name in ("running", "queued", "recent"):
            self.assertIn(f'data-section-toggle="{name}"', body)
            self.assertIn(f'aria-controls="{name}-panel"', body)
            self.assertIn(f'id="{name}-toggle"', body)
        self.assertEqual(body.count('type="button"'), 5)  # three sections + task/system Refresh

    def test_refresh_updates_contents_without_collapsing_or_scrolling(self):
        source = (Path(__file__).resolve().parents[1] / "dashboard/static/dashboard.js").read_text()
        update_section = source[source.index("function updateSection"):source.index("function installSectionToggles")]
        self.assertNotIn("aria-expanded", update_section)
        self.assertNotIn("panel.hidden", update_section)
        self.assertNotIn("scrollTo", source)
        self.assertNotIn("scrollTop", source)
        self.assertIn("STORAGE_KEY", source)
        self.assertIn("localStorage.setItem", source)

    def test_styles_include_mobile_layout_and_text_overflow_controls(self):
        source = (Path(__file__).resolve().parents[1] / "dashboard/static/dashboard.css").read_text()
        self.assertIn("@media (max-width: 620px)", source)
        self.assertIn("@media (max-width: 440px)", source)
        self.assertIn("text-overflow: ellipsis", source)
        self.assertIn("overflow-wrap: anywhere", source)

    def test_detail_shows_structured_security_not_worker_output(self):
        client = FakeOMCP()
        client.task["status"] = "succeeded"
        client.task["finished_at"] = NOW
        _, body = request(Dashboard(client), "/tasks/" + TASK_ID)
        self.assertIn("External credentials: denied", body)
        self.assertIn("&lt;b&gt;untrusted&lt;/b&gt;", body)

    def test_queued_task_detail_preserves_existing_cancel_action(self):
        client = FakeOMCP()
        client.task["status"] = "accepted"
        _, body = request(Dashboard(client), "/tasks/" + TASK_ID)
        self.assertIn("Cancel task", body)
        self.assertIn("/tasks/" + TASK_ID + "/cancel", body)

    def test_offline_is_safe_and_does_not_offer_action(self):
        seen, body = request(Dashboard(FakeOMCP(offline=True)))
        self.assertTrue(seen["status"].startswith("503"))
        self.assertIn("No task action was attempted", body)

    def test_submit_only_uses_routine_development_and_generates_request_id(self):
        client = FakeOMCP()
        app = Dashboard(client)
        _, page = request(app, "/new-task")
        request_id = re.search(r'name="request_id" value="([^"]+)', page).group(1)
        form = f"csrf={app.csrf_token}&request_id={request_id}&repository_profile=Delnavazan&worker=Ina&title=Fix&instructions=Test"
        seen, _ = request(app, "/new-task", "POST", form)
        self.assertTrue(seen["status"].startswith("200"))
        arguments = client.calls[-1][1]
        self.assertEqual(arguments["consequence_class"], "routine_development")
        self.assertEqual(arguments["request_id"], request_id)
        self.assertNotIn("requires_approval", arguments)

    def test_invalid_csrf_cannot_submit_or_cancel(self):
        client = FakeOMCP()
        app = Dashboard(client)
        seen, _ = request(app, "/new-task", "POST", "csrf=nope")
        self.assertTrue(seen["status"].startswith("403"))
        self.assertNotIn("submit_task", [name for name, _ in client.calls])
        seen, _ = request(app, "/tasks/" + TASK_ID + "/cancel", "POST", "csrf=nope")
        self.assertTrue(seen["status"].startswith("403"))

    def test_retrying_the_same_form_preserves_its_idempotency_key(self):
        client = FakeOMCP()
        app = Dashboard(client)
        _, page = request(app, "/new-task")
        request_id = re.search(r'name="request_id" value="([^"]+)', page).group(1)
        form = f"csrf={app.csrf_token}&request_id={request_id}&repository_profile=Delnavazan&worker=Ina&title=Fix&instructions=Test"
        request(app, "/new-task", "POST", form)
        request(app, "/new-task", "POST", form)
        submissions = [arguments for name, arguments in client.calls if name == "submit_task"]
        self.assertEqual([item["request_id"] for item in submissions], [request_id, request_id])

    def test_cancel_uses_fixed_operation(self):
        client = FakeOMCP()
        app = Dashboard(client)
        seen, _ = request(app, "/tasks/" + TASK_ID + "/cancel", "POST", f"csrf={app.csrf_token}&reason=Stop")
        self.assertTrue(seen["status"].startswith("303"))
        self.assertEqual(client.calls[-1], ("cancel_task", {"task_id": TASK_ID, "reason": "Stop"}))

    def test_dashboard_source_has_no_web_approval_environment_or_route(self):
        source = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
        text = source.read_text()
        self.assertNotIn("ORCHESTRA_DASHBOARD_APPROVAL_STATE_DIR", text)
        self.assertNotIn("ORCHESTRA_DASHBOARD_APPROVER_IDENTITY", text)
        self.assertNotIn('/approval"', text)
        self.assertNotIn("approval_client", text)

    def test_public_dashboard_never_renders_web_approval_controls(self):
        client = FakeOMCP()
        client.task["status"] = "approval_required"
        app = Dashboard(client)
        _, page = request(app, "/tasks/" + TASK_ID)
        self.assertIn("does not accept approval decisions", page)
        self.assertIn("owner-only local approval portal", page)
        self.assertIn("127.0.0.1:8767", page)
        self.assertNotIn('name="decision"', page)
        self.assertNotIn(f'action="/tasks/{TASK_ID}/approval"', page)
        seen, _ = request(app, "/tasks/" + TASK_ID + "/approval", "POST", "")
        self.assertTrue(seen["status"].startswith("404"))

    def test_approvals_page_is_read_only_and_explains_the_local_boundary(self):
        client = FakeOMCP()
        client.approvals = [{
            "task_id": APPROVAL_ID, "title": "Needs review", "worker": "Ina",
            "status": "approval_required", "created_at": NOW,
        }]
        app = Dashboard(client)
        _, page = request(app, "/approvals")
        self.assertIn("No approval controls are available", page)
        self.assertIn("ssh -N -L 8767:127.0.0.1:8767", page)
        self.assertIn("Needs review", page)
        self.assertNotIn('name="decision"', page)
        self.assertNotIn("Approve", page)
        self.assertNotIn("Reject", page)
        self.assertEqual(client.calls[-1], ("list_approvals", {}))

    def test_home_approval_badge_links_to_the_read_only_approvals_page(self):
        client = FakeOMCP()
        app = Dashboard(client)
        _, page = request(app, "/")
        self.assertIn('href="/approvals"', page)
        self.assertIn("waiting for approval", page)

    def test_malformed_task_ids_are_not_sent_to_omcp(self):
        for path, method in (
            ("/tasks/not-a-task", "GET"),
            ("/tasks/not-a-task/cancel", "POST"),
            ("/tasks/" + "A" * 36, "GET"),
        ):
            client = FakeOMCP()
            seen, _ = request(Dashboard(client), path, method)
            self.assertTrue(seen["status"].startswith("404"))
            self.assertEqual(client.calls, [])

    def test_oversized_body_returns_413_without_reading_or_mutating(self):
        class UnreadableBody:
            def read(self, _):
                raise AssertionError("oversized body must not be read")

        client = FakeOMCP()
        app = Dashboard(client)
        seen, _ = request(app, "/new-task", "POST", content_length="70001", body=UnreadableBody())
        self.assertTrue(seen["status"].startswith("413"))
        self.assertNotIn("submit_task", [name for name, _ in client.calls])

    def test_malformed_or_negative_content_length_is_a_client_error_without_mutation(self):
        for content_length in ("not-a-number", "-1"):
            client = FakeOMCP()
            app = Dashboard(client)
            seen, _ = request(app, "/new-task", "POST", content_length=content_length)
            self.assertTrue(seen["status"].startswith("400"))
            self.assertNotIn("submit_task", [name for name, _ in client.calls])


class OMCPClientTests(unittest.TestCase):
    class _ScriptedSocket:
        def __init__(self, parts):
            self.parts = iter(parts)
            self.timeouts = []

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def recv(self, _size):
            part = next(self.parts)
            if isinstance(part, BaseException):
                raise part
            return part

    def _serve_client(self, socket_path, responder):
        ready = threading.Event()
        received = []

        def serve():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(socket_path))
                listener.listen(1)
                ready.set()
                connection, _ = listener.accept()
                with connection:
                    data = b""
                    while True:
                        part = connection.recv(65536)
                        if not part:
                            return
                        data += part
                        while b"\n" in data:
                            line, data = data.split(b"\n", 1)
                            request = json.loads(line)
                            received.append(request)
                            response = responder(request, received)
                            if response is not None:
                                connection.sendall(json.dumps(response).encode() + b"\n")
                            if request.get("method") == "tools/call":
                                return

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(2))
        return thread, received

    @staticmethod
    def _successful_responder(request, _received):
        if request["method"] == "initialize":
            return {"jsonrpc": "2.0", "id": request["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "serverInfo": {"name": "orchestra-omcp", "version": "0.2.0"},
            }}
        if request["method"] == "tools/call":
            return {"jsonrpc": "2.0", "id": request["id"], "result": {
                "structuredContent": {
                    "running": [], "queued": [], "recent": [],
                    "counts": {"running": 0, "queued": 0, "recent": 0},
                },
            }}
        return None

    def test_task_snapshot_initializes_before_fixed_allowlist_tool_call(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "omcp.sock"
            thread, received = self._serve_client(socket_path, self._successful_responder)
            snapshot = OMCPClient(str(socket_path)).call(
                "task_snapshot", {"recent_limit": 20},
            )
            thread.join(2)
            self.assertEqual(snapshot["counts"]["queued"], 0)
            self.assertEqual([request["method"] for request in received], [
                "initialize", "notifications/initialized", "tools/call",
            ])
            self.assertEqual(received[0]["params"], {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "orchestra-dashboard", "version": "0.2.0"},
            })
            self.assertNotEqual(received[0]["id"], received[2]["id"])
            self.assertNotIn("id", received[1])
            self.assertEqual(received[2]["params"]["name"], "task_snapshot")
            self.assertEqual(received[2]["params"]["arguments"], {"recent_limit": 20})

    def test_initialize_error_fails_closed_before_tool_call(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "omcp.sock"

            def responder(request, _received):
                return {"jsonrpc": "2.0", "id": request["id"], "error": {
                    "code": -32002, "message": "MCP server is not initialized",
                }}

            thread, received = self._serve_client(socket_path, responder)
            with self.assertRaisesRegex(OMCPError, "initialization failed"):
                OMCPClient(str(socket_path)).call("bridge_status")
            thread.join(2)
            self.assertEqual([request["method"] for request in received], ["initialize"])

    def test_malformed_or_wrong_initialize_response_fails_closed(self):
        for response in (
            {"jsonrpc": "2.0", "id": 1, "result": []},
            {"jsonrpc": "2.0", "id": 999, "result": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
        ):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                socket_path = Path(directory) / "omcp.sock"

                def responder(_request, _received, response=response):
                    return response

                thread, received = self._serve_client(socket_path, responder)
                with self.assertRaises(OMCPError):
                    OMCPClient(str(socket_path)).call("bridge_status")
                thread.join(2)
                self.assertEqual([request["method"] for request in received], ["initialize"])

    def test_initialize_response_ids_are_type_strict(self):
        for response_id in (True, False, 1.0, "1", 2, None):
            with self.subTest(response_id=response_id), tempfile.TemporaryDirectory() as directory:
                socket_path = Path(directory) / "omcp.sock"

                def responder(_request, _received, response_id=response_id):
                    return {"jsonrpc": "2.0", "id": response_id, "result": {
                        "protocolVersion": "2025-06-18",
                    }}

                thread, received = self._serve_client(socket_path, responder)
                with self.assertRaisesRegex(OMCPError, "invalid response"):
                    OMCPClient(str(socket_path)).call("bridge_status")
                thread.join(2)
                self.assertEqual([request["method"] for request in received], ["initialize"])

    def test_initialize_result_and_error_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "omcp.sock"

            def responder(request, _received):
                return {"jsonrpc": "2.0", "id": request["id"], "result": {
                    "protocolVersion": "2025-06-18",
                }, "error": {"code": -32000}}

            thread, received = self._serve_client(socket_path, responder)
            with self.assertRaisesRegex(OMCPError, "initialization failed"):
                OMCPClient(str(socket_path)).call("bridge_status")
            thread.join(2)
            self.assertEqual([request["method"] for request in received], ["initialize"])

    def test_tool_response_id_validation_remains_in_effect_after_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "omcp.sock"

            def responder(request, received):
                if request["method"] == "tools/call":
                    return {"jsonrpc": "2.0", "id": request["id"] + 1, "result": {}}
                return self._successful_responder(request, received)

            thread, received = self._serve_client(socket_path, responder)
            with self.assertRaisesRegex(OMCPError, "invalid response"):
                OMCPClient(str(socket_path)).call("bridge_status")
            thread.join(2)
            self.assertEqual([request["method"] for request in received], [
                "initialize", "notifications/initialized", "tools/call",
            ])

    def test_tool_response_ids_are_type_strict(self):
        for response_id in (True, False, 2.0, "2", 3, None):
            with self.subTest(response_id=response_id), tempfile.TemporaryDirectory() as directory:
                socket_path = Path(directory) / "omcp.sock"

                def responder(request, received, response_id=response_id):
                    if request["method"] == "tools/call":
                        return {"jsonrpc": "2.0", "id": response_id, "result": {}}
                    return self._successful_responder(request, received)

                thread, received = self._serve_client(socket_path, responder)
                with self.assertRaisesRegex(OMCPError, "invalid response"):
                    OMCPClient(str(socket_path)).call("bridge_status")
                thread.join(2)
                self.assertEqual([request["method"] for request in received], [
                    "initialize", "notifications/initialized", "tools/call",
                ])

    def test_tool_result_and_error_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "omcp.sock"

            def responder(request, received):
                if request["method"] == "tools/call":
                    return {"jsonrpc": "2.0", "id": request["id"], "result": {
                        "structuredContent": {},
                    }, "error": {"code": -32000}}
                return self._successful_responder(request, received)

            thread, _received = self._serve_client(socket_path, responder)
            with self.assertRaisesRegex(OMCPError, "rejected"):
                OMCPClient(str(socket_path)).call("bridge_status")
            thread.join(2)

    def test_response_line_limit_excludes_the_newline_delimiter(self):
        client = OMCPClient("/unused")
        prefix, suffix = b'{"payload":"', b'"}'
        exact_line = prefix + b"x" * (client._MAX_RESPONSE_BYTES - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(exact_line), client._MAX_RESPONSE_BYTES)
        response = client._receive_line(self._ScriptedSocket([exact_line, b"\n"]))
        self.assertEqual(len(response["payload"]), len(exact_line) - len(prefix) - len(suffix))

    def test_response_line_one_byte_over_limit_is_rejected(self):
        client = OMCPClient("/unused")
        prefix, suffix = b'{"payload":"', b'"}'
        oversized = prefix + b"x" * (client._MAX_RESPONSE_BYTES + 1 - len(prefix) - len(suffix)) + suffix
        with self.assertRaisesRegex(OMCPError, "invalid response"):
            client._receive_line(self._ScriptedSocket([oversized]))

    def test_response_line_overflowing_chunk_with_newline_is_rejected(self):
        client = OMCPClient("/unused")
        prefix, suffix = b'{"payload":"', b'"}'
        oversized = prefix + b"x" * (client._MAX_RESPONSE_BYTES + 1 - len(prefix) - len(suffix)) + suffix
        with self.assertRaisesRegex(OMCPError, "invalid response"):
            client._receive_line(self._ScriptedSocket([oversized + b"\n"]))

    def test_response_deadline_is_total_not_per_recv(self):
        client = OMCPClient("/unused", timeout=1.0)
        connection = self._ScriptedSocket([b"{", b"}", b"\n"])
        with mock.patch.object(dashboard_omcp.time, "monotonic", side_effect=[0.0, 0.4, 0.8, 1.0]):
            with self.assertRaises(TimeoutError):
                client._receive_line(connection)
        self.assertEqual(connection.timeouts[0], 0.6)
        self.assertAlmostEqual(connection.timeouts[1], 0.2)

    def test_response_deadline_allows_normal_response_and_preserves_socket_timeout(self):
        client = OMCPClient("/unused", timeout=1.0)
        connection = self._ScriptedSocket([b"{}\n"])
        with mock.patch.object(dashboard_omcp.time, "monotonic", side_effect=[10.0, 10.25]):
            self.assertEqual(client._receive_line(connection), {})
        self.assertEqual(connection.timeouts, [0.75])

    def test_stalled_read_fails_closed(self):
        client = OMCPClient("/unused", timeout=1.0)
        with mock.patch.object(dashboard_omcp.time, "monotonic", side_effect=[0.0, 0.5]):
            with self.assertRaises(socket.timeout):
                client._receive_line(self._ScriptedSocket([socket.timeout("stalled")]))

    def test_socket_and_timeout_failures_are_dashboard_safe(self):
        with self.assertRaisesRegex(OMCPError, "Orchestra is unavailable"):
            OMCPClient("/definitely/not/a/socket").call("bridge_status")
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "omcp.sock"
            ready = threading.Event()

            def stall():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(str(socket_path))
                    listener.listen(1)
                    ready.set()
                    connection, _ = listener.accept()
                    with connection:
                        threading.Event().wait(0.2)

            thread = threading.Thread(target=stall, daemon=True)
            thread.start()
            self.assertTrue(ready.wait(2))
            with self.assertRaisesRegex(OMCPError, "Orchestra is unavailable"):
                OMCPClient(str(socket_path), timeout=0.01).call("bridge_status")
            thread.join(2)

    def test_instance_identity_cannot_change_between_dashboard_calls(self):
        client = OMCPClient("/tmp/unused-orchestra.sock")
        client._guard_instance({"instance_id": "a" * 36})
        client._guard_instance({"instance_id": "a" * 36})
        with self.assertRaisesRegex(OMCPError, "instance changed"):
            client._guard_instance({"instance_id": "b" * 36})
        with self.assertRaisesRegex(OMCPError, "invalid Orchestra instance"):
            client._guard_instance({"instance_id": ""})

    def test_tools_outside_the_allowlist_never_reach_the_socket(self):
        client = OMCPClient("/definitely/not/a/socket")
        with self.assertRaises(OMCPError):
            client.call("resolve_approval", {"task_id": TASK_ID})


if __name__ == "__main__":
    unittest.main()
