"""AX.25 frame encode / decode (UI + I-frames).

Phase 1 only needs:
  * Address-field encode/decode for ``LOCAL>REMOTE`` style headers.
  * Control-byte encode/decode covering UI frames (0x03), I-frames
    (variable, encodes N(S)/N(R)), and the supervisory frame primitives
    (RR/RNR/REJ — encode side only here, dispatch lives in Phase-2 L2 SM).
  * Information-field passthrough — the AX.25 header just wraps it.

The L2 connected-mode state machine that drives I-frame numbering and
retransmission is intentionally deferred to Phase 2 (see PHASE2-PLAN.md).
"""

from __future__ import annotations

from dataclasses import dataclass

PID_NO_LAYER3 = 0xF0
CONTROL_UI = 0x03


def _encode_callsign(call: str, *, last: bool, ssid: int = 0, command: bool = True, has_been_repeated: bool = False) -> bytes:
    """Encode one AX.25 address subfield (7 octets).

    Each callsign character is left-shifted by one. The 7th byte encodes the
    SSID and assorted flag bits (HDLC extension bit, command/response, "has
    been repeated" for digipeaters).
    """
    base = call.upper()
    if "-" in base:
        base, ssid_str = base.split("-", 1)
        ssid = int(ssid_str)
    if len(base) > 6 or not base.isalnum():
        raise ValueError(f"invalid callsign for AX.25 address: {call!r}")
    base = base.ljust(6, " ")
    out = bytearray(b << 1 for b in base.encode("ascii"))
    ssid_byte = 0x60  # bits 5,6 reserved (always 1)
    ssid_byte |= (ssid & 0x0F) << 1
    if last:
        ssid_byte |= 0x01  # extension bit set on last address
    if command:
        ssid_byte |= 0x80  # C bit
    if has_been_repeated:
        ssid_byte |= 0x80
    out.append(ssid_byte)
    return bytes(out)


@dataclass
class Address:
    callsign: str
    ssid: int = 0
    last: bool = False
    command: bool = False
    has_been_repeated: bool = False

    @classmethod
    def decode(cls, raw: bytes) -> "Address":
        if len(raw) != 7:
            raise ValueError("AX.25 address subfield must be 7 octets")
        chars = bytes(b >> 1 for b in raw[:6]).decode("ascii").rstrip()
        ssid_byte = raw[6]
        return cls(
            callsign=chars,
            ssid=(ssid_byte >> 1) & 0x0F,
            last=bool(ssid_byte & 0x01),
            command=bool(ssid_byte & 0x80),
            has_been_repeated=bool(ssid_byte & 0x80),
        )

    def encode(self) -> bytes:
        return _encode_callsign(
            self.callsign,
            last=self.last,
            ssid=self.ssid,
            command=self.command,
            has_been_repeated=self.has_been_repeated,
        )


@dataclass
class UIFrame:
    """Unnumbered Information frame — used for connectionless services
    (APRS, broadcasts). Phase 1 KISS path uses this to verify framing.
    """

    destination: Address
    source: Address
    info: bytes
    pid: int = PID_NO_LAYER3
    digipeaters: list[Address] | None = None

    def encode(self) -> bytes:
        digis = list(self.digipeaters or [])
        # Address ordering: dest, src, digi1..digiN. Last address has ext bit set.
        addrs: list[Address] = [
            Address(self.destination.callsign, self.destination.ssid, last=False, command=True),
            Address(self.source.callsign, self.source.ssid, last=(len(digis) == 0), command=False),
        ]
        for i, d in enumerate(digis):
            addrs.append(Address(d.callsign, d.ssid, last=(i == len(digis) - 1)))
        out = bytearray()
        for a in addrs:
            out += a.encode()
        out.append(CONTROL_UI)
        out.append(self.pid)
        out += self.info
        return bytes(out)

    @classmethod
    def decode(cls, raw: bytes) -> "UIFrame":
        # Address fields are 7 bytes each, terminated by extension bit.
        i = 0
        addrs: list[Address] = []
        while i + 7 <= len(raw):
            chunk = raw[i : i + 7]
            i += 7
            a = Address.decode(chunk)
            addrs.append(a)
            if a.last:
                break
        if len(addrs) < 2:
            raise ValueError("AX.25 frame missing destination/source")
        dest, src, *digis = addrs
        if i >= len(raw):
            raise ValueError("AX.25 frame truncated before control byte")
        control = raw[i]
        i += 1
        if control != CONTROL_UI:
            raise ValueError(f"not a UI frame (control={control:#04x})")
        pid = raw[i]
        i += 1
        info = raw[i:]
        return cls(destination=dest, source=src, info=info, pid=pid, digipeaters=digis)


def encode_iframe_control(ns: int, nr: int, *, poll: bool = False, modulo: int = 8) -> int:
    """Build the control field for an I-frame.

    Returns an 8-bit int for modulo-8, or a 16-bit int for modulo-128
    (extended sequence numbers, AX.25 v2.2 §4.2.4). The low byte is the
    first wire byte; callers serialise as
    ``ctrl.to_bytes(1 if modulo==8 else 2, 'little')``.

    Modulo-8 layout (1 byte):  ``N(R)<<5 | P/F<<4 | N(S)<<1``.
    Modulo-128 layout (2 bytes, LSB first): byte 1 = ``N(S)<<1`` (bit 0 =
    0 marks I-frame); byte 2 = ``N(R)<<1 | P/F``.
    """
    if modulo == 8:
        if not (0 <= ns < 8 and 0 <= nr < 8):
            raise ValueError("N(S) / N(R) out of range for modulo-8")
        p = 1 if poll else 0
        return (nr << 5) | (p << 4) | (ns << 1)
    if modulo == 128:
        if not (0 <= ns < 128 and 0 <= nr < 128):
            raise ValueError("N(S) / N(R) out of range for modulo-128")
        byte1 = (ns & 0x7F) << 1  # bit 0 = 0 (I-frame)
        byte2 = ((nr & 0x7F) << 1) | (1 if poll else 0)
        return (byte2 << 8) | byte1
    raise ValueError(f"unsupported modulo: {modulo!r}")


def encode_sframe_control(s_type: int, nr: int, *, poll: bool = False, modulo: int = 8) -> int:
    """Build the control field for an S-frame (RR/RNR/REJ/SREJ).

    ``s_type`` is the low-byte sub-code constant (``0x01``/``0x05``/
    ``0x09``/``0x0D``) — i.e. ``SS<<2 | 01`` already baked in. Returns an
    8-bit int for modulo-8 or a 16-bit int for modulo-128.

    Modulo-8: ``N(R)<<5 | P/F<<4 | s_type``.
    Modulo-128: byte 1 = ``s_type`` (high nybble reserved zero); byte 2 =
    ``N(R)<<1 | P/F``.
    """
    if modulo == 8:
        if not 0 <= nr < 8:
            raise ValueError("N(R) out of range for modulo-8")
        p = 1 if poll else 0
        return (nr << 5) | (p << 4) | s_type
    if modulo == 128:
        if not 0 <= nr < 128:
            raise ValueError("N(R) out of range for modulo-128")
        byte1 = s_type & 0x0F
        byte2 = ((nr & 0x7F) << 1) | (1 if poll else 0)
        return (byte2 << 8) | byte1
    raise ValueError(f"unsupported modulo: {modulo!r}")
