"""Hop-script runner tests.

The runner is fed by an in-memory ``AsyncByteStream`` whose ``recv`` blocks
on a queue. Each test scripts the inbound text and asserts the outbound
hop commands appear with the expected ``\\r`` suffix and that error /
timeout / EOF paths surface as ``HopScriptError``.
"""

from __future__ import annotations

import asyncio

import pytest

from whatspyc.transport.base import AsyncByteStream
from whatspyc.wps.hop_script import (
    DEFAULT_ERROR_TERMS,
    HopScriptError,
    HopStep,
    run_connect_script,
)


class _FakeStream(AsyncByteStream):
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self._inbox: asyncio.Queue[bytes] = asyncio.Queue()

    async def open(self) -> None: ...
    async def close(self) -> None: ...

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def recv(self) -> bytes:
        return await self._inbox.get()

    def push(self, data: bytes) -> None:
        self._inbox.put_nowait(data)

    def push_eof(self) -> None:
        self._inbox.put_nowait(b"")


@pytest.mark.asyncio
async def test_two_hop_happy_path() -> None:
    s = _FakeStream()
    s.push(b"\r*** Connected to MB7NPW\r")
    s.push(b"\r*** Connected to WPS\r")
    await run_connect_script(
        s,
        [
            HopStep(cmd="C MB7NPW", val="Connected to MB7NPW"),
            HopStep(cmd="C WPS", val="Connected to WPS"),
        ],
    )
    assert s.sent == [b"C MB7NPW\r", b"C WPS\r"]


@pytest.mark.asyncio
async def test_empty_script_is_noop() -> None:
    s = _FakeStream()
    await run_connect_script(s, [])
    assert s.sent == []


@pytest.mark.asyncio
async def test_wait_only_step_consumes_pushed_banner_without_sending() -> None:
    """A step with empty cmd is wait-only: it sends nothing and just waits
    for `val` to appear in the inbound buffer. Used to consume a server-
    pushed banner (canonically BPQ's `Connected to RHP Server` greeting)
    so it doesn't false-match a subsequent hop's `val`."""
    s = _FakeStream()
    s.push(b"Connected to RHP Server\r")
    s.push(b"BASTOK:GB7BSK} Connected to GB7WIN\r")
    events: list[str] = []
    await run_connect_script(
        s,
        [
            HopStep(cmd="", val="Connected to RHP Server"),
            HopStep(cmd="NC 1 !GB7WIN", val="Connected to GB7WIN"),
        ],
        on_progress=events.append,
    )
    # Only the second hop's command went out — the wait-only step sent
    # nothing, so the second hop's "Connected" val didn't false-match
    # the banner.
    assert s.sent == [b"NC 1 !GB7WIN\r"]
    # The wait-only step is plumbing — neither its (absent) cmd nor the
    # banner it consumes should appear in the progress stream, and it
    # doesn't count toward the user-visible hop total. The user wrote
    # exactly one hop, so the surfaced step is "[hop 1/1]".
    assert not any("hop 1/2" in e or "hop 2/2" in e for e in events)
    assert "[hop 1/1] > NC 1 !GB7WIN" in events
    assert any("[hop 1/1] < " in e and "Connected to GB7WIN" in e for e in events)


@pytest.mark.asyncio
async def test_aborts_on_error_token() -> None:
    s = _FakeStream()
    s.push(b"*** Failure with K4XYZ\r")
    with pytest.raises(HopScriptError, match="error token"):
        await run_connect_script(
            s, [HopStep(cmd="C K4XYZ", val="Connected")], default_timeout=2.0
        )


@pytest.mark.asyncio
async def test_val_match_beats_error_prefix_for_star_star_star() -> None:
    """``*** Connected`` contains ``*** `` but should win as a val match
    before the error scan flags it."""
    s = _FakeStream()
    s.push(b"*** Connected to WPS\r")
    await run_connect_script(
        s, [HopStep(cmd="C WPS", val="Connected to WPS")], default_timeout=2.0
    )
    assert s.sent == [b"C WPS\r"]


@pytest.mark.asyncio
async def test_per_step_timeout() -> None:
    s = _FakeStream()
    # Push nothing — runner should give up on its own.
    with pytest.raises(HopScriptError, match="timed out"):
        await run_connect_script(
            s, [HopStep(cmd="C WPS", val="Connected", timeout=0.1)]
        )


@pytest.mark.asyncio
async def test_eof_during_wait_raises() -> None:
    s = _FakeStream()
    s.push(b"some\r\nnoise without the magic word\r")
    s.push_eof()
    with pytest.raises(HopScriptError, match="stream closed"):
        await run_connect_script(
            s, [HopStep(cmd="C WPS", val="Connected")], default_timeout=2.0
        )


@pytest.mark.asyncio
async def test_buffer_carries_over_between_steps() -> None:
    """If hop N's recv pulls in bytes belonging to hop N+1's prompt, those
    bytes must remain visible to hop N+1's matcher — otherwise we'd
    deadlock waiting for text already on the wire."""
    s = _FakeStream()
    # Single big chunk containing both prompts.
    s.push(b"*** Connected to MB7NPW\r*** Connected to WPS\r")
    await run_connect_script(
        s,
        [
            HopStep(cmd="C MB7NPW", val="Connected to MB7NPW"),
            HopStep(cmd="C WPS", val="Connected to WPS"),
        ],
        default_timeout=2.0,
    )
    assert s.sent == [b"C MB7NPW\r", b"C WPS\r"]


@pytest.mark.asyncio
async def test_high_bit_bytes_dont_break_matching() -> None:
    """latin-1 decode keeps every input byte addressable, so high-bit
    framing bytes mixed in with prompt text don't blow up the matcher."""
    s = _FakeStream()
    s.push(b"\xc0noise\xc0 *** Connected to WPS\r")
    await run_connect_script(
        s, [HopStep(cmd="C WPS", val="Connected to WPS")], default_timeout=2.0
    )


def test_default_error_terms_include_web_client_set() -> None:
    """Sanity-check the abort vocabulary against the web-client list."""
    assert "Failure" in DEFAULT_ERROR_TERMS
    assert "Busy" in DEFAULT_ERROR_TERMS
    assert "*** " in DEFAULT_ERROR_TERMS
    assert "Network Error" in DEFAULT_ERROR_TERMS


@pytest.mark.asyncio
async def test_progress_callback_streams_lines_in_order() -> None:
    """The CLI wires this so the user sees the hop chain play out — verify
    each sent cmd and every complete inbound line surface in the right
    order with the right prefix and direction."""
    s = _FakeStream()
    s.push(b"\r\nWelcome to NODE7 (XRouter v3)\r\nNODE7:M0ABC} ")
    s.push(b"*** Connected to MB7NPW\r")
    s.push(b"MB7NPW:M0ABC} ")
    s.push(b"*** Connected to WPS\r")
    events: list[str] = []
    await run_connect_script(
        s,
        [
            HopStep(cmd="C MB7NPW", val="Connected to MB7NPW"),
            HopStep(cmd="C WPS", val="Connected to WPS"),
        ],
        default_timeout=2.0,
        on_progress=events.append,
    )
    assert events[0] == "[hop 1/2] > C MB7NPW"
    assert "[hop 2/2] > C WPS" in events
    # No "= matched" lines — they were noise.
    assert not any(" = matched " in e for e in events)
    # Inbound text should be visible — at minimum the welcome banner and
    # both ``*** Connected …`` lines.
    received = [e for e in events if " < " in e]
    assert any("Welcome to NODE7" in e for e in received)
    assert any("*** Connected to MB7NPW" in e for e in received)
    assert any("*** Connected to WPS" in e for e in received)


@pytest.mark.asyncio
async def test_progress_does_not_emit_post_match_tail_as_next_hop_line() -> None:
    """When ``val`` matches in the middle of a line, the bytes between the
    match and the line break must not survive into the next hop's buffer
    and get re-emitted as a stray "complete line" with the wrong hop
    prefix. Regression: a 2-hop session against a real BPQ node showed

        [hop 1/2] > NC 1 !GB7WIN
        [hop 1/2] < BASTOK:GB7BSK} Connected to GB7WIN
        [hop 2/2] > C 2 !GB7BDH
        [hop 2/2] < to GB7WIN     ← spurious

    because the runner rebased the buffer to ``match_end`` (mid-line)
    instead of past the line break."""
    s = _FakeStream()
    s.push(b"BASTOK:GB7BSK} Connected to GB7WIN\r")
    s.push(b"BASTOK:GB7BSK} Connected to GB7BDH\r")
    events: list[str] = []
    await run_connect_script(
        s,
        [
            HopStep(cmd="NC 1 !GB7WIN", val="Connected"),
            HopStep(cmd="C 2 !GB7BDH", val="Connected"),
        ],
        default_timeout=2.0,
        on_progress=events.append,
    )
    inbound = [e for e in events if " < " in e]
    assert "[hop 2/2] < to GB7WIN" not in inbound
    assert not any(e.startswith("[hop 2/2] < ") and "to GB7WIN" in e
                   and "GB7BDH" not in e for e in inbound)


@pytest.mark.asyncio
async def test_progress_does_not_repeat_lines_across_steps() -> None:
    """When the previous step's recv pulled bytes belonging to the next
    step's prompt, those lines must show up exactly once — not again on
    the next step's pre-emit pass."""
    s = _FakeStream()
    s.push(b"*** Connected to MB7NPW\r*** Connected to WPS\r")
    events: list[str] = []
    await run_connect_script(
        s,
        [
            HopStep(cmd="C MB7NPW", val="Connected to MB7NPW"),
            HopStep(cmd="C WPS", val="Connected to WPS"),
        ],
        default_timeout=2.0,
        on_progress=events.append,
    )
    inbound = [e for e in events if " < " in e]
    assert sum(1 for e in inbound if "Connected to MB7NPW" in e) == 1
    assert sum(1 for e in inbound if "Connected to WPS" in e) == 1
