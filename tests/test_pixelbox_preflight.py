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
        self.assertIn("--with-tests", help_result.stdout)
        self.assertIn("--require-health", help_result.stdout)
        verify_source = verify.read_text()
        self.assertIn("--allow-degraded", verify_source)
        self.assertIn("600|660", verify_source)
        self.assertIn("Verification PARTIAL", verify_source)
        self.assertIn("source-tree unit tests", verify_source)
        self.assertIn("UNCHECKED", verify_source)
        self.assertIn("release_digest", verify_source)
        for unit in (
            "orchestra-backup.service", "orchestra-backup.timer",
            "orchestra-alerts.service", "orchestra-alerts.timer",
        ):
            self.assertTrue((DEPLOY / "systemd" / unit).is_file())
            self.assertIn(unit, (DEPLOY / "install.sh").read_text())
        self.assertIn("--backup-config", verify_source)
        self.assertIn("--alerts-config", verify_source)
        self.assertTrue((DEPLOY / "config_preflight.py").is_file())
        install_source = install.read_text()
        self.assertIn("preflight_activation_config", install_source)
        self.assertIn("tar --acls --xattrs", install_source)
        self.assertIn('dirname "$STATE_ROOT")/backup/pre-activate', install_source)

    def test_installer_cannot_start_services_before_readiness_preflight(self):
        source = (DEPLOY / "install.sh").read_text()
        staged_gate = source.index('if [[ "$STAGED_ONLY" == "1" ]]')
        daemon_reload = source.index("systemctl daemon-reload")
        coordinator_config_gate = source.index(
            "from omcp.controller import Config; Config.load"
        )
        coordinator_start = source.index("systemctl enable orchestra-omcp.service")
        self.assertLess(staged_gate, daemon_reload)
        self.assertLess(daemon_reload, coordinator_config_gate)
        self.assertLess(coordinator_config_gate, coordinator_start)
        self.assertIn("orchestra-github-dispatch.service", source)
        self.assertIn("--apply requires a Linux ARM64 host", source)
        self.assertIn("units use this path", source)
        preflight_pos = source.index('preflight_activation_config "$release_root"')
        backup_pos = source.index('backup_activation_config "$old_sha"')
        install_units_pos = source.index('install_units "$release_root"', preflight_pos)
        self.assertLess(preflight_pos, backup_pos)
        self.assertLess(backup_pos, install_units_pos)
        self.assertIn("restore_activation_config", source)

        activate = source[source.index("activate_release() {") :]
        broker_restart = activate.index(
            "systemctl restart orchestra-github-dispatch.service"
        )
        broker_wait = activate.index("wait_for_github_dispatch_socket", broker_restart)
        coordinator_restart = activate.index(
            "systemctl restart orchestra-omcp.service", broker_wait
        )
        self.assertLess(broker_restart, broker_wait)
        self.assertLess(broker_wait, coordinator_restart)

    def test_installer_reapplies_broker_acl_after_config_directory_install(self):
        source = (DEPLOY / "install.sh").read_text()
        helper = DEPLOY / "config-access.sh"
        self.assertTrue(helper.is_file())
        subprocess.run(["bash", "-n", str(helper)], check=True)

        install = source.index(
            'install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_ROOT"'
        )
        staged_acl = source.index(
            'ensure_broker_traverse_acl "$CONFIG_ROOT"', install,
        )
        self.assertLess(install, staged_acl)
        preflight = source.index("preflight_activation()")
        activation_acl = source.index(
            'ensure_broker_traverse_acl "$CONFIG_ROOT"', preflight,
        )
        self.assertGreater(activation_acl, preflight)

        # Verify the actual broker identity, not only the ACL text.
        self.assertIn('sudo -u "$BROKER_USER" /usr/bin/test -x "$CONFIG_ROOT"', source)
        self.assertIn(
            'sudo -u "$BROKER_USER" /usr/bin/test -r "$CONFIG_ROOT/$file"', source,
        )
        helper_source = helper.read_text()
        self.assertNotIn("chmod -R", helper_source)
        self.assertNotIn("chown -R", helper_source)
        self.assertIn('u:$broker_user:--x', helper_source)

    def test_units_fail_closed_without_inputs_and_tolerate_optional_paths(self):
        coordinator = (
            DEPLOY / "systemd" / "orchestra-omcp.service"
        ).read_text()
        tunnel = (DEPLOY / "systemd" / "orchestra-tunnel.service").read_text()
        health = (DEPLOY / "systemd" / "orchestra-health.service").read_text()

        self.assertIn("ExecStartPre=/usr/bin/test -r /etc/orchestra/omcp.json", coordinator)
        self.assertIn("RestrictSUIDSGID=yes", coordinator)
        self.assertNotIn("orchestra-github-dispatch", coordinator)
        self.assertIn("ExecStartPre=/usr/bin/test -S ", tunnel)
        self.assertIn("After=orchestra-omcp.service", health)
        for optional in (
            "/home/hamed/.codex", "/home/hamed/.codex-ina",
            "/home/hamed/.config/tunnel-client",
        ):
            containing = coordinator if "codex" in optional else tunnel
            self.assertIn("ReadWritePaths=-" + optional, containing)

        backup = (DEPLOY / "systemd" / "orchestra-backup.service").read_text()
        self.assertIn("StateDirectory=orchestra-backup", backup)
        self.assertIn("StateDirectoryMode=0700", backup)
        self.assertIn("ReadWritePaths=-/mnt/mbox/OrchestraBackup", backup)
        self.assertIn("ReadOnlyPaths=/mnt/mbox", backup)
        self.assertNotIn("ReadWritePaths=/mnt/mbox\n", backup)

    def test_dashboard_and_approval_portal_units_are_loopback_only_and_hardened(self):
        dashboard = (DEPLOY / "systemd" / "orchestra-dashboard.service").read_text()
        portal = (DEPLOY / "systemd" / "orchestra-approval-portal.service").read_text()
        install = (DEPLOY / "install.sh").read_text()
        for unit, port in ((dashboard, "8082"), (portal, "8767")):
            self.assertIn("User=hamed", unit)
            self.assertIn("NoNewPrivileges=yes", unit)
            self.assertIn("ProtectSystem=strict", unit)
            self.assertIn("ProtectHome=read-only", unit)
            self.assertIn("ReadOnlyPaths=/home/hamed/orchestra-migration/state", unit)
            self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET", unit)
            self.assertIn("CapabilityBoundingSet=", unit)
            self.assertIn("AmbientCapabilities=", unit)
            self.assertNotIn("--allow-remote", unit)
        self.assertIn("ORCHESTRA_DASHBOARD_PORT=8082", dashboard)
        self.assertNotIn("APPROVAL_STATE_DIR", dashboard)
        self.assertNotIn("APPROVER_IDENTITY", dashboard)
        self.assertIn("--listen-host 127.0.0.1 --listen-port 8767", portal)
        self.assertIn("approval-portal.token", portal)
        self.assertNotIn("ReadWritePaths", portal)
        self.assertIn("orchestra-dashboard.service", install)
        self.assertIn("orchestra-approval-portal.service", install)

    def test_dashboard_standing_authorization_template_is_exact_and_not_auto_installed(self):
        import json
        fragment = json.loads(
            (DEPLOY / "omcp.dashboard-development.example.json").read_text()
        )
        self.assertEqual(set(fragment["repository_profiles"]), {"orchestra-dashboard"})
        template = fragment["standing_authorizations"][0]
        self.assertEqual(template["repository_profiles"], ["orchestra-dashboard"])
        self.assertEqual(template["workers"], ["ina"])
        self.assertEqual(template["writable_paths"], [])
        self.assertEqual(template["writable_files"], [
            "dashboard/app.py", "dashboard/system.py",
            "dashboard/static/system.js", "dashboard/README.md",
        ])
        example = json.loads((DEPLOY / "omcp.example.json").read_text())
        self.assertNotIn(
            template["id"],
            {item.get("id") for item in example.get("standing_authorizations", [])},
        )
        self.assertNotIn("orchestra-dashboard", example["repository_profiles"])

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

    def test_systemd_fallback_keeps_normal_workers_off_the_network(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with patch("omcp.platform.host_platform", return_value="linux"), patch(
                "omcp.platform.sandbox_executable", return_value="/usr/bin/systemd-run"
            ):
                command = sandbox_command(SandboxPolicy(
                    read_paths=(workspace,), backend="systemd", workdir=workspace,
                ), ["/usr/bin/true"])
        self.assertIn("--property=NoNewPrivileges=yes", command)
        self.assertIn("--property=PrivateTmp=yes", command)
        self.assertIn("--property=RestrictAddressFamilies=AF_UNIX", command)

    def test_systemd_provider_egress_requires_literal_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with patch("omcp.platform.host_platform", return_value="linux"), patch(
                "omcp.platform.sandbox_executable", return_value="/usr/bin/systemd-run"
            ):
                with self.assertRaisesRegex(SandboxUnavailable, "IP allowlist"):
                    sandbox_command(SandboxPolicy(
                        read_paths=(workspace,), network_hosts=("api.deepseek.com",),
                        backend="systemd", workdir=workspace,
                    ), ["/usr/bin/true"])
                command = sandbox_command(SandboxPolicy(
                    read_paths=(workspace,), network_hosts=("api.deepseek.com",),
                    network_addresses=("203.0.113.8",), backend="systemd", workdir=workspace,
                ), ["/usr/bin/true"])
        self.assertIn("--property=IPAddressDeny=any", command)
        self.assertIn("--property=IPAddressAllow=203.0.113.8/32", command)

    def test_systemd_launcher_runs_workers_as_a_dedicated_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with patch("omcp.platform.host_platform", return_value="linux"), patch(
                "omcp.platform.sandbox_executable", return_value="/usr/bin/systemd-run"
            ):
                command = sandbox_command(SandboxPolicy(
                    read_paths=(workspace,), backend="systemd", workdir=workspace,
                    worker_user="orchestra-worker", worker_group="orchestra-worker",
                ), ["/usr/bin/true"])
        self.assertIn("--uid=orchestra-worker", command)
        self.assertIn("--gid=orchestra-worker", command)

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
            "st_mode": stat.S_IFSOCK | 0o666,
            "st_uid": os.getuid(),
        })()
        with patch.object(Path, "lstat", return_value=fake):
            with self.assertRaisesRegex(PermissionError, "owner/group-only"):
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
        self.assertIn("separate owner approval", runbook)
        self.assertIn("A `0600` file under the same\nUID is not isolation", runbook)

    def test_coordinator_example_has_four_local_lanes_without_actions_dispatch(self):
        import json

        example = json.loads((DEPLOY / "omcp.example.json").read_text())
        self.assertEqual(set(example["workers"]), {
            "ina-codex", "hamed-codex", "deepseek-primary", "deepseek-secondary",
        })
        self.assertEqual(example["task_approval_mode"], "disabled")
        for worker in example["workers"].values():
            self.assertNotIn("github_dispatch_socket", worker)
            self.assertNotIn("github_service_uid", worker)


if __name__ == "__main__":
    unittest.main()
