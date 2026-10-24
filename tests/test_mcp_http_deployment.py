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
        self.assertIn("ExecStartPre=/usr/bin/test -r /etc/orchestra/mcp-http.env", unit)
        self.assertIn("ExecStartPre=/usr/bin/test -S /run/orchestra/omcp.sock", unit)
        self.assertIn("--host 127.0.0.1", unit)
        self.assertIn("IPAddressDeny=any", unit)
        self.assertIn("IPAddressAllow=127.0.0.1/32", unit)
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
        self.assertIn("REPLACE_WITH_RANDOM_TOKEN", source)

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
        self.assertIn("release_has_gateway_unit", install_units)
        disable = install_units.index("systemctl disable --now orchestra-mcp-http.service")
        remove = install_units.index('rm -f "$UNIT_ROOT/orchestra-mcp-http.service"')
        self.assertLess(disable, remove)
        self.assertIn('release_has_gateway_unit "$old_root"', source)


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
sudo() { printf '%s\n' "$*" >> "$calls"; }
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
        self.assertIn("disable --now orchestra-mcp-http.service", rollback)
        self.assertIn("rm -f", rollback)
        self.assertIn("orchestra-mcp-http.service", rollback)

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
