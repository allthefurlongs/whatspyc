"""KISS framing — escape/unescape data frames.

KISS uses a single octet ``0xC0`` (FEND) as frame boundary. Inside a frame,
``0xC0`` and ``0xDB`` (FESC) are escaped:

  * ``FEND`` -> ``FESC`` ``TFEND``  (0xDB 0xDC)
  * ``FESC`` -> ``FESC`` ``TFESC``  (0xDB 0xDD)

The first octet of an unescaped frame is the *type* byte: high nybble is the
port, low nybble is the command (``0`` = data frame, ``0x0C`` = data with
ACK request — see KISS extension below).

KISS ACKMODE (command nybble ``0x0C``) is a non-standard extension first
documented by G8BPQ and supported by the Linux kernel ``mkiss`` driver and
Direwolf. The host emits a data frame whose first 2 information bytes carry
a big-endian ACK id; the TNC echoes the same command nybble + id (with no
AX.25 payload) once the frame has actually gone on-air. This lets the L2
state machine start its T1 ack-timer from real over-the-air time rather
than from the moment we shoved bytes into the OS buffer.
"""

from __future__ import annotations

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

CMD_DATA = 0x00
CMD_DATA_ACK = 0x0C  # KISS ACKMODE extension (G8BPQ / mkiss)


def encode_frame(
    payload: bytes,
    *,
    port: int = 0,
    command: int = CMD_DATA,
    ack_id: int | None = None,
) -> bytes:
    """Wrap ``payload`` as a KISS frame on the given port.

    With the default ``command=CMD_DATA`` the result is a plain data frame.
    When ``command=CMD_DATA_ACK`` (the ACKMODE extension), ``ack_id`` must
    be a 16-bit unsigned integer; it's prepended to the payload as a
    big-endian 2-byte field before KISS escaping.
    """
    type_byte = ((port & 0x0F) << 4) | (command & 0x0F)
    body = bytearray()
    if command == CMD_DATA_ACK:
        if ack_id is None:
            raise ValueError("CMD_DATA_ACK frames require an ack_id")
        if not (0 <= ack_id <= 0xFFFF):
            raise ValueError(f"ack_id out of range for 2 bytes: {ack_id}")
        body += ack_id.to_bytes(2, "big")
    body += payload
    out = bytearray([FEND, type_byte])
    for b in body:
        if b == FEND:
            out += bytes([FESC, TFEND])
        elif b == FESC:
            out += bytes([FESC, TFESC])
        else:
            out.append(b)
    out.append(FEND)
    return bytes(out)


class FrameDecoder:
    """Buffered KISS-frame decoder. Feed bytes; iterate ``(port, command,
    payload)`` triples. The command nybble lets callers discriminate plain
    data frames (``0x00``) from ACKMODE replies (``0x0C``); the payload of
    a 0x0C reply is exactly the 2-byte ack id.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._in_frame = False
        self._cur = bytearray()

    def feed(self, data: bytes):
        i = 0
        while i < len(data):
            b = data[i]
            i += 1
            if b == FEND:
                if self._in_frame and len(self._cur) > 0:
                    yield self._finalise()
                self._in_frame = True
                self._cur = bytearray()
                continue
            if not self._in_frame:
                continue
            if b == FESC:
                if i >= len(data):
                    # buffer the trailing FESC for the next feed
                    self._cur.append(FESC)
                    return
                nxt = data[i]
                i += 1
                if nxt == TFEND:
                    self._cur.append(FEND)
                elif nxt == TFESC:
                    self._cur.append(FESC)
                else:
                    # protocol error — drop this frame
                    self._in_frame = False
                continue
            self._cur.append(b)

    def _finalise(self):
        type_byte = self._cur[0]
        port = (type_byte >> 4) & 0x0F
        command = type_byte & 0x0F
        payload = bytes(self._cur[1:])
        self._in_frame = False
        return port, command, payload
