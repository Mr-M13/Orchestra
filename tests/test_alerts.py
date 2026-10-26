"""Tests for deduplicated operational alerts and external dead-man pings."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from omcp.alerts import AlertConfig, AlertError, alert_health, evaluate, run


def _health(*, coordinator=True, readiness="ready", backup="healthy",
            recent_failed=0, recent_interrupted=0, disk=True) -> dict:
    return {"checks": {
        "coordinator": {"ok": coordinator},
        "readiness": {"status": readiness, "queue": {"stalled": False}},
        "tasks": {
            "recent_failed": recent_failed,
            "recent_interrupted": recent_interrupted,
            "recent_startup_failures": 0, "stale_accepted": 0,
        },
        "backup": {"status": backup},
        "disk": {"ok": disk},
    }}


def _config(root: Path) -> tuple[Path, Path]:
    state = root / "alert-state.json"
    path = root / "alerts.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "healthcheck_ping_url": "https://hc.example.invalid/check",
        "webhook_url": "https://notify.example.invalid/hook",
        "state_file": str(state),
        "threshold_seconds": 600,
        "dedup_seconds": 3600,
        "disk_free_bytes": 1024,
    }))
    path.chmod(0o600)
    return path, state


class AlertTests(unittest.TestCase):
    def test_config_rejects_public_file_and_invalid_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _ = _config(root)
            path.chmod(0o644)
            with self.assertRaisesRegex(AlertError, "owner-only"):
                AlertConfig.load(path)
            path.chmod(0o600)
            raw = json.loads(path.read_text())
            raw["webhook_url"] = "file:///tmp/x"
            path.write_text(json.dumps(raw))
            path.chmod(0o600)
            with self.assertRaisesRegex(AlertError, "webhook"):
                AlertConfig.load(path)

    def test_evaluate_deduplicates_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _ = _config(Path(directory))
            config = AlertConfig.load(path)
            state = {"schema_version": 1, "baseline_done": True, "active": {}, "seen_failures": []}
            failing = _health(coordinator=False, backup="failed", recent_failed=2)
            firing, recovering, state = evaluate(config, failing, state)
            self.assertGreaterEqual(len(firing), 2)
            self.assertEqual(recovering, [])
            again, _, state = evaluate(config, failing, state)
            self.assertEqual(again, [])
            _, recovered, state = evaluate(config, _health(), state)
            self.assertTrue(recovered)
            self.assertEqual(state["active"], {})

    def test_run_baselines_then_fires_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, state_path = _config(root)
            sent = []
            def sender(method, url, payload):
                sent.append((method, url, payload))
            with patch("omcp.alerts.collect_health", return_value=_health()):
                baseline = run(path, root / "omcp.json", sender=sender)
            self.assertEqual(baseline["status"], "baseline")
            self.assertEqual(sent, [("GET", "https://hc.example.invalid/check", None)])
            with patch("omcp.alerts.collect_health", return_value=_health(backup="failed")):
                fired = run(path, root / "omcp.json", sender=sender)
            self.assertEqual(fired["status"], "delivered")
            self.assertTrue(any(
                item[0] == "POST" and item[2]["status"] == "firing" for item in sent
            ))
            self.assertTrue(any(item[1].endswith("/fail") for item in sent))
            fired_count = sum(1 for item in sent if item[0] == "POST")
            with patch("omcp.alerts.collect_health", return_value=_health(backup="failed")):
                repeated = run(path, root / "omcp.json", sender=sender)
            self.assertEqual(repeated["firing"], 0)
            self.assertEqual(sum(1 for item in sent if item[0] == "POST"), fired_count)
            with patch("omcp.alerts.collect_health", return_value=_health()):
                recovered = run(path, root / "omcp.json", sender=sender)
            self.assertEqual(recovered["recovering"], 1)
            self.assertTrue(any(
                item[0] == "POST" and item[2]["status"] == "recovered" for item in sent
            ))
            self.assertTrue(state_path.is_file())

    def test_alert_health_is_unchecked_without_run_and_healthy_after_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, state_path = _config(root)
            self.assertEqual(alert_health(path)["status"], "unchecked")
            state_path.write_text(json.dumps({
                "schema_version": 1, "baseline_done": True, "active": {},
                "last_run": datetime.now(timezone.utc).isoformat(),
            }))
            state_path.chmod(0o600)
            self.assertEqual(alert_health(path)["status"], "healthy")

    def test_historical_failures_are_baselined_and_only_new_ids_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, state_path = _config(root)
            sent = []
            def sender(method, url, payload):
                sent.append((method, url, payload))
            old = _health()
            old["checks"]["tasks"]["recent_failed_ids"] = ["old-task"]
            with patch("omcp.alerts.collect_health", return_value=old):
                first = run(path, root / "omcp.json", sender=sender)
            self.assertEqual(first["firing"], 0)
            with patch("omcp.alerts.collect_health", return_value=old):
                second = run(path, root / "omcp.json", sender=sender)
            self.assertEqual(second["firing"], 0)
            new = _health()
            new["checks"]["tasks"]["recent_failed_ids"] = ["old-task", "new-task"]
            with patch("omcp.alerts.collect_health", return_value=new):
                third = run(path, root / "omcp.json", sender=sender)
            self.assertEqual(third["firing"], 1)
            self.assertTrue(any(
                item[0] == "POST" and "new" in json.dumps(item[2]) for item in sent
            ))

    def test_missing_channel_is_unchecked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, state_path = _config(root)
            raw = json.loads(path.read_text())
            raw["healthcheck_ping_url"] = None
            raw["webhook_url"] = None
            path.write_text(json.dumps(raw))
            path.chmod(0o600)
            result = run(path, root / "omcp.json")
            self.assertEqual(result["status"], "unchecked")
            self.assertFalse(state_path.exists())


if __name__ == "__main__":
    unittest.main()
