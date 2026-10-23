"""Tests for safe routine writable-path validation and materialisation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omcp.routine_paths import (
    UnsafeWritablePath, materialize_writable_paths, normalize_writable_path,
)


class RoutinePathValidationTests(unittest.TestCase):
    def test_accepts_safe_relative_paths(self):
        for value, expected in (
            ("tests", "tests"),
            ("platform/tests", "platform/tests"),
            ("theme/assets", "theme/assets"),
        ):
            self.assertEqual(normalize_writable_path(value), expected)

    def test_rejects_unsafe_paths(self):
        for value in (
            "", " ", "/tmp/work", "../outside", "a/../b", ".", ".git",
            ".codex", "platform/.git", "a b", "a;b", "a\\b",
        ):
            with self.subTest(value=value), self.assertRaises(UnsafeWritablePath):
                normalize_writable_path(value)


class RoutinePathMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_authorised_directory_is_accepted_unchanged(self):
        existing = self.workspace / "platform"
        existing.mkdir()
        (existing / "keep.txt").write_text("keep\n")
        paths = materialize_writable_paths(self.workspace, ["platform"])
        self.assertEqual(paths, (existing.resolve(),))
        self.assertEqual((existing / "keep.txt").read_text(), "keep\n")

    def test_creates_missing_authorised_directory_and_child_file(self):
        paths = materialize_writable_paths(self.workspace, ["tests", "new/area"])
        self.assertEqual(len(paths), 2)
        tests = self.workspace / "tests"
        self.assertTrue(tests.is_dir())
        (tests / "canary.txt").write_text("allowed\n")
        self.assertEqual((tests / "canary.txt").read_text(), "allowed\n")
        self.assertTrue((self.workspace / "new/area").is_dir())

    def test_rejects_path_traversal_and_absolute_paths(self):
        for value in ("../outside", "a/../../outside", str(self.outside)):
            with self.subTest(value=value), self.assertRaises(UnsafeWritablePath):
                materialize_writable_paths(self.workspace, [value])

    def test_rejects_symlink_escape(self):
        link = self.workspace / "link"
        link.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaisesRegex(UnsafeWritablePath, "symlink"):
            materialize_writable_paths(self.workspace, ["link"])
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_rejects_git_metadata_paths(self):
        (self.workspace / ".git").mkdir()
        for value in (".git", ".git/worktrees"):
            with self.subTest(value=value), self.assertRaises(UnsafeWritablePath):
                materialize_writable_paths(self.workspace, [value])

    def test_rejects_existing_non_directory(self):
        (self.workspace / "platform").write_text("not a directory\n")
        with self.assertRaisesRegex(UnsafeWritablePath, "directory"):
            materialize_writable_paths(self.workspace, ["platform"])

    def test_does_not_create_unauthorised_sibling(self):
        materialize_writable_paths(self.workspace, ["tests"])
        self.assertTrue((self.workspace / "tests").is_dir())
        self.assertFalse((self.workspace / "deployment").exists())

    def test_rejects_symlinked_workspace(self):
        linked_workspace = self.base / "linked-workspace"
        linked_workspace.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaisesRegex(UnsafeWritablePath, "real directory"):
            materialize_writable_paths(linked_workspace, ["tests"])


if __name__ == "__main__":
    unittest.main()
