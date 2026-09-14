"""Explicit, collision-checked installation; Godot parses its own project settings."""

import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from .errors import GodotKError
from .project import ProjectLock, atomic_json, state_dir

ADDON = "addons/godotk"
MARKER = ".godotk-install.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def packaged_files() -> dict[str, bytes]:
    root = files("godotk").joinpath("native", "addon")
    return {path.name: path.read_bytes() for path in root.iterdir() if path.is_file() and not path.name.endswith(".uid")}


def verify_install(project: Path) -> None:
    target = project / ADDON
    try:
        manifest = json.loads((target / MARKER).read_text())
        expected = manifest["files"]
        if not isinstance(expected, dict) or not expected:
            raise ValueError("Invalid manifest")
        for name, checksum in expected.items():
            if Path(name).name != name or digest((target / name).read_bytes()) != checksum:
                raise ValueError(name)
        allowed = set(expected) | {MARKER} | {name + ".uid" for name in expected if name.endswith(".gd")}
        if any(path.name not in allowed or not path.is_file() for path in target.iterdir()):
            raise ValueError("Unmanaged files")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise GodotKError("ADDON_CONFLICT", "Missing, changed, or unowned add-on files; refusing to overwrite/remove", path=str(target)) from exc


def configure(engine: Path, original: bytes, action: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="godotk-settings-") as directory:
        root = Path(directory)
        (root / "project.godot").write_text('config_version=5\n[application]\nconfig/name="GodotK Settings Helper"\n')
        config = root / "settings.cfg"
        config.write_bytes(original)
        helper = root / "configure.gd"
        helper.write_bytes(files("godotk").joinpath("native", "configure.gd").read_bytes())
        try:
            result = subprocess.run([str(engine), "--headless", "--path", str(root), "--log-file", str(root / "helper.log"),
                                     "--script", str(helper), "--", action, str(config)],
                                    capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GodotKError("SETUP_FAILED", "Godot settings helper failed") from exc
        if result.returncode or "GODOTK_SETUP_OK" not in result.stdout:
            raise GodotKError("SETUP_FAILED", "Godot refused the settings change", diagnostics=(result.stdout + result.stderr)[-4096:])
        return config.read_bytes()


def setup(project: Path, engine: Path, *, remove: bool = False) -> dict:
    with ProjectLock(project):
        target = project / ADDON
        if remove or target.exists():
            verify_install(project)
        if not remove and target.exists():
            current = packaged_files()
            if any((target / name).read_bytes() != data for name, data in current.items()):
                raise GodotKError("VERSION_MISMATCH", "Uninstall the previous add-on before installing this version")
            return {"installed": True, "changed": False, "path": str(target)}
        settings = project / "project.godot"
        original = settings.read_bytes()
        modified = configure(engine, original, "remove" if remove else "install")
        if settings.read_bytes() != original:
            raise GodotKError("PROJECT_CHANGED", "project.godot changed during setup; nothing applied")
        recovery = state_dir(project) / ("setup-" + uuid.uuid4().hex)
        recovery.mkdir(mode=0o700)
        (recovery / "project.godot.before").write_bytes(original)
        target_changed = False
        try:
            if remove:
                verify_install(project)
                shutil.move(str(target), str(recovery / "addon"))
                target_changed = True
            else:
                target.mkdir(parents=True)
                target_changed = True
                content = packaged_files()
                for name, data in content.items():
                    (target / name).write_bytes(data)
                atomic_json(target / MARKER, {"version": 1, "files": {name: digest(data) for name, data in content.items()}})
            fd, name = tempfile.mkstemp(prefix=".godotk-settings-", dir=project)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(modified)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(name, settings.stat().st_mode & 0o777)
                if settings.read_bytes() != original:
                    raise GodotKError("PROJECT_CHANGED", "project.godot changed during setup")
                os.replace(name, settings)
            finally:
                Path(name).unlink(missing_ok=True)
        except Exception:
            if target_changed and remove:
                shutil.move(str(recovery / "addon"), str(target))
            elif target_changed:
                shutil.move(str(target), str(recovery / "addon"))
            raise
        return {"installed": not remove, "changed": True, "path": str(target), "recovery": str(recovery),
                "settings_reserialized": True}
