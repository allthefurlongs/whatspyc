"""Codec tests — framing, splitting, and the compression delimiter quirk."""

from __future__ import annotations

import base64
import json
import zlib

from whatspyc.wps import codec


def test_encode_short_payload_is_uncompressed() -> None:
    obj = {"t": "k"}
    encoded = codec.encode(obj)
    assert encoded.endswith(b"\r\n")
    assert encoded[:-2] == b'{"t":"k"}'


def test_encode_compresses_when_shorter() -> None:
    # A repetitive payload should compress smaller than its JSON. Outbound
    # delimiter is the 4-byte form — the only thing the WPS server's input
    # check (which UTF-8-decodes the socket buffer and then matches
    # `chr(195)+chr(128)`) actually accepts.
    obj = {"t": "m", "_id": "x" * 200, "fc": "M0ABC", "tc": "T3EST", "m": "y" * 200, "ts": 1}
    encoded = codec.encode(obj)
    assert encoded[:4] == codec.DELIM_DOUBLE
    assert encoded[-6:] == codec.DELIM_DOUBLE + b"\r\n"
    inner = encoded[4:-6]
    decoded = json.loads(zlib.decompress(base64.b64decode(inner)).decode("utf-8"))
    assert decoded == obj


def test_decoder_splits_on_crlf() -> None:
    dec = codec.FrameDecoder()
    blob = b'{"t":"k"}\r\n{"t":"mr","_id":"abc"}\r\n'
    out = list(dec.feed(blob))
    assert out == [{"t": "k"}, {"t": "mr", "_id": "abc"}]


def test_decoder_handles_bare_cr() -> None:
    """Server's frame_and_compress emits bare \\r — accept that too."""
    dec = codec.FrameDecoder()
    blob = b'{"t":"k"}\r{"t":"mr","_id":"x"}\r'
    out = list(dec.feed(blob))
    assert out == [{"t": "k"}, {"t": "mr", "_id": "x"}]


def test_decoder_buffers_partial_frames() -> None:
    dec = codec.FrameDecoder()
    assert list(dec.feed(b'{"t":"k"')) == []
    assert list(dec.feed(b"}\r\n")) == [{"t": "k"}]


def test_decoder_handles_compressed_round_trip() -> None:
    """encode/decode round trip — exercises the 4-byte form the encoder emits."""
    obj = {"t": "m", "fc": "M0ABC", "tc": "T3EST", "m": "z" * 300, "ts": 1, "_id": "1-M0ABC"}
    encoded = codec.encode(obj)
    dec = codec.FrameDecoder()
    out = list(dec.feed(encoded))
    assert out == [obj]


def test_decoder_handles_compressed_with_utf8_pair_delim() -> None:
    """The server's outbound compressed frames use the 2-byte UTF-8 form
    `0xC3 0x80` (because `frame_and_compress_json_object` builds a `str` with
    `chr(192)` and then `.encode()`s it). Decoder must accept that form."""
    obj = {"t": "m", "fc": "M0ABC", "tc": "T3EST", "m": "z" * 300, "ts": 1, "_id": "1-M0ABC"}
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    inner = base64.b64encode(zlib.compress(raw, 9))
    blob = codec.DELIM_UTF8 + inner + codec.DELIM_UTF8 + b"\r\n"
    dec = codec.FrameDecoder()
    out = list(dec.feed(blob))
    assert out == [obj]


def test_decoder_handles_compressed_with_single_byte_delim() -> None:
    """Tolerate the bare-byte form too — never seen on the wire from the real
    server, but cheap insurance if some intermediate strips the round-trip."""
    obj = {"t": "m", "fc": "M0ABC", "tc": "T3EST", "m": "z" * 300, "ts": 1, "_id": "1-M0ABC"}
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    inner = base64.b64encode(zlib.compress(raw, 9))
    blob = codec.DELIM_SINGLE + inner + codec.DELIM_SINGLE + b"\r\n"
    dec = codec.FrameDecoder()
    out = list(dec.feed(blob))
    assert out == [obj]


def test_decoder_raises_FrameDecodeError_with_payload() -> None:
    """Non-JSON text (e.g. a node prompt that leaked past an incomplete
    connect_sequence) surfaces as FrameDecodeError carrying the offending
    bytes. WpsClient uses this to wrap the failure with a useful hint."""
    import pytest

    dec = codec.FrameDecoder()
    with pytest.raises(codec.FrameDecodeError) as info:
        list(dec.feed(b"*** Failure - unknown command\r"))
    assert info.value.payload == b"*** Failure - unknown command"
    assert "could not decode WPS frame" in str(info.value)
