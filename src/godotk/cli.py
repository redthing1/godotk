"""Compact JSON-first CLI; human-friendly help, machine-readable outcomes."""

import argparse
import asyncio
import json
from pathlib import Path

from . import __version__
from .client import call, close_session, open_session
from .engine import executable, version
from .errors import GodotKError
from .install import setup
from .project import project_path
from .supervisor import serve


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="godotk", description="Operate a real Godot editor and game through native APIs.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--project", default=".", help="Project directory (default: current directory)")
    root.add_argument("--godot", help="Explicit Godot executable; invalid selections never fall back")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Report the selected engine")
    commands.add_parser("install", help="Install/enable the add-on; close existing editors first")
    commands.add_parser("uninstall", help="Remove owned add-on/settings into recovery storage; close editors first")
    for name in ("open", "serve"):
        command = commands.add_parser(name, help="Launch a supervised editor" if name == "open" else "Run the project supervisor in the foreground")
        command.add_argument("--headless", action="store_true", help="Nonvisual mode; capture is unavailable")
    for name in ("status", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--request-id")
    command = commands.add_parser("close", help="Close the owned session; requires explicit discard")
    command.add_argument("--discard", action="store_true")
    command.add_argument("--request-id")
    command = commands.add_parser("play", help="Play the main or selected scene using Godot's native save-on-play behavior")
    command.add_argument("--scene", help="res:// scene path; otherwise project main scene")
    command.add_argument("--debug-breaks", action="store_true", help="Allow error-induced debugger pauses; default reports errors without pausing")
    command.add_argument("--request-id")
    command = commands.add_parser("inspect", help="Discover native trees, properties, methods, and signals")
    command.add_argument("--target", choices=("editor", "game"), default="editor")
    selection = command.add_mutually_exclusive_group()
    selection.add_argument("--path", default=".")
    selection.add_argument("--ref")
    command.add_argument("--depth", type=int, default=2)
    command.add_argument("--limit", type=int, default=64)
    command.add_argument("--section", choices=("tree", "properties", "methods", "signals"), default="tree")
    command.add_argument("--filter", default="", help="Case-insensitive member-name substring")
    command.add_argument("--offset", type=int, default=0, help="Member page offset; re-query after changes")
    command.add_argument("--read", action="append", default=[], metavar="PROPERTY", help="Read a named property (repeatable); defaults to properties section")
    command.add_argument("--run", help="Expected game generation")
    command.add_argument("--request-id")
    command = commands.add_parser("exec", help="Run a trusted @tool RefCounted GDScript with run(ctx); no implicit save/undo")
    command.add_argument("--file", required=True, type=Path, help="Local UTF-8 script; sent to Godot without copying into the project")
    command.add_argument("--target", choices=("editor", "game"), required=True)
    selection = command.add_mutually_exclusive_group()
    selection.add_argument("--path", default=".")
    selection.add_argument("--ref")
    command.add_argument("--params", default="{}", help="JSON object available as ctx.params")
    command.add_argument("--run", help="Expected game generation")
    command.add_argument("--request-id")
    command = commands.add_parser("capture", help="Capture a Godot render target only; never the desktop")
    command.add_argument("--target", choices=("editor", "game"), default="game")
    command.add_argument("--view", choices=("2d", "3d"), default="2d", help="Editor viewport type")
    command.add_argument("--index", type=int, default=0, help="Editor 3D viewport index")
    command.add_argument("--max-width", type=int, default=1280)
    command.add_argument("--run")
    command.add_argument("--request-id")
    command = commands.add_parser("input", help="Deliver a bounded native action/key press and release")
    kind = command.add_mutually_exclusive_group(required=True)
    kind.add_argument("--action")
    kind.add_argument("--key")
    command.add_argument("--frames", type=int, default=1, help="Process-frame waits, not deterministic physics steps")
    command.add_argument("--run")
    command.add_argument("--request-id")
    command = commands.add_parser("logs", help="Bounded native/process diagnostics with a cursor")
    command.add_argument("--after", type=int, default=0)
    command = commands.add_parser("outcome", help="Look up a retained request without replaying it")
    command.add_argument("id")
    return root


async def dispatch(args: argparse.Namespace) -> dict | None:
    command = args.command
    if command == "doctor":
        engine = executable(args.godot)
        return {"ok": True, "result": {"path": str(engine), "version": version(engine),
                "support": "Godot 4.6+; experimental"}}
    project = project_path(args.project)
    if command in ("install", "uninstall", "open", "serve"):
        engine = executable(args.godot)
        engine_version = version(engine)
        if command in ("install", "uninstall"):
            return {"ok": True, "result": setup(project, engine, remove=command == "uninstall")}
        if command == "open":
            return await open_session(project, engine, headless=args.headless)
        await serve(project, engine, engine_version, headless=args.headless)
        return None
    if command == "close":
        return await close_session(project, discard=args.discard, request_id=args.request_id)
    target = getattr(args, "target", "editor")
    native_args = {}
    if command == "play":
        native_args["debug_breaks"] = args.debug_breaks
        if args.scene:
            native_args["scene"] = args.scene
    elif command == "inspect":
        native_args = {"depth": args.depth, "limit": args.limit, "section": "properties" if args.read and args.section == "tree" else args.section,
                       "filter": args.filter, "offset": args.offset, "read": args.read}
        native_args["ref" if args.ref else "path"] = args.ref or args.path
    elif command == "exec":
        with args.file.open("rb") as stream:
            source = stream.read(65537)
        if not source or len(source) > 65536:
            raise GodotKError("INVALID_ARGUMENT", "Script must contain 1..65536 UTF-8 bytes")
        try:
            params = json.loads(args.params)
            json.dumps(params, allow_nan=False)
            source = source.decode("utf-8")
        except (ValueError, RecursionError) as exc:
            raise GodotKError("INVALID_ARGUMENT", "Supply UTF-8 GDScript and valid finite JSON params") from exc
        if not isinstance(params, dict):
            raise GodotKError("INVALID_ARGUMENT", "params must be a JSON object")
        native_args = {"source": source, "params": params}
        native_args["ref" if args.ref else "path"] = args.ref or args.path
    elif command == "capture":
        native_args = {"view": args.view, "index": args.index, "max_width": args.max_width}
    elif command == "input":
        target = "game"
        native_args = {"frames": args.frames, "action" if args.action else "key": args.action or args.key}
    elif command == "logs":
        native_args = {"after": args.after}
    elif command == "outcome":
        native_args = {"id": args.id}
    return await call(project, command, native_args, target=target, run=getattr(args, "run", None), request_id=getattr(args, "request_id", None))


def main() -> None:
    args = parser().parse_args()
    try:
        result = asyncio.run(dispatch(args))
    except GodotKError as exc:
        result = {"ok": False, "error": exc.as_dict()}
    except KeyboardInterrupt:
        raise SystemExit(130)
    except OSError as exc:
        result = {"ok": False, "error": {"code": "IO_ERROR", "message": str(exc)}}
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if not result.get("ok"):
            raise SystemExit(1)
