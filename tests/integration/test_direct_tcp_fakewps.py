"""DirectTcpStream end-to-end against the fake WPS server.

Spins ``tests/fake_wps.py`` up in-process and connects via the real
``DirectTcpStream`` + ``WpsClient`` stack so the handshake, type-`c`
exchange, message ack, and subscribe/he replies all run for real.
Always runs (no env gate).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from whatspyc.store.store import SqliteStore
from whatspyc.transport.direct_tcp import DirectTcpStream
from whatspyc.wps.client import WpsClient
from whatspyc.wps.connect_seq import ConnectSequence

# The fake server module sits at tests/fake_wps.py
from tests.fake_wps import handle as fake_handle


@pytest.mark.asyncio
async def test_handshake_and_message_ack(tmp_path: Path) -> None:
    server = await asyncio.start_server(fake_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    serve_task = asyncio.create_task(server.serve_forever())

    store = SqliteStore(tmp_path / "state.sqlite3")
    seq = ConnectSequence(idle_after=0.2)

    events: list[dict] = []

    async def hook(obj: dict) -> None:
        await seq.on_event(obj)
        events.append(obj)

    client = WpsClient(
        lambda: DirectTcpStream("127.0.0.1", port),
        store,
        my_call="M0ABC",
        name="Tester",
        on_event=hook,
        keepalive_interval=None,
        auto_reconnect=False,
    )
    try:
        await client.open()
        summary = await asyncio.wait_for(seq.wait(), 3.0)
        assert summary.server_message_count == 0
        assert summary.server_post_count == 0

        await client.send_message("M0XYZ", "hello fake")
        # Wait for an `mr` ack to land in the event stream.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if any(e.get("t") == "mr" for e in events):
                break
        assert any(e.get("t") == "mr" for e in events)

        await client.subscribe(7)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if any(e.get("t") == "cs" and e.get("cid") == 7 for e in events):
                break
        assert any(e.get("t") == "cs" and e.get("cid") == 7 for e in events)
    finally:
        await client.close()
        store.close()
        server.close()
        await server.wait_closed()
        serve_task.cancel()
        try:
            await serve_task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_edit_round_trip_against_fake_wps(tmp_path: Path) -> None:
    """End-to-end: send → edit → resend(edit) all flow over a real
    socket and the matching `mr` / `cpr` acks cancel the edit timers
    so no spurious _delivery_timeout fires."""
    server = await asyncio.start_server(fake_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    serve_task = asyncio.create_task(server.serve_forever())

    store = SqliteStore(tmp_path / "state.sqlite3")
    seq = ConnectSequence(idle_after=0.2)
    events: list[dict] = []

    async def hook(obj: dict) -> None:
        await seq.on_event(obj)
        events.append(obj)

    client = WpsClient(
        lambda: DirectTcpStream("127.0.0.1", port),
        store,
        my_call="M0ABC",
        name="Tester",
        on_event=hook,
        keepalive_interval=None,
        auto_reconnect=False,
        delivery_timeout_s=2,
    )
    try:
        await client.open()
        await asyncio.wait_for(seq.wait(), 3.0)

        # Send a DM, edit it, then /retrydm-style resend (which now
        # dispatches to `med` because edit_ts is set).
        msg_id = await client.send_message("M0XYZ", "v1")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if any(e.get("t") == "mr" and e.get("_id") == msg_id for e in events):
                break
        events.clear()

        await client.edit_message(msg_id, "v2")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if any(e.get("t") == "mr" and e.get("_id") == msg_id for e in events):
                break
        assert any(e.get("t") == "mr" and e.get("_id") == msg_id for e in events)
        # Pending-edit timer cancelled by the ack.
        assert msg_id not in client._pending_dm_edits

        # Resend should now emit `med` (edit_ts is set on the row).
        events.clear()
        await client.resend_message(msg_id)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if any(e.get("t") == "mr" and e.get("_id") == msg_id for e in events):
                break
        assert any(e.get("t") == "mr" and e.get("_id") == msg_id for e in events)

        # Channel-side mirror.
        ts = await client.post(7, "v1")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if any(e.get("t") == "cpr" and e.get("ts") == ts for e in events):
                break
        events.clear()
        await client.edit_post(7, ts, "v2")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if any(e.get("t") == "cpr" and e.get("ts") == ts for e in events):
                break
        assert any(e.get("t") == "cpr" and e.get("ts") == ts for e in events)
        assert ts not in client._pending_post_edits

        # Wait past delivery_timeout_s to make sure no late timer fires.
        events.clear()
        await asyncio.sleep(2.5)
        assert not any(e.get("t") == "_delivery_timeout" for e in events)
    finally:
        await client.close()
        store.close()
        server.close()
        await server.wait_closed()
        serve_task.cancel()
        try:
            await serve_task
        except (asyncio.CancelledError, Exception):
            pass
