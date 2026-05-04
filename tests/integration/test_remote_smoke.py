"""End-to-end smoke test against a real WhatsPac/RHP node.

Gated on ``WHATSPYC_INTEGRATION_HOST=<host>[:<port>]``. Optional env vars:

* ``WHATSPYC_INTEGRATION_TRANSPORT`` — ``rhp-ws`` (default) or ``rhp-tcp``.
* ``WHATSPYC_INTEGRATION_CALL`` — callsign to register (default ``TEST-1``).
* ``WHATSPYC_INTEGRATION_RADIO_PORT`` — XRouter radio port (default 1).
* ``WHATSPYC_INTEGRATION_REMOTE`` — service callsign (default ``WPS``).

Verifies the handshake completes and a clean close.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest


_HOST = os.environ.get("WHATSPYC_INTEGRATION_HOST")
pytestmark = pytest.mark.skipif(
    not _HOST,
    reason="set WHATSPYC_INTEGRATION_HOST=<host>[:<port>] to run integration tests",
)


@pytest.mark.asyncio
async def test_connect_handshake_clean_close(tmp_path: Path) -> None:
    from whatspyc.store.store import SqliteStore
    from whatspyc.transport.rhp_session import RhpConfig
    from whatspyc.transport.rhp_tcp import RhpTcpStream
    from whatspyc.transport.rhp_ws import RhpWebSocketStream
    from whatspyc.wps.client import WpsClient
    from whatspyc.wps.connect_seq import ConnectSequence

    transport = os.environ.get("WHATSPYC_INTEGRATION_TRANSPORT", "rhp-ws")
    my_call = os.environ.get("WHATSPYC_INTEGRATION_CALL", "TEST-1")
    radio_port = int(os.environ.get("WHATSPYC_INTEGRATION_RADIO_PORT", "1"))
    remote = os.environ.get("WHATSPYC_INTEGRATION_REMOTE", "WPS")

    host, _, port_s = (_HOST or "").partition(":")
    port = int(port_s) if port_s else (8086 if transport == "rhp-ws" else 9000)

    rhp_cfg = RhpConfig(
        pfam="ax25", port=radio_port, local=my_call, remote=remote, flags=0x80
    )
    store = SqliteStore(tmp_path / "state.sqlite3")

    def factory():
        if transport == "rhp-ws":
            return RhpWebSocketStream(host, port, rhp_cfg)
        return RhpTcpStream(host, port, rhp_cfg)

    seq = ConnectSequence(idle_after=2.0)

    async def hook(obj: dict) -> None:
        await seq.on_event(obj)

    client = WpsClient(
        factory,
        store,
        my_call=my_call,
        name="whatspyc-integration-test",
        on_event=hook,
        keepalive_interval=None,
        auto_reconnect=False,
    )

    try:
        await client.open()
        summary = await asyncio.wait_for(seq.wait(), 30.0)
        assert summary is not None
        # mc/pc are uint counters; just sanity-check they're non-negative ints.
        assert summary.server_message_count >= 0
        assert summary.server_post_count >= 0
    finally:
        await client.close()
        store.close()
