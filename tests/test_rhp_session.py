"""RHP session test using a fake send/recv pair.

We simulate the server side by exchanging dicts directly with the session,
verifying the open / send / recv / close handshake without a real socket.
"""

from __future__ import annotations

import asyncio

import pytest

from whatspyc.transport.rhp_session import RhpConfig, RhpSession


@pytest.mark.asyncio
async def test_open_send_recv_close_round_trip() -> None:
    out_q: asyncio.Queue = asyncio.Queue()
    in_q: asyncio.Queue = asyncio.Queue()

    async def send_message(obj):
        await out_q.put(obj)

    async def recv_message():
        return await in_q.get()

    cfg = RhpConfig(pfam="ax25", port=1, local="M0ABC", remote="WPS")
    session = RhpSession(cfg, send_message, recv_message)

    # Drive the open handshake from a parallel task — observe what the
    # session emits and reply on cue.
    async def fake_server() -> None:
        opened = await out_q.get()
        assert opened["type"] == "open"
        await in_q.put({"type": "openReply", "id": opened["id"], "handle": 7, "errcode": 0})
        # Status connected
        await in_q.put({"type": "status", "handle": 7, "flags": 2})
        # Then handle one send
        sent = await out_q.get()
        assert sent["type"] == "send"
        assert sent["handle"] == 7
        assert sent["data"] == "hello"
        await in_q.put({"type": "sendReply", "id": sent["id"], "handle": 7, "errcode": 0})
        # Push one recv into the inbox
        await in_q.put({"type": "recv", "handle": 7, "data": "world"})
        # Then a close
        closed = await out_q.get()
        assert closed["type"] == "close"

    server_task = asyncio.create_task(fake_server())
    await session.open()
    await session.send(b"hello")
    received = await session.recv()
    assert received == b"world"
    await session.close()
    await server_task


@pytest.mark.asyncio
async def test_open_serialises_port_as_string() -> None:
    """Browser web client sends `port` as a JSON string ("1") rather than a
    number. Some BPQ builds silently drop OPENs that arrive as a number,
    so we always coerce to string regardless of engine."""
    out_q: asyncio.Queue = asyncio.Queue()
    in_q: asyncio.Queue = asyncio.Queue()

    async def send_message(obj):
        await out_q.put(obj)

    async def recv_message():
        return await in_q.get()

    cfg = RhpConfig(pfam="ax25", port=1, local="M0ABC", remote="WPS")
    session = RhpSession(cfg, send_message, recv_message)

    async def fake_server() -> None:
        opened = await out_q.get()
        assert opened["port"] == "1"
        assert opened["remote"] == "WPS"
        await in_q.put({"type": "openReply", "id": opened["id"], "handle": 1, "errcode": 0})
        await in_q.put({"type": "status", "handle": 1, "flags": 2})

    asyncio.create_task(fake_server())
    await session.open()
    await session.close()


@pytest.mark.asyncio
async def test_open_omits_port_when_none() -> None:
    """If RhpConfig.port is None the field is dropped entirely. This is
    not the canonical shape for either engine — both XRouter and BPQ
    expect `port` present — but the omission path exists for non-standard
    setups and is exercised by `engine="custom"` users."""
    out_q: asyncio.Queue = asyncio.Queue()
    in_q: asyncio.Queue = asyncio.Queue()

    async def send_message(obj):
        await out_q.put(obj)

    async def recv_message():
        return await in_q.get()

    cfg = RhpConfig(pfam="ax25", local="M0ABC", remote="SWITCH")
    session = RhpSession(cfg, send_message, recv_message)

    async def fake_server() -> None:
        opened = await out_q.get()
        assert "port" not in opened
        assert opened["remote"] == "SWITCH"
        await in_q.put({"type": "openReply", "id": opened["id"], "handle": 2, "errcode": 0})
        await in_q.put({"type": "status", "handle": 2, "flags": 2})

    asyncio.create_task(fake_server())
    await session.open()
    await session.close()


@pytest.mark.asyncio
async def test_send_strips_trailing_lf_from_crlf_terminator() -> None:
    """RhpSession.send terminates WPS frames with bare ``\\r``, matching
    the web client. BPQ's RHP server miscodes the JSON-escaped ``\\n`` in
    the ``data`` field — it forwards a stray ``\\`` byte after the CR LF,
    contaminating the next frame on the WPS-facing socket. The node adds
    the LF itself so WPS still sees a proper ``\\r\\n`` terminator."""
    out_q: asyncio.Queue = asyncio.Queue()
    in_q: asyncio.Queue = asyncio.Queue()

    async def send_message(obj):
        await out_q.put(obj)

    async def recv_message():
        return await in_q.get()

    cfg = RhpConfig(pfam="ax25", port=1, local="M0ABC", remote="WPS")
    session = RhpSession(cfg, send_message, recv_message)

    async def fake_server() -> None:
        opened = await out_q.get()
        await in_q.put({"type": "openReply", "id": opened["id"], "handle": 1, "errcode": 0})
        await in_q.put({"type": "status", "handle": 1, "flags": 2})
        sent = await out_q.get()
        # CR LF in the input becomes bare CR on the wire.
        assert sent["data"] == '{"t":"k"}\r'
        await in_q.put({"type": "sendReply", "id": sent["id"], "handle": 1, "errcode": 0})
        # Sole CR (already-stripped form) goes through unchanged.
        sent = await out_q.get()
        assert sent["data"] == '{"t":"k"}\r'
        await in_q.put({"type": "sendReply", "id": sent["id"], "handle": 1, "errcode": 0})
        # Payload without any terminator is left alone.
        sent = await out_q.get()
        assert sent["data"] == "raw"
        await in_q.put({"type": "sendReply", "id": sent["id"], "handle": 1, "errcode": 0})

    asyncio.create_task(fake_server())
    await session.open()
    await session.send(b'{"t":"k"}\r\n')
    await session.send(b'{"t":"k"}\r')
    await session.send(b"raw")


@pytest.mark.asyncio
async def test_open_failure_raises() -> None:
    out_q: asyncio.Queue = asyncio.Queue()
    in_q: asyncio.Queue = asyncio.Queue()

    async def send_message(obj):
        await out_q.put(obj)

    async def recv_message():
        return await in_q.get()

    cfg = RhpConfig(pfam="ax25", port=1, local="M0ABC", remote="WPS")
    session = RhpSession(cfg, send_message, recv_message)

    async def fake_server() -> None:
        opened = await out_q.get()
        await in_q.put(
            {"type": "openReply", "id": opened["id"], "handle": 0, "errcode": 7, "errtext": "Invalid remote"}
        )

    asyncio.create_task(fake_server())
    with pytest.raises(RuntimeError, match="Invalid remote"):
        await session.open()


@pytest.mark.asyncio
async def test_open_returns_without_status_connected() -> None:
    """`openReply` with `errcode=0` is the readiness signal — match the
    web client and don't block on a follow-up STATUS connected. XRouter's
    ax25/stream socket to a local service (WPS) reliably delivers the
    `openReply` but doesn't always emit a STATUS afterwards; waiting
    for one hangs the connect indefinitely."""
    out_q: asyncio.Queue = asyncio.Queue()
    in_q: asyncio.Queue = asyncio.Queue()

    async def send_message(obj):
        await out_q.put(obj)

    async def recv_message():
        return await in_q.get()

    cfg = RhpConfig(pfam="ax25", port=1, local="M0ABC", remote="WPS")
    session = RhpSession(cfg, send_message, recv_message)

    async def fake_server() -> None:
        opened = await out_q.get()
        await in_q.put({"type": "openReply", "id": opened["id"], "handle": 4, "errcode": 0})
        # Deliberately no `status` frame.

    asyncio.create_task(fake_server())
    await asyncio.wait_for(session.open(), timeout=1.0)
    await session.close()
