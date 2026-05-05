"""Shared RHP v2 message-level state machine.

Both ``rhp_tcp`` and ``rhp_ws`` use the same JSON message exchange
(``open`` / ``send`` / ``recv`` / ``close``); only the framing of those
JSON messages on the wire differs (2-byte length prefix vs. one-WS-frame-each).

This module owns:

* OPEN / OPENREPLY exchange to obtain the socket handle.
* SEND wrapping of outbound application bytes.
* RECV unwrapping of inbound application bytes (the ``data`` field of RECV).
* AUTH (optional) for non-LAN clients.
* CLOSE on shutdown.
* STATUS / SENDREPLY observation (mostly for logging in Phase 1).

The WPS application data flows through ``data`` fields as JSON strings — RHP
JSON-escapes them so binary survives.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RhpConfig:
    """Configuration for opening one ax25/netrom STREAM socket via RHP."""

    pfam: str  # "ax25" or "netrom"
    local: str  # our callsign(-SSID)
    remote: str  # service callsign — typically "WPS" (Xrouter) or "SWITCH" (BPQ)
    # Radio-port index. The wire field is a JSON string ("1") regardless of
    # engine — `RhpSession.open` does the str() conversion. Pass an int here.
    # Omitting (None) drops the field from the open entirely; some setups
    # work this way but it is not the canonical shape — both Xrouter and
    # BPQ expect `port` to be present.
    port: int | None = None
    flags: int = 0x80  # active open
    auth_user: str | None = None
    auth_pass: str | None = None


class RhpSession:
    """Drives one RHP socket. Wire-format-agnostic.

    The transport supplies two async callables:
      * ``send_message(obj)`` — wire-encode and send one RHP JSON message.
      * ``recv_message()`` — receive and JSON-decode the next RHP message.

    The session presents the application bytes as an async API.
    """

    def __init__(self, cfg: RhpConfig, send_message, recv_message) -> None:
        self._cfg = cfg
        self._send_message = send_message
        self._recv_message = recv_message
        self._handle: int | None = None
        self._closed = False
        self._id_iter = itertools.count(1)
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._open_reply: asyncio.Future | None = None

    async def open(self) -> None:
        if self._cfg.auth_user is not None:
            await self._send_message(
                {
                    "type": "auth",
                    "id": next(self._id_iter),
                    "user": self._cfg.auth_user,
                    "pass": self._cfg.auth_pass or "",
                }
            )
            reply = await self._recv_message()
            if reply.get("errCode", reply.get("errcode", 0)) != 0:
                raise RuntimeError(f"RHP auth failed: {reply}")

        open_id = next(self._id_iter)
        self._open_reply = asyncio.get_running_loop().create_future()
        self._reader_task = asyncio.create_task(self._reader_loop())

        open_msg = {
            "type": "open",
            "id": open_id,
            "pfam": self._cfg.pfam,
            "mode": "stream",
            "local": self._cfg.local,
            "remote": self._cfg.remote,
            "flags": self._cfg.flags,
        }
        if self._cfg.port is not None:
            # The web client sends `port` as a string ("1") rather than a
            # number — both XRouter and BPQ stacks expect this shape, and
            # at least some BPQ builds silently drop the OPEN if it
            # arrives as a JSON number. Match the wire format exactly.
            open_msg["port"] = str(self._cfg.port)
        await self._send_message(open_msg)
        reply = await self._open_reply
        err = reply.get("errcode", reply.get("errCode", 0))
        if err != 0:
            raise RuntimeError(f"RHP open failed: {reply}")
        self._handle = reply["handle"]
        # Don't wait for a STATUS connected — the web client doesn't, and
        # XRouter doesn't reliably emit one for ax25/stream sessions to a
        # local service like WPS (an `openReply` with `errcode=0` is the
        # readiness signal). Inbound STATUS frames, when they do arrive,
        # are still observed by the reader loop for diagnostics.

    async def send(self, data: bytes) -> None:
        if self._handle is None or self._closed:
            raise RuntimeError("RHP socket not open")
        # Match the web client's wire shape: terminate WPS frames with bare
        # `\r`, not `\r\n`. BPQ's RHP server miscodes the JSON-escaped `\n`
        # in the `data` field — it forwards a stray `\` byte after the CR
        # LF, which contaminates the next frame at the WPS socket and
        # makes WPS reject it as invalid JSON. The node adds the `\n`
        # itself so WPS still sees a `\r\n`-terminated buffer.
        if data.endswith(b"\r\n"):
            data = data[:-1]
        # Fire-and-forget: the web client doesn't wait for SENDREPLY either.
        # Errors land in the reader loop's logger; if the link is truly
        # broken, the WS/TCP layer will surface a close.
        await self._send_message(
            {
                "type": "send",
                "id": next(self._id_iter),
                "handle": self._handle,
                "data": data.decode("latin-1"),
            }
        )

    async def recv(self) -> bytes:
        item = await self._inbox.get()
        if item is None:
            return b""
        return item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._handle is not None:
            try:
                await self._send_message(
                    {
                        "type": "close",
                        "id": next(self._id_iter),
                        "handle": self._handle,
                    }
                )
            except Exception:
                pass
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _reader_loop(self) -> None:
        try:
            while not self._closed:
                msg = await self._recv_message()
                t = msg.get("type")
                if t == "openReply":
                    if self._open_reply is not None and not self._open_reply.done():
                        self._open_reply.set_result(msg)
                elif t == "status":
                    # Observed for diagnostics only — the web client
                    # routes STATUS frames on the WPS handle to the
                    # inbox and proceeds without gating readiness on
                    # them. flags & 2 means CONNECTED, flags & 4 means
                    # BUSY (not implemented here).
                    logger.debug("RHP status: %s", msg)
                elif t == "sendReply":
                    err = msg.get("errcode", msg.get("errCode", 0))
                    if err != 0:
                        logger.warning("RHP send error: %s", msg)
                elif t == "recv":
                    data = msg.get("data", "")
                    if isinstance(data, str):
                        data = data.encode("latin-1")
                    await self._inbox.put(data)
                elif t == "close":
                    await self._inbox.put(None)
                    return
                # else: closeReply, statusReply, accept — ignored in Phase 1
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._inbox.put(None)
