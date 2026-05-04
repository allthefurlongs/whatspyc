"""Pre-handshake hop-script runner.

Most packet routes to WPS need a chain of node-level commands first, e.g.

    C MB7NPW   →   *** Connected to MB7NPW
    C WPS      →   *** Connected to WPS

This module drives that text-mode dialogue against an open
``AsyncByteStream`` *before* ``WpsClient`` sends its callsign-line +
type-`c` handshake. Behaviour mirrors the reference web client
(``reference/web-client/index.js`` line 773): each step sends ``cmd + b"\\r"``
and advances when ``val`` appears as a (case-sensitive) substring of the
accumulated inbound text. Known error tokens (``Failure`` / ``Busy`` /
``*** `` / ``Network Error``) are matched case-insensitively *after* the
``val`` check so ``*** Connected`` wins over ``*** ``.

Distinct from ``wps/connect_seq.py``, which orchestrates the *server-side*
type-`c` follow-up sequence after the WPS handshake completes.

Progress visibility: callers can pass ``on_progress=callback`` to
``run_connect_script`` to receive one notification per outbound command
and one per complete inbound text line. The CLI uses this to render the
hop chain to the terminal as it plays out, so the user can see what the
node is saying back.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from whatspyc.transport.base import AsyncByteStream

DEFAULT_ERROR_TERMS: tuple[str, ...] = ("Failure", "Busy", "*** ", "Network Error")
# No default timeout — packet-radio node prompts can take a long time over
# slow paths, and the web client doesn't bound them either. Per-step
# timeouts can still be set on individual ``HopStep``s when a test or
# config genuinely wants one.
DEFAULT_TIMEOUT: float | None = None

# Progress callback receives one complete event line at a time:
#   "[hop 1/2] > C MB7NPW"           — outbound command
#   "[hop 1/2] < Welcome to NODE7"   — inbound text (one line)
# Wait-only steps (cmd == "") emit nothing — they consume a server-pushed
# banner (BPQ's "Connected to RHP Server" / xrouter equivalent) and that
# plumbing isn't useful to surface.
ProgressFn = Callable[[str], None]


@dataclass(frozen=True)
class HopStep:
    cmd: str
    val: str
    timeout: float | None = None  # None ⇒ use runner default


class HopScriptError(RuntimeError):
    """Raised when a hop step fails (timeout, error token, EOF)."""


async def run_connect_script(
    stream: AsyncByteStream,
    steps: list[HopStep],
    *,
    error_terms: tuple[str, ...] = DEFAULT_ERROR_TERMS,
    default_timeout: float | None = DEFAULT_TIMEOUT,
    on_progress: ProgressFn | None = None,
) -> None:
    """Drive ``steps`` in order against ``stream``.

    Sends each step's ``cmd`` (suffix ``\\r``), then accumulates inbound
    bytes until either ``val`` is seen (advance) or an entry from
    ``error_terms`` appears (raise ``HopScriptError``). A per-step timeout
    bounds the wait. Buffer carries over between steps so trailing bytes
    of one step don't get lost — and so does the "already-emitted" cursor
    so we don't repeat lines already shown to the user.
    """
    if not steps:
        return
    upper_error_terms = tuple(t.upper() for t in error_terms)
    buf = ""
    emitted = 0  # length of buf already streamed via on_progress
    # Wait-only steps (cmd == "") are internal preamble and shouldn't show up
    # in user-visible hop numbering. Count and index only steps the user wrote.
    user_total = sum(1 for s in steps if s.cmd)
    user_idx = 0
    for step in steps:
        if step.cmd:
            user_idx += 1
            prefix = f"[hop {user_idx}/{user_total}]"
            step_progress = on_progress
            step_progress and step_progress(f"{prefix} > {step.cmd}")
            await stream.send(step.cmd.encode("utf-8") + b"\r")
        else:
            # Wait-only steps consume a server-pushed banner (BPQ's "Connected
            # to RHP Server", or an xrouter equivalent) so it doesn't
            # false-match a later hop's `val`. Suppress their progress output
            # entirely — they're plumbing, not a user-configured hop.
            prefix = ""
            step_progress = None
        timeout = step.timeout if step.timeout is not None else default_timeout
        try:
            buf, emitted = await asyncio.wait_for(
                _await_match(
                    stream, buf, emitted, step.val, upper_error_terms,
                    step_progress, prefix,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise HopScriptError(
                f"hop {user_idx}/{user_total} {step.cmd!r}: timed out after "
                f"{timeout:g}s waiting for {step.val!r}"
            ) from None


async def _await_match(
    stream: AsyncByteStream,
    buf: str,
    emitted: int,
    val: str,
    upper_error_terms: tuple[str, ...],
    on_progress: ProgressFn | None,
    prefix: str,
) -> tuple[str, int]:
    """Read until ``val`` is in ``buf``. Return ``(buf_after_match, new_emitted)``.

    Latin-1 keeps every input byte addressable as a single char, which is
    what the substring match needs (it's not interpreting JSON).
    """
    while True:
        idx = buf.find(val)
        if idx != -1:
            match_end = idx + len(val)
            consume_end = match_end
            if on_progress and emitted < match_end:
                # Flush up to the end of the line containing the match,
                # so the user sees the whole "*** Connected to WPS" line
                # rather than a fragment truncated at val's last char.
                # Anything past that line belongs to the next hop.
                end_cr = buf.find("\r", match_end)
                end_lf = buf.find("\n", match_end)
                line_breaks = [x for x in (end_cr, end_lf) if x != -1]
                flush_end = min(line_breaks) + 1 if line_breaks else match_end
                _emit_block(buf[emitted:flush_end], on_progress, prefix)
                # Drop the displayed line entirely — otherwise the bytes
                # between match_end and the line break get re-emitted as
                # a stray "complete line" on the next hop's pre-emit pass.
                consume_end = flush_end
            return buf[consume_end:], 0  # buffer rebased; emitted resets
        upper = buf.upper()
        for term in upper_error_terms:
            err_idx = upper.find(term)
            if err_idx != -1:
                snippet = buf[max(0, err_idx - 20): err_idx + len(term) + 40]
                raise HopScriptError(
                    f"node returned error token {term!r} while waiting for "
                    f"{val!r}: …{snippet!r}…"
                )
        # If val isn't here yet, every complete line in the buffer is
        # logically before the match — safe to surface them now.
        if on_progress:
            emitted = _emit_complete_lines(buf, emitted, on_progress, prefix)
        chunk = await stream.recv()
        if not chunk:
            raise HopScriptError(
                f"stream closed while waiting for {val!r} (buffer was {buf!r})"
            )
        buf += chunk.decode("latin-1")


def _emit_complete_lines(
    buf: str, emitted: int, on_progress: ProgressFn, prefix: str
) -> int:
    """Emit every fully-terminated line in ``buf[emitted:]``.

    A line is "complete" once we've seen a ``\\r`` or ``\\n`` after it.
    Trailing bytes after the last terminator stay buffered (they may be
    a partial line still arriving on the wire).
    """
    pending = buf[emitted:]
    last_break = max(pending.rfind("\r"), pending.rfind("\n"))
    if last_break < 0:
        return emitted
    complete = pending[: last_break + 1]
    _emit_block(complete, on_progress, prefix)
    return emitted + len(complete)


def _emit_block(text: str, on_progress: ProgressFn, prefix: str) -> None:
    """Split a chunk on CR/LF/CRLF and emit each non-empty line."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            on_progress(f"{prefix} < {stripped}")
