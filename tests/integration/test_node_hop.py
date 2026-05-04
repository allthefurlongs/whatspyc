"""End-to-end test for the connect-script hop runner.

Stands up a tiny "fake node" TCP server in front of ``tests.fake_wps``:

    client → [fake-node prompt] → C WPS → *** Connected → [fake_wps]

The node accepts a TCP connection, emits a node banner, expects the line
``C WPS\\r`` from the client, replies ``*** Connected to WPS\\r`` and then
splice-forwards bytes between the client and an internal TCP connection to
the fake_wps server. ``WpsClient`` (with a 1-step connect_script) walks
through the prompt and then completes a normal WPS handshake including a
message ack.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from whatspyc.store.store import SqliteStore
from whatspyc.transport.direct_tcp import DirectTcpStream
from whatspyc.wps.client import WpsClient
from whatspyc.wps.connect_seq import ConnectSequence
from whatspyc.wps.hop_script import HopStep

from tests.fake_wps import handle as fake_wps_handle


async def _splice(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(4096)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


def _make_node_handler(wps_port: int):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"\r\nWelcome to NODE7  (XRouter v3)\r\nNODE7:M0ABC} ")
        await writer.drain()
        # Read a line terminated by \r (the hop-script suffix).
        line_bytes = bytearray()
        while True:
            ch = await reader.read(1)
            if not ch:
                writer.close()
                return
            line_bytes.extend(ch)
            if ch == b"\r":
                break
        line = bytes(line_bytes).rstrip(b"\r\n").decode("latin-1").strip()
        if line.upper() != "C WPS":
            writer.write(f"*** Failure - unknown command {line!r}\r".encode("latin-1"))
            await writer.drain()
            writer.close()
            return
        writer.write(b"*** Connected to WPS\r")
        await writer.drain()

        # Splice this client connection to a fresh TCP connection to fake_wps.
        wps_reader, wps_writer = await asyncio.open_connection("127.0.0.1", wps_port)
        c2w = asyncio.create_task(_splice(reader, wps_writer))
        w2c = asyncio.create_task(_splice(wps_reader, writer))
        try:
            await asyncio.gather(c2w, w2c)
        finally:
            for w in (writer, wps_writer):
                try:
                    w.close()
                except Exception:
                    pass

    return handle


@pytest.mark.asyncio
async def test_one_hop_then_full_wps_handshake(tmp_path: Path) -> None:
    wps_server = await asyncio.start_server(fake_wps_handle, "127.0.0.1", 0)
    wps_port = wps_server.sockets[0].getsockname()[1]
    wps_task = asyncio.create_task(wps_server.serve_forever())

    node_server = await asyncio.start_server(_make_node_handler(wps_port), "127.0.0.1", 0)
    node_port = node_server.sockets[0].getsockname()[1]
    node_task = asyncio.create_task(node_server.serve_forever())

    store = SqliteStore(tmp_path / "state.sqlite3")
    seq = ConnectSequence(idle_after=0.2)
    events: list[dict] = []

    async def hook(obj: dict) -> None:
        await seq.on_event(obj)
        events.append(obj)

    client = WpsClient(
        lambda: DirectTcpStream("127.0.0.1", node_port),
        store,
        my_call="M0ABC",
        name="Tester",
        on_event=hook,
        keepalive_interval=None,
        auto_reconnect=False,
        connect_script=[HopStep(cmd="C WPS", val="Connected to WPS", timeout=2.0)],
    )
    try:
        await client.open()
        await asyncio.wait_for(seq.wait(), 3.0)

        # The connect_script ran *before* the WPS handshake — verify by
        # round-tripping a message ack through the WPS dialect.
        await client.send_message("M0XYZ", "hello via node")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if any(e.get("t") == "mr" for e in events):
                break
        assert any(e.get("t") == "mr" for e in events), (
            f"no `mr` ack — got {[e.get('t') for e in events]}"
        )
    finally:
        await client.close()
        store.close()
        for srv, task in ((node_server, node_task), (wps_server, wps_task)):
            srv.close()
            await srv.wait_closed()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_node_error_aborts_handshake(tmp_path: Path) -> None:
    """If the node responds with ``*** Failure``, the connect_script raises
    and ``client.open()`` propagates the error — no WPS bytes get sent."""
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"NODE7:M0ABC} ")
        await writer.drain()
        # Read whatever the client sends.
        await reader.read(64)
        writer.write(b"*** Failure - link to WPS down\r")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    serve_task = asyncio.create_task(server.serve_forever())

    store = SqliteStore(tmp_path / "state.sqlite3")
    client = WpsClient(
        lambda: DirectTcpStream("127.0.0.1", port),
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
        connect_script=[HopStep(cmd="C WPS", val="Connected to WPS", timeout=2.0)],
    )
    try:
        with pytest.raises(Exception, match="(?i)error token|failure"):
            await client.open()
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
