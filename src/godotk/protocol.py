"""Versioned, bounded JSON frames. Native stdout is never a protocol stream."""

import asyncio
import json
import struct

from .errors import GodotKError

VERSION = 1
MAX_FRAME = 1_048_576


def encode(message: dict) -> bytes:
    try:
        payload = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError) as exc:
        raise GodotKError("INVALID_MESSAGE", "Message is not JSON-safe") from exc
    if not 0 < len(payload) <= MAX_FRAME:
        raise GodotKError("MESSAGE_TOO_LARGE", "Message exceeds the wire limit", limit=MAX_FRAME)
    return struct.pack("!I", len(payload)) + payload


def decode(payload: bytes) -> dict:
    try:
        message = json.loads(payload, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise GodotKError("INVALID_MESSAGE", "Malformed JSON") from exc
    if not isinstance(message, dict):
        raise GodotKError("INVALID_MESSAGE", "Expected a JSON object")
    return message


def _reject_constant(value: str):
    raise ValueError(value)


async def receive(reader: asyncio.StreamReader) -> dict:
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise GodotKError("MESSAGE_TOO_LARGE", "Invalid frame length", limit=MAX_FRAME)
    return decode(await reader.readexactly(size))


async def send(writer: asyncio.StreamWriter, message: dict) -> None:
    writer.write(encode(message))
    await writer.drain()
