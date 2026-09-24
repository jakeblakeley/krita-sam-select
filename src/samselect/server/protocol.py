"""Length-prefixed frames shared by the Krita plugin and the backend server.

Stdlib only: this module is imported both by Krita's embedded Python and by
the backend venv. A frame is::

    b"SAM3" | u32 json length | u64 payload length | json header | payload

Requests carry ``{"id": int, "cmd": str, ...}``; responses echo ``id``.
Unsolicited server messages carry ``{"event": str, ...}`` instead of an id.
"""

from __future__ import annotations

import json
import struct

MAGIC = b"SAM3"
_PREFIX = struct.Struct(">4sIQ")
PREFIX_SIZE = _PREFIX.size
PROTOCOL_VERSION = 1


class ProtocolError(RuntimeError):
    pass


def encode(header: dict, payload: bytes = b"") -> bytes:
    body = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return _PREFIX.pack(MAGIC, len(body), len(payload)) + body + payload


def decode_prefix(data: bytes) -> tuple[int, int]:
    magic, header_len, payload_len = _PREFIX.unpack(data)
    if magic != MAGIC:
        raise ProtocolError(f"bad frame magic {magic!r}")
    return header_len, payload_len


class FrameReader:
    """Incremental parser for a byte stream that arrives in arbitrary chunks."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._need: tuple[int, int] | None = None

    def feed(self, data: bytes) -> list[tuple[dict, bytes]]:
        self._buf += data
        frames = []
        while True:
            if self._need is None:
                if len(self._buf) < PREFIX_SIZE:
                    break
                self._need = decode_prefix(bytes(self._buf[:PREFIX_SIZE]))
            header_len, payload_len = self._need
            total = PREFIX_SIZE + header_len + payload_len
            if len(self._buf) < total:
                break
            header = json.loads(bytes(self._buf[PREFIX_SIZE : PREFIX_SIZE + header_len]))
            payload = bytes(self._buf[PREFIX_SIZE + header_len : total])
            del self._buf[:total]
            self._need = None
            frames.append((header, payload))
        return frames


def read_frame(stream) -> tuple[dict, bytes] | None:
    """Blocking read of one frame from a binary file object; None on EOF."""
    prefix = _read_exact(stream, PREFIX_SIZE)
    if prefix is None:
        return None
    header_len, payload_len = decode_prefix(prefix)
    body = _read_exact(stream, header_len)
    payload = _read_exact(stream, payload_len) if payload_len else b""
    if body is None or payload is None:
        return None
    return json.loads(body), payload


def _read_exact(stream, n: int) -> bytes | None:
    chunks = []
    while n:
        chunk = stream.read(n)
        if not chunk:
            return None
        chunks.append(chunk)
        n -= len(chunk)
    return b"".join(chunks)
