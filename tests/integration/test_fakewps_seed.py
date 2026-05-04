"""End-to-end check that the seeded fake WPS server populates the client.

Connects with a fresh state-dir and a clean ``cc=[]`` connect record,
and asserts the client store and event stream end up populated with
seeded posts, DMs, online users, and ham names.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from whatspyc.store.store import SqliteStore
from whatspyc.transport.direct_tcp import DirectTcpStream
from whatspyc.wps.client import WpsClient
from whatspyc.wps.connect_seq import ConnectSequence

from tests.fake_wps import handle as fake_handle
from tests.fake_wps_seed import default_seed


@pytest.mark.asyncio
async def test_seeded_connect_populates_store(tmp_path: Path) -> None:
    seed = default_seed()

    async def conn(reader, writer):
        # Live drip-feed disabled so the test settles deterministically.
        await fake_handle(reader, writer, seed, live=False)

    server = await asyncio.start_server(conn, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    serve_task = asyncio.create_task(server.serve_forever())

    store = SqliteStore(tmp_path / "state.sqlite3")
    seq = ConnectSequence(idle_after=0.3)
    events: list[dict] = []

    async def hook(obj: dict) -> None:
        await seq.on_event(obj)
        events.append(obj)

    client = WpsClient(
        lambda: DirectTcpStream("127.0.0.1", port),
        store,
        my_call="N0CALL",
        name="Tester",
        on_event=hook,
        keepalive_interval=None,
        auto_reconnect=False,
    )
    try:
        await client.open()
        summary = await asyncio.wait_for(seq.wait(), 5.0)

        # c-reply counts non-zero.
        assert summary.server_message_count > 0
        assert summary.server_post_count > 0
        # Online users came through.
        assert "M0FOO" in summary.online_users

        # All seeded channels show up as subscribed in the local store.
        subscribed = {c["cid"] for c in store.list_channels() if c["subscribed"]}
        assert {1, 2, 6}.issubset(subscribed)

        # Each channel has its seeded posts.
        for cid in (1, 2, 6):
            assert store.recent_posts(cid), f"channel {cid} has no posts"

        # DMs from both seed peers landed.
        m0foo_msgs = store.recent_messages("M0FOO")
        g7bar_msgs = store.recent_messages("G7BAR")
        assert m0foo_msgs and g7bar_msgs

        # Ham name lookups populated.
        assert store.lookup_ham("M0FOO")["name"] == "Mike"
        assert store.lookup_ham("G7BAR")["name"] == "Sarah"
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
async def test_subscribing_seeded_channel_backfills(tmp_path: Path) -> None:
    """``/sub N`` against a seeded channel should reply with a populated cpb."""
    seed = default_seed()

    # Empty out one channel from the seed-on-connect path so we can test the
    # `cs`-driven backfill independently. We do this by pretending the client
    # already has every post in cid=1 (so the connect-time delta is empty),
    # then asking it to (re)subscribe with lcp=0.
    async def conn(reader, writer):
        await fake_handle(reader, writer, seed, live=False)

    server = await asyncio.start_server(conn, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    serve_task = asyncio.create_task(server.serve_forever())

    store = SqliteStore(tmp_path / "state.sqlite3")
    events: list[dict] = []

    async def hook(obj: dict) -> None:
        events.append(obj)

    client = WpsClient(
        lambda: DirectTcpStream("127.0.0.1", port),
        store,
        my_call="N0CALL",
        name="Tester",
        on_event=hook,
        keepalive_interval=None,
        auto_reconnect=False,
    )
    try:
        await client.open()
        # Wait for the connect sequence to settle.
        await asyncio.sleep(0.6)
        events.clear()

        # Re-subscribe to channel 1 with lcp=0 — server should send all posts again.
        await client.subscribe(1, last_post=0)
        for _ in range(60):
            await asyncio.sleep(0.05)
            if any(
                e.get("t") == "cpb" and e.get("cid") == 1 for e in events
            ):
                break
        cpbs = [e for e in events if e.get("t") == "cpb" and e.get("cid") == 1]
        assert cpbs, "expected a cpb backfill after subscribe"
        assert sum(len(cpb.get("p", [])) for cpb in cpbs) == len(
            seed.channel(1).posts
        )
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
