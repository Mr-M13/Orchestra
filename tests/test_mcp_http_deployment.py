from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy" / "pixelbox" / "install.sh"
CONFIG_ACCESS = ROOT / "deploy" / "pixelbox" / "config-access.sh"


def run_bash(source: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", source], cwd=ROOT, capture_output=True, text=True,
        check=False, timeout=30,
    )


class InstallerSourceTests(unittest.TestCase):
    def test_identity_and_gateway_access_are_prepared_before_gateway_config(self):
        source = INSTALL.read_text()
        stage = source[source.index("stage_configuration()"):source.index(
            "configuration_is_ready_for_activation()",
        )]
        identity = stage.index("prepare_mcp_identity")
        gateway_acl = stage.index("ensure_group_traverse_acl")
        gateway_env = stage.index('install_environment_file "$release_root" mcp-http.env')
        self.assertLess(identity, gateway_acl)
        self.assertLess(gateway_acl, gateway_env)
        self.assertIn('"$MCP_HTTP_USER" "$MCP_HTTP_GROUP"', stage)

    def test_gateway_unit_uses_matching_group_and_loopback_only_network_policy(self):
        unit = (ROOT / "deploy/pixelbox/systemd/orchestra-mcp-http.service").read_text()
        self.assertIn("User=orchestra-mcp-http", unit)
        self.assertIn("Group=orchestra-mcp", unit)
        self.assertNotIn("Group=orchestra-mcp-http", unit)
        self.assertIn("WorkingDirectory=/opt/orchestra/current", unit)
        self.assertIn("Environment=PYTHONPATH=/opt/orchestra/current", unit)
        self.assertIn(
            'ExecStartPre=/usr/bin/python3 -c "import omcp.http_gateway"', unit,
        )
        self.assertIn("ExecStartPre=/usr/bin/test -r /etc/orchestra/mcp-http.env", unit)
        self.assertIn("ExecStartPre=/usr/bin/test -S /run/orchestra/omcp.sock", unit)
        self.assertIn("--host 127.0.0.1", unit)
        self.assertIn("IPAddressDeny=any", unit)
        self.assertIn("IPAddressAllow=127.0.0.1/32", unit)
        self.assertIn("SocketBindDeny=any", unit)
        self.assertIn("SocketBindAllow=ipv4:tcp:8090", unit)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET", unit)
        self.assertIn("PrivateNetwork=no", unit)
        self.assertIn("CapabilityBoundingSet=", unit)
        self.assertNotIn("0.0.0.0", unit)
        self.assertNotIn("--host ::", unit)
        self.assertNotIn("--host 0.0.0.0", unit)

    def test_gateway_configuration_is_owner_only_and_service_readable(self):
        source = INSTALL.read_text()
        self.assertIn('sudo chown "$owner:$group" "$destination"', source)
        self.assertIn('sudo chmod 0600 "$destination"', source)
        self.assertIn('sudo -u "$MCP_HTTP_USER" /usr/bin/test -r "$env_file"', source)
        self.assertIn('sudo -u "$MCP_HTTP_USER" /usr/bin/test -x "$CONFIG_ROOT"', source)
        self.assertIn('must be owned by $MCP_HTTP_USER', source)
        self.assertIn('must have group $MCP_HTTP_GROUP', source)
        self.assertIn("gateway_environment_is_ready", source)
        example = (ROOT / "deploy/pixelbox/mcp-http.env.example").read_text()
        self.assertIn("REPLACE_WITH_RANDOM_TOKEN", example)

    def test_gateway_secret_is_never_echoed(self):
        install = INSTALL.read_text()
        verifier = (ROOT / "deploy/pixelbox/verify_mcp_http.sh").read_text()
        for source in (install, verifier):
            self.assertNotIn("echo $ORCHESTRA_MCP_HTTP_TOKEN", source)
            self.assertNotIn("echo \"$ORCHESTRA_MCP_HTTP_TOKEN\"", source)
            self.assertNotIn("print(token)", source)
        self.assertIn("read_token", verifier)

    def test_rollback_to_pre_https_release_removes_gateway_unit(self):
        source = INSTALL.read_text()
        install_units = source[source.index("install_units()"):source.index(
            "prepare_mcp_identity()",
        )]
        helper = source[source.index("stop_gateway_or_fail()"):source.index(
            "install_units()",
        )]
        self.assertIn("release_has_gateway_unit", install_units)
        self.assertIn("stop_gateway_or_fail", install_units)
        disable = helper.index("systemctl disable --now orchestra-mcp-http.service")
        remove = helper.index('rm -f "$UNIT_ROOT/orchestra-mcp-http.service"')
        self.assertLess(disable, remove)
        self.assertIn('release_has_gateway_unit "$old_root"', source)

    def test_first_activation_failure_removes_the_gateway_unit(self):
        source = INSTALL.read_text()
        failure = source[source.index('echo "Activation failed; restoring'):source.index("main()")]
        self.assertIn("stop_gateway_or_fail", failure)
        self.assertIn("systemctl daemon-reload", failure)
        helper = source[source.index("stop_gateway_or_fail()"):source.index("install_units()")]
        self.assertIn('rm -f "$UNIT_ROOT/orchestra-mcp-http.service"', helper)
        self.assertIn("a listener remains on port 8090", helper)

    def test_activation_waits_for_gateway_health_before_declaring_success(self):
        source = INSTALL.read_text()
        activation = source[source.index("activate_release()"):source.index("main()")]
        self.assertIn("wait_for_gateway_health", activation)
        self.assertIn("wait_for_omcp_socket", activation)
        health = source[source.index("wait_for_gateway_health()"):source.index("preflight_activation()")]
        self.assertIn("omcp.http_gateway_verify", health)
        self.assertIn("--cycles 1", health)
        self.assertIn("--env-file", health)
        unit = (ROOT / "deploy/pixelbox/systemd/orchestra-mcp-http.service").read_text()
        self.assertIn("WorkingDirectory=/opt/orchestra/current", unit)

    def test_tunnel_proxy_group_policy_is_explicit(self):
        tunnel = (ROOT / "deploy/pixelbox/systemd/orchestra-tunnel.service").read_text()
        self.assertIn("Environment=ORCHESTRA_OMCP_SOCKET_GROUP=orchestra-mcp", tunnel)
        proxy = (ROOT / "omcp/mcp_proxy.py").read_text()
        self.assertIn("expected_group", proxy)
        self.assertIn("coordinator socket group does not match policy", proxy)

    def test_rollback_validation_does_not_require_newer_helper_files(self):
        source = INSTALL.read_text()
        validation = source[source.index("validate_release_tree()"):source.index("stage_release()")]
        self.assertNotIn("config-access.sh", validation)
        self.assertIn("release_has_gateway_unit", validation)

    def test_portable_staging_rejects_gateway_releases(self):
        source = INSTALL.read_text()
        self.assertIn(
            "staging an HTTPS gateway release requires Linux account and ACL tools",
            source,
        )

    def test_live_verifier_checks_identity_mode_and_negative_auth(self):
        verifier = (ROOT / "deploy/pixelbox/verify_mcp_http.sh").read_text()
        module = (ROOT / "omcp/http_gateway_verify.py").read_text()
        self.assertIn("gateway environment must be owned by", verifier)
        self.assertIn("gateway environment must have group", verifier)
        self.assertIn("coordinator socket identity/mode does not match policy", verifier)
        self.assertIn("omcp.http_gateway_verify", verifier)
        self.assertIn("python3 -E", verifier)
        self.assertIn("wrong-token", module)
        self.assertIn("JSON-RPC response id mismatch", module)
        self.assertIn("127.0.0.1:8090", verifier)


class InstallerFunctionTests(unittest.TestCase):
    def test_install_units_upgrades_to_gateway_and_removes_it_on_rollback(self):
        result = run_bash(r'''
set -euo pipefail
source deploy/pixelbox/install.sh
calls="$(mktemp)"
unit_root="$(mktemp -d)"
release="$(mktemp -d)"
mkdir -p "$release/deploy/pixelbox/systemd"
cp deploy/pixelbox/systemd/orchestra-mcp-http.service "$release/deploy/pixelbox/systemd/"
sudo() {
  printf '%s\n' "$*" >> "$calls"
  [[ "$*" == "systemctl is-active --quiet orchestra-mcp-http.service" ]] && return 1
  return 0
}
UNIT_ROOT="$unit_root"
install_units "$release"
printf '%s\n' '---rollback---' >> "$calls"
old="$(mktemp -d)"
mkdir -p "$old/deploy/pixelbox/systemd"
install_units "$old"
cat "$calls"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        upgrade, rollback = result.stdout.split("---rollback---")
        self.assertIn("orchestra-mcp-http.service", upgrade)
        self.assertIn("rm -f", rollback)
        self.assertIn("orchestra-mcp-http.service", rollback)

    def test_rollback_stop_failure_fails_closed(self):
        result = run_bash(r'''
set -euo pipefail
source deploy/pixelbox/install.sh
unit_root="$(mktemp -d)"
old="$(mktemp -d)"
mkdir -p "$old/deploy/pixelbox/systemd"
sudo() {
  if [[ "$*" == "systemctl is-active --quiet orchestra-mcp-http.service" ]]; then
    return 0
  fi
  if [[ "$*" == "systemctl disable --now orchestra-mcp-http.service" ]]; then
    return 1
  fi
  return 0
}
UNIT_ROOT="$unit_root"
install_units "$old"
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not stop the HTTP gateway", result.stderr)

    def test_prepare_identity_creates_group_and_nologin_user(self):
        result = run_bash(r'''
set -euo pipefail
source deploy/pixelbox/install.sh
calls="$(mktemp)"
created_group=0
created_user=0
getent() {
  if [[ "$1" == "group" && "$2" == "orchestra-mcp" ]]; then
    (( created_group )) && { printf 'orchestra-mcp:x:4242:\n'; return 0; }
    return 2
  fi
  return 2
}
id() {
  if [[ "$1" == "-u" ]]; then printf '4243\n'; return 0; fi
  if [[ "$1" == "-g" ]]; then
    (( created_user )) && printf '4242\n' || printf '4243\n'
    return 0
  fi
  [[ "$1" == "orchestra-mcp-http" ]] && (( created_user ))
}
groupadd() { created_group=1; printf 'groupadd %s\n' "$*" >> "$calls"; }
useradd() { created_user=1; printf 'useradd %s\n' "$*" >> "$calls"; }
sudo() { "$@"; }
prepare_mcp_identity
cat "$calls"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("groupadd --system orchestra-mcp", result.stdout)
        self.assertIn(
            "useradd --system --no-create-home --home-dir /nonexistent "
            "--shell /usr/sbin/nologin --gid orchestra-mcp orchestra-mcp-http",
            result.stdout,
        )

    def test_prepare_identity_rejects_existing_user_with_wrong_primary_group(self):
        result = run_bash(r'''
set -euo pipefail
source deploy/pixelbox/install.sh
getent() {
  [[ "$1" == "group" && "$2" == "orchestra-mcp" ]] \
    && { printf 'orchestra-mcp:x:4242:\n'; return 0; }
  return 2
}
id() {
  if [[ "$1" == "-u" ]]; then printf '4243\n'; return 0; fi
  if [[ "$1" == "-g" ]]; then printf '9999\n'; return 0; fi
  [[ "$1" == "orchestra-mcp-http" ]] && return 0
  return 1
}
groupadd() { return 0; }
useradd() { return 0; }
sudo() { "$@"; }
prepare_mcp_identity
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must use orchestra-mcp as its primary group", result.stderr)

    def test_gateway_environment_install_preserves_existing_content(self):
        result = run_bash(r'''
set -euo pipefail
source deploy/pixelbox/install.sh
calls="$(mktemp)"
release="$(mktemp -d)"
mkdir -p "$release/deploy/pixelbox"
printf 'ORCHESTRA_MCP_HTTP_TOKEN=%s\n' "$(printf 'x%.0s' {1..40})" \
  > "$release/deploy/pixelbox/mcp-http.env.example"
CONFIG_ROOT="$(mktemp -d)"
sudo() { printf '%s\n' "$*" >> "$calls"; }
install_environment_file "$release" mcp-http.env orchestra-mcp-http orchestra-mcp
printf '%s\n' '---existing---' >> "$calls"
printf 'ORCHESTRA_MCP_HTTP_TOKEN=%s\n' "$(printf 'y%.0s' {1..40})" \
  > "$CONFIG_ROOT/mcp-http.env"
install_environment_file "$release" mcp-http.env orchestra-mcp-http orchestra-mcp
cat "$calls"
cat "$CONFIG_ROOT/mcp-http.env"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded, content = result.stdout.split("---existing---", 1)
        self.assertIn("install -m 0600 -o orchestra-mcp-http -g orchestra-mcp", recorded)
        self.assertIn("chown orchestra-mcp-http:orchestra-mcp", content)
        self.assertIn("chmod 0600", content)
        self.assertIn("y" * 40, content)
        self.assertNotIn("x" * 40, content)

    def test_config_access_grants_only_group_traverse(self):
        source = CONFIG_ACCESS.read_text()
        helper = source[source.index("ensure_group_traverse_acl()"):source.index(
            "ensure_broker_traverse_acl()",
        )]
        self.assertIn('setfacl -m "g:$allowed_group:--x"', helper)
        self.assertIn('[[ "$other" == "---" ]]', helper)
        self.assertNotIn("chmod -R", helper)
        self.assertNotIn("chown -R", helper)
        self.assertNotIn("rwx", helper)


if __name__ == "__main__":
    unittest.main()
