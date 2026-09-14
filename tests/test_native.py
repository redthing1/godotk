"""Opt-in headless tests. Supply an isolated, operator-provided Godot executable."""

import asyncio
import os
import json
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import unittest

from godotk.client import call, close_session
from godotk.engine import executable, version
from godotk.install import setup
from godotk.supervisor import Supervisor

ENGINE = os.environ.get("GODOTK_TEST_GODOT")


@unittest.skipUnless(ENGINE, "Set GODOTK_TEST_GODOT to opt into headless engine tests")
class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="godotk-native-test-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project with spaces"
        shutil.copytree(Path(__file__).parent / "fixtures/project", self.project)
        self.engine = executable(ENGINE)
        self.original = (self.project / "project.godot").read_bytes()
        installed = await asyncio.to_thread(setup, self.project, self.engine)
        self.assertTrue(installed["installed"])
        engine_version = await asyncio.to_thread(version, self.engine)
        self.host = Supervisor(self.project, self.engine, engine_version, headless=True)
        self.task = asyncio.create_task(self.host.run())
        for _ in range(300):
            if self.host.phase == "ready":
                return
            if self.task.done():
                await self.task
            await asyncio.sleep(0.05)
        self.host.shutdown.set()
        await self.task
        self.fail("Editor did not connect: " + "\n".join(entry["text"] for entry in self.host.logs))

    async def asyncTearDown(self):
        self.host.shutdown.set()
        await asyncio.wait_for(self.task, 20)

    async def command(self, op, args=None, **kwargs):
        result = await call(self.project, op, args, **kwargs)
        self.assertTrue(result.get("ok"), result)
        return result["result"]

    async def test_complete_nonvisual_game_cycle(self):
        state = await self.command("status")
        self.assertEqual(state["phase"], "ready")
        first = await self.command("play", request_id="first-play")
        run = first["game"]["run"]
        tree = await self.command("inspect", target="game", run=run)
        self.assertEqual(tree["tree"]["name"], "Fixture")
        ref = tree["tree"]["ref"]
        delivered = await self.command("input", {"action": "ui_accept", "frames": 3}, target="game", run=run, request_id="input-once")
        self.assertTrue(delivered["released"])
        again = await self.command("input", {"action": "ui_accept", "frames": 3}, target="game", run=run, request_id="input-once")
        self.assertEqual(delivered, again)
        tree = await self.command("inspect", target="game")
        text = tree["tree"]["children"][0]["text"]
        self.assertIn("presses=1 releases=1", text)
        self.assertIn("held=false", text)
        await self.command("input", {"key": "Space", "frames": 3}, target="game")
        tree = await self.command("inspect", target="game")
        self.assertIn("presses=2 releases=2", tree["tree"]["children"][0]["text"])
        capture = await call(self.project, "capture", target="game")
        self.assertEqual(capture["error"]["code"], "RENDERING_UNAVAILABLE")
        unknown_action = await call(self.project, "input", {"action": "does_not_exist"}, target="game")
        self.assertEqual(unknown_action["error"]["code"], "UNKNOWN_ACTION")
        self.assertFalse((self.host.directory / "artifacts").exists())
        await self.command("stop")
        second = await self.command("play")
        self.assertNotEqual(second["game"]["run"], run)
        stale = await call(self.project, "inspect", {"ref": ref}, target="game")
        self.assertEqual(stale["error"]["code"], "STALE_TARGET")
        logs = await self.command("logs")
        messages = "".join(entry["text"] for entry in logs["entries"])
        self.assertIn("FIXTURE_CUSTOM_LOOP_READY", messages)
        self.assertIn("FIXTURE_AUTOLOAD_READY", messages)
        self.assertNotIn("SCRIPT ERROR", messages)
        refused = await call(self.project, "close")
        self.assertEqual(refused["error"]["code"], "DISCARD_REQUIRED")
        await self.command("stop")

    async def test_install_recovery_and_removal(self):
        self.host.shutdown.set()
        await self.task
        result = await asyncio.to_thread(setup, self.project, self.engine, remove=True)
        self.assertFalse(result["installed"])
        self.assertFalse((self.project / "addons/godotk").exists())
        self.assertTrue((Path(result["recovery"]) / "addon/runtime.gd").exists())
        settings = (self.project / "project.godot").read_text()
        self.assertNotIn("addons/godotk", settings)
        self.assertIn("FixtureAutoload", settings)

    async def test_object_discovery_and_resource_navigation(self):
        await self.command("play")
        info = await self.command("inspect", {"section": "properties", "filter": "speed", "read": ["speed", "dynamic_value", "position", "sample"]}, target="game")
        prop = info["members"][0]
        self.assertEqual(prop["name"], "speed")
        self.assertEqual(prop["type_name"], "float")
        self.assertTrue(prop["editor_visible"])
        self.assertTrue(prop["stored"])
        self.assertIn("1000", prop["hint_string"])
        self.assertEqual(info["values"]["speed"], 300.0)
        self.assertEqual(info["values"]["dynamic_value"], 42)
        self.assertEqual(info["values"]["position"]["type"], "Vector2")
        resource_ref = info["values"]["sample"]["ref"]
        resource = await self.command("inspect", {"section": "properties", "ref": resource_ref, "read": ["resource_name"]}, target="game")
        self.assertEqual(resource["object"]["class"], "Gradient")
        self.assertEqual(resource["values"]["resource_name"], "Fixture gradient")
        methods = await self.command("inspect", {"section": "methods", "filter": "increment"}, target="game")
        self.assertEqual(methods["members"][0]["args"][0]["name"], "amount")
        self.assertEqual(methods["members"][0]["default_args"]["items"], [1])
        signals = await self.command("inspect", {"section": "signals", "filter": "custom_event"}, target="game")
        self.assertEqual(signals["members"][0]["args"][0]["name"], "amount")
        page = await self.command("inspect", {"section": "methods", "limit": 2}, target="game")
        self.assertEqual(page["next_offset"], 2)
        next_page = await self.command("inspect", {"section": "methods", "limit": 2, "offset": 2}, target="game")
        self.assertNotEqual(page["members"], next_page["members"])
        missing = await call(self.project, "inspect", {"section": "properties", "read": ["missing"]}, target="game")
        self.assertEqual(missing["error"]["code"], "UNKNOWN_PROPERTY")
        broken = await call(self.project, "inspect", {"section": "properties", "read": ["broken_getter"]}, target="game")
        self.assertEqual(broken["error"]["code"], "INSPECTION_ERROR", broken)
        self.assertTrue(broken["error"]["details"]["diagnostics"])
        await self.command("stop")
        await self.command("play")
        stale = await call(self.project, "inspect", {"section": "properties", "ref": resource_ref}, target="game")
        self.assertEqual(stale["error"]["code"], "STALE_TARGET")

    async def script(self, body, *, target="game", **kwargs):
        source = "@tool\nextends RefCounted\n\nfunc run(ctx: Dictionary) -> Variant:\n" + "\n".join("    " + line for line in body.splitlines()) + "\n"
        return await call(self.project, "exec", {"source": source}, target=target, **kwargs)

    async def test_execution_async_values_and_once_only(self):
        editor = await self.script("return ctx.target", target="editor")
        self.assertTrue(editor["ok"], editor)
        self.assertEqual(editor["result"]["value"], "editor")
        await self.command("play")
        body = "ctx.object.increment(2)\nawait ctx.tree.process_frame\nreturn ctx.object.presses"
        first = await self.script(body, request_id="script-once")
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["result"]["value"], 2)
        self.assertEqual(first, await self.script(body, request_id="script-once"))
        changed = await self.command("inspect", {"section": "properties", "read": ["presses"]}, target="game")
        self.assertEqual(changed["values"]["presses"], 2)
        values = await self.script('return [Vector2(3, 4), Color(1, 0, 0), 9223372036854775807, INF, {Vector2(1, 2): "key"}, ctx.object.sample]')
        self.assertTrue(values["ok"], values)
        items = values["result"]["value"]["items"]
        self.assertEqual(items[0], {"type": "Vector2", "value": [3, 4]})
        self.assertEqual(items[2], {"type": "int", "value": "9223372036854775807"})
        self.assertEqual(items[3]["type"], "float")
        self.assertEqual(items[4]["entries"][0][0]["type"], "Vector2")
        self.assertEqual(items[5]["class"], "Gradient")
        bounded = await self.script('return "x".repeat(20000)')
        self.assertTrue(bounded["result"]["truncated"])
        # A freed Node reference must not resolve, and refs must not keep it alive.
        made = await self.script('var node := Node.new()\nctx.root.add_child(node)\nreturn node')
        ref = made["result"]["value"]["ref"]
        freed = await call(self.project, "exec", {"ref": ref, "source": '@tool\nextends RefCounted\nfunc run(ctx):\n    ctx.object.free()\n'}, target="game")
        self.assertTrue(freed["ok"], freed)
        stale = await call(self.project, "inspect", {"section": "properties", "ref": ref}, target="game")
        self.assertEqual(stale["error"]["code"], "STALE_TARGET")

    async def test_editor_execution_and_discovery_do_not_implicitly_save(self):
        original_scene = (self.project / "main.tscn").read_bytes()
        opened = await self.script('EditorInterface.open_scene_from_path("res://main.tscn")\nawait ctx.tree.process_frame\nreturn EditorInterface.get_edited_scene_root()', target="editor")
        self.assertTrue(opened["ok"], opened)
        ref = opened["result"]["value"]["ref"]
        source = (self.project / "operation.gd").read_text()
        changed = await self.command("exec", {"source": source, "ref": ref}, target="editor")
        self.assertEqual(changed["value"], {"type": "Vector2", "value": [12, 34]})
        inspected = await self.command("inspect", {"section": "properties", "ref": ref, "read": ["position"]}, target="editor")
        self.assertEqual(inspected["values"]["position"], changed["value"])
        self.assertEqual((self.project / "main.tscn").read_bytes(), original_scene)

    async def test_execution_failures_preserve_diagnostics_and_partial_effects(self):
        await self.command("play")
        parse = await call(self.project, "exec", {"source": "@tool\nextends RefCounted\nfunc run(\n"}, target="game")
        self.assertEqual(parse["error"]["code"], "SCRIPT_PARSE_ERROR")
        self.assertTrue(parse["error"]["details"]["diagnostics"])
        bad = await self.script("ctx.object.fail_after_change()\nreturn 123")
        self.assertFalse(bad["ok"], bad)
        self.assertEqual(bad["error"]["code"], "SCRIPT_ERROR")
        details = bad["error"]["details"]
        self.assertEqual(details["effects"], "may_have_changed")
        self.assertTrue(any(item["attribution"] == "script_stack" for item in details["diagnostics"]))
        changed = await self.command("inspect", {"section": "properties", "read": ["presses"]}, target="game")
        self.assertEqual(changed["values"]["presses"], 10)
        recovered = await self.script("return null")
        self.assertTrue(recovered["ok"], recovered)
        self.assertIsNone(recovered["result"]["value"])
        contract = await call(self.project, "exec", {"source": "extends RefCounted\nfunc run(ctx):\n    return 1\n"}, target="game")
        self.assertEqual(contract["error"]["code"], "SCRIPT_CONTRACT")
        wrong_type = await call(self.project, "exec", {"source": "@tool\nextends RefCounted\nfunc run(ctx: String):\n    return ctx\n"}, target="game")
        self.assertEqual(wrong_type.get("error", {}).get("code"), "SCRIPT_CONTRACT", wrong_type)
        initializer = await call(self.project, "exec", {"source": "@tool\nextends RefCounted\nfunc _init(required):\n    pass\nfunc run(ctx):\n    return 1\n"}, target="game")
        self.assertEqual(initializer.get("error", {}).get("code"), "SCRIPT_CONTRACT", initializer)
        direct = await self.script("await ctx.tree.process_frame\nvar missing: Variant = null\nreturn missing.no_such_method()")
        self.assertEqual(direct.get("error", {}).get("code"), "SCRIPT_ERROR", direct)
        concurrent = await self.script("ctx.object.emit_background_error.call_deferred()\nawait ctx.tree.process_frame\nreturn 7")
        self.assertTrue(concurrent["ok"], concurrent)
        self.assertTrue(any(item["attribution"] == "concurrent_or_unattributed" for item in concurrent["result"]["diagnostics"]), concurrent)
        flooded = await self.script('for index in range(40):\n    push_error("BOUNDED_ERROR")\nreturn 1')
        self.assertEqual(flooded.get("error", {}).get("code"), "SCRIPT_ERROR", flooded)
        self.assertEqual(len(flooded["error"]["details"]["diagnostics"]), 32)
        self.assertGreaterEqual(flooded["error"]["details"]["diagnostics_dropped"], 8)

    async def test_opt_in_debugger_pause_is_observable_without_auto_resume(self):
        await self.command("play", {"debug_breaks": True})
        pending = asyncio.create_task(self.script("breakpoint\nreturn 1"))
        try:
            for _ in range(200):
                if self.host.game.get("debugger_paused"):
                    break
                await asyncio.sleep(0.025)
            status = await self.command("status")
            self.assertTrue(status["game"]["debugger_paused"], status)
            self.assertIsNotNone(status["busy"])
        finally:
            self.host.shutdown.set()
            await asyncio.wait_for(self.task, 20)
            response = await pending
        self.assertEqual(response["state"], "unknown")

    @unittest.skipIf(os.name == "nt", "POSIX process-loss test; Windows needs native validation")
    async def test_game_loss_resolves_pending_and_allows_relaunch(self):
        first = await self.command("play")
        game_pid = first["game"]["pid"]
        pending = asyncio.create_task(call(self.project, "input", {"action": "ui_accept", "frames": 600}, target="game"))
        for _ in range(100):
            if self.host.inflight:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(self.host.inflight)
        os.kill(game_pid, signal.SIGKILL)  # Fresh PID from this test's owned fixture game.
        response = await asyncio.wait_for(pending, 8)
        self.assertEqual(response["state"], "unknown")
        self.assertIsNone(self.host.game)
        next_game = await self.command("play")
        self.assertNotEqual(next_game["game"]["run"], first["game"]["run"])
        await self.command("stop")

    @unittest.skipIf(os.name == "nt", "POSIX process-loss test; Windows needs native validation")
    async def test_editor_loss_leaves_status_and_logs_available(self):
        self.host.process.kill()
        await self.host.process.wait()
        for _ in range(100):
            if self.host.phase == "editor_exited":
                break
            await asyncio.sleep(0.01)
        result = await self.command("status")
        self.assertEqual(result["phase"], "editor_exited")
        response = await call(self.project, "inspect")
        self.assertEqual(response["error"]["code"], "NOT_READY")

    async def test_cli_background_open_and_confirmed_close(self):
        self.host.shutdown.set()
        await self.task

        async def cli(*arguments):
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "godotk", "--project", str(self.project),
                "--godot", str(self.engine), *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, errors = await asyncio.wait_for(process.communicate(), 40)
            self.assertEqual(process.returncode, 0, (output, errors))
            return json.loads(output)

        try:
            opened, concurrent = await asyncio.gather(cli("open", "--headless"), cli("open", "--headless"))
            self.assertEqual(opened["result"]["phase"], "ready")
            self.assertEqual(concurrent["result"]["session"], opened["result"]["session"])
            again = await cli("open", "--headless")
            self.assertEqual(again["result"]["session"], opened["result"]["session"])
            await cli("play")
            inspected = await cli("inspect", "--target", "game", "--read", "speed")
            self.assertEqual(inspected["result"]["values"]["speed"], 300)
            executed = await cli("exec", "--target", "game", "--file", str(self.project / "operation.gd"))
            self.assertEqual(executed["result"]["value"], {"type": "Vector2", "value": [12, 34]})
            await cli("stop")
            closed = await cli("close", "--discard")
            self.assertTrue(closed["result"]["closed"])
            self.assertFalse((self.host.directory / "endpoint.json").exists())
        finally:
            if (self.host.directory / "endpoint.json").exists():
                await close_session(self.project, discard=True)
