"""Publish bounded, engine-produced image files; never capture the desktop."""

import hashlib
from pathlib import Path
import re
import struct

from .errors import GodotKError
from .project import atomic_json, read_json

MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_COUNT = 32
CAPTURE_ID = re.compile(r"[0-9a-f]{32}\Z")


class ArtifactStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def publish(self, result: dict) -> dict:
        identity = result.get("capture", "")
        if not isinstance(identity, str) or not CAPTURE_ID.fullmatch(identity):
            raise GodotKError("INVALID_ARTIFACT", "Invalid capture identity")
        expected = self.directory / (identity + ".png")
        supplied = Path(str(result.get("path", "")))
        if supplied.resolve() != expected.resolve() or expected.is_symlink() or not expected.is_file():
            raise GodotKError("INVALID_ARTIFACT", "Capture path is not a native session artifact")
        size = expected.stat().st_size
        if size > MAX_FILE:
            expected.unlink()
            raise GodotKError("ARTIFACT_LIMIT", "Generated image exceeded the 16 MiB artifact limit and was removed")
        data = expected.read_bytes()
        if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            raise GodotKError("INVALID_ARTIFACT", "Native output is not a PNG image")
        width, height = struct.unpack("!II", data[16:24])
        if (width, height) != (result.get("width"), result.get("height")) or not width or not height:
            raise GodotKError("INVALID_ARTIFACT", "Image dimensions do not match the capture metadata")
        checksum = hashlib.sha256(data).hexdigest()
        index_path = self.directory / "index.json"
        entries = read_json(index_path).get("entries", []) if index_path.exists() else []
        if not isinstance(entries, list) or len(entries) > MAX_COUNT:
            raise GodotKError("INVALID_ARTIFACT_INDEX", "Artifact registry is not valid; no retention cleanup performed")
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                or not CAPTURE_ID.fullmatch(entry["id"]) or type(entry.get("bytes")) is not int
                or not 0 <= entry["bytes"] <= MAX_FILE or not isinstance(entry.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
                raise GodotKError("INVALID_ARTIFACT_INDEX", "Artifact registry is not valid; no retention cleanup performed")
        entries = [entry for entry in entries if entry.get("id") != identity]
        entries.append({"id": identity, "bytes": size, "sha256": checksum})
        evicted = []
        preserved = []
        while len(entries) > MAX_COUNT or sum(entry["bytes"] for entry in entries) > MAX_TOTAL:
            entry = entries.pop(0)
            old_id = entry.get("id", "")
            if not isinstance(old_id, str) or not CAPTURE_ID.fullmatch(old_id):
                raise GodotKError("INVALID_ARTIFACT_INDEX", "Artifact registry contains an invalid identity")
            old = self.directory / (old_id + ".png")
            if old.is_file() and not old.is_symlink() and old.stat().st_size <= MAX_FILE and hashlib.sha256(old.read_bytes()).hexdigest() == entry["sha256"]:
                old.unlink()
                evicted.append(old_id)
            elif old.exists():
                preserved.append(old_id)
        atomic_json(index_path, {"entries": entries})
        return dict(result, bytes=size, sha256=checksum,
                    retention={"max_count": MAX_COUNT, "max_bytes": MAX_TOTAL, "evicted": evicted, "preserved_modified": preserved})
