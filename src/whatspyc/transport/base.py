"""Transport abstraction.

A transport hides whichever wire protocol carries WPS bytes — RHP-over-TCP,
RHP-over-WebSocket, or raw KISS — and presents a clean async byte-stream
interface to ``WpsClient``.
"""

from __future__ import annotations

import abc


class AsyncByteStream(abc.ABC):
    """An ordered, reliable byte stream of WPS application payloads.

    Each ``send`` writes one chunk of WPS bytes (the codec produces
    ``\\r\\n``-terminated frames). Each ``recv`` returns whatever bytes the
    transport has available; the codec layer reassembles frames from the
    concatenation.
    """

    @abc.abstractmethod
    async def open(self) -> None:
        """Establish the underlying transport (connect TCP/WS, send AUTH/OPEN
        for RHP, set up KISS port, etc.)."""

    @abc.abstractmethod
    async def send(self, data: bytes) -> None:
        """Write WPS bytes."""

    @abc.abstractmethod
    async def recv(self) -> bytes:
        """Read the next chunk of WPS bytes. Returns ``b""`` on clean close."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Tear down the transport."""

    @property
    def injects_callsign(self) -> bool:
        """True if the link already supplies WPS with the originating
        callsign — e.g. an AX.25 host node opens the WPS-facing TCP
        socket on our behalf and pre-sends the call. False (the default)
        means the link is passthrough and ``WpsClient`` must send the
        ``<CALL>\\r\\n`` line itself before the type-`c` JSON.
        """
        return False
