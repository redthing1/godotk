import base64
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from godotk.artifacts import ArtifactStore
from godotk.errors import GodotKError
from godotk.project import read_json

# A tiny synthetic PNG test asset, not a captured image.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9ZkAAAAASUVORK5CYII=")


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ArtifactStore(self.root)

    def image(self, number):
        identity = f"{number:032x}"
        path = self.root / (identity + ".png")
        path.write_bytes(PNG)
        return {"capture": identity, "path": str(path), "width": 1, "height": 1}

    def test_publish_and_retain(self):
        result = self.store.publish(self.image(1))
        self.assertEqual(result["sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertEqual(result["bytes"], len(PNG))
        for index in range(2, 35):
            result = self.store.publish(self.image(index))
        self.assertEqual(len(read_json(self.root / "index.json")["entries"]), 32)
        self.assertFalse((self.root / (f"{1:032x}.png")).exists())
        self.assertEqual(result["retention"]["evicted"], [f"{2:032x}"])

    def test_modified_artifacts_are_preserved(self):
        first = self.image(1)
        self.store.publish(first)
        Path(first["path"]).write_bytes(b"user changed this")
        with patch("godotk.artifacts.MAX_COUNT", 1):
            result = self.store.publish(self.image(2))
        self.assertTrue(Path(first["path"]).exists())
        self.assertEqual(result["retention"]["preserved_modified"], [f"{1:032x}"])

    def test_paths_dimensions_and_limits(self):
        result = self.image(1)
        with self.assertRaises(GodotKError):
            self.store.publish(dict(result, capture="../outside"))
        with self.assertRaises(GodotKError):
            self.store.publish(dict(result, width=2))
        with patch("godotk.artifacts.MAX_FILE", 1), self.assertRaises(GodotKError):
            self.store.publish(result)
        self.assertFalse(Path(result["path"]).exists())
