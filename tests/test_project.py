import os
from pathlib import Path
import tempfile
import unittest

from godotk.engine import executable
from godotk.errors import GodotKError
from godotk.install import digest, packaged_files, verify_install
from godotk.project import ProjectLock, atomic_json, project_path, read_json, state_dir


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / "project.godot").write_text("config_version=5\n")

    def test_identity_and_invalid_project(self):
        self.assertEqual(project_path(self.project / "."), self.project.resolve())
        with self.assertRaises(GodotKError):
            project_path(self.project / "absent")

    def test_private_atomic_state(self):
        path = state_dir(self.project) / "example.json"
        atomic_json(path, {"x": 1})
        atomic_json(path, {"x": 2})
        self.assertEqual(read_json(path), {"x": 2})
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(list(path.parent.glob(".write-*")))

    def test_exclusive_lock_and_release(self):
        with ProjectLock(self.project):
            with self.assertRaises(GodotKError):
                with ProjectLock(self.project):
                    pass
        with ProjectLock(self.project):
            pass

    def test_explicit_engine_never_falls_back(self):
        with self.assertRaises(GodotKError) as caught:
            executable(str(self.project / "does-not-exist"))
        self.assertEqual(caught.exception.code, "ENGINE_NOT_FOUND")

    def test_packaged_native_files_and_collision_checks(self):
        content = packaged_files()
        self.assertTrue({"editor.gd", "runtime.gd", "wire.gd", "common.gd", "plugin.cfg"} <= content.keys())
        target = self.project / "addons/godotk"
        target.mkdir(parents=True)
        for name, data in content.items():
            (target / name).write_bytes(data)
        atomic_json(target / ".godotk-install.json", {"files": {name: digest(data) for name, data in content.items()}})
        verify_install(self.project)
        (target / "editor.gd").write_text("user edit")
        with self.assertRaises(GodotKError):
            verify_install(self.project)
