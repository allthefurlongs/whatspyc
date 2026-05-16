"""LineUI input scrubbing: C0 controls + DEL stripped from stdin reads
so packet-node keepalive NULs (and any stray control byte) can't mask
the leading-``/`` check and cause ``/quit`` to post as a message."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

from whatspyc.store.store import SqliteStore
from whatspyc.ui.line import LineUI, _BPQ_STATUS_LINE_RE, _INPUT_CONTROL_STRIP
from whatspyc.ui.options import SessionOptions


def _make_ui(tmp_path: Path) -> LineUI:
    store = SqliteStore(tmp_path / "state.sqlite3")
    client = SimpleNamespace(
        _store=store,
        paused_channels=lambda: {},
        ham_name=lambda c: None,
        online_users=lambda: [],
        is_auto_reconnect=False,
        set_delivery_timeout_s=lambda v: None,
    )
    return LineUI(
        client,
        my_call="M0ABC",
        history_backfill=0,
        options=SessionOptions(),
        offline=True,
    )


def test_strip_set_excludes_str_strip_whitespace() -> None:
    """The strip set must NOT include the whitespace chars ``str.strip()``
    already removes — duplicating them is harmless but signals the
    author misunderstood the intent of the set."""
    for ws in ("\t", "\n", "\x0b", "\x0c", "\r", " "):
        assert ws not in _INPUT_CONTROL_STRIP, (
            f"{ws!r} is whitespace; str.strip() handles it"
        )


def test_strip_set_covers_c0_and_del() -> None:
    """NUL + a handful of representative C0 controls + DEL must all be
    in the set so they're scrubbed off the line."""
    for ch in ("\x00", "\x01", "\x03", "\x08", "\x0e", "\x1b", "\x1f", "\x7f"):
        assert ch in _INPUT_CONTROL_STRIP


def test_strip_set_preserves_printable_and_emoji() -> None:
    """Sanity-check: the set never touches printable ASCII or emoji
    codepoints — these would silently mangle user input if they leaked
    in."""
    for ch in ("/", "a", "Z", "0", " ", "\U0001f44d", "‍", "ÿ"):
        assert ch not in _INPUT_CONTROL_STRIP


def test_read_line_strips_leading_nuls(tmp_path: Path, monkeypatch) -> None:
    """Packet-node keepalive NULs that accumulate in the input buffer
    before a command must be stripped, so ``/quit`` is routed to the
    command handler and not posted to the current channel."""
    monkeypatch.setattr("sys.stdin", io.StringIO("\x00\x00/quit\r\n"))
    ui = _make_ui(tmp_path)
    line = asyncio.run(ui._read_line())
    assert line == "/quit"
    assert line.startswith("/")


def test_read_line_strips_mixed_controls(tmp_path: Path, monkeypatch) -> None:
    """Leading + trailing C0/DEL bytes are both stripped; whitespace in
    the middle of the user's text is preserved."""
    monkeypatch.setattr("sys.stdin", io.StringIO("\x00\x01hello world\x7f\r\n"))
    ui = _make_ui(tmp_path)
    line = asyncio.run(ui._read_line())
    assert line == "hello world"


def test_read_line_preserves_emoji(tmp_path: Path, monkeypatch) -> None:
    """Emoji codepoints are far above the C0+DEL range, so a message
    body containing one survives the strip unmodified."""
    monkeypatch.setattr("sys.stdin", io.StringIO("\x00hello \U0001f44d\r\n"))
    ui = _make_ui(tmp_path)
    line = asyncio.run(ui._read_line())
    assert line == "hello \U0001f44d"


def test_bpq_disconnect_status_line_dropped(tmp_path: Path, monkeypatch) -> None:
    """When the user leaves the WPS application back to the node prompt,
    BPQ writes ``*** Disconnected from Stream <N>`` into stdin. If a
    /dm or /ch target was set, that line would otherwise be posted as
    a chat message. ``_read_line`` returns an empty string so the run
    loop's empty-line skip drops it."""
    monkeypatch.setattr("sys.stdin", io.StringIO("*** Disconnected from Stream 10\r\n"))
    ui = _make_ui(tmp_path)
    line = asyncio.run(ui._read_line())
    assert line == ""


def test_bpq_status_regex_matches_variants() -> None:
    """The regex matches the canonical form and is lenient about what
    follows ``Stream`` — any stream id, with or without trailing junk."""
    for s in (
        "*** Disconnected from Stream 1",
        "*** Disconnected from Stream 10",
        "*** Disconnected from Stream 999",
        "*** Disconnected from Stream 10 (timeout)",
    ):
        assert _BPQ_STATUS_LINE_RE.match(s), s


def test_bpq_status_regex_ignores_user_text() -> None:
    """The prefix is specific enough that ordinary chat — including
    messages that happen to start with ``***`` — isn't swallowed."""
    for s in (
        "*** hello",
        "hello *** Disconnected from Stream 10",  # not at start
        "*** Disconnected from stream 10",        # case-sensitive
        "/quit",
        "ack received",
    ):
        assert not _BPQ_STATUS_LINE_RE.match(s), s
