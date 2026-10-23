"""Regression tests for the PixelBox broker configuration-directory ACL."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "pixelbox" / "config-access.sh"


def _acl(path: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["getfacl", "-cp", str(path)], capture_output=True, text=True, check=True,
    )
    entries: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        entries[f"{parts[0]}:{parts[1]}"] = parts[2].split()[0]
    return entries


@unittest.skipUnless(
    sys.platform.startswith("linux")
    and shutil.which("setfacl")
    and shutil.which("getfacl")
    and shutil.which("install"),
    "POSIX ACL tools and GNU install are required",
)
class BrokerConfigAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config = self.base / "etc" / "orchestra"
        self.broker_dir = self.config / "github-dispatch"
        self.broker_dir.mkdir(parents=True)
        self.config.chmod(0o700)
        self.broker_dir.chmod(0o700)
        self.broker = "nobody"
        if subprocess.run(
            ["id", self.broker], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False,
        ).returncode != 0:
            self.skipTest("nobody account is unavailable")

    def tearDown(self):
        self.temp.cleanup()

    def ensure_acl(self):
        env = {
            **os.environ,
            "ORCHESTRA_BROKER_USER": self.broker,
            "ORCHESTRA_ACL_SUDO": "",
        }
        return subprocess.run(
            ["bash", "-c", f'source "$1"; ensure_broker_traverse_acl "$2"',
             "_", str(HELPER), str(self.config)],
            env=env, capture_output=True, text=True, check=False, timeout=30,
        )

    def test_install_d_rewrites_mask_and_helper_restores_traverse_only(self):
        # Reproduce the exact installer regression: a named traverse entry is
        # installed, then `install -d -m 0700` recomputes the mask to ---.
        subprocess.run(
            ["setfacl", "-m", f"u:{self.broker}:--x,m::--x", str(self.config)],
            check=True,
        )
        subprocess.run(["install", "-d", "-m", "0700", str(self.config)], check=True)
        broken = _acl(self.config)
        self.assertEqual(broken[f"user:{self.broker}"], "--x")
        self.assertEqual(broken["mask:"], "---")

        completed = self.ensure_acl()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        fixed = _acl(self.config)
        self.assertEqual(fixed[f"user:{self.broker}"], "--x")
        self.assertIn("x", fixed["mask:"])
        self.assertEqual(fixed["other:"], "---")

    def test_repeated_staging_keeps_traverse_without_exposing_secrets(self):
        secret = self.config / "omcp.json"
        secret.write_text('{"secret": true}\n')
        secret.chmod(0o600)

        for _ in range(3):
            subprocess.run(
                ["install", "-d", "-m", "0700", str(self.config)], check=True,
            )
            completed = self.ensure_acl()
            self.assertEqual(completed.returncode, 0, completed.stderr)
            entries = _acl(self.config)
            self.assertEqual(entries[f"user:{self.broker}"], "--x")
            self.assertIn("x", entries["mask:"])
            self.assertEqual(entries["other:"], "---")
            self.assertNotIn("r", entries[f"user:{self.broker}"])

        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(f"user:{self.broker}", _acl(secret))

    def test_real_installer_stage_twice_preserves_broker_traverse(self):
        git = shutil.which("git")
        if git is None:
            self.skipTest("git is required for the installer staging test")
        release = subprocess.run(
            [git, "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        if release.returncode != 0:
            self.skipTest("the test tree is not a Git checkout")

        fake_bin = self.base / "fake-bin"
        fake_bin.mkdir()
        fake_sudo = fake_bin / "sudo"
        install = shutil.which("install")
        self.assertIsNotNone(install)
        fake_sudo.write_text(
            "#!/usr/bin/env bash\n"
            "set -e\n"
            "if [[ \"${1:-}\" == \"-u\" ]]; then shift 2; fi\n"
            "if [[ \"${1:-}\" == \"install\" ]]; then\n"
            "  shift\n"
            "  args=()\n"
            "  while (($#)); do\n"
            "    case \"$1\" in\n"
            "      -o|-g) shift 2 ;;\n"
            "      *) args+=(\"$1\"); shift ;;\n"
            "    esac\n"
            "  done\n"
            f"  exec {install} \"${{args[@]}}\"\n"
            "fi\n"
            "exec \"$@\"\n"
        )
        fake_sudo.chmod(0o700)
        temp = self.base / "tmp"
        temp.mkdir()
        env = {
            **os.environ,
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            "TMPDIR": str(temp),
            "ORCHESTRA_DEPLOY_APPROVED": "1",
            "ORCHESTRA_INSTALL_ROOT": str(self.base / "opt/orchestra"),
            "ORCHESTRA_CONFIG_ROOT": str(self.config),
            "ORCHESTRA_STATE_ROOT": str(self.base / "state"),
            "ORCHESTRA_SERVICE_USER": getpass.getuser(),
            "ORCHESTRA_BROKER_USER": self.broker,
            "ORCHESTRA_ACL_SUDO": "",
        }
        command = [
            "bash", str(ROOT / "deploy/pixelbox/install.sh"),
            "--stage", "--sha", release.stdout.strip(),
        ]
        for _ in range(2):
            completed = subprocess.run(
                command, env=env, capture_output=True, text=True,
                check=False, timeout=180,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            entries = _acl(self.config)
            self.assertEqual(entries[f"user:{self.broker}"], "--x")
            self.assertIn("x", entries["mask:"])
            self.assertEqual(entries["other:"], "---")

    def test_helper_preserves_an_existing_broader_mask(self):
        # Another named entry may legitimately need read access.  Repairing the
        # broker traverse must not reduce the existing mask or that entry.
        other = "daemon"
        if subprocess.run(
            ["id", other], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False,
        ).returncode != 0:
            self.skipTest("daemon account is unavailable")
        subprocess.run(
            ["setfacl", "-m", f"u:{other}:r--,m::r-x", str(self.config)],
            check=True,
        )
        completed = self.ensure_acl()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        entries = _acl(self.config)
        self.assertEqual(entries[f"user:{self.broker}"], "--x")
        self.assertEqual(entries[f"user:{other}"], "r--")
        self.assertIn("r", entries["mask:"])
        self.assertIn("x", entries["mask:"])
        self.assertEqual(entries["other:"], "---")


if __name__ == "__main__":
    unittest.main()
