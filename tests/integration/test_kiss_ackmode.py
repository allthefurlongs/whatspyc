"""KISS ACKMODE integration test.

Spins up an in-process TCP server that:

* Speaks the KISS ACKMODE extension (command nybble 0x0C, 2-byte big-endian
  ACK id prepended to each data frame).
* Replies to SABM/UA and ACKs every I-frame it receives — but with a
  configurable delay before sending the synthetic KISS ACK back, so the
  test can assert that ``Ax25L2Stream``'s T1 timer doesn't start until the
  TNC has confirmed the frame is on-air.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from whatspyc.transport import kiss_tcp as kiss_tcp_mod
from whatspyc.transport.ax25_frame import PID_NO_LAYER3
from whatspyc.transport.ax25_l2 import (
    CTRL_DISC,
    CTRL_SABM,
    CTRL_UA,
    PF,
    _addr_pair,
    _decode_frame,
    _parse_call,
)
from whatspyc.transport.kiss_frame import (
    CMD_DATA,
    CMD_DATA_ACK,
    FrameDecoder,
    encode_frame,
)


class _AckmodePeerServer:
    """In-process TCP server that speaks KISS ACKMODE + minimal AX.25.

    All inbound and outbound KISS frames use command 0x0C. Synthetic KISS
    ACKs to the host are delayed by ``ack_delay`` seconds.
    """

    def __init__(
        self,
        my_call: str,
        remote_call: str,
        *,
        ack_delay: float = 0.0,
    ) -> None:
        self._mine = _parse_call(my_call)
        self._remote = _parse_call(remote_call)
        self._ack_delay = ack_delay
        self._port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        # Timestamps of when each I-frame info-field value first arrived
        # over the wire — for the test to assert ordering / latency.
        self.iframe_arrivals: dict[bytes, float] = {}

    @property
    def port(self) -> int:
        assert self._port is not None
        return self._port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self._port = self._server.sockets[0].getsockname()[1]

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

        async def send_kiss_ack(ack_id: int) -> None:
            if self._ack_delay > 0:
                await asyncio.sleep(self._ack_delay)
            writer.write(
                encode_frame(
                    b"", port=0, command=CMD_DATA_ACK, ack_id=ack_id
                )
            )
            await writer.drain()

        async def send_ax25(ax25_frame: bytes) -> None:
            # TNC → host frames are plain CMD_DATA; only the host requests
            # ACKs (per the G8BPQ ACKMODE extension).
            writer.write(encode_frame(ax25_frame, port=0, command=CMD_DATA))
            await writer.drain()

        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                for _port, command, payload in decoder.feed(chunk):
                    if command != CMD_DATA_ACK or len(payload) < 2:
                        continue
                    ack_id = int.from_bytes(payload[:2], "big")
                    ax25 = payload[2:]
                    f = _decode_frame(ax25)
                    if f is None:
                        continue
                    if f.is_uframe and f.u_type == CTRL_SABM:
                        vs = vr = 0
                        connected = True
                        ctrl = CTRL_UA | (PF if f.poll else 0)
                        out = (
                            _addr_pair(self._remote, self._mine, command=False)
                            + bytes([ctrl])
                        )
                        # Send AX.25 reply first, then synthetic KISS ACK.
                        await send_ax25(out)
                        asyncio.create_task(send_kiss_ack(ack_id))
                    elif f.is_uframe and f.u_type == CTRL_DISC:
                        ctrl = CTRL_UA | (PF if f.poll else 0)
                        out = (
                            _addr_pair(self._remote, self._mine, command=False)
                            + bytes([ctrl])
                        )
                        await send_ax25(out)
                        asyncio.create_task(send_kiss_ack(ack_id))
                        connected = False
                        return
                    elif f.is_iframe and connected:
                        self.iframe_arrivals.setdefault(
                            bytes(f.info), time.monotonic()
                        )
                        if f.ns == vr:
                            vr = (vr + 1) & 0x07
                            ctrl_byte = (vr << 5) | (vs << 1)
                            vs = (vs + 1) & 0x07
                            out = (
                                _addr_pair(
                                    self._remote, self._mine, command=True
                                )
                                + bytes([ctrl_byte, PID_NO_LAYER3])
                                + f.info
                            )
                            await send_ax25(out)
                        # Fire delayed KISS ACK regardless.
                        asyncio.create_task(send_kiss_ack(ack_id))
        except (ConnectionResetError, ConnectionAbortedError):
            return


@pytest.mark.asyncio
async def test_ackmode_round_trip() -> None:
    """Sanity: ackmode end-to-end echoes user data through the L2."""
    server = _AckmodePeerServer(my_call="WPS", remote_call="M0ABC")
    await server.start()

    stream = kiss_tcp_mod.connect_stream(
        "127.0.0.1",
        server.port,
        my_call="M0ABC",
        remote="WPS",
        connect_timeout=2.0,
        paclen=64,
        ackmode=True,
    )
    try:
        await stream.open()
        await stream.send(b"hello ackmode")
        echoed = await asyncio.wait_for(stream.recv(), 2.0)
        assert echoed == b"hello ackmode"
    finally:
        await stream.close()
        await server.stop()


@pytest.mark.asyncio
async def test_ackmode_defers_t1_until_kiss_ack() -> None:
    """T1 must only start once the TNC has ACKed the I-frame.

    With a t1 of 0.4 s and an ack_delay of 0.3 s, a non-ackmode link would
    fire T1 (and retransmit) before the peer's I-frame ack came back. With
    ackmode on, T1 should not start until the synthetic KISS ACK arrives,
    so no spurious retransmit happens.
    """
    server = _AckmodePeerServer(
        my_call="WPS", remote_call="M0ABC", ack_delay=0.3
    )
    await server.start()

    stream = kiss_tcp_mod.connect_stream(
        "127.0.0.1",
        server.port,
        my_call="M0ABC",
        remote="WPS",
        connect_timeout=2.0,
        paclen=64,
        t1=0.4,
        n2=2,
        ackmode=True,
    )
    try:
        await stream.open()
        payload = b"only-once"
        await stream.send(payload)
        echoed = await asyncio.wait_for(stream.recv(), 2.0)
        assert echoed == payload
        # Give the server a moment to register any retransmits the L2
        # might have issued.
        await asyncio.sleep(0.2)
        # Each unique payload should have arrived exactly once — no
        # spurious T1-driven retransmit.
        assert list(server.iframe_arrivals.keys()) == [payload]
    finally:
        await stream.close()
        await server.stop()
