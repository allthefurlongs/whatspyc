"""RHP v2 over WebSocket.

Endpoint is ``ws://{host}:{port}/rhp``. Each WS frame carries one RHP JSON
message — no length prefix.

Wire shape matches the production web client byte-for-byte: compact JSON
(no whitespace separators) and an ``Origin: <scheme>://<host>:<port>``
upgrade header. Some RHP server stacks (notably BPQ on certain
configurations) silently drop messages from connections that don't carry
an Origin or that arrive with non-canonical JSON.

WS-level Ping/Pong is disabled (``ping_interval=None``). The browser
``WebSocket`` API doesn't expose Pings to JS, so the production web
client never sends them — meaning RHP servers aren't required to handle
them, and at least some don't. Liveness is covered by the application-
level ``{"t":"k"}`` keep-alive that ``WpsClient`` already drives.

``close_timeout`` is dropped to 1s (default 10s). BPQ's RHP-WS server
doesn't reply to WebSocket close frames, so the library would otherwise
sit on the full 10s on every ``/quit`` waiting for a close ack that
never arrives. The application-level RHP ``close`` we send first already
tells BPQ to drop the AX.25 link; the WS close handshake is purely
graceful-TCP nicety.
"""

from __future__ import annotations

import json

import websockets

from whatspyc.transport.base import AsyncByteStream
from whatspyc.transport.rhp_session import RhpConfig, RhpSession


class RhpWebSocketStream(AsyncByteStream):
    def __init__(self, host: str, port: int, cfg: RhpConfig, *, scheme: str = "ws") -> None:
        self._scheme = scheme
        self._host = host
        self._port = port
        self._url = f"{scheme}://{host}:{port}/rhp"
        self._cfg = cfg
        self._ws = None
        self._session: RhpSession | None = None

    @property
    def injects_callsign(self) -> bool:
        return True

    async def open(self) -> None:
        origin = f"{self._scheme}://{self._host}:{self._port}"
        self._ws = await websockets.connect(
            self._url,
            additional_headers={"Origin": origin},
            ping_interval=None,
            close_timeout=1,
        )
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
        if self._ws is not None:
            await self._ws.close()

    async def _send_message(self, obj: dict) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(obj, separators=(",", ":")))

    async def _recv_message(self) -> dict:
        assert self._ws is not None
        frame = await self._ws.recv()
        if isinstance(frame, bytes):
            frame = frame.decode("utf-8")
        return json.loads(frame)
