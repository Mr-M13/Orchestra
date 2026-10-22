"""The only test admitted by the initial PixelBox socket-free profile."""
from __future__ import annotations

import hashlib
import unittest


class SocketFreeCanaryTests(unittest.TestCase):
    def test_stdlib_integrity_sanity(self):
        self.assertEqual(hashlib.sha256(b"orchestra-canary").hexdigest(),
                         "b6760495043681399120652933d1fb8ba13228133737a0147662681262dff633")
