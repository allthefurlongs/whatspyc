"""AX.25 v2.2 connected-mode Layer-2 state machine.

Carries a reliable byte stream over a KISS link to a WPS host node:

* **Connection setup**: SABM (modulo-8) / SABME (modulo-128) / UA / DISC
  / DM / FRMR. SABME automatically falls back to SABM on FRMR or DM,
  surfacing a log warning, so peers that don't speak v2.2 extended still
  connect at modulo-8.
* **Sequencing**: I-frame N(S)/N(R), modulo 8 or 128 selectable per
  session. RR / RNR / REJ at any modulo, plus SREJ in modulo-128 for
  selective retransmit.
* **Optional segmentation** (PID 0x08, AX.25 §4.3.3.2) when the user
  passes ``segmentation=True``. Off by default — the WPS codec already
  reframes correctly on ``\\r\\n``.
* **Optional KISS ACKMODE** (G8BPQ command 0x0C) when ``ackmode=True``,
  so T1 starts from the moment a frame is genuinely on-air.
* **Optional digipeaters**: a fixed digi list rides on every I/S/U frame
  of the connected session.
* **Timers**: T1 (ack timeout), T3 (link idle), N2 (retry counter); a
  configurable window size ``k`` (max 7 mod-8, max 127 mod-128) and
  per-frame ``paclen``.

The state machine wraps a *lower* ``AsyncByteStream`` whose ``send`` /
``recv`` carry one raw AX.25 frame each — i.e. ``KissSerialUI`` /
``KissTcpUI``. The L2 stream itself satisfies the same ``AsyncByteStream``
contract, exposing the I-frame information field as ordered bytes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

from whatspyc.transport.ax25_frame import PID_NO_LAYER3, Address
from whatspyc.transport.ax25_segment import PID_SEGMENT, Reassembler, segment
from whatspyc.transport.base import AsyncByteStream

logger = logging.getLogger(__name__)

# U-frames are always 1-byte control regardless of modulo. PF for U/S/I
# in modulo-8 is bit 4 (0x10); in modulo-128 the I/S frame PF lives in
# bit 0 of the 2nd control byte (mask 0x100 over the 16-bit control int).
PF = 0x10  # modulo-8 PF (and U-frame PF at any modulo)
PF128 = 0x100  # modulo-128 PF on the 2-byte I/S control

# U-frames (M-bits encode type; the PF bit is masked out for matching)
CTRL_SABM = 0x2F
CTRL_SABME = 0x6F  # extended-mode connect (modulo-128, AX.25 v2.2 §4.2.4)
CTRL_DISC = 0x43
CTRL_DM = 0x0F
CTRL_UA = 0x63
CTRL_FRMR = 0x87
CTRL_UI = 0x03

# S-frame SS sub-codes (low nibble; the rest is N(R)<<5 | PF for mod-8,
# or a separate byte 2 for mod-128).
S_RR = 0x01
S_RNR = 0x05
S_REJ = 0x09
S_SREJ = 0x0D  # selective reject — extended mode only (§6.7.4)

DEFAULT_T1 = 10.0
DEFAULT_T3 = 300.0
DEFAULT_N2 = 10
DEFAULT_K = 4
DEFAULT_PACLEN = 256


class State(Enum):
    DISCONNECTED = "DISCONNECTED"
    AWAITING_CONNECTION = "AWAITING_CONNECTION"
    CONNECTED = "CONNECTED"
    AWAITING_RELEASE = "AWAITING_RELEASE"


def _parse_call(spec: str) -> Address:
    s = spec.upper().strip()
    ssid = 0
    if "-" in s:
        s, ssid_s = s.split("-", 1)
        ssid = int(ssid_s)
    return Address(callsign=s, ssid=ssid)


def _addr_path(
    remote: Address,
    mine: Address,
    digis: list[Address] | None = None,
    *,
    command: bool,
) -> bytes:
    """Build the address field for a frame: dest, src, then each digi.

    AX.25 v2.2 §3.12 / §6.3.1: the address field carries up to 8 digi
    addresses between source and destination. The last subfield in the
    chain (the final digi if any, otherwise the source) has its extension
    bit (``last=True``) set.

    Command vs. response is encoded in the C-bits of dest+src: command
    frames carry dest C=1, src C=0; responses reversed. Bit 7 of the SSID
    byte is the C-bit on dest/src and the H-bit ("has been repeated") on
    digis. Outbound digi subfields always clear H so the next hop knows
    it still needs to forward.
    """
    digis = list(digis or [])
    last_is_src = not digis
    out = bytearray()
    out += Address(
        remote.callsign,
        remote.ssid,
        last=False,
        command=command,
    ).encode()
    out += Address(
        mine.callsign,
        mine.ssid,
        last=last_is_src,
        command=not command,
    ).encode()
    for i, d in enumerate(digis):
        out += Address(
            d.callsign,
            d.ssid,
            last=(i == len(digis) - 1),
            command=False,
            has_been_repeated=False,
        ).encode()
    return bytes(out)


# Back-compat alias for callers that don't deal with digipeaters (most
# tests, and the loopback fixture). Equivalent to ``_addr_path(..., [])``.
def _addr_pair(remote: Address, mine: Address, *, command: bool) -> bytes:
    return _addr_path(remote, mine, [], command=command)


@dataclass
class _Parsed:
    """One parsed AX.25 frame.

    ``control`` is an 8-bit value for U-frames (always) and modulo-8 I/S
    frames; it's a 16-bit little-endian value for modulo-128 I/S frames
    (low byte = first wire byte). ``modulo`` records which width applies
    so the ns/nr/poll properties decode correctly.
    """

    dest: Address
    src: Address
    control: int
    pid: int | None
    info: bytes
    modulo: int = 8

    @property
    def is_iframe(self) -> bool:
        return (self.control & 0x01) == 0

    @property
    def is_sframe(self) -> bool:
        return (self.control & 0x03) == 0x01

    @property
    def is_uframe(self) -> bool:
        return (self.control & 0x03) == 0x03

    @property
    def ns(self) -> int:
        if self.modulo == 128:
            return (self.control >> 1) & 0x7F
        return (self.control >> 1) & 0x07

    @property
    def nr(self) -> int:
        if self.modulo == 128:
            return (self.control >> 9) & 0x7F  # bits 9-15 of the 16-bit ctrl
        return (self.control >> 5) & 0x07

    @property
    def poll(self) -> bool:
        if self.is_uframe:
            return bool(self.control & PF)
        if self.modulo == 128:
            return bool(self.control & PF128)
        return bool(self.control & PF)

    @property
    def s_type(self) -> int:
        # Low nibble of byte 1 — same layout for mod-8 and mod-128.
        return self.control & 0x0F

    @property
    def u_type(self) -> int:
        return self.control & ~PF

    @property
    def is_command(self) -> bool:
        # v2.2 cmd/resp encoding lives in the C-bits of dest+src.
        return self.dest.command and not self.src.command


def _decode_frame(raw: bytes, *, modulo: int = 8) -> _Parsed | None:
    """Parse one raw AX.25 frame.

    ``modulo`` selects the I/S-frame control-field width: 1 byte for
    modulo-8, 2 bytes for modulo-128. U-frames are always 1-byte control
    regardless. Returns ``None`` on malformed input.
    """
    try:
        i = 0
        addrs: list[Address] = []
        while i + 7 <= len(raw):
            chunk = raw[i : i + 7]
            i += 7
            a = Address.decode(chunk)
            addrs.append(a)
            if a.last:
                break
        if len(addrs) < 2 or i >= len(raw):
            return None
        dest, src = addrs[0], addrs[1]
        if i >= len(raw):
            return None
        first_ctrl = raw[i]
        i += 1
        # U-frame: always 1-byte control. I/S: 1 byte mod-8, 2 bytes mod-128.
        is_uframe = (first_ctrl & 0x03) == 0x03
        if is_uframe or modulo == 8:
            control = first_ctrl
        else:
            if i >= len(raw):
                return None
            control = first_ctrl | (raw[i] << 8)
            i += 1
        pid: int | None = None
        info = b""
        if (first_ctrl & 0x01) == 0:  # I-frame
            if i >= len(raw):
                return None
            pid = raw[i]
            i += 1
            info = raw[i:]
        elif is_uframe and (first_ctrl & ~PF) == CTRL_FRMR:
            info = raw[i:]
        return _Parsed(
            dest=dest,
            src=src,
            control=control,
            pid=pid,
            info=info,
            modulo=modulo if not is_uframe else 8,
        )
    except Exception:
        return None


class Ax25L2Stream(AsyncByteStream):
    """Connected-mode AX.25 byte stream over a KISS lower.

    AX.25 reaches WPS via a host node (MB7NPW-9 / WPS application etc.),
    which TCP-forwards into the WPS daemon and pre-sends the originating
    callsign. Hence ``injects_callsign = True``.
    """

    @property
    def injects_callsign(self) -> bool:
        return True

    def __init__(
        self,
        lower: AsyncByteStream,
        *,
        my_call: str,
        remote_call: str,
        t1: float = DEFAULT_T1,
        t3: float = DEFAULT_T3,
        n2: int = DEFAULT_N2,
        window: int = DEFAULT_K,
        paclen: int = DEFAULT_PACLEN,
        connect_timeout: float = 30.0,
        digipeaters: list[str] | None = None,
        ackmode: bool = False,
        modulo: int = 8,
        segmentation: bool = False,
    ) -> None:
        if modulo not in (8, 128):
            raise ValueError(f"modulo must be 8 or 128 (got {modulo!r})")
        self._lower = lower
        self._mine = _parse_call(my_call)
        self._remote = _parse_call(remote_call)
        self._digis = [_parse_call(d) for d in (digipeaters or [])]
        self._ackmode = ackmode
        self._modulo = modulo
        # Effective modulo after handshake — drops back to 8 if the peer
        # rejects SABME with FRMR/DM and we fall back to SABM.
        self._eff_modulo = modulo
        self._segmentation = segmentation
        self._t1_dur = t1
        self._t3_dur = t3
        self._n2 = n2
        # Modulo-128 allows up to 127 in flight; modulo-8 caps at 7.
        max_window = 127 if modulo == 128 else 7
        self._k = max(1, min(window, max_window))
        self._paclen = max(1, paclen)
        self._connect_timeout = connect_timeout

        self._state = State.DISCONNECTED
        self._vs = 0
        self._vr = 0
        self._va = 0
        # _inflight values are (pid, info) so retransmits preserve the
        # original PID byte. _send_buf is the same shape for the queue
        # of pending I-frames.
        self._inflight: dict[int, tuple[int, bytes]] = {}
        self._send_buf: list[tuple[int, bytes]] = []
        self._reassembler = Reassembler()
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()

        self._reject_outstanding = False
        self._peer_busy = False
        self._self_busy = False
        self._poll_pending = False
        self._retry = 0

        self._t1_handle: asyncio.TimerHandle | None = None
        self._t3_handle: asyncio.TimerHandle | None = None
        self._reader_task: asyncio.Task | None = None
        self._writer_task: asyncio.Task | None = None
        # Outbox items are (frame, on_air_callback). The callback (if any)
        # runs under self._lock once the frame is confirmed on-air —
        # immediately on send for non-ackmode lowers, or after the KISS
        # ACKMODE reply for ackmode lowers.
        self._outq: asyncio.Queue = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._fully_disconnected = asyncio.Event()
        self._connect_error: Exception | None = None

    # ------------------------------------------------------------------
    # AsyncByteStream contract
    # ------------------------------------------------------------------

    async def open(self) -> None:
        await self._lower.open()
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._writer_task = asyncio.create_task(self._writer_loop())
        async with self._lock:
            self._reset_seq()
            self._eff_modulo = self._modulo
            self._state = State.AWAITING_CONNECTION
            # SABME for modulo-128 (extended), SABM otherwise. The peer
            # may FRMR/DM the SABME — see _handle_u for the fallback path
            # that retries with SABM and drops to modulo-8.
            ctrl = CTRL_SABME if self._modulo == 128 else CTRL_SABM
            self._send_uframe(ctrl, command=True, poll=True)
            self._start_t1()
        try:
            await asyncio.wait_for(self._connected.wait(), self._connect_timeout)
        except asyncio.TimeoutError:
            await self._tear_down(
                ConnectionError(
                    f"AX.25 SABM to {self._remote.callsign}-{self._remote.ssid} timed out"
                )
            )
            raise
        if self._connect_error is not None:
            raise self._connect_error

    async def send(self, data: bytes) -> None:
        if not data:
            return
        async with self._lock:
            if self._state != State.CONNECTED:
                raise ConnectionError(
                    f"AX.25 link not connected (state={self._state.name})"
                )
            if self._segmentation and len(data) > self._paclen:
                # Wrap the APDU in PID-0x08 segments. Each segment's info
                # field already contains the segment header; the L2 just
                # transmits them in order with PID == 0x08.
                for info in segment(
                    data, paclen=self._paclen, original_pid=PID_NO_LAYER3
                ):
                    self._send_buf.append((PID_SEGMENT, info))
            else:
                for chunk in self._chunked(data):
                    self._send_buf.append((PID_NO_LAYER3, chunk))
            self._pump_outbox()

    async def recv(self) -> bytes:
        item = await self._inbox.get()
        return b"" if item is None else item

    async def close(self) -> None:
        async with self._lock:
            if self._state == State.CONNECTED:
                self._send_uframe(CTRL_DISC, command=True, poll=True)
                self._state = State.AWAITING_RELEASE
                self._retry = 0
                self._start_t1()
        try:
            await asyncio.wait_for(self._fully_disconnected.wait(), 5.0)
        except asyncio.TimeoutError:
            pass
        await self._shutdown_tasks()
        await self._lower.close()

    # ------------------------------------------------------------------
    # Reader / writer
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        try:
            while True:
                try:
                    raw = await self._lower.recv()
                except (ConnectionResetError, ConnectionAbortedError, OSError):
                    raw = b""
                if not raw:
                    await self._lower_closed()
                    return
                frame = _decode_frame(raw, modulo=self._eff_modulo)
                if frame is None:
                    continue
                # Drop frames not addressed to us. Digipeaters in the
                # path don't change this gate: the destination on the
                # wire is still us — each digi just forwards the frame
                # and flips its own H-bit. We don't inspect H on inbound;
                # any path is accepted as long as dest matches.
                if (
                    frame.dest.callsign.upper() != self._mine.callsign.upper()
                    or frame.dest.ssid != self._mine.ssid
                ):
                    continue
                async with self._lock:
                    self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover — surface unexpected
            logger.exception("AX.25 reader loop crashed")
            await self._lower_closed()

    async def _writer_loop(self) -> None:
        try:
            while True:
                item = await self._outq.get()
                if item is None:
                    return
                frame, on_air = item
                try:
                    ack_id = await self._lower.send(frame)
                except Exception as exc:
                    logger.warning("AX.25 lower send failed: %s", exc)
                    await self._lower_closed()
                    return
                if self._ackmode and ack_id is not None:
                    try:
                        await asyncio.wait_for(
                            self._lower.ack_for(ack_id), self._t1_dur
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "KISS ACKMODE: TNC didn't ACK frame within %ss "
                            "— treating as T1 expiry",
                            self._t1_dur,
                        )
                        # Force a T1 retransmit cycle. The current frame is
                        # presumed lost or stuck in the TNC.
                        asyncio.create_task(self._t1_handler())
                        continue
                    except Exception as exc:
                        logger.warning("KISS ACKMODE wait failed: %s", exc)
                        await self._lower_closed()
                        return
                if on_air is not None:
                    async with self._lock:
                        try:
                            on_air()
                        except Exception:
                            logger.exception("on-air callback failed")
        except asyncio.CancelledError:
            raise

    async def _lower_closed(self) -> None:
        async with self._lock:
            if self._state != State.DISCONNECTED:
                self._state = State.DISCONNECTED
                if not self._connected.is_set():
                    self._connect_error = ConnectionError("lower transport closed")
                    self._connected.set()
            self._stop_t1()
            self._stop_t3()
            self._fully_disconnected.set()
        await self._inbox.put(None)

    # ------------------------------------------------------------------
    # Frame dispatch (already inside self._lock)
    # ------------------------------------------------------------------

    def _dispatch(self, f: _Parsed) -> None:
        if f.is_uframe:
            self._handle_u(f)
        elif f.is_sframe:
            self._handle_s(f)
        elif f.is_iframe:
            self._handle_i(f)

    def _handle_u(self, f: _Parsed) -> None:
        u = f.u_type
        if u == CTRL_SABM or u == CTRL_SABME:
            # peer initiated (re)connect — accept and pin the modulo to
            # whichever variant they chose.
            self._reset_seq()
            self._eff_modulo = 128 if u == CTRL_SABME else 8
            self._send_uframe(CTRL_UA, command=False, poll=f.poll)
            if self._state != State.CONNECTED:
                self._state = State.CONNECTED
                self._stop_t1()
                self._restart_t3()
                self._connected.set()
        elif u == CTRL_UA:
            if self._state == State.AWAITING_CONNECTION:
                self._reset_seq()
                self._state = State.CONNECTED
                self._stop_t1()
                self._restart_t3()
                self._connected.set()
            elif self._state == State.AWAITING_RELEASE:
                self._stop_t1()
                self._state = State.DISCONNECTED
                self._fully_disconnected.set()
                self._inbox.put_nowait(None)
        elif u == CTRL_DISC:
            self._send_uframe(CTRL_UA, command=False, poll=f.poll)
            self._state = State.DISCONNECTED
            self._stop_t1()
            self._stop_t3()
            self._fully_disconnected.set()
            self._inbox.put_nowait(None)
        elif u == CTRL_DM:
            self._stop_t1()
            self._stop_t3()
            if self._state == State.AWAITING_CONNECTION:
                if self._try_sabme_fallback("DM"):
                    return
                self._connect_error = ConnectionRefusedError(
                    f"peer DM in response to SABM ({self._remote.callsign})"
                )
                self._state = State.DISCONNECTED
                self._connected.set()
                self._inbox.put_nowait(None)
            elif self._state in (State.CONNECTED, State.AWAITING_RELEASE):
                self._state = State.DISCONNECTED
                self._fully_disconnected.set()
                self._inbox.put_nowait(None)
        elif u == CTRL_FRMR:
            if self._state == State.AWAITING_CONNECTION and self._try_sabme_fallback("FRMR"):
                return
            logger.warning("AX.25 FRMR received from peer: info=%r", f.info)
            self._state = State.DISCONNECTED
            self._stop_t1()
            self._stop_t3()
            self._fully_disconnected.set()
            self._inbox.put_nowait(None)

    def _try_sabme_fallback(self, reason: str) -> bool:
        """Drop from SABME (modulo-128) to SABM (modulo-8) on peer reject.

        Some peers FRMR or DM a SABME because they don't speak v2.2
        extended. Retry once with SABM at modulo-8 before declaring the
        connection refused. Returns ``True`` if a retry was issued.
        """
        if self._eff_modulo != 128:
            return False
        logger.warning(
            "AX.25 peer rejected SABME with %s; falling back to SABM "
            "(modulo-8)",
            reason,
        )
        self._eff_modulo = 8
        # Cap the window for modulo-8.
        if self._k > 7:
            self._k = 7
        self._reset_seq()
        self._stop_t1()
        self._send_uframe(CTRL_SABM, command=True, poll=True)
        self._start_t1()
        return True

    def _handle_s(self, f: _Parsed) -> None:
        if self._state != State.CONNECTED:
            return
        s = f.s_type
        if s == S_RR:
            self._peer_busy = False
            self._process_nr(f.nr)
        elif s == S_RNR:
            self._peer_busy = True
            self._process_nr(f.nr)
        elif s == S_REJ:
            self._peer_busy = False
            self._process_nr(f.nr)
            self._retransmit_from(f.nr)
        elif s == S_SREJ:
            # §6.7.4: SREJ N(R) acks frames < N(R) AND requests
            # retransmission of exactly frame N(R). One frame, not the
            # whole window, unlike REJ.
            self._peer_busy = False
            self._process_nr(f.nr)
            if f.nr in self._inflight:
                pid, info = self._inflight[f.nr]
                self._send_iframe(f.nr, self._vr, info, pid=pid)
        else:
            return
        if f.is_command and f.poll:
            self._send_sframe(
                S_RNR if self._self_busy else S_RR, command=False, poll=True
            )
        if (not f.is_command) and f.poll and self._poll_pending:
            self._poll_pending = False
            self._stop_t1()
            self._retry = 0
            # peer's N(R) just told us the truth; resend anything still unacked.
            seq = self._va
            while seq != self._vs:
                if seq in self._inflight:
                    pid, info = self._inflight[seq]
                    self._send_iframe(seq, self._vr, info, pid=pid)
                seq = (seq + 1) & self._seq_mask
            if self._inflight:
                self._start_t1()
        self._restart_t3()
        self._pump_outbox()

    def _handle_i(self, f: _Parsed) -> None:
        if self._state != State.CONNECTED:
            self._send_uframe(CTRL_DM, command=False, poll=f.poll)
            return
        # ack progress first
        self._process_nr(f.nr)
        if f.ns == self._vr:
            self._vr = (self._vr + 1) & self._seq_mask
            self._reject_outstanding = False
            if f.pid == PID_SEGMENT:
                # Layer-3 segment: feed into the reassembler. Only emit
                # to the inbox once the final segment arrives. Drop the
                # frame silently if reassembly fails — the L2's normal
                # retransmit logic will recover the lost segment.
                done = self._reassembler.feed(f.info)
                if done is not None:
                    _orig_pid, apdu = done
                    self._inbox.put_nowait(apdu)
            else:
                self._inbox.put_nowait(f.info)
            self._send_sframe(
                S_RNR if self._self_busy else S_RR, command=False, poll=f.poll
            )
        else:
            if not self._reject_outstanding and not self._self_busy:
                self._reject_outstanding = True
                # Prefer SREJ over REJ in extended mode: peer only needs
                # to retransmit the one missing frame instead of going
                # back-N.
                rej_type = S_SREJ if self._eff_modulo == 128 else S_REJ
                self._send_sframe(rej_type, command=False, poll=f.poll)
            elif f.poll:
                self._send_sframe(
                    S_RNR if self._self_busy else S_RR, command=False, poll=True
                )
        self._restart_t3()
        self._pump_outbox()

    # ------------------------------------------------------------------
    # Ack accounting
    # ------------------------------------------------------------------

    def _process_nr(self, nr: int) -> None:
        # All seqs in [V(A), nr) are now acknowledged.
        if not self._in_window(nr):
            # invalid N(R) — peer is confused. Just ignore for now.
            return
        while self._va != nr:
            self._inflight.pop(self._va, None)
            self._va = (self._va + 1) & self._seq_mask
        if self._inflight:
            self._restart_t1()
        else:
            self._stop_t1()
            self._retry = 0
            self._poll_pending = False

    def _in_window(self, nr: int) -> bool:
        # nr must lie in the closed-open range [V(A), V(S)] (mod 8 or 128).
        # i.e. distance from V(A) to nr must be <= distance from V(A) to V(S).
        span = (self._vs - self._va) & self._seq_mask
        cand = (nr - self._va) & self._seq_mask
        return cand <= span

    def _retransmit_from(self, nr: int) -> None:
        if not self._in_window(nr):
            return
        seq = nr
        while seq != self._vs:
            if seq in self._inflight:
                pid, info = self._inflight[seq]
                self._send_iframe(seq, self._vr, info, pid=pid)
            seq = (seq + 1) & 0x07

    # ------------------------------------------------------------------
    # Outbox pump
    # ------------------------------------------------------------------

    def _pump_outbox(self) -> None:
        if self._state != State.CONNECTED or self._peer_busy:
            return
        while self._send_buf and len(self._inflight) < self._k:
            pid, info = self._send_buf.pop(0)
            ns = self._vs
            self._inflight[ns] = (pid, info)
            self._vs = (self._vs + 1) & self._seq_mask
            self._send_iframe(ns, self._vr, info, pid=pid, defer_t1=self._ackmode)
            if not self._ackmode and self._t1_handle is None:
                self._start_t1()

    # ------------------------------------------------------------------
    # Frame builders / writers
    # ------------------------------------------------------------------

    def _enqueue(self, frame: bytes, on_air=None) -> None:
        self._outq.put_nowait((frame, on_air))

    @property
    def _seq_mask(self) -> int:
        return 0x7F if self._eff_modulo == 128 else 0x07

    def _ctrl_iframe_bytes(self, ns: int, nr: int, *, poll: bool) -> bytes:
        if self._eff_modulo == 128:
            byte1 = (ns & 0x7F) << 1  # bit 0 = 0 → I-frame
            byte2 = ((nr & 0x7F) << 1) | (1 if poll else 0)
            return bytes([byte1, byte2])
        ctrl = (nr << 5) | ((1 if poll else 0) << 4) | (ns << 1)
        return bytes([ctrl])

    def _ctrl_sframe_bytes(self, s_type: int, nr: int, *, poll: bool) -> bytes:
        if self._eff_modulo == 128:
            byte1 = s_type & 0x0F
            byte2 = ((nr & 0x7F) << 1) | (1 if poll else 0)
            return bytes([byte1, byte2])
        ctrl = (nr << 5) | ((1 if poll else 0) << 4) | s_type
        return bytes([ctrl])

    def _send_uframe(
        self, control: int, *, command: bool, poll: bool, info: bytes = b""
    ) -> None:
        # U-frames are always 1-byte control regardless of modulo.
        if poll:
            control |= PF
        frame = (
            _addr_path(self._remote, self._mine, self._digis, command=command)
            + bytes([control])
            + info
        )
        self._enqueue(frame)

    def _send_sframe(self, s_type: int, *, command: bool, poll: bool, nr: int | None = None) -> None:
        if nr is None:
            nr = self._vr
        frame = _addr_path(
            self._remote, self._mine, self._digis, command=command
        ) + self._ctrl_sframe_bytes(s_type, nr, poll=poll)
        self._enqueue(frame)

    def _send_iframe(
        self,
        ns: int,
        nr: int,
        info: bytes,
        *,
        pid: int = PID_NO_LAYER3,
        defer_t1: bool = False,
    ) -> None:
        frame = (
            _addr_path(self._remote, self._mine, self._digis, command=True)
            + self._ctrl_iframe_bytes(ns, nr, poll=False)
            + bytes([pid])
            + info
        )
        on_air = self._make_iframe_on_air_cb() if defer_t1 else None
        self._enqueue(frame, on_air)

    def _make_iframe_on_air_cb(self):
        """Build a callback that runs once the writer-loop confirms an
        I-frame went on-air via KISS ACKMODE. Starts T1 if not already
        running and the link still has unacked frames in flight.
        """

        def cb() -> None:
            if (
                self._state == State.CONNECTED
                and self._inflight
                and self._t1_handle is None
            ):
                self._start_t1()

        return cb

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _chunked(self, data: bytes) -> list[bytes]:
        return [data[i : i + self._paclen] for i in range(0, len(data), self._paclen)]

    def _reset_seq(self) -> None:
        self._vs = 0
        self._vr = 0
        self._va = 0
        self._inflight.clear()
        self._send_buf.clear()
        self._reject_outstanding = False
        self._peer_busy = False
        self._self_busy = False
        self._poll_pending = False
        self._retry = 0

    async def _tear_down(self, exc: Exception) -> None:
        async with self._lock:
            self._state = State.DISCONNECTED
            self._connect_error = exc
            self._connected.set()
            self._stop_t1()
            self._stop_t3()
            self._fully_disconnected.set()
        await self._shutdown_tasks()
        try:
            await self._lower.close()
        except Exception:
            pass

    async def _shutdown_tasks(self) -> None:
        if self._writer_task is not None:
            self._outq.put_nowait(None)
            try:
                await asyncio.wait_for(self._writer_task, 1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._writer_task.cancel()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Timers — TimerHandle callbacks must be sync; trampoline through tasks.
    # ------------------------------------------------------------------

    def _start_t1(self) -> None:
        self._stop_t1()
        loop = asyncio.get_event_loop()
        self._t1_handle = loop.call_later(self._t1_dur, self._on_t1)

    def _restart_t1(self) -> None:
        self._start_t1()

    def _stop_t1(self) -> None:
        if self._t1_handle is not None:
            self._t1_handle.cancel()
            self._t1_handle = None

    def _restart_t3(self) -> None:
        self._stop_t3()
        loop = asyncio.get_event_loop()
        self._t3_handle = loop.call_later(self._t3_dur, self._on_t3)

    def _stop_t3(self) -> None:
        if self._t3_handle is not None:
            self._t3_handle.cancel()
            self._t3_handle = None

    def _on_t1(self) -> None:
        self._t1_handle = None
        asyncio.create_task(self._t1_handler())

    def _on_t3(self) -> None:
        self._t3_handle = None
        asyncio.create_task(self._t3_handler())

    async def _t1_handler(self) -> None:
        async with self._lock:
            self._retry += 1
            if self._retry > self._n2:
                self._state = State.DISCONNECTED
                err = ConnectionError(
                    f"AX.25 retry limit (N2={self._n2}) exceeded"
                )
                if not self._connected.is_set():
                    self._connect_error = err
                    self._connected.set()
                self._fully_disconnected.set()
                self._stop_t1()
                self._stop_t3()
                self._inbox.put_nowait(None)
                return
            if self._state == State.AWAITING_CONNECTION:
                self._send_uframe(CTRL_SABM, command=True, poll=True)
                self._start_t1()
            elif self._state == State.AWAITING_RELEASE:
                self._send_uframe(CTRL_DISC, command=True, poll=True)
                self._start_t1()
            elif self._state == State.CONNECTED:
                # Solicit peer's N(R) before retransmitting.
                self._poll_pending = True
                self._send_sframe(
                    S_RNR if self._self_busy else S_RR, command=True, poll=True
                )
                self._start_t1()

    async def _t3_handler(self) -> None:
        async with self._lock:
            if self._state != State.CONNECTED:
                return
            self._poll_pending = True
            self._send_sframe(
                S_RNR if self._self_busy else S_RR, command=True, poll=True
            )
            self._retry = 0
            self._start_t1()
