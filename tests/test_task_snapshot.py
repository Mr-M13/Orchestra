from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from omcp.controller import (
    ACTIVE, QUEUED, TERMINAL, AuthorizationError, Config, Principal,
    TaskController, Worker,
)


ALL_PERMISSIONS = frozenset({
    "status:read", "tasks:submit", "tasks:read", "results:read",
    "workers:read", "tasks:cancel", "approvals:read",
    "host_operations:submit",
})


def task_id(number: int) -> str:
    return f"{number:08d}-0000-0000-0000-000000000000"


def task(number: int, status: str, **overrides):
    value = {
        "task_id": task_id(number),
        "title": f"Task {number}",
        "worker": "ina",
        "status": status,
        "created_at": f"2026-10-24T10:{number % 60:02d}:00+00:00",
        "repository": "owner/repo",
        "instructions": "private task instructions",
    }
    value.update(overrides)
    return value


def snapshot(tasks, limit=20):
    return TaskController.build_task_snapshot(
        {item["task_id"]: item for item in tasks}, limit,
    )


class ClassificationTests(unittest.TestCase):
    def test_every_supported_status_has_exactly_one_home(self):
        statuses = sorted(ACTIVE | QUEUED | TERMINAL)
        tasks = [task(index, status) for index, status in enumerate(statuses)]
        result = snapshot(tasks)

        running_ids = {item["task_id"] for item in result["running"]}
        queued_ids = {item["task_id"] for item in result["queued"]}
        recent_ids = {item["task_id"] for item in result["recent"]}
        self.assertEqual(running_ids, {
            item["task_id"] for item in tasks if item["status"] in ACTIVE
        })
        self.assertEqual(queued_ids, {
            item["task_id"] for item in tasks if item["status"] in QUEUED
        })
        self.assertEqual(recent_ids, {
            item["task_id"] for item in tasks if item["status"] in TERMINAL
        })
        self.assertFalse(running_ids & queued_ids)
        self.assertFalse(running_ids & recent_ids)
        self.assertFalse(queued_ids & recent_ids)

    def test_approval_pending_is_queued_but_explicitly_distinguished(self):
        result = snapshot([
            task(1, "accepted"),
            task(2, "approval_required"),
        ])
        self.assertEqual(result["counts"]["queued"], 2)
        self.assertEqual(result["counts"]["ready"], 1)
        self.assertEqual(result["counts"]["approval_pending"], 1)
        self.assertEqual(
            [item["queue_state"] for item in result["queued"]],
            ["ready", "approval_pending"],
        )
        self.assertNotIn(
            task_id(2), {item["task_id"] for item in result["running"]},
        )

    def test_running_sorted_oldest_first_and_never_truncated(self):
        tasks = [
            task(1, "running", started_at="2026-10-24T12:00:00+00:00"),
            task(2, "starting", started_at="2026-10-24T10:00:00+00:00"),
            task(3, "cancel_requested", started_at="2026-10-24T11:00:00+00:00"),
        ]
        result = snapshot(tasks)
        self.assertEqual(
            [item["task_id"] for item in result["running"]],
            [task_id(2), task_id(3), task_id(1)],
        )

    def test_queued_ready_tasks_follow_scheduler_fifo_then_approval_pending(self):
        tasks = [
            task(9, "approval_required", created_at="2026-10-24T09:00:00+00:00"),
            task(3, "accepted", created_at="2026-10-24T11:00:00+00:00"),
            task(1, "accepted", created_at="2026-10-24T10:00:00+00:00"),
            task(2, "accepted", created_at="2026-10-24T10:30:00+00:00"),
        ]
        result = snapshot(tasks)
        self.assertEqual(
            [item["task_id"] for item in result["queued"]],
            [task_id(1), task_id(2), task_id(3), task_id(9)],
        )

    def test_recent_uses_finished_time_falls_back_to_created_and_is_limited(self):
        tasks = [
            task(index, "succeeded", finished_at=f"2026-10-24T12:{index:02d}:00+00:00")
            for index in range(25)
        ]
        tasks.append(task(99, "failed", created_at="2026-10-24T13:00:00+00:00"))
        result = snapshot(tasks, 20)
        self.assertEqual(len(result["recent"]), 20)
        self.assertEqual(result["recent"][0]["task_id"], task_id(99))
        self.assertEqual(
            [item["terminal_at"] for item in result["recent"]],
            sorted((item["terminal_at"] for item in result["recent"]), reverse=True),
        )
        self.assertEqual(result["counts"]["recent"], 20)
        self.assertEqual(result["counts"]["recent_limit"], 20)

    def test_more_than_five_hundred_running_and_queued_are_complete(self):
        tasks = [task(index, "running") for index in range(525)]
        tasks += [task(1000 + index, "accepted") for index in range(525)]
        result = snapshot(tasks)
        self.assertEqual(result["counts"]["running"], 525)
        self.assertEqual(result["counts"]["queued"], 525)
        self.assertEqual(len(result["running"]), 525)
        self.assertEqual(len(result["queued"]), 525)

    def test_transitions_move_a_task_without_duplication(self):
        tasks = {task_id(1): task(1, "accepted")}

        queued = TaskController.build_task_snapshot(tasks, 20)
        self.assertEqual([item["task_id"] for item in queued["queued"]], [task_id(1)])
        self.assertEqual(queued["running"], [])
        self.assertEqual(queued["recent"], [])

        tasks[task_id(1)]["status"] = "running"
        running = TaskController.build_task_snapshot(tasks, 20)
        self.assertEqual([item["task_id"] for item in running["running"]], [task_id(1)])
        self.assertEqual(running["queued"], [])
        self.assertEqual(running["recent"], [])

        tasks[task_id(1)].update({
            "status": "failed", "finished_at": "2026-10-24T15:00:00+00:00",
        })
        recent = TaskController.build_task_snapshot(tasks, 20)
        self.assertEqual([item["task_id"] for item in recent["recent"]], [task_id(1)])
        self.assertEqual(recent["running"], [])
        self.assertEqual(recent["queued"], [])

    def test_unavailable_worker_does_not_make_a_running_task_terminal(self):
        result = snapshot([task(1, "running", worker="worker-that-went-away")])
        self.assertEqual(result["counts"]["running"], 1)
        self.assertEqual(result["recent"], [])

    def test_private_instructions_are_not_in_the_dashboard_projection(self):
        result = snapshot([
            task(1, "running"),
            task(2, "accepted"),
            task(3, "succeeded", finished_at="2026-10-24T12:00:00+00:00"),
        ])
        for section in ("running", "queued", "recent"):
            self.assertNotIn("instructions", result[section][0])

    def test_empty_snapshot_has_zero_counts(self):
        result = snapshot([])
        self.assertEqual(result["running"], [])
        self.assertEqual(result["queued"], [])
        self.assertEqual(result["recent"], [])
        self.assertEqual(result["counts"], {
            "running": 0, "queued": 0, "ready": 0,
            "approval_pending": 0, "recent": 0, "recent_limit": 20,
        })

    def test_recent_limit_is_bounded(self):
        for limit in (0, 101, True, "20"):
            with self.assertRaises(ValueError):
                snapshot([], limit)


class SnapshotPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name) / "project"
        root.mkdir()
        self.config = Config(
            "owner/repo", root, Path(self.temp.name) / "state", "origin/main",
            {"ina": Worker("ina", ("codex",))},
            Principal("chatgpt:test", frozenset({"status:read"}), "test-session"),
        )
        self.controller = TaskController(
            self.config, popen=Mock(), workspace_preparer=lambda *_: root,
        )

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def test_snapshot_requires_existing_task_read_permission(self):
        with self.assertRaises(AuthorizationError):
            self.controller.task_snapshot()


class SnapshotControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name) / "project"
        root.mkdir()
        self.config = Config(
            "owner/repo", root, Path(self.temp.name) / "state", "origin/main",
            {"ina": Worker("ina", ("codex",))},
            Principal("chatgpt:test", ALL_PERMISSIONS, "test-session"),
        )
        self.controller = TaskController(
            self.config, popen=Mock(), workspace_preparer=lambda *_: root,
        )

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def test_snapshot_includes_generated_at_and_uses_one_classification_pass(self):
        self.controller.tick = Mock()
        self.controller.state["tasks"] = {
            task(1, "accepted")["task_id"]: task(1, "accepted"),
            task(2, "succeeded", finished_at="2026-10-24T12:00:00+00:00")["task_id"]:
                task(2, "succeeded", finished_at="2026-10-24T12:00:00+00:00"),
        }
        result = self.controller.task_snapshot(20)
        self.assertIn("generated_at", result)
        self.assertEqual(result["counts"]["queued"], 1)
        self.assertEqual(result["counts"]["recent"], 1)
        self.assertEqual(result["recent"][0]["progress"], "Objective unverified")


if __name__ == "__main__":
    unittest.main()
