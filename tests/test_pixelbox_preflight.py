"""Independent, non-live preflight checks for the PixelBox deployment assets."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from omcp.health import _iso_age_seconds
from omcp.mcp_proxy import _validate_socket_path, connect
from omcp.platform import SandboxPolicy, SandboxUnavailable, sandbox_command
from omcp.runner import opencode_commands


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "pixelbox"


class PixelBoxDeploymentPreflightTests(unittest.TestCase):
    def test_scripts_parse_and_default_to_read_only_operations(self):
        install = DEPLOY / "install.sh"
        verify = DEPLOY / "verify.sh"
        for script in (install, verify):
            self.assertTrue(os.access(script, os.X_OK), script)
            subprocess.run(["bash", "-n", str(script)], check=True)

        planned = subprocess.run(
            ["bash", str(install)], capture_output=True, text=True,
            check=False, timeout=30,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertIn("No files or services were changed", planned.stdout)

        help_result = subprocess.run(
            ["bash", str(verify), "--help"], capture_output=True, text=True,
            check=False, timeout=30,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--live", help_result.stdout)
        verify_source = verify.read_text()
        self.assertIn('if [[ "$LIVE" == "0" ]]', verify_source)
        self.assertIn("health_args+=(--allow-degraded)", verify_source)

    def test_installer_cannot_start_services_before_readiness_preflight(self):
        source = (DEPLOY / "install.sh").read_text()
        staged_gate = source.index('if [[ "$STAGED_ONLY" == "1" ]]')
        config_gate = source.index("Config.load")
        daemon_reload = source.index("systemctl daemon-reload")
        service_start = source.index("systemctl enable --now")
        self.assertLess(staged_gate, config_gate)
        self.assertLess(config_gate, daemon_reload)
        self.assertLess(daemon_reload, service_start)
        self.assertIn("--apply requires a Linux ARM64 host", source)
        self.assertIn("units use this path", source)

    def test_units_fail_closed_without_inputs_and_tolerate_optional_paths(self):
        coordinator = (
            DEPLOY / "systemd" / "orchestra-omcp.service"
        ).read_text()
        tunnel = (DEPLOY / "systemd" / "orchestra-tunnel.service").read_text()
        health = (DEPLOY / "systemd" / "orchestra-health.service").read_text()

        self.assertIn("ExecStartPre=/usr/bin/test -r /etc/orchestra/omcp.json", coordinator)
        self.assertIn("ExecStartPre=/usr/bin/test -S ", tunnel)
        self.assertIn("After=orchestra-omcp.service", health)
        for optional in (
            "/home/hamed/.codex", "/home/hamed/.codex-ina",
            "/home/hamed/.config/tunnel-client",
        ):
            containing = coordinator if "codex" in optional else tunnel
            self.assertIn("ReadWritePaths=-" + optional, containing)

    def test_bubblewrap_isolates_namespaces_and_clears_service_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            policy = SandboxPolicy(
                read_paths=(workspace,), workdir=workspace,
                environment={"XDG_CONFIG_HOME": str(workspace / "config")},
            )
            with patch("omcp.platform.host_platform", return_value="linux"), patch(
                "omcp.platform.sandbox_executable", return_value="/usr/bin/bwrap"
            ):
                command = sandbox_command(policy, ["/usr/bin/python3", "-V"])
        self.assertIn("--unshare-all", command)
        self.assertIn("--unshare-net", command)
        self.assertIn("--clearenv", command)
        self.assertIn("XDG_CONFIG_HOME", command)
        self.assertIn(str(workspace / "config"), command)
        self.assertIn("--new-session", command)
        self.assertEqual(command[-3:], ["--", "/usr/bin/python3", "-V"])

    def test_provider_sandbox_reshare_is_limited_to_network_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            policy = SandboxPolicy(
                write_paths=(workspace,), network_hosts=("api.deepseek.com",),
            )
            with patch("omcp.platform.host_platform", return_value="linux"), patch(
                "omcp.platform.sandbox_executable", return_value="/usr/bin/bwrap"
            ):
                command = sandbox_command(policy, ["/usr/bin/python3", "-V"])
        self.assertIn("--unshare-all", command)
        self.assertIn("--share-net", command)
        self.assertNotIn("--unshare-net", command)

    def test_opencode_loopback_gateway_fails_closed_without_filtered_linux_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            runtime = root / "runtime"
            workspace.mkdir()
            runtime.mkdir()
            prompt = root / "prompt.txt"
            prompt.write_text("bounded test")
            executable = root / "opencode-cli"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            with patch("omcp.platform.host_platform", return_value="linux"), patch(
                "omcp.platform.sandbox_executable", return_value="/usr/bin/bwrap"
            ):
                with self.assertRaisesRegex(SandboxUnavailable, "seatbelt.*not available"):
                    opencode_commands(
                        str(executable), workspace, prompt, runtime,
                        "deepseek", "deepseek/deepseek-chat", "api.deepseek.com",
                        "http://127.0.0.1:49152/abcdefghijklmnopqrstuvwxyzABCDEFG_123456/v1",
                        49153,
                    )

    def test_future_heartbeat_does_not_prove_liveness(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        recent = datetime.now(timezone.utc) - timedelta(seconds=2)
        self.assertIsNone(_iso_age_seconds(future.isoformat()))
        self.assertGreaterEqual(_iso_age_seconds(recent.isoformat()), 0)

    def test_proxy_rejects_non_socket_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            endpoint = Path(directory) / "mcp.sock"
            endpoint.write_text("not a socket")
            endpoint.chmod(0o600)
            with self.assertRaisesRegex(ConnectionError, "not a Unix socket"):
                _validate_socket_path(endpoint)
            with self.assertRaisesRegex(ConnectionError, "not a Unix socket"):
                connect(endpoint, timeout=1)

    def test_proxy_rejects_socket_metadata_that_is_not_owner_only(self):
        endpoint = Path("/does/not/matter")
        fake = type("Metadata", (), {
            "st_mode": stat.S_IFSOCK | 0o660,
            "st_uid": os.getuid(),
        })()
        with patch.object(Path, "lstat", return_value=fake):
            with self.assertRaisesRegex(PermissionError, "owner-only"):
                _validate_socket_path(endpoint)

    def test_runbook_preserves_staging_evidence_without_claiming_live_cutover(self):
        runbook = (ROOT / "docs" / "PIXELBOX-MIGRATION.md").read_text()
        readme = (ROOT / "README.md").read_text()
        self.assertIn(
            "Linux ARM64 staging, live deployment verification, and production cutover have\n"
            "not been performed",
            runbook,
        )
        self.assertIn("portable release-staging logic are locally tested", readme)
        for document in (runbook, readme):
            self.assertNotIn("Linux ARM64 staging verification complete", document)
        self.assertIn("verify.sh --live", runbook)


if __name__ == "__main__":
    unittest.main()
