"""Current readiness versus historical task history."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from omcp.readiness import build_readiness


def _worker(adapter: str) -> SimpleNamespace:
    return SimpleNamespace(adapter=adapter)


def _config(adapters: dict[str, str], limit: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        workers={name: _worker(adapter) for name, adapter in adapters.items()},
        max_parallel_tasks=limit,
    )


def _state(tasks: dict | None = None) -> dict:
    return {"tasks": tasks or {}}


def _health(**values):
    return values


class ReadinessTests(unittest.TestCase):
    def test_no_verified_coding_worker_is_unavailable(self):
        readiness = build_readiness(
            _config({"ina": "codex"}), _state(),
            worker_health=lambda name: _health(
                proven_healthy=False, health_reason="no_terminal_attempt",
                last_attempt=None,
            ),
        )
        self.assertEqual(readiness["status"], "unavailable")
        self.assertIn("no_operational_coding_worker", readiness["reasons"])
        self.assertFalse(readiness["can_complete_now"])

    def test_verified_idle_coding_worker_is_ready(self):
        readiness = build_readiness(
            _config({"ina": "codex", "codex": "codex"}), _state(),
            worker_health=lambda name: _health(
                proven_healthy=name == "ina",
                health_reason="verified_success" if name == "ina" else "no_terminal_attempt",
                last_attempt=None,
            ),
        )
        self.assertEqual(readiness["status"], "ready")
        by_name = {item["worker"]: item for item in readiness["workers"]}
        self.assertEqual(by_name["ina"]["role"], "coding")
        self.assertTrue(by_name["ina"]["available_now"])
        self.assertFalse(by_name["codex"]["operational"])

    def test_busy_capacity_is_degraded_not_unavailable(self):
        now = datetime.now(timezone.utc)
        tasks = {
            "a": {"task_id": "a", "worker": "ina", "status": "running",
                  "created_at": now.isoformat()},
            "b": {"task_id": "b", "worker": "codex", "status": "running",
                  "created_at": now.isoformat()},
        }
        readiness = build_readiness(
            _config({"ina": "codex", "codex": "codex"}), _state(tasks),
            worker_health=lambda name: _health(
                proven_healthy=True, health_reason="verified_success", last_attempt=None,
            ),
        )
        self.assertEqual(readiness["status"], "degraded")
        self.assertIn("execution_capacity_saturated", readiness["reasons"])
        self.assertFalse(readiness["can_complete_now"])

    def test_deepseek_is_response_only_and_does_not_satisfy_coding_readiness(self):
        readiness = build_readiness(
            _config({"deepseek": "deepseek"}), _state(),
            worker_health=lambda name: _health(
                proven_healthy=False, health_reason="response_only",
                last_attempt={"status": "succeeded"},
            ),
        )
        self.assertEqual(readiness["status"], "unavailable")
        self.assertEqual(readiness["workers"][0]["role"], "response_only")
        self.assertTrue(readiness["workers"][0]["operational"])

    def test_actions_worker_is_unproven_until_a_success(self):
        readiness = build_readiness(
            _config({"actions-codex": "github_actions"}), _state(),
            worker_health=lambda name: _health(
                proven_healthy=False, health_reason="last_attempt_failed",
                last_attempt={"status": "failed"},
            ),
        )
        self.assertEqual(readiness["status"], "unavailable")
        self.assertEqual(readiness["workers"][0]["role"], "remote_unproven")

    def test_stale_accepted_queue_is_degraded(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        readiness = build_readiness(
            _config({"ina": "codex"}), _state({
                "a": {"task_id": "a", "worker": "ina", "status": "accepted",
                      "created_at": old},
            }),
            worker_health=lambda name: _health(
                proven_healthy=True, health_reason="verified_success", last_attempt=None,
            ),
        )
        self.assertEqual(readiness["status"], "degraded")
        self.assertIn("queue_not_advancing", readiness["reasons"])

    def test_historical_failure_does_not_make_a_healthy_last_worker_unready(self):
        # The last verified result is successful; older failures are history.
        readiness = build_readiness(
            _config({"ina": "codex"}), _state(),
            worker_health=lambda name: _health(
                proven_healthy=True, health_reason="verified_success",
                last_attempt={"status": "succeeded"},
            ),
        )
        self.assertEqual(readiness["status"], "ready")


if __name__ == "__main__":
    unittest.main()
