"""Operator-supplied engine selection. Never download or silently replace it."""

import os
from pathlib import Path
import shutil
import subprocess

from .errors import GodotKError


def executable(selected: str | None = None) -> Path:
    explicit = selected or os.environ.get("GODOTK_GODOT")
    candidates = [explicit] if explicit else [shutil.which("godot"), shutil.which("godot4")]
    if not explicit and os.sys.platform == "darwin":
        candidates += ["/Applications/Godot.app/Contents/MacOS/Godot", "/Applications/Godot_mono.app/Contents/MacOS/Godot"]
    for value in candidates:
        if not value:
            continue
        path = Path(value).expanduser().resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise GodotKError("ENGINE_NOT_FOUND", "Selected Godot is not executable" if explicit else "Supply --godot or GODOTK_GODOT", selected=explicit)


def version(engine: Path) -> str:
    try:
        result = subprocess.run([str(engine), "--version"], capture_output=True, text=True, timeout=10, check=True)
        text = result.stdout.strip()
        parts = text.split(".")
        if len(parts) < 2 or int(parts[0]) != 4 or int(parts[1]) < 6:
            raise ValueError(text)
        return text
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise GodotKError("UNSUPPORTED_ENGINE", "Requires Godot 4.6 or newer within Godot 4", path=str(engine)) from exc
