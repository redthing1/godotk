"""Short-lived clients. A missing reply is never permission to replay an action."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .errors import GodotKError
from .project import read_json, state_dir
from .protocol import VERSION, receive, send


async def call(project: Path, op: str, args: dict | None = None, *, target: str = "editor",
               run: str | None = None, request_id: str | None = None, timeout: float = 30) -> dict:
    request_id = request_id or uuid.uuid4().hex
    endpoint = read_json(state_dir(project) / "endpoint.json")
    port = endpoint.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise GodotKError("NO_SESSION", "Invalid endpoint port")
    writer = None
    submitted = False
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await send(writer, {"kind": "hello", "version": VERSION, "role": "client", "token": endpoint.get("token")})
            welcome = await receive(reader)
            if welcome.get("kind") != "welcome" or welcome.get("status", {}).get("session") != endpoint.get("session"):
                raise GodotKError("SESSION_MISMATCH", "Session authentication/identity did not match")
            request = {"kind": "request", "id": request_id, "op": op, "args": args or {}, "target": target}
            if run is not None:
                request["run"] = run
            submitted = True
            await send(writer, request)
            return await receive(reader)
    except (TimeoutError, OSError, asyncio.IncompleteReadError) as exc:
        raise GodotKError("OUTCOME_UNKNOWN" if submitted else "NO_SESSION",
                          "No reply; query the retained outcome before doing anything again" if submitted else "Supervisor is not reachable",
                          request_id=request_id) from exc
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def open_session(project: Path, engine: Path, *, headless: bool) -> dict:
    existing = None
    process = None

    def checked_phase(response: dict) -> str:
        status = response.get("result", {})
        if status.get("engine", {}).get("path") != str(engine) or status.get("headless") != headless:
            raise GodotKError("SESSION_CONFIGURATION", "Existing session has different engine/display settings; close it explicitly first")
        phase = status.get("phase", "")
        if phase not in ("starting", "ready"):
            raise GodotKError("SESSION_NOT_READY", "Session needs attention; inspect status/logs and close explicitly", phase=phase)
        return phase

    try:
        existing = await call(project, "status", timeout=1)
    except GodotKError as exc:
        if exc.code not in ("NO_SESSION", "SESSION_MISMATCH"):
            raise
    else:
        if checked_phase(existing) == "ready":
            return existing
    directory = state_dir(project)
    log_path = directory / "supervisor.log"
    if existing is None:
        with log_path.open("wb") as log:
            command = [sys.executable, "-m", "godotk", "--project", str(project), "--godot", str(engine), "serve"]
            if headless:
                command.append("--headless")
            kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000008 | 0x00000200}
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = None
        try:
            response = await call(project, "status", timeout=1)
            phase = checked_phase(response)
            if phase == "ready":
                return response
        except GodotKError as exc:
            if exc.code != "NO_SESSION":
                raise
        if process and process.poll() is not None and response is None:
            raise GodotKError("STARTUP_FAILED", "Supervisor exited during startup", log=str(log_path))
        await asyncio.sleep(0.1)
    raise GodotKError("STARTUP_TIMEOUT", "Session may still be starting; inspect status/logs before closing or retrying", log=str(log_path))


async def close_session(project: Path, *, discard: bool, request_id: str | None = None) -> dict:
    path = state_dir(project) / "endpoint.json"
    session = read_json(path).get("session")
    result = await call(project, "close", {"discard": discard}, request_id=request_id)
    if not result.get("ok"):
        return result
    deadline = time.monotonic() + 16
    while path.exists():
        try:
            if read_json(path).get("session") != session:
                break
        except GodotKError:
            break
        if time.monotonic() >= deadline:
            raise GodotKError("CLOSE_NOT_CONFIRMED", "Shutdown is not yet confirmed; inspect status")
        await asyncio.sleep(0.1)
    result["result"] = {"closed": True, "discard": discard}
    return result
