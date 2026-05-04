"""KISS over a TCP connection (Direwolf / QtSoundModem default :8001).

Same shape as ``kiss_serial`` but uses ``asyncio.open_connection`` instead
of pyserial-asyncio. ``connect_stream`` wraps the UI carrier in the
``transport.ax25_l2.Ax25L2Stream`` state machine for connected-mode use.
"""

from __future__ import annotations

import asyncio

from whatspyc.transport.ax25_l2 import Ax25L2Stream
from whatspyc.transport.base import AsyncByteStream
from whatspyc.transport.kiss_frame import (
    CMD_DATA,
    CMD_DATA_ACK,
    FrameDecoder,
    encode_frame,
)


class KissTcpUI(AsyncByteStream):
    """KISS-over-TCP transport for *unconnected* UI traffic.

    See ``KissSerialUI`` for the ``ackmode`` semantics — the TCP variant
    is identical apart from the underlying carrier.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8001,
        *,
        kiss_port: int = 0,
        ackmode: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._kiss_port = kiss_port
        self._ackmode = ackmode
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._decoder = FrameDecoder()
        self._inbox: asyncio.Queue[bytes] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._next_ack_id = 1
        self._ack_waiters: dict[int, asyncio.Future[None]] = {}

    @property
    def ackmode(self) -> bool:
        return self._ackmode

    async def open(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def send(self, data: bytes) -> int | None:
        assert self._writer is not None
        if self._ackmode:
            ack_id = self._next_ack_id
            self._next_ack_id = (self._next_ack_id + 1) & 0xFFFF
            if self._next_ack_id == 0:
                self._next_ack_id = 1
            self._ack_waiters[ack_id] = asyncio.get_event_loop().create_future()
            self._writer.write(
                encode_frame(
                    data,
                    port=self._kiss_port,
                    command=CMD_DATA_ACK,
                    ack_id=ack_id,
                )
            )
            await self._writer.drain()
            return ack_id
        self._writer.write(encode_frame(data, port=self._kiss_port))
        await self._writer.drain()
        return None

    async def ack_for(self, ack_id: int) -> None:
        fut = self._ack_waiters.get(ack_id)
        if fut is None:
            return
        try:
            await fut
        finally:
            self._ack_waiters.pop(ack_id, None)

    async def recv(self) -> bytes:
        return await self._inbox.get()

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        for fut in self._ack_waiters.values():
            if not fut.done():
                fut.cancel()
        self._ack_waiters.clear()

    async def _reader_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    return
                for _port, command, payload in self._decoder.feed(chunk):
                    if command == CMD_DATA_ACK and len(payload) == 2:
                        ack_id = int.from_bytes(payload, "big")
                        fut = self._ack_waiters.get(ack_id)
                        if fut is not None and not fut.done():
                            fut.set_result(None)
                        continue
                    if command == CMD_DATA:
                        await self._inbox.put(payload)
        except asyncio.CancelledError:
            raise


def connect_stream(
    host: str,
    port: int,
    my_call: str,
    remote: str,
    *,
    kiss_port: int = 0,
    ackmode: bool = False,
    digipeaters: list[str] | None = None,
    **l2_kwargs,
) -> AsyncByteStream:
    """Build a connected-mode AX.25 byte stream over a KISS TCP link.

    ``l2_kwargs`` are forwarded to ``Ax25L2Stream``.
    """
    lower = KissTcpUI(host, port, kiss_port=kiss_port, ackmode=ackmode)
    return Ax25L2Stream(
        lower,
        my_call=my_call,
        remote_call=remote,
        ackmode=ackmode,
        digipeaters=list(digipeaters or []),
        **l2_kwargs,
    )
