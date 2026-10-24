import os
import subprocess
import sys
import time
import unittest

from orchestra.process_identity import (
    ProcessIdentity,
    ProcessIdentityError,
    capture_process_identity,
)


class ProcessIdentityTests(unittest.TestCase):
    def test_real_process_identity_is_stable_across_exec(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            first = capture_process_identity(
                process.pid, expected_uid=os.geteuid(),
            )
            time.sleep(0.05)
            second = capture_process_identity(
                process.pid, expected_uid=os.geteuid(),
            )
            self.assertIsNotNone(first)
            self.assertEqual(first, second)
            self.assertEqual(first.pid, process.pid)
            self.assertEqual(first.pgid, process.pid)
            self.assertEqual(first.uid, os.geteuid())
        finally:
            process.terminate()
            process.wait(timeout=2)

    def test_expected_uid_mismatch_fails_closed(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertIsNone(capture_process_identity(
                process.pid, expected_uid=os.geteuid() + 100_000,
            ))
        finally:
            process.terminate()
            process.wait(timeout=2)

    def test_invalid_pid_and_unsupported_inputs_fail_closed(self):
        self.assertIsNone(capture_process_identity(0))
        self.assertIsNone(capture_process_identity(True))
        self.assertIsNone(capture_process_identity(-1))
        self.assertIsNone(capture_process_identity("1"))

    def test_identity_round_trip_and_validation(self):
        identity = ProcessIdentity(
            platform="linux", pid=123, pgid=123, uid=os.getuid(),
            start_ticks=456,
        )
        self.assertEqual(ProcessIdentity.from_dict(identity.to_dict()), identity)
        with self.assertRaises(ProcessIdentityError):
            ProcessIdentity.from_dict({"platform": "linux", "pid": 1})
        with self.assertRaises(ProcessIdentityError):
            ProcessIdentity(
                platform="linux", pid=10, pgid=10, uid=0, start_ticks=None,
            )
        with self.assertRaises(ProcessIdentityError):
            ProcessIdentity(
                platform="darwin", pid=10, pgid=10, uid=0,
                start_seconds=1, start_microseconds=1_000_000,
            )


if __name__ == "__main__":
    unittest.main()
