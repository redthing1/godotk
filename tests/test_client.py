from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from godotk.client import open_session
from godotk.errors import GodotKError


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_starting_session_waits_without_spawning(self):
        with tempfile.TemporaryDirectory() as directory:
            project, engine = Path(directory), Path("/provided/godot")
            def response(phase):
                return {"ok": True, "result": {"phase": phase, "engine": {"path": str(engine)}, "headless": True}}
            with patch("godotk.client.call", AsyncMock(side_effect=[response("starting"), response("ready")])), patch("godotk.client.subprocess.Popen") as spawn:
                result = await open_session(project, engine, headless=True)
                self.assertEqual(result["result"]["phase"], "ready")
                spawn.assert_not_called()

    async def test_existing_session_configuration_is_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            response = {"ok": True, "result": {"phase": "ready", "engine": {"path": "/other/godot"}, "headless": True}}
            with patch("godotk.client.call", AsyncMock(return_value=response)), self.assertRaises(GodotKError) as caught:
                await open_session(Path(directory), Path("/provided/godot"), headless=True)
            self.assertEqual(caught.exception.code, "SESSION_CONFIGURATION")
