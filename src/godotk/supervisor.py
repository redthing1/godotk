"""One foreground supervisor per project; native state stays inside Godot."""

import asyncio
import codecs
from collections import OrderedDict, deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import time

from .errors import GodotKError
from .artifacts import ArtifactStore
from .install import verify_install
from .project import ProjectLock, atomic_json, read_json, state_dir
from .protocol import VERSION, receive, send

NATIVE_OPS = {"inspect", "exec", "play", "stop", "capture", "input"}


class Supervisor:
    def __init__(self, project: Path, engine: Path, engine_version: str, *, headless: bool = False):
        self.project, self.engine, self.engine_version = project, engine, engine_version
        self.headless = headless
        self.directory = state_dir(project)
        self.session = secrets.token_hex(16)
        self.client_token, self.editor_token = secrets.token_hex(32), secrets.token_hex(32)
        self.phase = "starting"
        self.editor = None
        self.process = None
        self.game = None
        self.inflight = None
        self.records: OrderedDict[str, dict] = OrderedDict()
        self.fingerprints: dict[str, str] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.logs = deque(maxlen=512)
        self.log_sequence = 0
        self.shutdown = asyncio.Event()
        self.connections: set[asyncio.StreamWriter] = set()
        self.tasks: set[asyncio.Task] = set()
        self.artifacts = ArtifactStore(self.directory / "artifacts")

    def log(self, source: str, text: str) -> None:
        self.log_sequence += 1
        bounded = text.encode("utf-8")[:4096].decode("utf-8", errors="ignore")
        self.logs.append({"seq": self.log_sequence, "source": source, "text": bounded, "time": time.time()})

    def status(self) -> dict:
        return {"session": self.session, "project": str(self.project), "phase": self.phase,
                "engine": {"path": str(self.engine), "version": self.engine_version},
                "editor_pid": self.process.pid if self.process else None,
                "headless": self.headless, "game": self.game, "busy": self.inflight,
                "dirty": {"known": False}, "protocol": VERSION}

    @staticmethod
    def failure(request_id: str, error: GodotKError, state: str = "failed") -> dict:
        return {"id": request_id, "ok": False, "state": state, "error": error.as_dict()}

    def complete(self, request_id: str, reply: dict) -> None:
        if request_id not in self.pending:
            return
        record = self.records[request_id]
        # A late result may resolve a previous unknown outcome, never a new request.
        record.clear()
        record.update(reply)
        future = self.pending.pop(request_id)
        if not future.done():
            future.set_result(dict(record))
        if self.inflight == request_id:
            self.inflight = None

    def disconnected(self, *, game_only: bool = False) -> None:
        for request_id in list(self.pending):
            if game_only and self.fingerprints.get("target:" + request_id) != "game":
                continue
            self.complete(request_id, self.failure(request_id, GodotKError(
                "OUTCOME_UNKNOWN", "Native target disconnected; operation may have taken effect"), "unknown"))

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.tasks.add(task)
        if len(self.connections) >= 32:
            writer.close()
            self.tasks.discard(task)
            return
        self.connections.add(writer)
        role = None
        try:
            hello = await asyncio.wait_for(receive(reader), 5)
            role = hello.get("role")
            token = hello.get("token")
            expected = self.client_token if role == "client" else self.editor_token if role == "editor" else None
            if hello.get("kind") != "hello" or hello.get("version") != VERSION or not isinstance(token, str) or expected is None or not hmac.compare_digest(token, expected):
                raise GodotKError("AUTH_FAILED", "Invalid role, version, or credentials")
            if role == "editor":
                if self.editor is not None:
                    raise GodotKError("EDITOR_CONNECTED", "An editor is already connected")
                self.editor = writer
                self.phase = "ready"
                await send(writer, {"kind": "welcome", "session": self.session, "version": VERSION})
                await self.editor_messages(reader)
            else:
                await send(writer, {"kind": "welcome", "status": self.status(), "version": VERSION})
                request = await asyncio.wait_for(receive(reader), 10)
                response = await self.request(request)
                await asyncio.wait_for(send(writer, response), 5)
        except GodotKError as exc:
            try:
                await send(writer, self.failure("", exc))
            except (OSError, ConnectionError):
                pass
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, OSError):
            pass
        finally:
            if writer is self.editor:
                self.editor = None
                self.game = None
                if self.phase not in ("closing", "editor_exited"):
                    self.phase = "editor_disconnected"
                self.disconnected()
            self.connections.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.tasks.discard(task)

    async def editor_messages(self, reader: asyncio.StreamReader) -> None:
        while True:
            message = await receive(reader)
            kind = message.get("kind")
            if kind == "result":
                request_id = message.get("id")
                if request_id in self.pending:
                    ok = message.get("ok") is True
                    reply = {"id": request_id, "ok": ok, "state": "completed" if ok else "failed"}
                    reply["result" if ok else "error"] = message.get("result" if ok else "error", {})
                    if ok and self.fingerprints.get("op:" + request_id) == "capture":
                        try:
                            reply["result"] = self.artifacts.publish(reply["result"])
                        except GodotKError as exc:
                            reply = self.failure(request_id, exc)
                    self.complete(request_id, reply)
            elif kind == "game":
                game = message.get("game")
                if game is None:
                    self.disconnected(game_only=True)
                elif not isinstance(game, dict) or not isinstance(game.get("run"), str):
                    continue
                self.game = game
            elif kind == "log":
                self.log("native", str(message.get("text", "")))

    async def request(self, request: dict) -> dict:
        request_id = request.get("id", "")
        try:
            if request.get("kind") != "request" or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                raise GodotKError("INVALID_REQUEST", "A bounded request ID is required")
            op, args = request.get("op"), request.get("args", {})
            if not isinstance(args, dict) or not isinstance(op, str):
                raise GodotKError("INVALID_REQUEST", "Expected operation and argument object")
            if op == "status":
                result = self.status()
            elif op == "logs":
                after = args.get("after", 0)
                if not isinstance(after, int) or isinstance(after, bool) or after < 0:
                    raise GodotKError("INVALID_ARGUMENT", "after must be a nonnegative integer")
                entries = []
                used = 0
                for entry in self.logs:
                    if entry["seq"] <= after:
                        continue
                    used += len(json.dumps(entry, ensure_ascii=False).encode())
                    if used > 196608:
                        break
                    entries.append(entry)
                cursor = entries[-1]["seq"] if entries else after
                result = {"entries": entries, "cursor": cursor, "latest": self.log_sequence,
                          "more": cursor < self.log_sequence, "dropped": bool(self.logs and after < self.logs[0]["seq"] - 1)}
            elif op == "outcome":
                original_id = args.get("id")
                if not isinstance(original_id, str) or original_id not in self.records:
                    raise GodotKError("OUTCOME_NOT_RETAINED", "No retained outcome; do not assume it was never executed")
                result = dict(self.records[original_id])
            elif op == "close":
                if args.get("discard") is not True:
                    raise GodotKError("DISCARD_REQUIRED", "Dirty state is not fully known; use --discard to acknowledge unsaved changes")
                self.phase = "closing"
                asyncio.get_running_loop().call_later(0.1, self.shutdown.set)
                result = {"closing": True, "discard": True}
            elif op in NATIVE_OPS:
                return await self.forward(request)
            else:
                raise GodotKError("UNKNOWN_OPERATION", "Unsupported operation", op=op)
            return {"id": request_id, "ok": True, "state": "completed", "result": result}
        except GodotKError as exc:
            return self.failure(request_id, exc)

    async def forward(self, request: dict) -> dict:
        request_id = request["id"]
        content = {key: value for key, value in request.items() if key != "id"}
        fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()
        if request_id in self.records:
            if self.fingerprints[request_id] != fingerprint:
                raise GodotKError("REQUEST_ID_REUSED", "Request ID was previously used for different content")
            return dict(self.records[request_id])
        if self.inflight:
            raise GodotKError("BUSY", "A native operation is still in flight", request_id=self.inflight)
        if self.editor is None:
            raise GodotKError("NOT_READY", "Editor bridge is not connected")
        target = request.get("target", "editor")
        if target not in ("editor", "game"):
            raise GodotKError("INVALID_TARGET", "Choose editor or game")
        if request["op"] in ("play", "stop") and target != "editor":
            raise GodotKError("INVALID_TARGET", "Lifecycle operations target the editor")
        if target == "game":
            if not self.game:
                raise GodotKError("NOT_RUNNING", "No connected game")
            if request.get("run") is not None and request["run"] != self.game["run"]:
                raise GodotKError("STALE_TARGET", "Game run changed; inspect again")
            if not self.game.get("ready"):
                raise GodotKError("NOT_READY", "Game scene is not ready")
            if self.game.get("debugger_paused"):
                raise GodotKError("DEBUGGER_PAUSED", "Game is in the native debugger; resume there before issuing game operations")
            request = dict(request, run=self.game["run"])
        while len(self.records) >= 64:
            old, _ = self.records.popitem(last=False)
            self.fingerprints.pop(old, None)
            self.fingerprints.pop("target:" + old, None)
            self.fingerprints.pop("op:" + old, None)
        self.records[request_id] = {"id": request_id, "ok": False, "state": "running"}
        self.fingerprints[request_id] = fingerprint
        self.fingerprints["target:" + request_id] = target
        self.fingerprints["op:" + request_id] = request["op"]
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.inflight = request_id
        try:
            await asyncio.wait_for(send(self.editor, dict(request, target=target)), 5)
        except (OSError, ConnectionError, TimeoutError):
            if self.editor:
                self.editor.close()
            self.disconnected()
        try:
            return await asyncio.wait_for(asyncio.shield(future), 20)
        except TimeoutError:
            reply = self.failure(request_id, GodotKError("OUTCOME_UNKNOWN", "Native operation exceeded the reply deadline; query outcome, do not retry"), "unknown")
            self.records[request_id].update(reply)
            return reply

    async def pump_output(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while data := await self.process.stdout.read(4096):
            self.log("process", decoder.decode(data))
        tail = decoder.decode(b"", final=True)
        if tail:
            self.log("process", tail)
        code = await self.process.wait()
        self.log("process", f"Editor exited with code {code}")
        if self.phase != "closing":
            self.phase = "editor_exited"

    async def stop_owned_process(self) -> None:
        if not self.process or self.process.returncode is not None:
            return
        if self.editor:
            try:
                await send(self.editor, {"kind": "shutdown"})
                await asyncio.wait_for(self.process.wait(), 5)
                return
            except (TimeoutError, OSError, ConnectionError):
                pass
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(self.process.pid), "/T", "/F",
                                                              stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(killer.wait(), 5)
            else:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except TimeoutError:
                    os.killpg(self.process.pid, signal.SIGKILL)
            await asyncio.wait_for(self.process.wait(), 5)
        except ProcessLookupError:
            pass

    async def run(self) -> None:
        verify_install(self.project)
        with ProjectLock(self.project):
            server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            # Godot's debugger server needs a concrete port in the advertised URI.
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                debug_port = reservation.getsockname()[1]
            environment = dict(os.environ, GODOTK_PORT=str(port), GODOTK_TOKEN=self.editor_token,
                               GODOTK_SESSION=self.session, GODOTK_ARTIFACTS=str(self.directory / "artifacts"))
            command = [str(self.engine), "--editor", "--path", str(self.project),
                       "--debug-server", f"tcp://127.0.0.1:{debug_port}"]
            if self.headless:
                command.append("--headless")
            kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000200}
            output_task = None
            endpoint = self.directory / "endpoint.json"
            try:
                self.process = await asyncio.create_subprocess_exec(*command, env=environment, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **kwargs)
                output_task = asyncio.create_task(self.pump_output())
                atomic_json(endpoint, {"version": VERSION, "port": port, "token": self.client_token,
                                       "session": self.session, "pid": os.getpid()})
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    try:
                        loop.add_signal_handler(sig, self.shutdown.set)
                    except (NotImplementedError, RuntimeError):
                        pass
                await self.shutdown.wait()
            finally:
                self.phase = "closing"
                await self.stop_owned_process()
                server.close()
                await server.wait_closed()
                for writer in tuple(self.connections):
                    writer.close()
                for task in tuple(self.tasks):
                    task.cancel()
                if self.tasks:
                    await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
                if output_task:
                    await asyncio.gather(output_task, return_exceptions=True)
                try:
                    if read_json(endpoint).get("session") == self.session:
                        endpoint.unlink(missing_ok=True)
                except GodotKError:
                    pass


async def serve(project: Path, engine: Path, engine_version: str, *, headless: bool = False) -> None:
    await Supervisor(project, engine, engine_version, headless=headless).run()
