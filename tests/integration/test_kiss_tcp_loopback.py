"""KISS-TCP + AX.25 L2 wiring loopback.

Spins up an in-process TCP server that speaks just enough of KISS+AX.25 to
respond to SABM/UA and echo I-frames. Drives it via
``kiss_tcp.connect_stream`` to verify the whole stack — KISS framing,
AX.25 v2.2 connected mode, and the ``Ax25L2Stream`` byte-stream contract
— glues together end-to-end.

Always runs (no env gate) because the peer is in-process.
"""

from __future__ import annotations

import asyncio

import pytest

from whatspyc.transport import kiss_tcp as kiss_tcp_mod
from whatspyc.transport.ax25_frame import PID_NO_LAYER3
from whatspyc.transport.ax25_l2 import (
    CTRL_DISC,
    CTRL_SABM,
    CTRL_UA,
    PF,
    S_RR,
    _addr_pair,
    _decode_frame,
    _parse_call,
)
from whatspyc.transport.kiss_frame import FrameDecoder, encode_frame


class _PeerServer:
    """In-process TCP server that simulates an AX.25 peer over KISS."""

    def __init__(self, my_call: str, remote_call: str) -> None:
        # 'mine' is the peer's view of itself; 'remote' is the client we expect.
        self._mine = _parse_call(my_call)
        self._remote = _parse_call(remote_call)
        self._port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self.peer_received: list = []

    @property
    def port(self) -> int:
        assert self._port is not None
        return self._port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sock = self._server.sockets[0]
        self._port = sock.getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        decoder = FrameDecoder()
        vs = vr = 0
        connected = False
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                for _port, _cmd, payload in decoder.feed(chunk):
                    f = _decode_frame(payload)
                    if f is None:
                        continue
                    self.peer_received.append(f)
                    if f.is_uframe and f.u_type == CTRL_SABM:
                        vs = vr = 0
                        connected = True
                        ctrl = CTRL_UA | (PF if f.poll else 0)
                        out = (
                            _addr_pair(self._remote, self._mine, command=False)
                            + bytes([ctrl])
                        )
                        writer.write(encode_frame(out))
                        await writer.drain()
                    elif f.is_uframe and f.u_type == CTRL_DISC:
                        ctrl = CTRL_UA | (PF if f.poll else 0)
                        out = (
                            _addr_pair(self._remote, self._mine, command=False)
                            + bytes([ctrl])
                        )
                        writer.write(encode_frame(out))
                        await writer.drain()
                        connected = False
                        return
                    elif f.is_iframe and connected:
                        if f.ns == vr:
                            vr = (vr + 1) & 0x07
                            # Echo back as an I-frame; N(R) here acks f.ns.
                            ctrl = (vr << 5) | (vs << 1)
                            vs = (vs + 1) & 0x07
                            out = (
                                _addr_pair(self._remote, self._mine, command=True)
                                + bytes([ctrl, PID_NO_LAYER3])
                                + f.info
                            )
                            writer.write(encode_frame(out))
                            await writer.drain()
                        else:
                            ctrl_byte = (vr << 5) | S_RR
                            out = (
                                _addr_pair(self._remote, self._mine, command=False)
                                + bytes([ctrl_byte])
                            )
                            writer.write(encode_frame(out))
                            await writer.drain()
        except (ConnectionResetError, ConnectionAbortedError):
            return


@pytest.mark.asyncio
async def test_kiss_tcp_l2_round_trip() -> None:
    server = _PeerServer(my_call="WPS", remote_call="M0ABC")
    await server.start()

    stream = kiss_tcp_mod.connect_stream(
        "127.0.0.1",
        server.port,
        my_call="M0ABC",
        remote="WPS",
        connect_timeout=2.0,
        paclen=64,
    )
    try:
        await stream.open()
        await stream.send(b"hello over kiss")
        echoed = await asyncio.wait_for(stream.recv(), 2.0)
        assert echoed == b"hello over kiss"
    finally:
        await stream.close()
        await server.stop()
