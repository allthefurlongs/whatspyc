"""KISS framing — escape/unescape round-trip + boundary cases.

The decoder yields ``(port, command, payload)`` triples. ``command`` is
the low nybble of the KISS type byte: ``0x00`` for plain data frames and
``0x0C`` for ACKMODE-extension frames where the payload is exactly the
2-byte big-endian ACK id.
"""

from __future__ import annotations

import pytest

from whatspyc.transport import kiss_frame


def test_round_trip_simple() -> None:
    payload = b"Hello, world"
    framed = kiss_frame.encode_frame(payload, port=0)
    dec = kiss_frame.FrameDecoder()
    out = list(dec.feed(framed))
    assert out == [(0, kiss_frame.CMD_DATA, payload)]


def test_round_trip_with_fend_and_fesc_in_payload() -> None:
    payload = bytes([0xC0, 0xDB, 0x00, 0x42, 0xC0, 0xDB])
    framed = kiss_frame.encode_frame(payload, port=3)
    dec = kiss_frame.FrameDecoder()
    out = list(dec.feed(framed))
    assert out == [(3, kiss_frame.CMD_DATA, payload)]


def test_partial_feed_buffers_correctly() -> None:
    payload = b"split me"
    framed = kiss_frame.encode_frame(payload)
    dec = kiss_frame.FrameDecoder()
    half = len(framed) // 2
    assert list(dec.feed(framed[:half])) == []
    assert list(dec.feed(framed[half:])) == [(0, kiss_frame.CMD_DATA, payload)]


def test_decoder_handles_back_to_back_frames() -> None:
    a = kiss_frame.encode_frame(b"one")
    b = kiss_frame.encode_frame(b"two")
    dec = kiss_frame.FrameDecoder()
    out = list(dec.feed(a + b))
    assert [p for _, _, p in out] == [b"one", b"two"]


def test_ackmode_round_trip() -> None:
    """ACKMODE-extension frames carry a 2-byte ACK id ahead of the payload.

    The decoder strips the type byte and surfaces the (still-prefixed)
    information field plus the command nybble; callers (the KISS UI
    layer) split the 2-byte id off the front when ``command == 0x0C``.
    """
    payload = b"ax25 frame contents"
    framed = kiss_frame.encode_frame(
        payload, port=0, command=kiss_frame.CMD_DATA_ACK, ack_id=0x1234
    )
    dec = kiss_frame.FrameDecoder()
    out = list(dec.feed(framed))
    assert out == [(0, kiss_frame.CMD_DATA_ACK, b"\x12\x34" + payload)]


def test_ackmode_default_path_unchanged() -> None:
    """Default-call (no ack_id, no command kwarg) still emits CMD_DATA."""
    framed_default = kiss_frame.encode_frame(b"hi", port=2)
    framed_explicit = kiss_frame.encode_frame(
        b"hi", port=2, command=kiss_frame.CMD_DATA
    )
    assert framed_default == framed_explicit
    dec = kiss_frame.FrameDecoder()
    out = list(dec.feed(framed_default))
    assert out == [(2, kiss_frame.CMD_DATA, b"hi")]


def test_ackmode_requires_ack_id() -> None:
    with pytest.raises(ValueError):
        kiss_frame.encode_frame(b"x", command=kiss_frame.CMD_DATA_ACK)


def test_ackmode_id_range_validated() -> None:
    with pytest.raises(ValueError):
        kiss_frame.encode_frame(
            b"x", command=kiss_frame.CMD_DATA_ACK, ack_id=0x1_0000
        )
