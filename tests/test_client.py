"""WpsClient lifecycle tests: keep-alive + auto-reconnect."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from whatspyc.store.store import SqliteStore
from whatspyc.transport.base import AsyncByteStream
from whatspyc.wps import codec
from whatspyc.wps.client import WpsClient


class _FakeStream(AsyncByteStream):
    """In-memory AsyncByteStream — captures sends, plays back canned recvs."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self._inbox: asyncio.Queue[bytes] = asyncio.Queue()
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def recv(self) -> bytes:
        return await self._inbox.get()

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self._inbox.put(b"")

    def push(self, data: bytes) -> None:
        self._inbox.put_nowait(data)

    def push_eof(self) -> None:
        self._inbox.put_nowait(b"")


@pytest.mark.asyncio
async def test_handshake_sends_callsign_when_passthrough(tmp_path: Path) -> None:
    """Default ``AsyncByteStream.injects_callsign`` is False — client must
    send ``<CALL>\\r\\n`` itself before the type-`c` JSON."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    assert stream.injects_callsign is False
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    await client.close()
    store.close()

    assert stream.sent[0] == b"M0ABC\r\n"
    assert b'"t":"c"' in stream.sent[1]


@pytest.mark.asyncio
async def test_handshake_skips_callsign_when_injected(tmp_path: Path) -> None:
    """RHP transports advertise ``injects_callsign = True``: the
    upstream node has already sent the callsign on the WPS-facing
    socket. Sending it again breaks the handshake — WPS treats the
    second copy as the first JSON frame and disconnects with the
    "I didn't recognise that command" reply."""

    class _InjectingStream(_FakeStream):
        @property
        def injects_callsign(self) -> bool:
            return True

    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _InjectingStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    await client.close()
    store.close()

    assert not any(s.startswith(b"M0ABC") for s in stream.sent)
    assert b'"t":"c"' in stream.sent[0]


@pytest.mark.asyncio
async def test_keepalive_sends_periodic_k(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=0.05,
        auto_reconnect=False,
    )
    await client.open()
    # Wait long enough for ~3 keep-alives.
    await asyncio.sleep(0.2)
    await client.close()
    store.close()

    # Drop the callsign line + connect record; remaining sends should be `k` frames.
    sent_payloads = [s for s in stream.sent if not s.startswith(b"M0ABC")]
    # First sent is the type-`c` connect record.
    assert any(b'"t":"c"' in s for s in sent_payloads)
    # Subsequent sends are keep-alives.
    keepalives = [s for s in sent_payloads if b'"t":"k"' in s]
    assert len(keepalives) >= 2


@pytest.mark.asyncio
async def test_auto_reconnect_after_eof(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.sqlite3")
    streams: list[_FakeStream] = []

    def factory() -> AsyncByteStream:
        s = _FakeStream()
        streams.append(s)
        return s

    events: list[dict] = []

    async def on_event(obj: dict) -> None:
        if str(obj.get("t", "")).startswith("_"):
            events.append(obj)

    client = WpsClient(
        factory,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=True,
        reconnect_initial_backoff=0.05,
        reconnect_max_backoff=0.2,
        on_event=on_event,
    )
    await client.open()
    assert len(streams) == 1

    # Drop the link.
    streams[0].push_eof()

    # Wait for reconnect to land.
    for _ in range(50):
        await asyncio.sleep(0.05)
        if len(streams) >= 2 and any(e.get("t") == "_reconnected" for e in events):
            break
    assert len(streams) >= 2
    assert any(e.get("t") == "_disconnect" for e in events)
    assert any(e.get("t") == "_reconnecting" for e in events)
    assert any(e.get("t") == "_reconnected" for e in events)
    assert client.is_connected

    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_auto_reconnect_off_by_default(tmp_path: Path) -> None:
    """auto_reconnect defaults to False — EOF must NOT spawn a reconnect
    loop and the link must stay disconnected."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    streams: list[_FakeStream] = []

    def factory() -> AsyncByteStream:
        s = _FakeStream()
        streams.append(s)
        return s

    events: list[dict] = []

    async def on_event(obj: dict) -> None:
        if str(obj.get("t", "")).startswith("_"):
            events.append(obj)

    client = WpsClient(
        factory,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        on_event=on_event,
    )
    await client.open()
    assert len(streams) == 1
    streams[0].push_eof()

    # Give the reader a chance to react to the EOF.
    for _ in range(10):
        await asyncio.sleep(0.05)
        if any(e.get("t") == "_disconnect" for e in events):
            break
    assert any(e.get("t") == "_disconnect" for e in events)
    assert not any(e.get("t") == "_reconnecting" for e in events)
    assert len(streams) == 1

    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_reconnect_max_retries_gives_up(tmp_path: Path) -> None:
    """``reconnect_max_retries=N`` caps the loop at N attempts, then emits
    ``_reconnect_giveup`` and stops."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    streams: list[AsyncByteStream] = []

    class _BrokenStream(AsyncByteStream):
        async def open(self) -> None:
            raise ConnectionRefusedError("nope")

        async def send(self, data: bytes) -> None:  # pragma: no cover
            raise AssertionError("send before open")

        async def recv(self) -> bytes:  # pragma: no cover
            raise AssertionError("recv before open")

        async def close(self) -> None:
            pass

    first = _FakeStream()

    def factory() -> AsyncByteStream:
        s: AsyncByteStream = first if not streams else _BrokenStream()
        streams.append(s)
        return s

    events: list[dict] = []

    async def on_event(obj: dict) -> None:
        if str(obj.get("t", "")).startswith("_"):
            events.append(obj)

    client = WpsClient(
        factory,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=True,
        reconnect_initial_backoff=0.01,
        reconnect_max_backoff=0.02,
        reconnect_max_retries=3,
        on_event=on_event,
    )
    await client.open()
    first.push_eof()

    for _ in range(100):
        await asyncio.sleep(0.02)
        if any(e.get("t") == "_reconnect_giveup" for e in events):
            break
    giveup = [e for e in events if e.get("t") == "_reconnect_giveup"]
    assert len(giveup) == 1
    assert giveup[0]["attempts"] == 3
    reconnecting = [e for e in events if e.get("t") == "_reconnecting"]
    assert len(reconnecting) == 3
    failed = [e for e in events if e.get("t") == "_reconnect_failed"]
    assert len(failed) == 3
    assert not client.is_connected

    await client.close()
    store.close()


def test_reconnect_max_retries_negative_rejected(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.sqlite3")
    try:
        with pytest.raises(ValueError, match="reconnect_max_retries"):
            WpsClient(
                lambda: _FakeStream(),
                store,
                my_call="M0ABC",
                name="Tester",
                reconnect_max_retries=-1,
            )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_decode_error_pre_handshake_surfaces_hint(tmp_path: Path) -> None:
    """Regression for the user-reported scenario: connect_sequence finishes
    before the chain actually reaches WPS, so the next bytes the reader
    sees are plain text from a node prompt. The error must carry a
    useful hint rather than a raw JSONDecodeError."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    events: list[dict] = []

    async def hook(obj: dict) -> None:
        events.append(obj)

    client = WpsClient(
        lambda: stream,
        store,
        my_call="N0CALL",
        name="Tester",
        on_event=hook,
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    # Pretend the node sent a plain-text failure line instead of WPS frames.
    stream.push(b"*** Failure - unknown command\r")
    for _ in range(20):
        await asyncio.sleep(0.01)
        if any(e.get("t") == "_error" for e in events):
            break
    err_events = [e for e in events if e.get("t") == "_error"]
    assert err_events, f"no _error event; got types {[e.get('t') for e in events]}"
    assert "connect_sequence likely incomplete" in err_events[0]["exc"]
    assert "Failure" in err_events[0]["exc"]
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_online_users_seeded_from_o_and_kept_in_sync(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    assert client.online_users() == []  # cleared at handshake start

    stream.push(codec.encode({"t": "o", "o": ["M0FOO", "G7BAR", "2E0BAZ"]}))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if client.online_users():
            break
    assert client.online_users() == ["2E0BAZ", "G7BAR", "M0FOO"]

    stream.push(codec.encode({"t": "uc", "c": "M7QRP"}))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if "M7QRP" in client.online_users():
            break
    assert "M7QRP" in client.online_users()

    stream.push(codec.encode({"t": "ud", "c": "G7BAR"}))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if "G7BAR" not in client.online_users():
            break
    assert "G7BAR" not in client.online_users()
    assert client.online_users() == ["2E0BAZ", "M0FOO", "M7QRP"]

    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_online_users_cleared_on_reconnect(tmp_path: Path) -> None:
    """After a reconnect we should wait for the new `o` payload — stale
    entries from the previous session must not linger."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    streams: list[_FakeStream] = []

    def factory() -> AsyncByteStream:
        s = _FakeStream()
        streams.append(s)
        return s

    events: list[dict] = []

    async def on_event(obj: dict) -> None:
        if str(obj.get("t", "")).startswith("_"):
            events.append(obj)

    client = WpsClient(
        factory,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=True,
        reconnect_initial_backoff=0.05,
        reconnect_max_backoff=0.2,
        on_event=on_event,
    )
    await client.open()
    streams[0].push(codec.encode({"t": "o", "o": ["M0FOO", "G7BAR"]}))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if client.online_users():
            break
    assert client.online_users() == ["G7BAR", "M0FOO"]

    streams[0].push_eof()
    for _ in range(50):
        await asyncio.sleep(0.05)
        if len(streams) >= 2 and any(e.get("t") == "_reconnected" for e in events):
            break
    assert client.is_connected
    # Fresh handshake → roster cleared, awaiting the next `o`.
    assert client.online_users() == []

    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_unpause_channel_sends_cu_with_post_count(tmp_path: Path) -> None:
    """unpause_channel(pc=N) emits the documented `cu` body and clears the
    cid from the local paused-channels map (server clears its side too)."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    # Server reports the channel is paused.
    stream.push(codec.encode({"t": "pch", "ch": [{"cid": 6, "pt": 712}]}))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if client.paused_channels():
            break
    assert client.paused_channels() == {6: 712}

    await client.unpause_channel(6, post_count=50)
    assert client.paused_channels() == {}
    sent = [s for s in stream.sent if b'"t":"cu"' in s]
    assert len(sent) == 1
    assert b'"cid":6' in sent[0]
    assert b'"pc":50' in sent[0]
    # Must not include lts when post_count was specified.
    assert b'"lts"' not in sent[0]
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_unpause_channel_with_logged_ts(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    await client.unpause_channel(0, logged_ts=1_700_000_000_000)
    sent = [s for s in stream.sent if b'"t":"cu"' in s]
    assert len(sent) == 1
    assert b'"lts":1700000000000' in sent[0]
    assert b'"pc"' not in sent[0]
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_unpause_channel_rejects_zero_or_both_args(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    with pytest.raises(ValueError, match="exactly one"):
        await client.unpause_channel(0)
    with pytest.raises(ValueError, match="exactly one"):
        await client.unpause_channel(0, post_count=10, logged_ts=1)
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_edit_post_emits_cped_with_cid_ts_text_edts(tmp_path: Path) -> None:
    """edit_post mirrors the web client's `cped` shape — cid + original
    ts + new text, with edts filled in from the wall clock."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    store.upsert_post(
        6,
        {"ts": 1_777_821_179_422, "fc": "M0ABC", "p": "original"},
    )
    await client.edit_post(6, 1_777_821_179_422, "fixed text")
    sent = [s for s in stream.sent if b'"t":"cped"' in s]
    assert len(sent) == 1
    frame = sent[0]
    assert b'"cid":6' in frame
    assert b'"ts":1777821179422' in frame
    assert b'"p":"fixed text"' in frame
    assert b'"edts":' in frame
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_request_post_batch_sends_cpb(tmp_path: Path) -> None:
    """request_post_batch emits a client-form cpb with the requested count."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    await client.request_post_batch(3, 10)
    sent = [s for s in stream.sent if b'"t":"cpb"' in s]
    assert len(sent) == 1
    assert b'"cid":3' in sent[0]
    assert b'"pc":10' in sent[0]
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_subscribe_and_wait_returns_pc_from_ack(tmp_path: Path) -> None:
    """subscribe_and_wait sends `cs`, blocks until the matching ack
    arrives, and returns the server's `pc` count."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()

    async def _ack_after(delay: float) -> None:
        await asyncio.sleep(delay)
        stream.push(codec.encode({"t": "cs", "cid": 3, "s": 1, "pc": 2500}))

    asyncio.create_task(_ack_after(0.01))
    pc = await client.subscribe_and_wait(3, timeout=2.0)
    assert pc == 2500

    cs_frames = [s for s in stream.sent if b'"t":"cs"' in s]
    assert len(cs_frames) == 1
    assert b'"cid":3' in cs_frames[0]
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_subscribe_and_wait_times_out_without_ack(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    with pytest.raises(asyncio.TimeoutError):
        await client.subscribe_and_wait(3, timeout=0.1)
    # Waiter must be cleaned up so a subsequent retry isn't blocked by it.
    assert 3 not in client._cs_ack_waiters
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_close_cancels_pending_subscribe_waiters(tmp_path: Path) -> None:
    """A Ctrl+Q during a hung `subscribe_and_wait` must let the caller's
    await unblock — close() cancels every pending cs waiter so the
    `await asyncio.wait_for(fut, ...)` raises CancelledError instead of
    sitting on an orphaned future."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()

    sub_task = asyncio.create_task(client.subscribe_and_wait(3))
    # Let the task install its waiter before we tear the client down.
    await asyncio.sleep(0.01)
    assert 3 in client._cs_ack_waiters

    await client.close()

    with pytest.raises(asyncio.CancelledError):
        await sub_task
    assert client._cs_ack_waiters == {}
    store.close()


@pytest.mark.asyncio
async def test_auto_backfill_no_longer_fires_on_subscribe_ack(tmp_path: Path) -> None:
    """The /sub UI flow is now responsible for the cs→cpb hop. The
    client-side auto-fire on cs has been removed; auto_backfill_post_count
    only affects the pch (paused channels at connect) path."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
        auto_backfill_post_count=15,
    )
    await client.open()
    stream.push(codec.encode({"t": "cs", "cid": 3, "s": 1, "pc": 100}))
    await asyncio.sleep(0.1)
    cpb_frames = [s for s in stream.sent if b'"t":"cpb"' in s]
    assert cpb_frames == [], "client should not auto-cpb on cs anymore"
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_pch_does_not_auto_unpause(tmp_path: Path) -> None:
    """`pch` populates the local paused-channels map but never auto-fires
    a `cu` — even when ``auto_backfill_post_count`` is set. Unpause is
    explicit (UI modal or ``/unpause``); auto-pulling would race the
    user's confirm and skip the modal entirely."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
        auto_backfill_post_count=20,
    )
    await client.open()
    stream.push(
        codec.encode(
            {"t": "pch", "ch": [{"cid": 0, "pt": 712}, {"cid": 6, "pt": 5}]}
        )
    )
    # Give the dispatch loop a few ticks; assert it never sends a cu.
    for _ in range(20):
        await asyncio.sleep(0.01)
    cu_frames = [s for s in stream.sent if b'"t":"cu"' in s]
    assert cu_frames == [], "client must not auto-unpause on pch"
    # Local state mirrors the server's pch so UIs can render the suffix
    # and the modal knows the pending count.
    assert client.paused_channels() == {0: 712, 6: 5}
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_paused_channels_cleared_on_reconnect(tmp_path: Path) -> None:
    """Server's pch counts are per-connection; reconnect must wipe the
    local map so a stale prompt isn't carried over from the previous link."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    streams: list[_FakeStream] = []

    def factory() -> AsyncByteStream:
        s = _FakeStream()
        streams.append(s)
        return s

    events: list[dict] = []

    async def on_event(obj: dict) -> None:
        if str(obj.get("t", "")).startswith("_"):
            events.append(obj)

    client = WpsClient(
        factory,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=True,
        reconnect_initial_backoff=0.05,
        reconnect_max_backoff=0.2,
        on_event=on_event,
    )
    await client.open()
    streams[0].push(codec.encode({"t": "pch", "ch": [{"cid": 0, "pt": 50}]}))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if client.paused_channels():
            break
    assert client.paused_channels() == {0: 50}

    streams[0].push_eof()
    for _ in range(50):
        await asyncio.sleep(0.05)
        if len(streams) >= 2 and any(e.get("t") == "_reconnected" for e in events):
            break
    assert client.is_connected
    assert client.paused_channels() == {}

    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_send_raises_when_disconnected(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    stream.push_eof()
    # Wait for reader_loop to notice the EOF.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if not client.is_connected:
            break
    assert not client.is_connected
    with pytest.raises(ConnectionError):
        await client.keep_alive()
    await client.close()
    store.close()


# ---------------------------------------------------------------------------
# Verbose-render persistence: receipt metadata + delivery acks
# ---------------------------------------------------------------------------


async def _make_connected_client(tmp_path: Path) -> tuple[WpsClient, SqliteStore, _FakeStream]:
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    return client, store, stream


@pytest.mark.asyncio
async def test_inbound_message_marks_realtime_and_received_ts(tmp_path: Path) -> None:
    """Real-time inbound `m` from someone else: store row gets
    ``realtime=1`` and a ``received_ts`` near now (so verbose render
    can compute "Received real-time in Xs")."""
    client, store, stream = await _make_connected_client(tmp_path)
    stream.push(
        codec.encode(
            {"t": "m", "_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
             "m": "hi", "ts": 100}
        )
    )
    for _ in range(20):
        await asyncio.sleep(0.01)
        row = store.lookup_message_by_id("100-M0FOO")
        if row is not None:
            break
    assert row is not None
    assert row["realtime"] == 1
    assert row["received_ts"] is not None and row["received_ts"] > 0
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_inbound_batch_marks_realtime_zero(tmp_path: Path) -> None:
    """`mb` batch: ``realtime=0``, receipt time set."""
    client, store, stream = await _make_connected_client(tmp_path)
    stream.push(
        codec.encode(
            {"t": "mb", "md": {"mt": 1, "mc": 0}, "m": [
                {"_id": "200-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
                 "m": "hello", "ts": 200}
            ]}
        )
    )
    for _ in range(20):
        await asyncio.sleep(0.01)
        row = store.lookup_message_by_id("200-M0FOO")
        if row is not None:
            break
    assert row is not None
    assert row["realtime"] == 0
    assert row["received_ts"] is not None
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_inbound_message_from_self_skips_receipt_metadata(tmp_path: Path) -> None:
    """When WPS broadcasts our own message back to us (e.g. across
    multi-device sessions), we must NOT mark our own row as
    'received realtime' — the sender row already exists with NULL
    receipt cols and the verbose render uses delivered_ts for outbound."""
    client, store, stream = await _make_connected_client(tmp_path)
    # Pre-insert a row as if we just sent it.
    store.upsert_message(
        {"_id": "300-M0ABC", "fc": "M0ABC", "tc": "M0FOO",
         "m": "hi", "ts": 300, "ms": 0}
    )
    stream.push(
        codec.encode(
            {"t": "m", "_id": "300-M0ABC", "fc": "M0ABC", "tc": "M0FOO",
             "m": "hi", "ts": 300}
        )
    )
    for _ in range(20):
        await asyncio.sleep(0.01)
        row = store.lookup_message_by_id("300-M0ABC")
        if row is not None and row.get("received_ts") is not None:
            break
    row = store.lookup_message_by_id("300-M0ABC")
    assert row is not None
    assert row["realtime"] is None
    assert row["received_ts"] is None
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_mr_marks_outbound_message_delivered(tmp_path: Path) -> None:
    """`mr` ack flips msg_status=1 and writes delivered_ts on the
    matching outbound row."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.upsert_message(
        {"_id": "400-M0ABC", "fc": "M0ABC", "tc": "M0FOO",
         "m": "hi", "ts": 400, "ms": 0}
    )
    stream.push(codec.encode({"t": "mr", "_id": "400-M0ABC"}))
    for _ in range(20):
        await asyncio.sleep(0.01)
        row = store.lookup_message_by_id("400-M0ABC")
        if row is not None and row.get("delivered_ts") is not None:
            break
    row = store.lookup_message_by_id("400-M0ABC")
    assert row is not None
    assert row["msg_status"] == 1
    assert row["delivered_ts"] is not None
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_cpr_marks_outbound_post_delivered_using_dts(tmp_path: Path) -> None:
    """`cpr` ack with `dts` writes the server-side delivery timestamp
    onto the matching outbound post. Lookup is via (from_call, ts)."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.set_subscription(7, True)
    store.upsert_post(7, {"ts": 1000, "fc": "M0ABC", "p": "ours"})
    stream.push(codec.encode({"t": "cpr", "ts": 1000, "dts": 1234}))
    for _ in range(20):
        await asyncio.sleep(0.01)
        row = store.lookup_post(7, 1000)
        if row is not None and row.get("delivered_ts") is not None:
            break
    row = store.lookup_post(7, 1000)
    assert row is not None
    assert row["delivered_ts"] == 1234
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_send_message_uses_seconds_ts_and_ms_id(tmp_path: Path) -> None:
    """DM wire convention: `ts` is seconds-since-epoch (web client sends
    `Math.round(Date.now()/1e3)`), but `_id` keeps a ms prefix
    (`${Math.round(Date.now())}-${call}`). The ms in `_id` must equal
    the wire ts * 1000."""
    client, store, stream = await _make_connected_client(tmp_path)
    msg_id = await client.send_message("M0FOO", "hello")
    m_frames = [f for f in stream.sent if b'"t":"m"' in f]
    assert len(m_frames) == 1
    frame = m_frames[0]
    # The ms prefix in `_id` is wire-ts * 1000 (modulo any sub-second
    # boundary crossed between the two `time.time()` reads — but
    # `send_message` reads the clock once and divides, so they match).
    ms_prefix = int(msg_id.split("-", 1)[0])
    expected_seconds = ms_prefix // 1000
    assert f'"ts":{expected_seconds}'.encode() in frame
    # Sanity-check: the wire ts looks like seconds (small) not ms.
    assert expected_seconds < 1_000_000_000_000
    # Local row was persisted with the same seconds-magnitude ts.
    row = store.lookup_message_by_id(msg_id)
    assert row is not None and row["ts"] == expected_seconds
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_send_message_marks_msg_status_zero(tmp_path: Path) -> None:
    """`send_message` must persist the row with ``msg_status=0`` so the
    verbose render can tell "sent, not yet acked" apart from "row
    predates the delivery columns" (where msg_status is NULL)."""
    client, store, stream = await _make_connected_client(tmp_path)
    msg_id = await client.send_message("M0FOO", "hello")
    row = store.lookup_message_by_id(msg_id)
    assert row is not None
    assert row["msg_status"] == 0
    assert row["delivered_ts"] is None
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_resend_message_reuses_original_id_and_ts(tmp_path: Path) -> None:
    """resend_message rebuilds the m frame from the local row preserving
    the original ``_id`` and ``ts`` — that's what makes the WPS server's
    `_id` dedupe path treat it as a resend rather than a fresh send."""
    client, store, stream = await _make_connected_client(tmp_path)
    msg_id = await client.send_message("M0FOO", "hello")
    original_frames = list(stream.sent)

    await client.resend_message(msg_id)
    new_frames = stream.sent[len(original_frames):]
    m_frames = [f for f in new_frames if b'"t":"m"' in f]
    assert len(m_frames) == 1
    frame = m_frames[0]
    assert f'"_id":"{msg_id}"'.encode() in frame
    # DM `ts` is seconds-since-epoch on the wire (web client convention),
    # while `_id`'s leading half is the ms equivalent — so the resent
    # frame's `ts` is the original `_id`'s ms half divided by 1000.
    ts = int(msg_id.split("-", 1)[0]) // 1000
    assert f'"ts":{ts}'.encode() in frame
    assert b'"fc":"M0ABC"' in frame
    assert b'"tc":"M0FOO"' in frame
    assert b'"m":"hello"' in frame
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_send_message_with_reply_id_emits_r_field(tmp_path: Path) -> None:
    """``reply_id`` becomes the wire ``r`` field on the outgoing ``m``
    frame and is mirrored into the local store row."""
    client, store, stream = await _make_connected_client(tmp_path)
    msg_id = await client.send_message(
        "M0FOO", "answering", reply_id="100-M0FOO"
    )
    m_frames = [f for f in stream.sent if b'"t":"m"' in f]
    assert any(b'"r":"100-M0FOO"' in f for f in m_frames)
    row = store.lookup_message_by_id(msg_id)
    assert row is not None and row["reply_id"] == "100-M0FOO"
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_post_with_reply_emits_rts_and_rfc(tmp_path: Path) -> None:
    """``reply_ts`` + ``reply_from`` populate the wire ``rts`` and ``rfc``
    fields on the outgoing ``cp`` frame and are mirrored locally."""
    client, store, stream = await _make_connected_client(tmp_path)
    ts = await client.post(
        5, "responding here", reply_ts=1_700_000_000_000, reply_from="m0foo"
    )
    cp_frames = [f for f in stream.sent if b'"t":"cp"' in f]
    assert any(b'"rts":1700000000000' in f for f in cp_frames)
    # reply_from is upper-cased before going on the wire so it matches
    # peer renders that use the canonical callsign form.
    assert any(b'"rfc":"M0FOO"' in f for f in cp_frames)
    row = store.lookup_post(5, ts)
    assert row is not None
    assert row["reply_ts"] == 1_700_000_000_000
    assert row["reply_from"] == "M0FOO"
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_resend_message_preserves_reply_id(tmp_path: Path) -> None:
    """A re-send of an unedited reply re-emits ``r`` from the stored
    row so the parent attribution is preserved in the wire frame."""
    client, store, stream = await _make_connected_client(tmp_path)
    msg_id = await client.send_message(
        "M0FOO", "answering", reply_id="100-M0FOO"
    )
    original = list(stream.sent)
    await client.resend_message(msg_id)
    new_frames = stream.sent[len(original):]
    m_frames = [f for f in new_frames if b'"t":"m"' in f]
    assert len(m_frames) == 1
    assert b'"r":"100-M0FOO"' in m_frames[0]
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_resend_post_preserves_reply_attribution(tmp_path: Path) -> None:
    client, store, stream = await _make_connected_client(tmp_path)
    ts = await client.post(
        5, "responding", reply_ts=1_700_000_000_000, reply_from="M0FOO"
    )
    original = list(stream.sent)
    await client.resend_post(5, ts)
    new_frames = stream.sent[len(original):]
    cp_frames = [f for f in new_frames if b'"t":"cp"' in f]
    assert len(cp_frames) == 1
    assert b'"rts":1700000000000' in cp_frames[0]
    assert b'"rfc":"M0FOO"' in cp_frames[0]
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_resend_message_unknown_id_raises(tmp_path: Path) -> None:
    client, store, stream = await _make_connected_client(tmp_path)
    with pytest.raises(ValueError, match="no local message"):
        await client.resend_message("0-M0ABC")
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_resend_message_refuses_inbound_row(tmp_path: Path) -> None:
    """Only outbound rows can be retried — there's no protocol path for
    re-sending a message we received from someone else."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.upsert_message(
        {"_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
         "m": "from peer", "ts": 100}
    )
    with pytest.raises(ValueError, match="Cannot retry sending other users DMs"):
        await client.resend_message("100-M0FOO")
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_resend_post_reuses_original_cid_and_ts(tmp_path: Path) -> None:
    """resend_post rebuilds the cp frame preserving (cid, ts) so the
    server's post dedupe re-emits the cpr ack rather than storing a
    duplicate."""
    client, store, stream = await _make_connected_client(tmp_path)
    ts = await client.post(7, "hello channel")
    original_frames = list(stream.sent)

    await client.resend_post(7, ts)
    new_frames = stream.sent[len(original_frames):]
    cp_frames = [f for f in new_frames if b'"t":"cp"' in f]
    assert len(cp_frames) == 1
    frame = cp_frames[0]
    assert b'"cid":7' in frame
    assert f'"ts":{ts}'.encode() in frame
    assert b'"fc":"M0ABC"' in frame
    assert b'"p":"hello channel"' in frame
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_resend_post_unknown_raises(tmp_path: Path) -> None:
    client, store, stream = await _make_connected_client(tmp_path)
    with pytest.raises(ValueError, match="no local post"):
        await client.resend_post(7, 1234)
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_resend_post_refuses_other_authors_row(tmp_path: Path) -> None:
    client, store, stream = await _make_connected_client(tmp_path)
    store.upsert_post(7, {"ts": 1000, "fc": "M0FOO", "p": "their post"})
    with pytest.raises(ValueError, match="Cannot retry sending other users posts"):
        await client.resend_post(7, 1000)
    await client.close()
    store.close()


# ---------------------------------------------------------------------------
# Per-row delivery timeout: emits _delivery_timeout when no ack arrives,
# and is cancelled by the matching mr/cpr ack.
# ---------------------------------------------------------------------------


async def _make_client_with_timeout(
    tmp_path: Path, *, delivery_timeout_s: int | None
) -> tuple[WpsClient, SqliteStore, _FakeStream, list[dict]]:
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    events: list[dict] = []

    async def on_event(obj: dict) -> None:
        events.append(obj)

    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
        delivery_timeout_s=delivery_timeout_s,
        on_event=on_event,
    )
    await client.open()
    return client, store, stream, events


@pytest.mark.asyncio
async def test_delivery_timeout_dm_fires_when_no_ack(tmp_path: Path) -> None:
    """No ``mr`` arrives before the deadline → ``_delivery_timeout`` event
    surfaces with the row's lid + peer + ts so the UI can render the
    timeout notice and the /retrydm hint."""
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=1
    )
    msg_id = await client.send_message("M0FOO", "hi")
    # Wait long enough for the timer to fire.
    for _ in range(30):
        await asyncio.sleep(0.1)
        if any(e.get("t") == "_delivery_timeout" for e in events):
            break
    matches = [e for e in events if e.get("t") == "_delivery_timeout"]
    assert len(matches) == 1
    e = matches[0]
    assert e["kind"] == "dm"
    assert e["msg_id"] == msg_id
    assert e["peer"] == "M0FOO"
    # lid is the SQLite rowid the upsert assigned — must round-trip.
    assert isinstance(e["lid"], int) and e["lid"] > 0
    assert isinstance(e["ts"], int)
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_delivery_timeout_dm_cancelled_by_mr_ack(tmp_path: Path) -> None:
    """Receiving the ``mr`` ack before the deadline cancels the timer —
    no ``_delivery_timeout`` should ever fire."""
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=2
    )
    msg_id = await client.send_message("M0FOO", "hi")
    stream.push(codec.encode({"t": "mr", "_id": msg_id}))
    # Wait past the deadline.
    await asyncio.sleep(2.5)
    assert not any(e.get("t") == "_delivery_timeout" for e in events)
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_delivery_timeout_post_fires_with_cid_and_lid(tmp_path: Path) -> None:
    """No ``cpr`` ack within ``delivery_timeout_s`` → timeout event for
    a post carries the channel id and lid the UI needs to render the
    /retrypost hint."""
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=1
    )
    store.set_subscription(7, True)
    ts = await client.post(7, "hello channel")
    for _ in range(30):
        await asyncio.sleep(0.1)
        if any(e.get("t") == "_delivery_timeout" for e in events):
            break
    matches = [e for e in events if e.get("t") == "_delivery_timeout"]
    assert len(matches) == 1
    e = matches[0]
    assert e["kind"] == "post"
    assert e["cid"] == 7
    assert e["ts"] == ts
    assert isinstance(e["lid"], int) and e["lid"] > 0
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_delivery_timeout_post_cancelled_by_cpr_ack(tmp_path: Path) -> None:
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=2
    )
    store.set_subscription(7, True)
    ts = await client.post(7, "hello")
    stream.push(codec.encode({"t": "cpr", "ts": ts, "dts": ts + 5}))
    await asyncio.sleep(2.5)
    assert not any(e.get("t") == "_delivery_timeout" for e in events)
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_delivery_timeout_disabled_when_none(tmp_path: Path) -> None:
    """``delivery_timeout_s=None`` disables the feature — no timer is
    scheduled and no event is ever emitted."""
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=None
    )
    await client.send_message("M0FOO", "hi")
    await client.post(7, "hi")
    await asyncio.sleep(0.5)
    assert not any(e.get("t") == "_delivery_timeout" for e in events)
    # And neither task dict was populated, so close has nothing to cancel.
    assert client._dm_timeout_tasks == {}
    assert client._post_timeout_tasks == {}
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_delivery_timeout_fires_independent_of_show_acks(tmp_path: Path) -> None:
    """The client emits ``_delivery_timeout`` regardless of any UI-level
    show_acks setting — the client doesn't know about that option, and
    the UI's render path is what skips ack lines.
    """
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=1
    )
    await client.send_message("M0FOO", "hi")
    await asyncio.sleep(1.5)
    assert any(e.get("t") == "_delivery_timeout" for e in events)
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_delivery_timeout_close_cancels_pending_timers(tmp_path: Path) -> None:
    """``close`` must cancel in-flight timers so they don't fire after
    the client has been torn down (which would emit on a vanished
    on_event)."""
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=10
    )
    await client.send_message("M0FOO", "hi")
    await client.post(7, "hi")
    assert client._dm_timeout_tasks
    assert client._post_timeout_tasks
    await client.close()
    assert client._dm_timeout_tasks == {}
    assert client._post_timeout_tasks == {}
    # Give any pending callback a chance to misbehave; it shouldn't.
    await asyncio.sleep(0.1)
    assert not any(e.get("t") == "_delivery_timeout" for e in events)
    store.close()


@pytest.mark.asyncio
async def test_delivery_timeout_resend_reschedules_timer(tmp_path: Path) -> None:
    """A resend after a partial delay should restart the timer — that's
    the user's "I haven't seen an ack, try again" path, and they want a
    fresh timeout window for the new attempt rather than firing
    immediately on the original deadline."""
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=1
    )
    msg_id = await client.send_message("M0FOO", "hi")
    # Sleep most of the original window, then resend.
    await asyncio.sleep(0.7)
    await client.resend_message(msg_id)
    # Just before the *original* deadline would have fired — no event yet.
    await asyncio.sleep(0.5)
    assert not any(e.get("t") == "_delivery_timeout" for e in events)
    # Past the resend's fresh deadline — now it should fire.
    await asyncio.sleep(0.8)
    matches = [e for e in events if e.get("t") == "_delivery_timeout"]
    assert len(matches) == 1
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_delivery_timeout_set_delivery_timeout_s_validates(tmp_path: Path) -> None:
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=60
    )
    client.set_delivery_timeout_s(120)
    assert client.delivery_timeout_s == 120
    client.set_delivery_timeout_s(None)
    assert client.delivery_timeout_s is None
    with pytest.raises(ValueError, match="positive int"):
        client.set_delivery_timeout_s(0)
    with pytest.raises(ValueError, match="positive int"):
        client.set_delivery_timeout_s(-5)
    await client.close()
    store.close()


def test_delivery_timeout_ctor_validates(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.sqlite3")
    with pytest.raises(ValueError, match="positive int"):
        WpsClient(
            lambda: _FakeStream(),
            store,
            my_call="M0ABC",
            name="Tester",
            delivery_timeout_s=0,
        )
    with pytest.raises(ValueError, match="positive int"):
        WpsClient(
            lambda: _FakeStream(),
            store,
            my_call="M0ABC",
            name="Tester",
            delivery_timeout_s=-1,
        )
    store.close()


# ---------------------------------------------------------------------------
# Inbound edit handlers + sender-side local-store update + edit timeouts
# + edit-aware retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_med_updates_existing_dm_row(tmp_path: Path) -> None:
    """Real-time `med` overwrites the body and bumps edit_ts on the
    existing message row; bumps the global last_edit cursor so the
    next reconnect doesn't re-fetch this same edit."""
    client, store, stream = await _make_connected_client(tmp_path)
    # Pre-existing row from a prior `m` arrival.
    store.upsert_message(
        {"_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
         "m": "original", "ts": 100}
    )
    stream.push(
        codec.encode(
            {"t": "med", "_id": "100-M0FOO", "m": "edited body", "edts": 200}
        )
    )
    for _ in range(20):
        await asyncio.sleep(0.01)
        row = store.lookup_message_by_id("100-M0FOO")
        if row is not None and row["body"] == "edited body":
            break
    row = store.lookup_message_by_id("100-M0FOO")
    assert row is not None
    assert row["body"] == "edited body"
    assert row["edit_ts"] == 200
    # Cursor bumped — the connect record will use it next time.
    rec = store.connect_record("Tester", "M0ABC", 0.92)
    assert rec["led"] >= 200
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_inbound_med_for_unknown_msg_is_ignored(tmp_path: Path) -> None:
    """Edit landing for a message we don't have locally must not
    create a phantom row (messages.to_call is NOT NULL — INSERT would
    fail); the apply_*_edit path is UPDATE-only so the row count is 0
    and the handler silently no-ops."""
    client, store, stream = await _make_connected_client(tmp_path)
    stream.push(
        codec.encode(
            {"t": "med", "_id": "999-M0XYZ", "m": "ghost", "edts": 999}
        )
    )
    await asyncio.sleep(0.05)
    assert store.lookup_message_by_id("999-M0XYZ") is None
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_inbound_medb_uses_med_array_key(tmp_path: Path) -> None:
    """Wire shape uses the `med` key for the array, not `m`. The
    historical bug was reading `o["m"]` and silently dropping every
    edit during a reconnect — this regression test pins the key."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.upsert_message(
        {"_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
         "m": "v1", "ts": 100}
    )
    store.upsert_message(
        {"_id": "200-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
         "m": "v1", "ts": 200}
    )
    stream.push(
        codec.encode(
            {"t": "medb", "med": [
                {"_id": "100-M0FOO", "m": "v2", "edts": 150},
                {"_id": "200-M0FOO", "m": "v2", "edts": 250},
            ]}
        )
    )
    for _ in range(20):
        await asyncio.sleep(0.01)
        a = store.lookup_message_by_id("100-M0FOO")
        b = store.lookup_message_by_id("200-M0FOO")
        if a and a["body"] == "v2" and b and b["body"] == "v2":
            break
    a = store.lookup_message_by_id("100-M0FOO")
    b = store.lookup_message_by_id("200-M0FOO")
    assert a is not None and a["body"] == "v2" and a["edit_ts"] == 150
    assert b is not None and b["body"] == "v2" and b["edit_ts"] == 250
    rec = store.connect_record("Tester", "M0ABC", 0.92)
    assert rec["led"] == 250
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_inbound_cped_updates_post_and_channel_cursor(tmp_path: Path) -> None:
    """Real-time `cped` rewrites the post body, bumps edit_ts, and
    bumps the per-channel last_edit cursor (which feeds the channel's
    `led` field in the connect record)."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.set_subscription(7, True)
    store.upsert_post(7, {"ts": 1000, "fc": "G7BAR", "p": "first"})
    stream.push(
        codec.encode(
            {"t": "cped", "cid": 7, "ts": 1000, "p": "fixed", "edts": 1500}
        )
    )
    for _ in range(20):
        await asyncio.sleep(0.01)
        row = store.lookup_post(7, 1000)
        if row is not None and row["body"] == "fixed":
            break
    row = store.lookup_post(7, 1000)
    assert row is not None
    assert row["body"] == "fixed"
    assert row["edit_ts"] == 1500
    rec = store.connect_record("Tester", "M0ABC", 0.92)
    cc = next(c for c in rec["cc"] if c["cid"] == 7)
    assert cc["led"] >= 1500
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_inbound_cpedb_uses_ed_array_key(tmp_path: Path) -> None:
    """Connect-batch post edits use the `ed` array key per the server
    source and the web client. Each entry carries cid+ts+p+edts."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.set_subscription(7, True)
    store.upsert_post(7, {"ts": 1000, "fc": "G7BAR", "p": "v1"})
    store.upsert_post(7, {"ts": 2000, "fc": "G7BAR", "p": "v1"})
    stream.push(
        codec.encode(
            {"t": "cpedb", "ed": [
                {"cid": 7, "ts": 1000, "p": "v2", "edts": 1100},
                {"cid": 7, "ts": 2000, "p": "v2", "edts": 2100},
            ]}
        )
    )
    for _ in range(20):
        await asyncio.sleep(0.01)
        a = store.lookup_post(7, 1000)
        b = store.lookup_post(7, 2000)
        if a and a["body"] == "v2" and b and b["body"] == "v2":
            break
    a = store.lookup_post(7, 1000)
    b = store.lookup_post(7, 2000)
    assert a["body"] == "v2" and a["edit_ts"] == 1100
    assert b["body"] == "v2" and b["edit_ts"] == 2100
    rec = store.connect_record("Tester", "M0ABC", 0.92)
    cc = next(c for c in rec["cc"] if c["cid"] == 7)
    assert cc["led"] == 2100
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_edit_message_writes_local_row_immediately(tmp_path: Path) -> None:
    """Sender doesn't get the edit echoed back from the server (only
    `mr`), so the only way the sender's UI reflects the edit is if
    edit_message updates the local row at send time."""
    client, store, stream = await _make_connected_client(tmp_path)
    msg_id = await client.send_message("M0FOO", "original")
    await client.edit_message(msg_id, "fixed")
    row = store.lookup_message_by_id(msg_id)
    assert row is not None
    assert row["body"] == "fixed"
    assert row["edit_ts"] is not None and row["edit_ts"] > 0
    # And the wire frame carrying the edit was actually sent.
    sent = [s for s in stream.sent if b'"t":"med"' in s]
    assert len(sent) == 1
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_edit_post_writes_local_row_immediately(tmp_path: Path) -> None:
    client, store, stream = await _make_connected_client(tmp_path)
    store.set_subscription(7, True)
    ts = await client.post(7, "original")
    await client.edit_post(7, ts, "fixed")
    row = store.lookup_post(7, ts)
    assert row is not None
    assert row["body"] == "fixed"
    assert row["edit_ts"] is not None and row["edit_ts"] > 0
    sent = [s for s in stream.sent if b'"t":"cped"' in s]
    assert len(sent) == 1
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_edit_message_refuses_other_users_dms(tmp_path: Path) -> None:
    """edit_message raises ValueError when the row's from_call isn't ours
    — the UI surfaces this as ``[Cannot edit other users DMs]``."""
    client, store, stream = await _make_connected_client(tmp_path)
    foreign_id = "1700000000000-M0FOO"
    store.upsert_message(
        {
            "_id": foreign_id,
            "fc": "M0FOO",
            "tc": "M0ABC",
            "m": "hello",
            "ts": 1_700_000_000_000,
        }
    )
    with pytest.raises(ValueError, match="Cannot edit other users DMs"):
        await client.edit_message(foreign_id, "hijacked")
    # No `med` frame went out, and the row is unchanged.
    assert not any(b'"t":"med"' in s for s in stream.sent)
    row = store.lookup_message_by_id(foreign_id)
    assert row is not None and row["body"] == "hello"
    assert row["edit_ts"] is None
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_edit_post_refuses_other_users_posts(tmp_path: Path) -> None:
    """edit_post raises ValueError when the post's from_call isn't ours."""
    client, store, stream = await _make_connected_client(tmp_path)
    foreign_ts = 1_700_000_000_000
    store.upsert_post(
        7,
        {"ts": foreign_ts, "fc": "M0FOO", "p": "hello"},
    )
    with pytest.raises(ValueError, match="Cannot edit other users posts"):
        await client.edit_post(7, foreign_ts, "hijacked")
    assert not any(b'"t":"cped"' in s for s in stream.sent)
    row = store.lookup_post(7, foreign_ts)
    assert row is not None and row["body"] == "hello"
    assert row["edit_ts"] is None
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_edit_message_refuses_unknown_id(tmp_path: Path) -> None:
    client, store, stream = await _make_connected_client(tmp_path)
    with pytest.raises(ValueError, match="no local message"):
        await client.edit_message("9999999999999-M0NOPE", "anything")
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_edit_post_refuses_unknown_row(tmp_path: Path) -> None:
    client, store, stream = await _make_connected_client(tmp_path)
    with pytest.raises(ValueError, match="no local post"):
        await client.edit_post(7, 1_777_777_777_777, "anything")
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_edit_dm_timeout_fires_when_unacked(tmp_path: Path) -> None:
    """An edit's `mr` ack failing to arrive in time fires
    ``_delivery_timeout`` with ``is_edit=True`` and the same lid the
    UI uses to drive /retrydm."""
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=1
    )
    msg_id = await client.send_message("M0FOO", "hi")
    # Ack the original send so its timer is cancelled — only the edit
    # timeout should fire.
    stream.push(codec.encode({"t": "mr", "_id": msg_id}))
    await asyncio.sleep(0.1)
    events.clear()
    await client.edit_message(msg_id, "fixed")
    for _ in range(30):
        await asyncio.sleep(0.1)
        if any(e.get("t") == "_delivery_timeout" for e in events):
            break
    matches = [e for e in events if e.get("t") == "_delivery_timeout"]
    assert len(matches) == 1
    e = matches[0]
    assert e["kind"] == "dm"
    assert e["is_edit"] is True
    assert e["msg_id"] == msg_id
    assert isinstance(e["lid"], int) and e["lid"] > 0
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_edit_post_timeout_fires_when_unacked(tmp_path: Path) -> None:
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=1
    )
    store.set_subscription(7, True)
    ts = await client.post(7, "hi")
    stream.push(codec.encode({"t": "cpr", "ts": ts, "dts": ts + 5}))
    await asyncio.sleep(0.1)
    events.clear()
    await client.edit_post(7, ts, "fixed")
    for _ in range(30):
        await asyncio.sleep(0.1)
        if any(e.get("t") == "_delivery_timeout" for e in events):
            break
    matches = [e for e in events if e.get("t") == "_delivery_timeout"]
    assert len(matches) == 1
    e = matches[0]
    assert e["kind"] == "post"
    assert e["is_edit"] is True
    assert e["cid"] == 7
    assert e["ts"] == ts
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_edit_dm_timeout_cancelled_by_mr_ack(tmp_path: Path) -> None:
    """`mr` ack arriving for an edit (same wire shape as for the
    original send) must cancel the pending-edit timer alongside the
    original-send timer."""
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=2
    )
    msg_id = await client.send_message("M0FOO", "hi")
    stream.push(codec.encode({"t": "mr", "_id": msg_id}))
    await asyncio.sleep(0.1)
    events.clear()
    await client.edit_message(msg_id, "fixed")
    stream.push(codec.encode({"t": "mr", "_id": msg_id}))
    await asyncio.sleep(2.5)
    assert not any(e.get("t") == "_delivery_timeout" for e in events)
    # Pending-edit map cleared.
    assert client._pending_dm_edits == {}
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_edit_post_timeout_cancelled_by_cpr_ack(tmp_path: Path) -> None:
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=2
    )
    store.set_subscription(7, True)
    ts = await client.post(7, "hi")
    stream.push(codec.encode({"t": "cpr", "ts": ts, "dts": ts + 5}))
    await asyncio.sleep(0.1)
    events.clear()
    await client.edit_post(7, ts, "fixed")
    stream.push(codec.encode({"t": "cpr", "ts": ts, "dts": ts + 9}))
    await asyncio.sleep(2.5)
    assert not any(e.get("t") == "_delivery_timeout" for e in events)
    assert client._pending_post_edits == {}
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_newer_edit_supersedes_older_pending_edit(tmp_path: Path) -> None:
    """A second edit before the first's ack arrives replaces the
    pending-edit token; the first timer's late-firing path sees a
    mismatched token and silently no-ops, so we don't get a phantom
    timeout for an edit the user already overwrote."""
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=1
    )
    msg_id = await client.send_message("M0FOO", "hi")
    stream.push(codec.encode({"t": "mr", "_id": msg_id}))
    await asyncio.sleep(0.1)
    events.clear()
    await client.edit_message(msg_id, "edit-a")
    # Quickly supersede with a second edit.
    await asyncio.sleep(0.2)
    await client.edit_message(msg_id, "edit-b")
    # The first timer would have fired around 1s after edit-a; wait
    # past edit-a's deadline but before edit-b's. Only one should
    # eventually fire (the edit-b one), and its lid should match.
    for _ in range(40):
        await asyncio.sleep(0.1)
        if any(e.get("t") == "_delivery_timeout" for e in events):
            break
    matches = [e for e in events if e.get("t") == "_delivery_timeout"]
    assert len(matches) == 1
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_resend_message_for_edited_row_emits_med(tmp_path: Path) -> None:
    """If the row has an edit_ts, /retrydm (which calls resend_message)
    re-emits the latest edit as a `med` — that's the user's pending
    operation and what they actually want delivered."""
    client, store, stream = await _make_connected_client(tmp_path)
    msg_id = await client.send_message("M0FOO", "v1")
    await client.edit_message(msg_id, "v2")
    before = list(stream.sent)
    await client.resend_message(msg_id)
    new_frames = stream.sent[len(before):]
    med_frames = [f for f in new_frames if b'"t":"med"' in f]
    m_frames = [f for f in new_frames if b'"t":"m"' in f and b'"t":"med"' not in f]
    assert len(med_frames) == 1
    assert m_frames == []
    frame = med_frames[0]
    assert f'"_id":"{msg_id}"'.encode() in frame
    assert b'"m":"v2"' in frame
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_resend_message_for_unedited_row_emits_m(tmp_path: Path) -> None:
    """No edit_ts → /retrydm falls back to the original `m` frame
    (existing pre-edit-aware behaviour)."""
    client, store, stream = await _make_connected_client(tmp_path)
    msg_id = await client.send_message("M0FOO", "v1")
    before = list(stream.sent)
    await client.resend_message(msg_id)
    new_frames = stream.sent[len(before):]
    m_frames = [
        f for f in new_frames if b'"t":"m"' in f and b'"t":"med"' not in f
    ]
    med_frames = [f for f in new_frames if b'"t":"med"' in f]
    assert len(m_frames) == 1
    assert med_frames == []
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_resend_post_for_edited_row_emits_cped(tmp_path: Path) -> None:
    client, store, stream = await _make_connected_client(tmp_path)
    store.set_subscription(7, True)
    ts = await client.post(7, "v1")
    await client.edit_post(7, ts, "v2")
    before = list(stream.sent)
    await client.resend_post(7, ts)
    new_frames = stream.sent[len(before):]
    cped_frames = [f for f in new_frames if b'"t":"cped"' in f]
    cp_frames = [
        f for f in new_frames if b'"t":"cp"' in f and b'"t":"cped"' not in f
    ]
    assert len(cped_frames) == 1
    assert cp_frames == []
    frame = cped_frames[0]
    assert f'"ts":{ts}'.encode() in frame
    assert b'"p":"v2"' in frame
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_edit_timeout_disabled_when_none(tmp_path: Path) -> None:
    """``delivery_timeout_s=None`` disables the edit timer machinery
    too — symmetry with the original-send path."""
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=None
    )
    msg_id = await client.send_message("M0FOO", "hi")
    await client.edit_message(msg_id, "fixed")
    await asyncio.sleep(0.5)
    assert not any(e.get("t") == "_delivery_timeout" for e in events)
    assert client._dm_edit_timeout_tasks == {}
    assert client._pending_dm_edits == {}
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_close_cancels_pending_edit_timers(tmp_path: Path) -> None:
    client, store, stream, events = await _make_client_with_timeout(
        tmp_path, delivery_timeout_s=10
    )
    msg_id = await client.send_message("M0FOO", "hi")
    await client.edit_message(msg_id, "fixed")
    store.set_subscription(7, True)
    ts = await client.post(7, "hi")
    await client.edit_post(7, ts, "fixed-post")
    assert client._dm_edit_timeout_tasks
    assert client._post_edit_timeout_tasks
    await client.close()
    assert client._dm_edit_timeout_tasks == {}
    assert client._post_edit_timeout_tasks == {}
    assert client._pending_dm_edits == {}
    assert client._pending_post_edits == {}
    store.close()


@pytest.mark.asyncio
async def test_inbound_own_post_via_cpb_seeds_delivered_ts(tmp_path: Path) -> None:
    """A `cpb` backfill carrying a post whose `fc` is our callsign means
    the server has it — even if it was sent from a different client
    instance, or after a state-dir wipe. The store must record a
    non-NULL `delivered_ts` so the UI doesn't render the row as if it's
    still pending an ack."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    stream.push(codec.encode({
        "t": "cpb",
        "cid": 7,
        "p": [
            {"ts": 1_700_000_001_000, "fc": "M0ABC", "p": "from us"},
            {"ts": 1_700_000_002_000, "fc": "M0FOO", "p": "from peer"},
        ],
    }))
    for _ in range(40):
        await asyncio.sleep(0.01)
        if store.lookup_post(7, 1_700_000_001_000):
            break
    own = store.lookup_post(7, 1_700_000_001_000)
    peer = store.lookup_post(7, 1_700_000_002_000)
    assert own is not None and peer is not None
    # Our own post comes back from the server → delivered_ts seeded.
    assert own["delivered_ts"] == 1_700_000_001_000
    # Peer post is "received-only" — delivered_ts is sender-side metadata
    # and stays NULL for inbound messages from other callsigns.
    assert peer["delivered_ts"] is None
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_inbound_own_dm_via_mb_seeds_delivered_ts(tmp_path: Path) -> None:
    """Same as the post case but for `mb` — DM backfill carrying our own
    DMs (e.g. another client instance's send) must record delivered_ts
    so the UI doesn't dim the row as still-pending."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    # DM `ts` is in seconds on the wire; delivered_ts is in ms.
    stream.push(codec.encode({
        "t": "mb",
        "m": [
            {"_id": "1700000001000-M0ABC", "fc": "M0ABC", "tc": "M0FOO",
             "m": "from us", "ts": 1_700_000_001},
        ],
    }))
    for _ in range(40):
        await asyncio.sleep(0.01)
        if store.lookup_message_by_id("1700000001000-M0ABC"):
            break
    row = store.lookup_message_by_id("1700000001000-M0ABC")
    assert row is not None
    assert row["delivered_ts"] == 1_700_000_001 * 1000


@pytest.mark.asyncio
async def test_cpr_ack_overrides_synthetic_delivered_ts(tmp_path: Path) -> None:
    """If a `cpb` backfill seeds `delivered_ts` from the row's own ts,
    a later authoritative `cpr` ack with `dts` must still take
    precedence. mark_post_delivered uses a plain UPDATE so the ack
    timestamp wins over the synthetic seed."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    stream = _FakeStream()
    client = WpsClient(
        lambda: stream,
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )
    await client.open()
    stream.push(codec.encode({
        "t": "cpb",
        "cid": 7,
        "p": [{"ts": 1_700_000_001_000, "fc": "M0ABC", "p": "x"}],
    }))
    for _ in range(40):
        await asyncio.sleep(0.01)
        if store.lookup_post(7, 1_700_000_001_000):
            break
    assert store.lookup_post(7, 1_700_000_001_000)["delivered_ts"] == 1_700_000_001_000
    stream.push(codec.encode({"t": "cpr", "ts": 1_700_000_001_000, "dts": 1_700_000_001_500}))
    for _ in range(40):
        await asyncio.sleep(0.01)
        if store.lookup_post(7, 1_700_000_001_000)["delivered_ts"] == 1_700_000_001_500:
            break
    assert store.lookup_post(7, 1_700_000_001_000)["delivered_ts"] == 1_700_000_001_500
    await client.close()
    store.close()


# ---------------------------------------------------------------------------
# Reactions: outbound react_*, inbound mem/memb/cpem/cpemb
# ---------------------------------------------------------------------------


async def _wait_for(predicate, *, attempts: int = 40, interval: float = 0.01) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@pytest.mark.asyncio
async def test_react_message_persists_locally(tmp_path: Path) -> None:
    """WPS doesn't echo DM reactions back to the sender (the relay path
    in `wps.py` targets `message_to_update['fc']`). The client must
    write the row locally on outbound so the UI can show it
    immediately."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.upsert_message(
        {"_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": 100}
    )
    await client.react_message("100-M0FOO", "1f44d")
    rows = store.list_message_emojis("100-M0FOO")
    assert rows == [
        {"emoji": "1f44d", "callsign": "M0ABC", "emoji_ts": rows[0]["emoji_ts"]}
    ]
    # Wire frame matches the documented `mem` shape.
    sent = [s for s in stream.sent if b'"t":"mem"' in s]
    assert len(sent) == 1
    assert b'"a":1' in sent[0]
    assert b'"_id":"100-M0FOO"' in sent[0]
    assert b'"e":"1f44d"' in sent[0]
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_react_post_persists_locally(tmp_path: Path) -> None:
    """Post-reactions: `post_emoji_handler` in wps.py also skips the
    sender. Outbound write goes to the local store with our callsign."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.set_subscription(7, True)
    store.upsert_post(7, {"ts": 1_700_000_000, "fc": "M0FOO", "p": "hi"})
    await client.react_post(7, 1_700_000_000, "1f389")
    rows = store.list_post_emojis(7, 1_700_000_000)
    assert {(r["emoji"], r["callsign"]) for r in rows} == {("1f389", "M0ABC")}
    sent = [s for s in stream.sent if b'"t":"cpem"' in s]
    assert len(sent) == 1
    assert b'"cid":7' in sent[0]
    assert b'"ts":1700000000' in sent[0]
    assert b'"e":"1f389"' in sent[0]
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_react_message_remove_clears_local_row(tmp_path: Path) -> None:
    client, store, stream = await _make_connected_client(tmp_path)
    store.upsert_message(
        {"_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": 100}
    )
    await client.react_message("100-M0FOO", "1f44d", add=True)
    assert store.list_message_emojis("100-M0FOO")
    await client.react_message("100-M0FOO", "1f44d", add=False)
    assert store.list_message_emojis("100-M0FOO") == []
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_react_message_normalises_literal_to_hex_codepoint(tmp_path: Path) -> None:
    """Literal-character pickers (the TUI emoji grid, OS emoji
    keyboards) feed `react_message` a single Unicode char. The wire
    form per MESSAGES.md is the hex codepoint string, and that's what
    the web client and other peers expect — so the client must
    normalise before sending and storing."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.upsert_message(
        {"_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": 100}
    )
    await client.react_message("100-M0FOO", "👍")
    sent = [s for s in stream.sent if b'"t":"mem"' in s]
    assert len(sent) == 1
    assert b'"e":"1f44d"' in sent[0]
    rows = store.list_message_emojis("100-M0FOO")
    assert [r["emoji"] for r in rows] == ["1f44d"]
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_inbound_mem_attributes_peer_and_keeps_ours(tmp_path: Path) -> None:
    """Real-time `mem` carries the *full* current emoji list. New
    emojis we don't have a row for are attributed to the DM peer;
    rows we wrote ourselves (via outbound react_message) keep their
    callsign."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.upsert_message(
        {"_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": 100}
    )
    # We react first; outbound write attributes to us.
    await client.react_message("100-M0FOO", "2764")
    # Server then echoes a full list including a new emoji from peer.
    stream.push(codec.encode(
        {"t": "mem", "_id": "100-M0FOO", "e": ["2764", "1f44d"], "ets": 1_700_000_500}
    ))
    ok = await _wait_for(
        lambda: any(
            r["emoji"] == "1f44d" for r in store.list_message_emojis("100-M0FOO")
        )
    )
    assert ok
    rows = {r["emoji"]: r["callsign"] for r in store.list_message_emojis("100-M0FOO")}
    assert rows == {"2764": "M0ABC", "1f44d": "M0FOO"}
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_inbound_memb_batch(tmp_path: Path) -> None:
    """Connect-batch `memb` walks `mem[]` and applies each entry
    using the same attribution logic as real-time `mem`."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.upsert_message(
        {"_id": "1-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "a", "ts": 100}
    )
    store.upsert_message(
        {"_id": "2-G7BAR", "fc": "G7BAR", "tc": "M0ABC", "m": "b", "ts": 200}
    )
    stream.push(codec.encode({
        "t": "memb",
        "mem": [
            {"_id": "1-M0FOO", "e": ["1f44d"], "ets": 1_700_000_001},
            {"_id": "2-G7BAR", "e": ["1f603", "2764"], "ets": 1_700_000_002},
        ],
    }))
    ok = await _wait_for(
        lambda: store.list_message_emojis("1-M0FOO")
        and store.list_message_emojis("2-G7BAR")
    )
    assert ok
    a = store.list_message_emojis("1-M0FOO")
    b = store.list_message_emojis("2-G7BAR")
    assert {(r["emoji"], r["callsign"]) for r in a} == {("1f44d", "M0FOO")}
    assert {(r["emoji"], r["callsign"]) for r in b} == {
        ("1f603", "G7BAR"),
        ("2764", "G7BAR"),
    }
    # `last_emoji` cursor advanced to the highest ets.
    record = store.connect_record(name="T", callsign="M0ABC", version=0.1)
    assert record["le"] == 1_700_000_002
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_inbound_cpem_uses_server_injected_fc(tmp_path: Path) -> None:
    """`post_emoji_handler` in wps.py sets `fc = callsign` on the
    relayed object before forwarding. The client uses that callsign as
    the reactor — no inference needed."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.set_subscription(5, True)
    store.upsert_post(5, {"ts": 1_700_000_000, "fc": "M0FOO", "p": "hi"})
    stream.push(codec.encode({
        "t": "cpem",
        "a": 1,
        "cid": 5,
        "ts": 1_700_000_000,
        "ets": 1_700_000_500,
        "e": "1f44d",
        "fc": "M1XYZ",
    }))
    ok = await _wait_for(
        lambda: store.list_post_emojis(5, 1_700_000_000)
    )
    assert ok
    rows = store.list_post_emojis(5, 1_700_000_000)
    assert {(r["emoji"], r["callsign"]) for r in rows} == {("1f44d", "M1XYZ")}
    # Remove path.
    stream.push(codec.encode({
        "t": "cpem",
        "a": 0,
        "cid": 5,
        "ts": 1_700_000_000,
        "ets": 1_700_000_600,
        "e": "1f44d",
        "fc": "M1XYZ",
    }))
    ok = await _wait_for(
        lambda: not store.list_post_emojis(5, 1_700_000_000)
    )
    assert ok
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_inbound_cpemb_replaces_full_state(tmp_path: Path) -> None:
    """`cpemb` always carries the *latest complete* state per
    `(cid, ts)` — a removed reaction won't appear in the batch and
    must be dropped locally."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.set_subscription(5, True)
    store.upsert_post(5, {"ts": 1_700_000_000, "fc": "M0FOO", "p": "hi"})
    # Pre-existing local row that the batch will *not* re-affirm.
    store.upsert_post_emoji(5, 1_700_000_000, "STALE", "OLDCALL", 1)
    stream.push(codec.encode({
        "t": "cpemb",
        "e": [
            {
                "cid": 5,
                "ts": 1_700_000_000,
                "ets": 1_700_000_900,
                "e": [
                    {"e": "1f44d", "c": ["M1ABC", "M2DEF"]},
                    {"e": "2764", "c": ["M3GHI"]},
                ],
            }
        ],
    }))
    ok = await _wait_for(
        lambda: any(
            r["emoji"] == "2764"
            for r in store.list_post_emojis(5, 1_700_000_000)
        )
    )
    assert ok
    rows = store.list_post_emojis(5, 1_700_000_000)
    assert {(r["emoji"], r["callsign"]) for r in rows} == {
        ("1f44d", "M1ABC"),
        ("1f44d", "M2DEF"),
        ("2764", "M3GHI"),
    }
    await client.close()
    store.close()


@pytest.mark.asyncio
async def test_inbound_cpb_persists_embedded_reactions(tmp_path: Path) -> None:
    """Mid-session `/sub` flow (cs → cpb) doesn't trigger a follow-up
    `cpemb` from the server — historic reactions ride inline on each
    post as `e: [{e, c[]}, ...]` plus an `ets` cursor (see
    `dbGetPostsBatch` in `wps/db.py` — only `dts`/`t`/`cid` are
    stripped). Without applying that embedded state, reactions on
    historic posts stay invisible until the next reconnect, when the
    connect handler's `cpemb` finally delivers them."""
    client, store, stream = await _make_connected_client(tmp_path)
    store.set_subscription(7, True)
    stream.push(codec.encode({
        "t": "cpb",
        "cid": 7,
        "p": [
            {
                "ts": 1_700_000_001_000,
                "fc": "M0FOO",
                "p": "first",
                "ets": 1_700_000_500_000,
                "e": [
                    {"e": "1f44d", "c": ["M1ABC", "M2DEF"]},
                    {"e": "2764", "c": ["M3GHI"]},
                ],
            },
            {
                # Post with no reactions: no `e`/`ets` — handler must
                # leave its (empty) emoji table untouched.
                "ts": 1_700_000_002_000,
                "fc": "M0FOO",
                "p": "second",
            },
        ],
    }))
    ok = await _wait_for(
        lambda: bool(store.list_post_emojis(7, 1_700_000_001_000))
        and store.lookup_post(7, 1_700_000_002_000) is not None
    )
    assert ok
    rows = store.list_post_emojis(7, 1_700_000_001_000)
    assert {(r["emoji"], r["callsign"]) for r in rows} == {
        ("1f44d", "M1ABC"),
        ("1f44d", "M2DEF"),
        ("2764", "M3GHI"),
    }
    assert store.list_post_emojis(7, 1_700_000_002_000) == []
    # `last_emoji` cursor advanced so the next reconnect's connect
    # record won't ask for these again via `cpemb`.
    record = store.connect_record(name="T", callsign="M0ABC", version=0.1)
    cc = {entry["cid"]: entry for entry in record.get("cc", [])}
    assert cc[7]["le"] == 1_700_000_500_000
    await client.close()
    store.close()
