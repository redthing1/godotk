import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from godotk.cli import dispatch, parser
from godotk.errors import GodotKError


class CLITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / "project.godot").write_text("config_version=5\n")
        self.script = self.project / "operation.gd"
        self.script.write_text("@tool\nextends RefCounted\nfunc run(ctx):\n    return ctx.params\n")

    def args(self, *words):
        return parser().parse_args(["--project", str(self.project), *words])

    async def test_exec_reads_local_source_and_explicit_context(self):
        with patch("godotk.cli.call", new_callable=AsyncMock) as call:
            await dispatch(self.args("exec", "--target", "game", "--file", str(self.script), "--params", '{"amount":3}', "--ref", "run:42", "--request-id", "once"))
        self.assertEqual(call.call_args.args[1], "exec")
        payload = call.call_args.args[2]
        self.assertEqual(payload["source"], self.script.read_text())
        self.assertEqual(payload["params"], {"amount": 3})
        self.assertEqual(payload["ref"], "run:42")
        self.assertEqual(call.call_args.kwargs["request_id"], "once")

    async def test_exec_rejects_invalid_params_and_source_before_sending(self):
        with patch("godotk.cli.call", new_callable=AsyncMock) as call:
            for params in ('[]', '{"x":NaN}', '{"x":1e999}', '{'):
                with self.subTest(params=params), self.assertRaises(GodotKError):
                    await dispatch(self.args("exec", "--target", "editor", "--file", str(self.script), "--params", params))
            for source in (b"", b"x" * 65537, b"\xff"):
                self.script.write_bytes(source)
                with self.subTest(source_size=len(source)), self.assertRaises(GodotKError):
                    await dispatch(self.args("exec", "--target", "editor", "--file", str(self.script)))
            call.assert_not_called()

    async def test_property_reads_select_properties_and_preserve_selection(self):
        with patch("godotk.cli.call", new_callable=AsyncMock) as call:
            await dispatch(self.args("inspect", "--target", "game", "--path", "Player", "--read", "position", "--read", "speed"))
        payload = call.call_args.args[2]
        self.assertEqual(payload["section"], "properties")
        self.assertEqual(payload["path"], "Player")
        self.assertEqual(payload["read"], ["position", "speed"])
