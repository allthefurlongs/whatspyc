"""Messages round-trip + parse dispatch."""

from __future__ import annotations

from whatspyc.wps import messages as msgs


def test_message_round_trip() -> None:
    m = msgs.Message(
        from_call="M0ABC",
        to_call="T3EST",
        message="hello",
        timestamp=1_700_000_000_000,
        msg_id="1-M0ABC",
    )
    d = m.to_dict()
    assert d == {
        "t": "m",
        "_id": "1-M0ABC",
        "fc": "M0ABC",
        "tc": "T3EST",
        "m": "hello",
        "ts": 1_700_000_000_000,
    }
    parsed = msgs.parse(d)
    assert isinstance(parsed, msgs.Message)
    assert parsed.message == "hello"


def test_server_only_fields_are_not_emitted() -> None:
    m = msgs.Message(
        from_call="M0ABC",
        to_call="T3EST",
        message="hi",
        timestamp=1,
        msg_status=99,  # server-only — must be dropped on send
        logged_ts=2,
    )
    d = m.to_dict()
    assert "ms" not in d
    assert "lts" not in d


def test_parse_disambiguates_connect_server_from_client() -> None:
    server_reply = {"t": "c", "mc": 5, "pc": 3, "v": 0.5}
    parsed = msgs.parse(server_reply)
    assert isinstance(parsed, msgs.ConnectServer)
    assert parsed.msg_count == 5


def test_parse_unknown_type_returns_raw() -> None:
    out = msgs.parse({"t": "?_unknown", "x": 1})
    assert out == {"t": "?_unknown", "x": 1}


def test_unpause_channel_uses_cu_not_uc() -> None:
    """Docs say `uc` for unpause; the server actually keys on `cu`."""
    u = msgs.UnpauseChannel(channel_id=6, post_count=50)
    d = u.to_dict()
    assert d["t"] == "cu"
    assert d["cid"] == 6
    assert d["pc"] == 50
