from __future__ import annotations

import io
import re
import unittest
from datetime import datetime, timezone
from wsgiref.util import setup_testing_defaults

from dashboard.app import Dashboard


NOW = datetime.now(timezone.utc).isoformat()
TASK_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class FakeOMCP:
    def __init__(self, offline: bool = False) -> None:
        self.offline = offline
        self.calls = []
        self.task = {"task_id": TASK_ID, "title": "<script>alert(1)</script>", "worker": "Ina", "repository_profile": "Delnavazan", "status": "running", "created_at": NOW, "started_at": NOW, "consequence_class": "routine_development", "authorization": {"mode": "standing", "authorization_id": "routine-1"}, "execution_capabilities": {"network": False, "external_credentials": False, "git_metadata": "read_only"}}

    def call(self, tool, arguments=None):
        self.calls.append((tool, arguments or {}))
        if self.offline:
            from dashboard.omcp import OMCPError
            raise OMCPError("offline")
        if tool == "bridge_status": return {"status": "ok", "repository_profiles": ["Delnavazan"]}
        if tool == "worker_availability": return {"workers": [{"worker": "Ina", "available": False, "current_task_id": self.task["task_id"]}]}
        if tool == "list_tasks": return {"tasks": [self.task, {**self.task, "task_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "title": "done", "status": "succeeded", "finished_at": NOW}]}
        if tool == "list_approvals": return {"approvals": []}
        if tool == "task_status": return self.task
        if tool == "task_result": return {**self.task, "result": "<b>untrusted</b>"}
        if tool == "submit_task": return {**self.task, "status": "approval_required"}
        if tool == "cancel_task": return {**self.task, "status": "cancel_requested"}
        raise AssertionError(tool)


def request(app, path="/", method="GET", form="", content_length=None, body=None):
    environ = {}; setup_testing_defaults(environ); environ.update({"PATH_INFO": path, "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(form)) if content_length is None else content_length, "wsgi.input": body or io.BytesIO(form.encode())})
    seen = {}; body = b"".join(app(environ, lambda status, headers: seen.update(status=status, headers=dict(headers)))).decode()
    return seen, body


class DashboardTests(unittest.TestCase):
    def test_home_escapes_untrusted_task_text_and_has_responsive_viewport(self):
        _, body = request(Dashboard(FakeOMCP()))
        self.assertIn('name="viewport"', body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("<script>alert(1)</script>", body)

    def test_detail_shows_structured_security_not_worker_output(self):
        client = FakeOMCP(); client.task["status"] = "succeeded"; client.task["finished_at"] = NOW
        _, body = request(Dashboard(client), "/tasks/" + TASK_ID)
        self.assertIn("External credentials: denied", body)
        self.assertIn("&lt;b&gt;untrusted&lt;/b&gt;", body)

    def test_offline_is_safe_and_does_not_offer_action(self):
        seen, body = request(Dashboard(FakeOMCP(offline=True)))
        self.assertTrue(seen["status"].startswith("503"))
        self.assertIn("No task action was attempted", body)

    def test_submit_only_uses_routine_development_and_generates_request_id(self):
        client = FakeOMCP(); app = Dashboard(client)
        _, page = request(app, "/new-task")
        request_id = re.search(r'name="request_id" value="([^"]+)', page).group(1)
        form = f"csrf={app.csrf_token}&request_id={request_id}&repository_profile=Delnavazan&worker=Ina&title=Fix&instructions=Test"
        seen, _ = request(app, "/new-task", "POST", form)
        self.assertTrue(seen["status"].startswith("200"))
        arguments = client.calls[-1][1]
        self.assertEqual(arguments["consequence_class"], "routine_development")
        self.assertEqual(arguments["request_id"], request_id)

    def test_invalid_csrf_cannot_submit_or_cancel(self):
        client = FakeOMCP(); app = Dashboard(client)
        seen, _ = request(app, "/new-task", "POST", "csrf=nope")
        self.assertTrue(seen["status"].startswith("403"))
        self.assertNotIn("submit_task", [name for name, _ in client.calls])
        seen, _ = request(app, "/tasks/" + TASK_ID + "/cancel", "POST", "csrf=nope")
        self.assertTrue(seen["status"].startswith("403"))

    def test_retrying_the_same_form_preserves_its_idempotency_key(self):
        client = FakeOMCP(); app = Dashboard(client)
        _, page = request(app, "/new-task")
        request_id = re.search(r'name="request_id" value="([^"]+)', page).group(1)
        form = f"csrf={app.csrf_token}&request_id={request_id}&repository_profile=Delnavazan&worker=Ina&title=Fix&instructions=Test"
        request(app, "/new-task", "POST", form)
        request(app, "/new-task", "POST", form)
        submissions = [arguments for name, arguments in client.calls if name == "submit_task"]
        self.assertEqual([item["request_id"] for item in submissions], [request_id, request_id])

    def test_cancel_uses_fixed_operation(self):
        client = FakeOMCP(); app = Dashboard(client)
        seen, _ = request(app, "/tasks/" + TASK_ID + "/cancel", "POST", f"csrf={app.csrf_token}&reason=Stop")
        self.assertTrue(seen["status"].startswith("303"))
        self.assertEqual(client.calls[-1], ("cancel_task", {"task_id": TASK_ID, "reason": "Stop"}))

    def test_malformed_task_ids_are_not_sent_to_omcp(self):
        for path, method in (("/tasks/not-a-task", "GET"), ("/tasks/not-a-task/cancel", "POST"), ("/tasks/" + "A" * 36, "GET")):
            client = FakeOMCP()
            seen, _ = request(Dashboard(client), path, method)
            self.assertTrue(seen["status"].startswith("404"))
            self.assertEqual(client.calls, [])

    def test_oversized_body_returns_413_without_reading_or_mutating(self):
        class UnreadableBody:
            def read(self, _):
                raise AssertionError("oversized body must not be read")
        client = FakeOMCP(); app = Dashboard(client)
        seen, _ = request(app, "/new-task", "POST", content_length="70001", body=UnreadableBody())
        self.assertTrue(seen["status"].startswith("413"))
        self.assertNotIn("submit_task", [name for name, _ in client.calls])

    def test_malformed_or_negative_content_length_is_a_client_error_without_mutation(self):
        for content_length in ("not-a-number", "-1"):
            client = FakeOMCP(); app = Dashboard(client)
            seen, _ = request(app, "/new-task", "POST", content_length=content_length)
            self.assertTrue(seen["status"].startswith("400"))
            self.assertNotIn("submit_task", [name for name, _ in client.calls])
