"""WPS application-layer framing and compression.

Two things to know:

* Frames are line-delimited JSON. Outgoing frames terminate with ``\\r\\n``
  (the server splits on exactly that). Incoming frames may terminate with
  either ``\\r\\n`` or bare ``\\r`` because the server's own send path emits
  only ``\\r``.

* Compression is applied per-frame when the compressed form is shorter than
  the JSON. The compressed wire format is::

      <DELIM> base64( zlib( json_text ) ) <DELIM> <line-terminator>

  The server is asymmetric here:

  * Outbound (server → client) it emits ``DELIM = 0xC3 0x80`` — those two
    bytes are ``chr(192).encode("utf-8")``, since
    ``frame_and_compress_json_object`` builds a ``str`` containing
    ``chr(192)`` and then ``.encode()``s it.
  * Inbound (client → server) the dispatch loop ``.decode()``s the socket
    bytes as UTF-8 and then matches ``message[:2] == chr(195) + chr(128)``.
    The only wire form that round-trips through that path is the **4-byte**
    sequence ``0xC3 0x83 0xC2 0x80`` — i.e. take the natural delimiter byte
    ``0xC0``, decode it as latin-1 (→ ``chr(192)``), encode UTF-8
    (→ ``0xC3 0x80``), decode latin-1 again (→ ``chr(195) + chr(128)``),
    encode UTF-8 (→ ``0xC3 0x83 0xC2 0x80``). Sending the single-byte
    ``0xC0`` makes the server's UTF-8 decode raise; sending the 2-byte
    ``0xC3 0x80`` decodes to ``chr(192)`` which fails the
    ``chr(195)+chr(128)`` compression check and gets treated as garbage.

  So encode emits the 4-byte form (``DELIM_OUT``); decode accepts the
  2-byte form the server actually emits (``DELIM_UTF8``) and tolerates the
  bare ``0xC0`` and the 4-byte forms in case any intermediate normalises
  one of them.
"""

from __future__ import annotations

import base64
import json
import zlib
from typing import Iterator

DELIM_SINGLE = b"\xc0"
DELIM_UTF8 = b"\xc3\x80"
DELIM_DOUBLE = b"\xc3\x83\xc2\x80"
DELIM_OUT = DELIM_DOUBLE
LINE_END = b"\r\n"


def encode(obj: dict) -> bytes:
    """Encode a JSON object as a single WPS frame, compressed if shorter."""
    text = json.dumps(obj, separators=(",", ":"))
    raw = text.encode("utf-8")
    compressed_payload = base64.b64encode(zlib.compress(raw, 9))
    framed_compressed = DELIM_OUT + compressed_payload + DELIM_OUT + LINE_END
    framed_plain = raw + LINE_END
    return framed_compressed if len(framed_compressed) < len(framed_plain) else framed_plain


def _strip_delim(payload: bytes) -> bytes | None:
    """If ``payload`` is wrapped with any recognised compression delimiter,
    return the inner base64 bytes. Otherwise None."""
    # Check the 4-byte form before the 2-byte form: 0xC3 0x83 0xC2 0x80
    # starts with 0xC3 0x83 (not 0xC3 0x80) so the orderings don't collide,
    # but checking the longest first keeps the intent obvious.
    if payload.startswith(DELIM_DOUBLE) and payload.endswith(DELIM_DOUBLE) and len(payload) >= 8:
        return payload[4:-4]
    if payload.startswith(DELIM_UTF8) and payload.endswith(DELIM_UTF8) and len(payload) >= 4:
        return payload[2:-2]
    if payload.startswith(DELIM_SINGLE) and payload.endswith(DELIM_SINGLE) and len(payload) >= 2:
        return payload[1:-1]
    return None


class FrameDecodeError(ValueError):
    """A frame couldn't be decoded as JSON (raw or compressed). Carries the
    offending payload so callers can surface it in error messages — by far
    the most common cause is an *unfinished* connect_sequence whose last
    hop didn't actually land at WPS, so the next bytes the reader sees are
    plain text from a node prompt rather than framed JSON."""

    def __init__(self, message: str, payload: bytes) -> None:
        super().__init__(message)
        self.payload = payload


def _decode_one(payload: bytes) -> dict:
    """Decode a single frame's payload (already stripped of line terminator)."""
    inner = _strip_delim(payload)
    try:
        if inner is not None:
            decompressed = zlib.decompress(base64.b64decode(inner))
            return json.loads(decompressed.decode("utf-8"))
        return json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, zlib.error, ValueError) as exc:
        # Cap the snippet so a flood of garbage doesn't blow up the log.
        snippet = payload[:120]
        raise FrameDecodeError(
            f"could not decode WPS frame ({type(exc).__name__}: {exc}); "
            f"first {len(snippet)} bytes: {snippet!r}",
            payload,
        ) from exc


class FrameDecoder:
    """Buffered frame splitter. Feed bytes; iterate complete JSON objects.

    Splits on ``\\r\\n`` first, then falls back to bare ``\\r`` for any
    remaining segment. This tolerates the server's mixed terminators without
    losing the boundary between successive frames.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[dict]:
        self._buf.extend(data)
        while True:
            frame, rest = self._split_one(bytes(self._buf))
            if frame is None:
                return
            self._buf = bytearray(rest)
            frame = frame.strip()
            if not frame:
                continue
            yield _decode_one(frame)

    @staticmethod
    def _split_one(buf: bytes) -> tuple[bytes | None, bytes]:
        # Split on '\r' (the always-present component of either '\r\n' or
        # bare '\r' terminators), then absorb an optional following '\n'.
        i = buf.find(b"\r")
        if i == -1:
            return None, buf
        frame = buf[:i]
        rest = buf[i + 1 :]
        if rest.startswith(b"\n"):
            rest = rest[1:]
        return frame, rest
