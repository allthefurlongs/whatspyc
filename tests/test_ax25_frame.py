"""AX.25 frame encode/decode tests (UI frame round trip)."""

from __future__ import annotations

import pytest

from whatspyc.transport import ax25_frame


def test_address_round_trip_with_ssid() -> None:
    a = ax25_frame.Address(callsign="M0ABC", ssid=7, last=True, command=False)
    encoded = a.encode()
    assert len(encoded) == 7
    decoded = ax25_frame.Address.decode(encoded)
    assert decoded.callsign == "M0ABC"
    assert decoded.ssid == 7
    assert decoded.last is True


def test_ui_frame_round_trip() -> None:
    frame = ax25_frame.UIFrame(
        destination=ax25_frame.Address("WPS"),
        source=ax25_frame.Address("M0ABC", ssid=7),
        info=b'{"t":"k"}',
    )
    raw = frame.encode()
    decoded = ax25_frame.UIFrame.decode(raw)
    assert decoded.destination.callsign == "WPS"
    assert decoded.source.callsign == "M0ABC"
    assert decoded.source.ssid == 7
    assert decoded.info == b'{"t":"k"}'


def test_ui_frame_with_digipeaters() -> None:
    frame = ax25_frame.UIFrame(
        destination=ax25_frame.Address("WPS"),
        source=ax25_frame.Address("M0ABC"),
        info=b"hello",
        digipeaters=[ax25_frame.Address("RELAY1"), ax25_frame.Address("RELAY2")],
    )
    raw = frame.encode()
    decoded = ax25_frame.UIFrame.decode(raw)
    assert decoded.info == b"hello"
    assert [d.callsign for d in (decoded.digipeaters or [])] == ["RELAY1", "RELAY2"]


def test_iframe_control_byte_modulo8() -> None:
    # I-frame: low bit 0, P/F bit, N(R)<<5, N(S)<<1
    ctrl = ax25_frame.encode_iframe_control(ns=3, nr=5, poll=True)
    assert ctrl & 0x01 == 0  # I-frame marker
    assert (ctrl >> 5) & 0x07 == 5
    assert (ctrl >> 1) & 0x07 == 3
    assert (ctrl >> 4) & 0x01 == 1


def test_iframe_control_byte_modulo128() -> None:
    """Modulo-128 I-frame control field: 2 bytes, LSB first.

    Byte 1: N(S)<<1 (bit 0 = 0 marks I-frame).
    Byte 2: N(R)<<1 | P/F.
    """
    ctrl = ax25_frame.encode_iframe_control(ns=42, nr=99, poll=True, modulo=128)
    assert 0 <= ctrl <= 0xFFFF
    byte1 = ctrl & 0xFF
    byte2 = (ctrl >> 8) & 0xFF
    assert byte1 & 0x01 == 0  # I-frame marker
    assert (byte1 >> 1) & 0x7F == 42  # N(S)
    assert byte2 & 0x01 == 1  # P/F
    assert (byte2 >> 1) & 0x7F == 99  # N(R)


def test_iframe_control_modulo128_range_validated() -> None:
    with pytest.raises(ValueError):
        ax25_frame.encode_iframe_control(ns=128, nr=0, modulo=128)
    with pytest.raises(ValueError):
        ax25_frame.encode_iframe_control(ns=0, nr=128, modulo=128)


def test_sframe_control_modulo8() -> None:
    # RR with N(R)=5, P/F=0, modulo=8.
    ctrl = ax25_frame.encode_sframe_control(0x01, nr=5, poll=False, modulo=8)
    assert ctrl == (5 << 5) | 0x01


def test_sframe_control_modulo128() -> None:
    # SREJ (0x0D) with N(R)=70, P/F=1, modulo=128.
    ctrl = ax25_frame.encode_sframe_control(0x0D, nr=70, poll=True, modulo=128)
    byte1 = ctrl & 0xFF
    byte2 = (ctrl >> 8) & 0xFF
    assert byte1 == 0x0D
    assert byte2 == (70 << 1) | 1
