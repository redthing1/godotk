"""Project identity, private session files, and single-supervisor ownership."""

import json
import os
from pathlib import Path
import tempfile

from .errors import GodotKError


def project_path(path: str | Path) -> Path:
    project = Path(path).expanduser().resolve()
    if not (project / "project.godot").is_file():
        raise GodotKError("NOT_A_PROJECT", "Expected a directory containing project.godot", path=str(project))
    return project


def state_dir(project: Path) -> Path:
    path = project / ".godot" / "godotk"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def atomic_json(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GodotKError("NO_SESSION", "No readable session state", path=str(path)) from exc
    if not isinstance(data, dict):
        raise GodotKError("NO_SESSION", "Invalid session state", path=str(path))
    return data


class ProjectLock:
    """OS lock, not a PID heuristic. The lock file intentionally survives exit."""

    def __init__(self, project: Path):
        self.path = state_dir(project) / "session.lock"
        self.stream = None

    def __enter__(self):
        self.stream = self.path.open("a+b")
        self.path.chmod(0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise GodotKError("PROJECT_BUSY", "Another godotk operation owns this project") from exc
        return self

    def __exit__(self, *_):
        if self.stream:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()
            self.stream = None
