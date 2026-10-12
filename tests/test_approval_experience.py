import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from omcp.approval_notifications import ApprovalNotificationAgent
from omcp.approval_portal import PortalAuth, ensure_owner_secret, make_handler, owner_state_dir


TASK_ID = "11111111-1111-1111-1111-111111111111"


def pending_task():
    return {
        "task_id": TASK_ID,
        "request_id": "approval-test",
        "title": "Review approval workflow",
        "instructions": "Inspect all changes and run tests.",
        "worker": "codex",
        "repository_profile": "orchestra",
        "repository": "Mr-M13/Orchestra",
        "base_ref": "origin/main",
        "status": "approval_required",
        "created_at": "2026-10-12T00:00:00+00:00",
        "consequence_class": "security_change",
        "approval": {
            "status": "pending",
            "requested_at": "2026-10-12T00:00:00+00:00",
            "expires_at": "2026-10-12T00:30:00+00:00",
        },
    }


class ApprovalNotificationAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.task = pending_task()
        (self.state / "state.json").write_text(json.dumps({
            "schema_version": 1, "tasks": {TASK_ID: self.task}, "requests": {},
        }))
        self.commands = []
        self.removed = []
        self.current_time = [datetime(2026, 10, 12, tzinfo=timezone.utc)]

    def tearDown(self):
        self.temp.cleanup()

    def agent(self, runner=None):
        return ApprovalNotificationAgent(
            self.state, self.state / "Notifier", "http://127.0.0.1:8767",
            self.state / "approval-portal.token",
            runner=runner or (lambda command: self.commands.append(command)),
            remover=lambda identifier: self.removed.append(identifier),
            now=lambda: self.current_time[0],
        )

    def test_notification_is_delivered_once_across_restart(self):
        first = self.agent()
        self.assertEqual(first.scan_once()["sent"], 1)
        self.assertEqual(len(self.commands), 1)
        spec = json.loads(Path(self.commands[0][2]).read_text())
        self.assertEqual(spec["task_id"], TASK_ID)
        self.assertEqual(spec["repository"], "Mr-M13/Orchestra")
        self.assertEqual(spec["worker"], "codex")

        self.assertEqual(first.scan_once()["unchanged"], 1)
        restarted = self.agent()
        self.assertEqual(restarted.scan_once()["unchanged"], 1)
        self.assertEqual(len(self.commands), 1)

    def test_failed_delivery_retries_with_same_deterministic_identifier(self):
        calls = []

        def flaky(command):
            calls.append(command)
            if len(calls) == 1:
                raise RuntimeError("simulated delivery failure")

        agent = self.agent(flaky)
        self.assertEqual(agent.scan_once()["failed"], 1)
        self.assertEqual(agent.scan_once()["unchanged"], 1)
        self.current_time[0] += timedelta(seconds=5)
        self.assertEqual(agent.scan_once()["sent"], 1)
        first_spec = json.loads(Path(calls[0][2]).read_text())
        second_spec = json.loads(Path(calls[1][2]).read_text())
        self.assertEqual(first_spec["notification_id"], second_spec["notification_id"])

    def test_cancelled_or_expired_request_closes_notification(self):
        agent = self.agent()
        agent.scan_once()
        self.task["status"] = "cancelled"
        self.task["approval"]["status"] = "rejected"
        (self.state / "state.json").write_text(json.dumps({
            "schema_version": 1, "tasks": {TASK_ID: self.task}, "requests": {},
        }))
        self.assertEqual(agent.scan_once()["closed"], 1)
        self.assertEqual(len(self.removed), 1)
        ledger = json.loads((self.state / "approval-notifications.json").read_text())
        record = next(iter(ledger["approvals"].values()))
        self.assertEqual(record["terminal_status"], "cancelled")

    def test_stale_pending_state_does_not_notify_after_expiry(self):
        self.task["approval"]["expires_at"] = "2026-10-11T23:59:59+00:00"
        (self.state / "state.json").write_text(json.dumps({
            "schema_version": 1, "tasks": {TASK_ID: self.task}, "requests": {},
        }))
        agent = self.agent()
        self.assertEqual(agent.scan_once()["closed"], 1)
        self.assertEqual(self.commands, [])
        ledger = json.loads((self.state / "approval-notifications.json").read_text())
        self.assertEqual(next(iter(ledger["approvals"].values()))["terminal_status"], "expired")


class PortalAuthTests(unittest.TestCase):
    def test_session_signature_expiry_and_csrf_are_server_side(self):
        clock = [1000.0]
        auth = PortalAuth("11" * 32, now=lambda: clock[0])
        cookie, session_id = auth.new_session()
        self.assertEqual(auth.validate(cookie), session_id)
        self.assertNotEqual(auth.csrf(session_id, TASK_ID), auth.csrf(session_id, TASK_ID[:-1] + "2"))
        clock[0] += 901
        self.assertIsNone(auth.validate(cookie))

    def test_owner_secret_has_restricted_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "portal.token"
            first = ensure_owner_secret(path)
            second = ensure_owner_secret(path)
            self.assertEqual(first, second)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_portal_requires_owner_only_state_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir(mode=0o700)
            self.assertEqual(owner_state_dir(state), state.resolve())
            state.chmod(0o755)
            with self.assertRaisesRegex(PermissionError, "owner-only"):
                owner_state_dir(state)


class ApprovalPortalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.token = "22" * 32
        self.decisions = []
        self.details = pending_task()

        def local_request(_state, request):
            if request["action"] == "details":
                return dict(self.details)
            if request["action"] == "history":
                public = {key: value for key, value in self.details.items() if key != "instructions"}
                return {"approvals": [public]}
            if request["action"] == "resolve":
                self.decisions.append(dict(request))
                self.details["status"] = "running" if request["decision"] == "approve" else "rejected"
                self.details["approval"] = {"status": "approved" if request["decision"] == "approve" else "rejected"}
                return dict(self.details)
            raise AssertionError(request)

        handler = make_handler(self.state, self.token, PortalAuth(self.token), local_request)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_unauthenticated_access_is_denied_and_bootstrap_opens_full_details(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.opener.open(self.base + f"/task/{TASK_ID}")
        self.assertEqual(context.exception.code, 401)
        response = self.opener.open(self.base + f"/bootstrap/{self.token}/task/{TASK_ID}")
        page = response.read().decode()
        self.assertNotIn(self.token, response.geturl())
        self.assertIn("Inspect all changes and run tests.", page)
        self.assertIn("Mr-M13/Orchestra", page)
        self.assertIn("security boundary", page)

    def test_csrf_protected_approval_uses_local_resolution(self):
        page = self.opener.open(
            self.base + f"/bootstrap/{self.token}/task/{TASK_ID}"
        ).read().decode()
        marker = 'name="csrf" value="'
        csrf = page.split(marker, 1)[1].split('"', 1)[0]
        data = urllib.parse.urlencode({
            "csrf": csrf, "decision": "approve", "reason": "Reviewed exact scope",
        }).encode()
        response = self.opener.open(urllib.request.Request(
            self.base + f"/task/{TASK_ID}/decision", data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.decisions[0]["decision"], "approve")
        self.assertTrue(self.decisions[0]["approver_identity"].startswith("local:approval-portal:"))

        bad = urllib.parse.urlencode({
            "csrf": "bad", "decision": "reject", "reason": "No",
        }).encode()
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.opener.open(urllib.request.Request(
                self.base + f"/task/{TASK_ID}/decision", data=bad, method="POST",
            ))
        self.assertEqual(context.exception.code, 403)
        self.assertEqual(len(self.decisions), 1)


if __name__ == "__main__":
    unittest.main()
