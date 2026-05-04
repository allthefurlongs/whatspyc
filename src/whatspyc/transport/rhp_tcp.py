"""RHP v2 over a raw TCP connection.

Each JSON message on the wire is preceded by a 2-byte big-endian length.
"""

from __future__ import annotations

import asyncio
import json

from whatspyc.transport.base import AsyncByteStream
from whatspyc.transport.rhp_session import RhpConfig, RhpSession


class RhpTcpStream(AsyncByteStream):
    def __init__(self, host: str, port: int, cfg: RhpConfig) -> None:
        self._host = host
        self._port = port
        self._cfg = cfg
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._session: RhpSession | None = None
        self._send_lock = asyncio.Lock()

    @property
    def injects_callsign(self) -> bool:
        return True

    async def open(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        self._session = RhpSession(self._cfg, self._send_message, self._recv_message)
        await self._session.open()

    async def send(self, data: bytes) -> None:
        assert self._session is not None
        await self._session.send(data)

    async def recv(self) -> bytes:
        assert self._session is not None
        return await self._session.recv()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    async def _send_message(self, obj: dict) -> None:
        assert self._writer is not None
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        if len(body) > 0xFFFF:
            raise ValueError("RHP message exceeds 65535 bytes")
        async with self._send_lock:
            self._writer.write(len(body).to_bytes(2, "big") + body)
            await self._writer.drain()

    async def _recv_message(self) -> dict:
        assert self._reader is not None
        header = await self._reader.readexactly(2)
        length = int.from_bytes(header, "big")
        body = await self._reader.readexactly(length)
        return json.loads(body.decode("utf-8"))
