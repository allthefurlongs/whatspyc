"""Passthrough TCP transport — raw WPS bytes, no extra framing.

WPS itself listens on TCP 63001 by default (direct, no RF). This transport
is also the thing to point at the in-tree ``tests/fake_wps.py`` server for
offline UI smoke testing.

There is deliberately no RHP framing here: send writes go straight to the
socket and recv reads return whatever bytes are available. The WPS codec
layer does its own ``\\r\\n`` / compression framing on top.
"""

from __future__ import annotations

import asyncio

from whatspyc.transport.base import AsyncByteStream


class DirectTcpStream(AsyncByteStream):
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port
        )

    async def send(self, data: bytes) -> None:
        assert self._writer is not None
        self._writer.write(data)
        await self._writer.drain()

    async def recv(self) -> bytes:
        assert self._reader is not None
        try:
            return await self._reader.read(4096)
        except (ConnectionResetError, ConnectionAbortedError, OSError):
            return b""

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
