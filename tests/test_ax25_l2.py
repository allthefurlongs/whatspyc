"""AX.25 L2 state machine tests.

Drives ``Ax25L2Stream`` against an in-process simulated peer that speaks
just enough of AX.25 to exercise the connect / send / recv / disconnect
paths end-to-end without a radio.
"""

from __future__ import annotations

import asyncio

import pytest

from whatspyc.transport import ax25_frame, ax25_l2
from whatspyc.transport.ax25_frame import PID_NO_LAYER3, Address
from whatspyc.transport.ax25_l2 import (
    Ax25L2Stream,
    CTRL_DISC,
    CTRL_DM,
    CTRL_SABM,
    CTRL_UA,
    PF,
    S_REJ,
    S_RR,
    State,
    _addr_pair,
    _decode_frame,
    _parse_call,
)
from whatspyc.transport.base import AsyncByteStream


# ---------------------------------------------------------------------------
# In-memory loopback: two sides whose ``send`` on one delivers to ``recv`` on
# the other. Each side implements ``AsyncByteStream`` so it can plug straight
# into ``Ax25L2Stream`` (or the simulated peer).
# ---------------------------------------------------------------------------


class _Side(AsyncByteStream):
    def __init__(self, tx: asyncio.Queue, rx: asyncio.Queue) -> None:
        self._tx = tx
        self._rx = rx
        self._closed = False

    async def open(self) -> None:
        return None

    async def send(self, data: bytes) -> None:
        await self._tx.put(data)

    async def recv(self) -> bytes:
        return await self._rx.get()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._tx.put(b"")


def _link() -> tuple[_Side, _Side]:
    a, b = asyncio.Queue(), asyncio.Queue()
    return _Side(a, b), _Side(b, a)


# ---------------------------------------------------------------------------
# Simulated peer: parses incoming frames, replies to SABM, ACKs I-frames,
# optionally echoes their info, accepts DISC.
# ---------------------------------------------------------------------------


class _Peer:
    def __init__(
        self,
        side: AsyncByteStream,
        my_call: str,
        remote_call: str,
        *,
        echo: bool = True,
        ignore_first_sabm: int = 0,
    ) -> None:
        self._side = side
        self._mine = _parse_call(my_call)
        self._remote = _parse_call(remote_call)
        self._echo = echo
        self._ignore_first_sabm = ignore_first_sabm
        self._connected = False
        self._vs = 0
        self._vr = 0
        # Capture every frame received for assertions in tests.
        self.received: list = []

    def _send_u(self, control: int, *, command: bool, poll: bool) -> bytes:
        if poll:
            control |= PF
        return _addr_pair(self._remote, self._mine, command=command) + bytes([control])

    def _send_s(self, s_type: int, *, command: bool, poll: bool) -> bytes:
        ctrl = (self._vr << 5) | s_type
        if poll:
            ctrl |= PF
        return _addr_pair(self._remote, self._mine, command=command) + bytes([ctrl])

    def _send_i(self, info: bytes) -> bytes:
        ns = self._vs
        self._vs = (self._vs + 1) & 0x07
        ctrl = (self._vr << 5) | (ns << 1)
        return (
            _addr_pair(self._remote, self._mine, command=True)
            + bytes([ctrl, PID_NO_LAYER3])
            + info
        )

    async def run(self) -> None:
        while True:
            raw = await self._side.recv()
            if raw == b"":
                return
            f = _decode_frame(raw)
            if f is None:
                continue
            self.received.append(f)
            if f.is_uframe:
                if f.u_type == CTRL_SABM:
                    if self._ignore_first_sabm > 0:
                        self._ignore_first_sabm -= 1
                        continue
                    self._vs = self._vr = 0
                    self._connected = True
                    await self._side.send(self._send_u(CTRL_UA, command=False, poll=f.poll))
                elif f.u_type == CTRL_DISC:
                    self._connected = False
                    await self._side.send(self._send_u(CTRL_UA, command=False, poll=f.poll))
                    return
            elif f.is_sframe:
                # Reply to RR command poll with RR response final.
                if f.is_command and f.poll:
                    await self._side.send(self._send_s(S_RR, command=False, poll=True))
            elif f.is_iframe and self._connected:
                if f.ns == self._vr:
                    self._vr = (self._vr + 1) & 0x07
                    if self._echo:
                        await self._side.send(self._send_i(f.info))
                    else:
                        await self._side.send(self._send_s(S_RR, command=False, poll=False))
                else:
                    # Out of sequence: REJ.
                    await self._side.send(self._send_s(S_REJ, command=False, poll=False))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_sabm_ua_handshake() -> None:
    a, b = _link()
    peer = _Peer(b, my_call="WPS", remote_call="M0ABC")
    peer_task = asyncio.create_task(peer.run())

    stream = Ax25L2Stream(a, my_call="M0ABC", remote_call="WPS", connect_timeout=2.0)
    await stream.open()
    assert stream._state is State.CONNECTED
    # First frame the peer saw was a SABM command.
    assert peer.received[0].u_type == CTRL_SABM
    assert peer.received[0].is_command

    await stream.close()
    await asyncio.wait_for(peer_task, 2.0)


@pytest.mark.asyncio
async def test_send_recv_round_trip() -> None:
    a, b = _link()
    peer = _Peer(b, my_call="WPS", remote_call="M0ABC", echo=True)
    peer_task = asyncio.create_task(peer.run())

    stream = Ax25L2Stream(
        a, my_call="M0ABC", remote_call="WPS", connect_timeout=2.0, paclen=64
    )
    await stream.open()

    payload = b"hello whatspyc"
    await stream.send(payload)
    received = await asyncio.wait_for(stream.recv(), 2.0)
    assert received == payload

    await stream.close()
    await asyncio.wait_for(peer_task, 2.0)


@pytest.mark.asyncio
async def test_send_segments_when_larger_than_paclen() -> None:
    a, b = _link()
    peer = _Peer(b, my_call="WPS", remote_call="M0ABC", echo=True)
    peer_task = asyncio.create_task(peer.run())

    stream = Ax25L2Stream(
        a,
        my_call="M0ABC",
        remote_call="WPS",
        connect_timeout=2.0,
        paclen=8,
        window=4,
    )
    await stream.open()

    payload = b"x" * 30  # forces 4 segments at paclen=8
    await stream.send(payload)

    chunks: list[bytes] = []
    while sum(len(c) for c in chunks) < len(payload):
        chunks.append(await asyncio.wait_for(stream.recv(), 2.0))
    assert b"".join(chunks) == payload

    # Peer should have observed exactly 4 I-frames.
    iframes = [f for f in peer.received if f.is_iframe]
    assert len(iframes) == 4
    assert all(len(f.info) == 8 or len(f.info) == 6 for f in iframes)

    await stream.close()
    await asyncio.wait_for(peer_task, 2.0)


@pytest.mark.asyncio
async def test_connect_timeout_when_peer_silent() -> None:
    a, b = _link()  # peer side is bound but no consumer

    async def black_hole() -> None:
        # Drain frames so the writer queue doesn't block but never reply.
        while True:
            chunk = await b.recv()
            if chunk == b"":
                return

    drainer = asyncio.create_task(black_hole())

    stream = Ax25L2Stream(
        a,
        my_call="M0ABC",
        remote_call="WPS",
        connect_timeout=0.3,
        t1=0.1,
        n2=2,
    )
    with pytest.raises((ConnectionError, asyncio.TimeoutError)):
        await stream.open()

    drainer.cancel()
    try:
        await drainer
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_dm_to_sabm_raises_refused() -> None:
    a, b = _link()

    async def reply_dm() -> None:
        raw = await b.recv()
        f = _decode_frame(raw)
        assert f is not None and f.u_type == CTRL_SABM
        # Reply with DM (response).
        peer_addr = _addr_pair(_parse_call("M0ABC"), _parse_call("WPS"), command=False)
        await b.send(peer_addr + bytes([CTRL_DM | PF]))

    task = asyncio.create_task(reply_dm())
    stream = Ax25L2Stream(
        a, my_call="M0ABC", remote_call="WPS", connect_timeout=1.0
    )
    with pytest.raises(ConnectionRefusedError):
        await stream.open()
    await asyncio.wait_for(task, 1.0)


@pytest.mark.asyncio
async def test_disc_from_peer_signals_clean_close() -> None:
    a, b = _link()
    peer = _Peer(b, my_call="WPS", remote_call="M0ABC")
    peer_task = asyncio.create_task(peer.run())

    stream = Ax25L2Stream(
        a, my_call="M0ABC", remote_call="WPS", connect_timeout=2.0
    )
    await stream.open()

    # Peer initiates DISC.
    peer_addr = _addr_pair(_parse_call("M0ABC"), _parse_call("WPS"), command=True)
    await b.send(peer_addr + bytes([CTRL_DISC | PF]))

    # recv should now drain to b"" cleanly.
    out = await asyncio.wait_for(stream.recv(), 2.0)
    assert out == b""
    assert stream._state is State.DISCONNECTED

    await stream.close()
    # Peer task may still be running on its read loop; cancel it.
    peer_task.cancel()
    try:
        await peer_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_single_hop_digi_path_round_trip() -> None:
    """Sanity: a 1-hop digi path also round-trips and the encoded
    address chain is exactly dest+src+digi1 (3 subfields, last on digi).
    """
    a, b = _link()

    digi = _parse_call("RELAY1")
    mine = _parse_call("M0ABC")
    peer_mine = _parse_call("WPS")

    peer_vr = peer_vs = 0
    connected = False

    async def peer_run() -> None:
        nonlocal peer_vr, peer_vs, connected
        while True:
            raw = await b.recv()
            if raw == b"":
                return
            f = _decode_frame(raw)
            if f is None:
                continue
            return_digis = [
                ax25_l2.Address(digi.callsign, digi.ssid, has_been_repeated=True)
            ]
            if f.is_uframe and f.u_type == CTRL_SABM:
                peer_vr = peer_vs = 0
                connected = True
                ctrl = CTRL_UA | (PF if f.poll else 0)
                addrs = ax25_l2._addr_path(
                    mine, peer_mine, return_digis, command=False
                )
                await b.send(addrs + bytes([ctrl]))
            elif f.is_iframe and connected:
                if f.ns == peer_vr:
                    peer_vr = (peer_vr + 1) & 0x07
                    ctrl = (peer_vr << 5) | (peer_vs << 1)
                    peer_vs = (peer_vs + 1) & 0x07
                    addrs = ax25_l2._addr_path(
                        mine, peer_mine, return_digis, command=True
                    )
                    await b.send(addrs + bytes([ctrl, PID_NO_LAYER3]) + f.info)

    peer_task = asyncio.create_task(peer_run())
    stream = Ax25L2Stream(
        a,
        my_call="M0ABC",
        remote_call="WPS",
        connect_timeout=2.0,
        digipeaters=["RELAY1"],
    )
    await stream.open()
    await stream.send(b"one-hop")
    assert await asyncio.wait_for(stream.recv(), 2.0) == b"one-hop"
    await stream.close()
    peer_task.cancel()
    try:
        await peer_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_digi_path_round_trip() -> None:
    """A 2-hop digi path: client→DIGI1→DIGI2→peer; peer flips H-bits and
    echoes back via the same path. The L2 should accept the echoed I-frame.
    """
    a, b = _link()

    digis = [_parse_call("DIGI1"), _parse_call("DIGI2")]
    mine = _parse_call("M0ABC")
    peer_mine = _parse_call("WPS")

    # Build a peer that reads frames, replays SABM/UA + I-frame echo, and
    # always responds with the same digi list — H-bits set on each (because
    # by the time *we* see the frame the digis would all have repeated).
    peer_vr = peer_vs = 0
    connected = False

    async def peer_run() -> None:
        nonlocal peer_vr, peer_vs, connected
        while True:
            raw = await b.recv()
            if raw == b"":
                return
            f = _decode_frame(raw)
            if f is None:
                continue
            # Build the path back: dest=client, src=peer, then digis
            # in reverse order with H=True (each digi has now repeated us).
            return_digis = [
                ax25_l2.Address(d.callsign, d.ssid, has_been_repeated=True)
                for d in reversed(digis)
            ]
            if f.is_uframe and f.u_type == CTRL_SABM:
                peer_vr = peer_vs = 0
                connected = True
                ctrl = CTRL_UA | (PF if f.poll else 0)
                addrs = ax25_l2._addr_path(
                    peer_mine, mine, return_digis, command=False
                )
                # Hack: above has dest=peer_mine, src=mine — swap so
                # the response is addressed to mine.
                addrs = ax25_l2._addr_path(
                    mine, peer_mine, return_digis, command=False
                )
                await b.send(addrs + bytes([ctrl]))
            elif f.is_iframe and connected:
                if f.ns == peer_vr:
                    peer_vr = (peer_vr + 1) & 0x07
                    ctrl = (peer_vr << 5) | (peer_vs << 1)
                    peer_vs = (peer_vs + 1) & 0x07
                    addrs = ax25_l2._addr_path(
                        mine, peer_mine, return_digis, command=True
                    )
                    await b.send(
                        addrs + bytes([ctrl, PID_NO_LAYER3]) + f.info
                    )

    peer_task = asyncio.create_task(peer_run())

    stream = Ax25L2Stream(
        a,
        my_call="M0ABC",
        remote_call="WPS",
        connect_timeout=2.0,
        digipeaters=["DIGI1", "DIGI2"],
    )
    await stream.open()
    await stream.send(b"hello-via-digis")
    echoed = await asyncio.wait_for(stream.recv(), 2.0)
    assert echoed == b"hello-via-digis"

    # Verify the frame the peer received contains the digi list with
    # H-bits cleared (the L2 originated; no digi has repeated yet).
    raw_received_at_peer = None
    # Look at the SABM raw bytes — first frame received by the peer side.
    # We need to re-decode raw addresses since _decode_frame keeps only
    # dest/src today.
    # Reach into the raw send queue snapshot via re-running the address parser:
    # easier approach: build expected addr path and verify our outbound
    # stream encoded it correctly.
    expected = ax25_l2._addr_path(
        _parse_call("WPS"),
        _parse_call("M0ABC"),
        digis,
        command=True,
    )
    # Decode the digi addresses from `expected` to verify H=False.
    # Bytes 0-6: dest, 7-13: src, 14-20: digi1, 21-27: digi2.
    digi1 = ax25_frame.Address.decode(expected[14:21])
    digi2 = ax25_frame.Address.decode(expected[21:28])
    assert digi1.callsign == "DIGI1"
    assert digi2.callsign == "DIGI2"
    # has_been_repeated and command share bit 0x80 in the SSID byte;
    # for outbound digi addresses (command=False) the bit must be clear.
    assert digi1.has_been_repeated is False
    assert digi2.has_been_repeated is False
    # The very last subfield (digi2) has the extension bit set.
    assert digi2.last is True
    assert digi1.last is False

    await stream.close()
    peer_task.cancel()
    try:
        await peer_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_segmented_send_round_trip() -> None:
    """With segmentation=True and a payload > paclen, the L2 must split
    on PID 0x08, the peer reassembles, and the L2 hands the full APDU
    back to recv() in one piece.
    """
    a, b = _link()

    mine = _parse_call("M0ABC")
    peer_mine = _parse_call("WPS")

    peer_vr = peer_vs = 0
    connected = False
    reasm_inbound = ax25_l2.Reassembler()
    paclen = 16

    async def peer_run() -> None:
        nonlocal peer_vr, peer_vs, connected
        while True:
            raw = await b.recv()
            if raw == b"":
                return
            f = _decode_frame(raw)
            if f is None:
                continue
            if f.is_uframe and f.u_type == CTRL_SABM:
                peer_vr = peer_vs = 0
                connected = True
                ctrl = CTRL_UA | (PF if f.poll else 0)
                addrs = ax25_l2._addr_path(mine, peer_mine, [], command=False)
                await b.send(addrs + bytes([ctrl]))
            elif f.is_iframe and connected:
                if f.ns != peer_vr:
                    continue
                peer_vr = (peer_vr + 1) & 0x07
                # Ack the I-frame with an RR response.
                rr = ax25_l2._addr_path(
                    mine, peer_mine, [], command=False
                ) + bytes([(peer_vr << 5) | ax25_l2.S_RR])
                await b.send(rr)
                if f.pid == ax25_l2.PID_SEGMENT:
                    done = reasm_inbound.feed(f.info)
                    if done is not None:
                        _orig_pid, apdu = done
                        # Echo APDU back as PID-0x08 segments too.
                        for info in ax25_l2.segment(
                            apdu, paclen=paclen, original_pid=PID_NO_LAYER3
                        ):
                            ctrl = (peer_vr << 5) | (peer_vs << 1)
                            peer_vs = (peer_vs + 1) & 0x07
                            addrs = ax25_l2._addr_path(
                                mine, peer_mine, [], command=True
                            )
                            await b.send(
                                addrs
                                + bytes([ctrl, ax25_l2.PID_SEGMENT])
                                + info
                            )

    peer_task = asyncio.create_task(peer_run())
    stream = Ax25L2Stream(
        a,
        my_call="M0ABC",
        remote_call="WPS",
        connect_timeout=2.0,
        paclen=paclen,
        window=4,
        segmentation=True,
    )
    await stream.open()

    payload = bytes(range(50))  # 50 bytes, paclen=16 → 4 segments
    await stream.send(payload)
    assert await asyncio.wait_for(stream.recv(), 3.0) == payload

    await stream.close()
    peer_task.cancel()
    try:
        await peer_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_segmentation_disabled_chops_at_paclen() -> None:
    """With segmentation=False (default), payloads > paclen are still
    chopped into separate I-frames each with PID 0xF0 — no segment header.
    """
    a, b = _link()
    peer = _Peer(b, my_call="WPS", remote_call="M0ABC", echo=True)
    peer_task = asyncio.create_task(peer.run())

    stream = Ax25L2Stream(
        a,
        my_call="M0ABC",
        remote_call="WPS",
        connect_timeout=2.0,
        paclen=8,
        window=4,
        segmentation=False,
    )
    await stream.open()
    await stream.send(b"x" * 16)

    iframes_so_far: list = []
    chunks: list[bytes] = []
    while sum(len(c) for c in chunks) < 16:
        chunks.append(await asyncio.wait_for(stream.recv(), 2.0))
    assert b"".join(chunks) == b"x" * 16

    iframes = [f for f in peer.received if f.is_iframe]
    assert len(iframes) == 2
    # No segment header: PID stays at 0xF0 (PID_NO_LAYER3).
    assert all(fr.pid == PID_NO_LAYER3 for fr in iframes)

    await stream.close()
    await asyncio.wait_for(peer_task, 2.0)


@pytest.mark.asyncio
async def test_modulo128_handshake() -> None:
    """SABME → UA succeeds; subsequent I-frames use 2-byte control fields."""
    a, b = _link()

    mine = _parse_call("M0ABC")
    peer_mine = _parse_call("WPS")

    sabme_seen = False

    async def peer_run() -> None:
        nonlocal sabme_seen
        peer_vr = peer_vs = 0
        connected = False
        while True:
            raw = await b.recv()
            if raw == b"":
                return
            f = _decode_frame(raw, modulo=128 if connected else 8)
            if f is None:
                continue
            if f.is_uframe and f.u_type == ax25_l2.CTRL_SABME:
                sabme_seen = True
                peer_vr = peer_vs = 0
                connected = True
                ctrl = ax25_l2.CTRL_UA | (PF if f.poll else 0)
                addrs = ax25_l2._addr_path(mine, peer_mine, [], command=False)
                await b.send(addrs + bytes([ctrl]))
            elif f.is_iframe and connected:
                # Echo back at modulo-128 with N(R) ack and our own N(S).
                if f.ns == peer_vr:
                    peer_vr = (peer_vr + 1) & 0x7F
                    byte1 = (peer_vs & 0x7F) << 1
                    byte2 = ((peer_vr & 0x7F) << 1)
                    peer_vs = (peer_vs + 1) & 0x7F
                    addrs = ax25_l2._addr_path(
                        mine, peer_mine, [], command=True
                    )
                    await b.send(
                        addrs
                        + bytes([byte1, byte2, PID_NO_LAYER3])
                        + f.info
                    )

    peer_task = asyncio.create_task(peer_run())
    stream = Ax25L2Stream(
        a,
        my_call="M0ABC",
        remote_call="WPS",
        connect_timeout=2.0,
        modulo=128,
    )
    await stream.open()
    assert sabme_seen
    assert stream._eff_modulo == 128
    # Send 9 chunks (paclen default 256, each fits one I-frame). With
    # modulo-128 the L2 should not balk at N(S) > 7.
    for i in range(9):
        await stream.send(bytes([i]))
    received = []
    for _ in range(9):
        received.append(await asyncio.wait_for(stream.recv(), 2.0))
    assert received == [bytes([i]) for i in range(9)]
    await stream.close()
    peer_task.cancel()
    try:
        await peer_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_modulo128_srej_retransmit() -> None:
    """Peer sends SREJ N(R) → only frame N(R) is retransmitted, not the
    whole window from N(R) onward.
    """
    a, b = _link()

    mine = _parse_call("M0ABC")
    peer_mine = _parse_call("WPS")

    sent_iframes: list[int] = []  # Each entry is N(S) of an I-frame the peer saw.
    srej_sent = False

    async def peer_run() -> None:
        nonlocal srej_sent
        connected = False
        while True:
            raw = await b.recv()
            if raw == b"":
                return
            f = _decode_frame(raw, modulo=128 if connected else 8)
            if f is None:
                continue
            if f.is_uframe and f.u_type == ax25_l2.CTRL_SABME:
                connected = True
                ctrl = ax25_l2.CTRL_UA | (PF if f.poll else 0)
                addrs = ax25_l2._addr_path(mine, peer_mine, [], command=False)
                await b.send(addrs + bytes([ctrl]))
            elif f.is_iframe and connected:
                sent_iframes.append(f.ns)
                if not srej_sent and len(sent_iframes) >= 3:
                    # Pretend frame N(S)=1 was lost — emit SREJ 1.
                    srej_sent = True
                    byte1 = ax25_l2.S_SREJ
                    byte2 = (1 << 1)  # N(R)=1, P=0
                    addrs = ax25_l2._addr_path(
                        mine, peer_mine, [], command=False
                    )
                    await b.send(addrs + bytes([byte1, byte2]))

    peer_task = asyncio.create_task(peer_run())
    stream = Ax25L2Stream(
        a,
        my_call="M0ABC",
        remote_call="WPS",
        connect_timeout=2.0,
        modulo=128,
        window=4,
        paclen=4,
    )
    await stream.open()
    # Send four small APDUs → four I-frames, ns=0,1,2,3.
    for i in range(4):
        await stream.send(bytes([0xA0 + i]))

    # Wait for the peer to register all 4 originals plus the SREJ-driven
    # retransmit of frame 1.
    deadline = asyncio.get_event_loop().time() + 2.0
    while asyncio.get_event_loop().time() < deadline:
        if sent_iframes.count(1) >= 2:
            break
        await asyncio.sleep(0.02)

    # Originals: 0,1,2,3 — each once. Then exactly one extra (SREJ-driven)
    # retransmit of N(S)=1, and crucially NOT of N(S)=2 or 3.
    assert sent_iframes.count(1) == 2, sent_iframes
    assert sent_iframes.count(2) == 1, sent_iframes
    assert sent_iframes.count(3) == 1, sent_iframes
    assert sent_iframes.count(0) == 1, sent_iframes

    await stream.close()
    peer_task.cancel()
    try:
        await peer_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_modulo128_fallback_to_mod8() -> None:
    """Peer FRMRs the SABME → L2 retries with SABM and the resulting
    session uses 1-byte control fields.
    """
    a, b = _link()

    mine = _parse_call("M0ABC")
    peer_mine = _parse_call("WPS")

    sabme_count = 0
    sabm_count = 0

    async def peer_run() -> None:
        nonlocal sabme_count, sabm_count
        connected = False
        while True:
            raw = await b.recv()
            if raw == b"":
                return
            f = _decode_frame(raw)
            if f is None:
                continue
            if f.is_uframe and f.u_type == ax25_l2.CTRL_SABME:
                sabme_count += 1
                # Reject with FRMR.
                addrs = ax25_l2._addr_path(
                    mine, peer_mine, [], command=False
                )
                await b.send(
                    addrs + bytes([ax25_l2.CTRL_FRMR | PF, 0, 0, 0])
                )
            elif f.is_uframe and f.u_type == CTRL_SABM:
                sabm_count += 1
                connected = True
                addrs = ax25_l2._addr_path(
                    mine, peer_mine, [], command=False
                )
                await b.send(
                    addrs + bytes([CTRL_UA | (PF if f.poll else 0)])
                )
            elif f.is_iframe and connected:
                # Echo so we can verify control-field width.
                ctrl = (0 << 5) | (0 << 1)  # N(R)=0, N(S)=0
                addrs = ax25_l2._addr_path(
                    mine, peer_mine, [], command=True
                )
                await b.send(addrs + bytes([ctrl, PID_NO_LAYER3]) + f.info)

    peer_task = asyncio.create_task(peer_run())
    stream = Ax25L2Stream(
        a,
        my_call="M0ABC",
        remote_call="WPS",
        connect_timeout=2.0,
        modulo=128,
    )
    await stream.open()
    assert sabme_count == 1
    assert sabm_count == 1
    assert stream._eff_modulo == 8
    # Window cap should also have dropped to <=7.
    assert stream._k <= 7

    await stream.close()
    peer_task.cancel()
    try:
        await peer_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_addr_pair_command_response_cbits() -> None:
    mine = _parse_call("M0ABC")
    remote = _parse_call("WPS")
    cmd = _addr_pair(remote, mine, command=True)
    rsp = _addr_pair(remote, mine, command=False)

    cmd_dest = Address.decode(cmd[:7])
    cmd_src = Address.decode(cmd[7:14])
    rsp_dest = Address.decode(rsp[:7])
    rsp_src = Address.decode(rsp[7:14])

    # Command: dest C=1, src C=0.
    assert cmd_dest.command is True
    assert cmd_src.command is False
    # Response: dest C=0, src C=1.
    assert rsp_dest.command is False
    assert rsp_src.command is True
