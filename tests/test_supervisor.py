import asyncio
from pathlib import Path
import tempfile
import unittest

from godotk.errors import GodotKError
from godotk.protocol import receive, send
from godotk.supervisor import Supervisor


class FakeEditor:
    def __init__(self, host):
        self.host = host
        self.calls = 0
        self.payload = bytearray()
        self.auto_reply = True

    def write(self, data):
        self.calls += 1
        self.payload.extend(data)

    async def drain(self):
        if self.auto_reply:
            request_id = self.host.inflight
            self.host.complete(request_id, {"id": request_id, "ok": True, "state": "completed", "result": {"applied": 1}})

    def close(self):
        pass


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = Supervisor(Path(self.temp.name), Path("/provided/godot"), "4.6.2", headless=True)
        self.editor = FakeEditor(self.host)
        self.host.editor = self.editor

    def request(self, identity="one", **values):
        return {"kind": "request", "id": identity, "op": "inspect", "target": "editor", "args": {}, **values}

    async def test_request_recovery_does_not_replay(self):
        request = self.request()
        first = await self.host.request(request)
        second = await self.host.request(request)
        self.assertEqual(first, second)
        self.assertEqual(self.editor.calls, 1)
        lookup = await self.host.request(self.request("lookup", op="outcome", args={"id": "one"}))
        self.assertEqual(lookup["result"], first)

    async def test_id_reuse_and_stale_run(self):
        await self.host.request(self.request())
        conflict = await self.host.request(self.request(op="play"))
        self.assertEqual(conflict["error"]["code"], "REQUEST_ID_REUSED")
        self.host.game = {"run": "new", "ready": True}
        stale = await self.host.request(self.request("two", target="game", run="old"))
        self.assertEqual(stale["error"]["code"], "STALE_TARGET")
        self.assertEqual(self.editor.calls, 1)

    async def test_busy_status_and_disconnect_uncertainty(self):
        self.editor.auto_reply = False
        pending = asyncio.create_task(self.host.request(self.request()))
        await asyncio.sleep(0)
        status = await self.host.request(self.request("s", op="status"))
        self.assertEqual(status["result"]["busy"], "one")
        busy = await self.host.request(self.request("two"))
        self.assertEqual(busy["error"]["code"], "BUSY")
        self.host.disconnected()
        result = await pending
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(self.host.inflight)

    async def test_close_requires_discard(self):
        response = await self.host.request(self.request(op="close"))
        self.assertEqual(response["error"]["code"], "DISCARD_REQUIRED")
        self.assertFalse(self.host.shutdown.is_set())

    async def test_debugger_paused_refuses_new_game_operations(self):
        self.host.game = {"run": "current", "ready": True, "debugger_paused": True}
        response = await self.host.request(self.request(target="game", op="exec"))
        self.assertEqual(response["error"]["code"], "DEBUGGER_PAUSED")
        self.assertEqual(self.editor.calls, 0)

    async def test_socket_disconnect_does_not_overwrite_confirmed_process_exit(self):
        self.host.editor = None
        server = await asyncio.start_server(self.host.handle, "127.0.0.1", 0)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            await send(writer, {"kind": "hello", "version": 1, "role": "editor", "token": self.host.editor_token})
            await receive(reader)
            self.host.phase = "editor_exited"
            writer.close()
            await writer.wait_closed()
            await asyncio.gather(*tuple(self.host.tasks))
            self.assertEqual(self.host.phase, "editor_exited")
        finally:
            server.close()
            await server.wait_closed()

    async def test_retention_bounds(self):
        for index in range(80):
            await self.host.request(self.request(str(index)))
        self.assertEqual(len(self.host.records), 64)
        self.assertNotIn("0", self.host.records)
        missing = await self.host.request(self.request("lookup", op="outcome", args={"id": "0"}))
        self.assertEqual(missing["error"]["code"], "OUTCOME_NOT_RETAINED")
        for index in range(700):
            self.host.log("test", "x" * 5000)
        self.assertEqual(len(self.host.logs), 512)
        self.assertEqual(len(self.host.logs[0]["text"]), 4096)

    async def test_logs_are_byte_bounded_and_pageable(self):
        for _ in range(600):
            self.host.log("test", "場" * 4096)
        after = 0
        count = 0
        while True:
            response = await self.host.request(self.request("logs", op="logs", args={"after": after}))
            result = response["result"]
            count += len(result["entries"])
            self.assertTrue(all(len(entry["text"].encode()) <= 4096 for entry in result["entries"]))
            if not result["more"]:
                break
            self.assertGreater(result["cursor"], after)
            after = result["cursor"]
        self.assertEqual(count, 512)

    async def test_loopback_auth_rejects_wrong_role_token(self):
        self.host.editor = None
        server = await asyncio.start_server(self.host.handle, "127.0.0.1", 0)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            await send(writer, {"kind": "hello", "version": 1, "role": "editor", "token": self.host.client_token})
            response = await receive(reader)
            self.assertEqual(response["error"]["code"], "AUTH_FAILED")
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0)
        finally:
            server.close()
            await server.wait_closed()
            if self.host.tasks:
                await asyncio.gather(*tuple(self.host.tasks), return_exceptions=True)
