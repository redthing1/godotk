import asyncio
import struct
import unittest

from godotk.errors import GodotKError
from godotk.protocol import MAX_FRAME, decode, encode, receive


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_unicode_frame(self):
        expected = {"name": "場景", "integer": 2**63 - 1}
        reader = asyncio.StreamReader()
        encoded = encode(expected)
        for byte in encoded:
            reader.feed_data(bytes([byte]))
        self.assertEqual(await receive(reader), expected)

    async def test_two_frames(self):
        reader = asyncio.StreamReader()
        reader.feed_data(encode({"a": 1}) + encode({"b": 2}))
        self.assertEqual(await receive(reader), {"a": 1})
        self.assertEqual(await receive(reader), {"b": 2})

    async def test_oversized_header_rejected_before_payload(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", MAX_FRAME + 1))
        with self.assertRaises(GodotKError):
            await receive(reader)

    async def test_truncated_frame(self):
        reader = asyncio.StreamReader()
        reader.feed_data(encode({"a": 1})[:-1])
        reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await receive(reader)

    def test_invalid_json(self):
        for value in (b"[]", b"null", b'{"a":NaN}', b"\xff", b"{"):
            with self.subTest(value=value), self.assertRaises(GodotKError):
                decode(value)

    def test_encoding_bound_and_nonfinite(self):
        for value in ({"x": float("inf")}, {"x": object()}, {"x": "x" * MAX_FRAME}):
            with self.subTest(value=type(value["x"])), self.assertRaises(GodotKError):
                encode(value)
