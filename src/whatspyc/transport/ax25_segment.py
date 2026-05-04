"""AX.25 v2.2 §4.3.3.2 — Layer-3 segmenter / reassembler.

When an APDU is too big to ride in a single I-frame, the L3 segmenter
splits it across N I-frames carrying PID 0x08. The information field of
each segment starts with a 1-byte sub-header:

* **First segment**: ``[N | 0x80, original_pid, ...data]``. Bit 7 marks
  this as the first segment; bits 0-6 carry the count of segments still
  *to follow* (so a single-segment APDU has ``N == 0`` and a header byte
  of ``0x80``). The original payload's PID — what the receiver should
  treat the reassembled APDU as — rides in the next byte.
* **Subsequent segments**: ``[N, ...data]``. ``N`` decreases by one each
  time and reaches zero on the final segment.

The receiver concatenates the data fields in arrival order and surfaces
the assembled APDU once ``N == 0`` is seen. The spec forbids interleaving
segmentation streams over a single AX.25 link, so the reassembler only
needs to track a single in-progress APDU per peer.

This module is pure and side-effect-free; the L2 wires it in only when
``segmentation=True`` is passed to :class:`Ax25L2Stream`.
"""

from __future__ import annotations

from whatspyc.transport.ax25_frame import PID_NO_LAYER3

PID_SEGMENT = 0x08


def segment(
    payload: bytes,
    *,
    paclen: int,
    original_pid: int = PID_NO_LAYER3,
) -> list[bytes]:
    """Split ``payload`` into a list of segment information fields.

    Each returned ``bytes`` is meant to be sent as the information field
    of one I-frame whose PID is ``0x08``. ``paclen`` bounds the per-frame
    information field length (header byte(s) + data); the segmenter sizes
    chunks so that no segment exceeds it.

    Always returns at least one segment, even for empty / small payloads.
    Raises ``ValueError`` if the payload would need more than 128
    segments (the spec's per-APDU cap).
    """
    if paclen < 3:
        raise ValueError("paclen must be >= 3 to carry a segment header + PID")
    first_data_max = paclen - 2  # header byte + original_pid byte
    rest_data_max = paclen - 1  # header byte only

    if not payload:
        # Single empty segment is still legal: lets PID 0x08 carry a
        # zero-length APDU without needing a special case in the L2.
        return [bytes([0x80, original_pid])]

    chunks: list[bytes] = [payload[:first_data_max]]
    rest = payload[first_data_max:]
    while rest:
        chunks.append(rest[:rest_data_max])
        rest = rest[rest_data_max:]
    total = len(chunks)
    if total - 1 > 0x7F:
        raise ValueError(
            f"payload too large for AX.25 segmentation: needs {total} "
            f"segments, max 128"
        )
    out: list[bytes] = []
    for i, chunk in enumerate(chunks):
        n = total - 1 - i
        if i == 0:
            out.append(bytes([n | 0x80, original_pid]) + chunk)
        else:
            out.append(bytes([n]) + chunk)
    return out


class Reassembler:
    """Stateful reassembler for one AX.25 segmentation stream.

    Feed PID-0x08 information fields in arrival order via :meth:`feed`;
    once the final segment lands, the next call returns
    ``(original_pid, complete_apdu)``. All other calls return ``None``.

    Out-of-order or malformed inputs reset internal state and are
    silently dropped (returning ``None``) — the L2 will fall back on its
    own retransmit machinery to recover the lost segment.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._original_pid: int | None = None
        self._expected_remaining: int | None = None

    def feed(self, info: bytes) -> tuple[int, bytes] | None:
        if not info:
            self._reset()
            return None
        header = info[0]
        is_first = bool(header & 0x80)
        n = header & 0x7F
        if is_first:
            if len(info) < 2:
                self._reset()
                return None
            self._buf = bytearray(info[2:])
            self._original_pid = info[1]
            self._expected_remaining = n
        else:
            if self._expected_remaining is None:
                # Got a continuation without ever seeing a first
                # segment — drop and resync at the next first segment.
                return None
            if n != self._expected_remaining - 1:
                # Lost or duplicate segment. Drop the partial APDU and
                # wait for the next first-segment to resync.
                self._reset()
                return None
            self._buf += info[1:]
            self._expected_remaining = n
        if n == 0:
            apdu = bytes(self._buf)
            pid = self._original_pid or PID_NO_LAYER3
            self._reset()
            return pid, apdu
        return None

    def _reset(self) -> None:
        self._buf = bytearray()
        self._original_pid = None
        self._expected_remaining = None
