"""AX.25 §4.3.3.2 segmenter / reassembler tests."""

from __future__ import annotations

import pytest

from whatspyc.transport.ax25_frame import PID_NO_LAYER3
from whatspyc.transport.ax25_segment import PID_SEGMENT, Reassembler, segment


def _round_trip(payload: bytes, paclen: int) -> tuple[bytes, list[bytes]]:
    pieces = segment(payload, paclen=paclen)
    r = Reassembler()
    out = None
    for p in pieces:
        result = r.feed(p)
        if result is not None:
            out = result
    assert out is not None, f"reassembler never completed for paclen={paclen}"
    pid, apdu = out
    assert pid == PID_NO_LAYER3
    return apdu, pieces


def test_segment_empty_payload_one_segment() -> None:
    pieces = segment(b"", paclen=8)
    assert pieces == [bytes([0x80, PID_NO_LAYER3])]


def test_segment_one_byte_payload() -> None:
    apdu, pieces = _round_trip(b"x", paclen=8)
    assert apdu == b"x"
    assert len(pieces) == 1
    assert pieces[0][0] == 0x80  # first + final, N=0


def test_segment_paclen_payload() -> None:
    """Exactly paclen bytes — chunked into multiple segments because the
    1-byte sub-header eats one byte per segment.
    """
    payload = bytes(range(8))  # paclen=8 ⇒ 6 in first, 7 in subsequent
    apdu, pieces = _round_trip(payload, paclen=8)
    assert apdu == payload
    # 8 bytes ÷ (6 first + 7 subsequent...) — 8 = 6 + 2, so 2 segments.
    assert len(pieces) == 2


def test_segment_paclen_plus_one() -> None:
    payload = bytes(range(9))
    apdu, pieces = _round_trip(payload, paclen=8)
    assert apdu == payload
    # 9 = 6 + 3 — 2 segments.
    assert len(pieces) == 2


def test_segment_4paclen_minus_3() -> None:
    paclen = 8
    payload = bytes(range(4 * paclen - 3))  # 29 bytes
    apdu, pieces = _round_trip(payload, paclen=paclen)
    assert apdu == payload
    # 29 = 6 + 7 + 7 + 7 + 2 — 5 segments.
    assert len(pieces) == 5


def test_segment_first_segment_carries_pid() -> None:
    pieces = segment(b"abc", paclen=8, original_pid=0xCC)
    assert pieces[0][1] == 0xCC


def test_segment_count_in_header() -> None:
    """Sub-header N decrements from segments-1 down to 0 on the last."""
    pieces = segment(bytes(range(20)), paclen=8)
    # First byte of each segment header.
    n_values = [p[0] & 0x7F for p in pieces]
    assert n_values[-1] == 0
    assert n_values == list(range(len(pieces) - 1, -1, -1))
    # Only the first has bit 7 set.
    is_first = [bool(p[0] & 0x80) for p in pieces]
    assert is_first[0] is True
    assert all(not f for f in is_first[1:])


def test_segment_no_segment_exceeds_paclen() -> None:
    pieces = segment(bytes(range(4 * 8 - 3)), paclen=8)
    for p in pieces:
        assert len(p) <= 8


def test_reassembler_drops_continuation_without_first() -> None:
    r = Reassembler()
    # Continuation segment with N=0 — looks "complete" but no first seen.
    assert r.feed(bytes([0x00]) + b"orphan") is None


def test_reassembler_recovers_after_lost_continuation() -> None:
    """A dropped middle segment poisons the in-progress APDU; the next
    first-segment must reset state so subsequent APDUs reassemble cleanly.
    """
    r = Reassembler()
    a_pieces = segment(b"abcdefghij", paclen=6)
    b_pieces = segment(b"xyz", paclen=6)
    # Feed all of A *except* the second segment.
    assert len(a_pieces) >= 3
    r.feed(a_pieces[0])
    # skip a_pieces[1]
    assert r.feed(a_pieces[2]) is None  # gap detected
    # Now feed B fully.
    out = None
    for p in b_pieces:
        out = r.feed(p) or out
    assert out is not None
    pid, apdu = out
    assert pid == PID_NO_LAYER3 and apdu == b"xyz"


def test_segment_paclen_too_small() -> None:
    with pytest.raises(ValueError):
        segment(b"x", paclen=2)


def test_pid_segment_constant() -> None:
    assert PID_SEGMENT == 0x08
