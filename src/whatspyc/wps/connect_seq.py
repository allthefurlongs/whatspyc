"""Connect-sequence orchestrator.

After type-`c`, the server triggers a chain of follow-ups (``mb``, ``cpb``,
``memb``, ``medb``, ``cpedb``, ``cpemb``, ``pch``, ``u``, ``he``, ``o``,
``cu``-driven unpause, etc.). Most of those land in handlers registered on
``WpsClient`` and persist into the local store automatically. This helper
exists so call sites can ``await`` until the connect sequence appears to
have completed (best-effort) and decide what to do about paused channels.

We use a quiescence heuristic: once the server has been silent for
``idle_after`` seconds *and* we've seen at least one type-`c` reply, consider
the sequence settled. Same approach the web client takes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class ConnectSummary:
    server_message_count: int = 0
    server_post_count: int = 0
    welcome: bool = False
    paused_channels: list[dict] = None  # type: ignore[assignment]
    online_users: list[str] = None  # type: ignore[assignment]


class ConnectSequence:
    def __init__(self, *, idle_after: float = 3.0) -> None:
        self._idle_after = idle_after
        self._summary = ConnectSummary(paused_channels=[], online_users=[])
        self._got_c_reply = asyncio.Event()
        self._last_event = asyncio.Event()
        self._loop_task: asyncio.Task | None = None

    async def on_event(self, obj: dict) -> None:
        t = obj.get("t")
        if t == "c" and "n" not in obj:
            self._summary.server_message_count = obj.get("mc", 0)
            self._summary.server_post_count = obj.get("pc", 0)
            self._summary.welcome = bool(obj.get("w", 0))
            self._got_c_reply.set()
        elif t == "pch":
            self._summary.paused_channels = list(obj.get("ch", []))
        elif t == "o":
            self._summary.online_users = list(obj.get("o", []))
        self._last_event.set()
        self._last_event.clear()

    async def wait(self, timeout: float | None = None) -> ConnectSummary:
        """Wait for the server's type-`c` reply, then settle once the link
        goes quiet. Returns whatever we observed, even on timeout.

        Default is no timeout — packet networks can be very slow and the
        web client doesn't bound this either. Tests pass an explicit
        ``timeout`` to keep themselves fast. ``idle_after`` (set in the
        constructor) is the quiescence detector, not a failure timeout."""
        try:
            await asyncio.wait_for(self._got_c_reply.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return self._summary
        # Quiescence: keep waiting while events keep arriving.
        while True:
            try:
                await asyncio.wait_for(self._last_event.wait(), timeout=self._idle_after)
            except asyncio.TimeoutError:
                return self._summary
