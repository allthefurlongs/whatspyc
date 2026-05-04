"""KISS over a serial port.

``KissSerialUI`` is the unconnected (UI-frame-only) byte stream — useful
for protocols that ride on top of unnumbered information frames.

``connect_stream`` wraps ``KissSerialUI`` in the AX.25 L2 state machine
(``transport.ax25_l2.Ax25L2Stream``), giving a reliable connected-mode
byte stream suitable for WPS.
"""

from __future__ import annotations

import asyncio

import serial_asyncio

from whatspyc.transport.ax25_l2 import Ax25L2Stream
from whatspyc.transport.base import AsyncByteStream
from whatspyc.transport.kiss_frame import (
    CMD_DATA,
    CMD_DATA_ACK,
    FrameDecoder,
    encode_frame,
)


class KissSerialUI(AsyncByteStream):
    """KISS-over-serial transport for *unconnected* UI traffic only.

    With ``ackmode=True`` outbound data frames use the KISS ACKMODE
    extension (command nybble 0x0C) so the TNC echoes a synthetic ACK
    once the frame is actually on-air. ``ack_for(ack_id)`` resolves when
    the matching ACK arrives. Many TNCs don't support this — leave
    ``ackmode=False`` (the default) for them.
    """

    def __init__(
        self,
        device: str,
        baud: int = 9600,
        *,
        kiss_port: int = 0,
        ackmode: bool = False,
    ) -> None:
        self._device = device
        self._baud = baud
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
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._device, baudrate=self._baud
        )
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def send(self, data: bytes) -> int | None:
        """Send ``data`` as a KISS data frame.

        Returns the ack id when ``ackmode`` is on, else ``None``. Callers
        wanting to wait for the on-air ACK should pass the returned id to
        :meth:`ack_for`.
        """
        assert self._writer is not None
        if self._ackmode:
            ack_id = self._next_ack_id
            self._next_ack_id = (self._next_ack_id + 1) & 0xFFFF
            if self._next_ack_id == 0:  # 0 reserved as "no ack"
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
        """Block until the TNC ACKs the frame with ``ack_id``."""
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
    device: str,
    baud: int,
    my_call: str,
    remote: str,
    *,
    kiss_port: int = 0,
    ackmode: bool = False,
    digipeaters: list[str] | None = None,
    **l2_kwargs,
) -> AsyncByteStream:
    """Build a connected-mode AX.25 byte stream over a KISS serial link.

    ``ackmode`` enables the KISS ACKMODE extension on the lower layer and
    tells the L2 to defer T1 until each I-frame is actually on-air.

    ``l2_kwargs`` are forwarded to ``Ax25L2Stream`` (e.g. ``t1``, ``t3``,
    ``n2``, ``window``, ``paclen``, ``connect_timeout``, ``modulo``,
    ``segmentation``).
    """
    lower = KissSerialUI(device, baud, kiss_port=kiss_port, ackmode=ackmode)
    return Ax25L2Stream(
        lower,
        my_call=my_call,
        remote_call=remote,
        ackmode=ackmode,
        digipeaters=list(digipeaters or []),
        **l2_kwargs,
    )
