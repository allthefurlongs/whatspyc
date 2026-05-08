"""Reply-rendering helpers — resolver + prefix builders."""

from __future__ import annotations

from pathlib import Path

from whatspyc.store.store import SqliteStore
from whatspyc.ui import (
    reply_call_for,
    reply_natural_key,
    reply_prefix_text,
    resolve_reply_meta,
)


def test_dm_reply_persists_round_trip(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_message(
        {"_id": "100-M0ABC", "fc": "M0ABC", "tc": "T3EST", "m": "parent body", "ts": 100}
    )
    s.upsert_message(
        {
            "_id": "200-T3EST",
            "fc": "T3EST",
            "tc": "M0ABC",
            "m": "this is a reply",
            "ts": 200,
            "r": "100-M0ABC",
        }
    )
    rows = s.recent_messages("M0ABC")
    reply_row = next(r for r in rows if r["id"] == "200-T3EST")
    assert reply_row["reply_id"] == "100-M0ABC"
    s.close()


def test_post_reply_persists_round_trip(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_post(5, {"ts": 1_700_000_000_000, "fc": "M0ABC", "p": "hello"})
    s.upsert_post(
        5,
        {
            "ts": 1_700_000_001_000,
            "fc": "T3EST",
            "p": "responding",
            "rts": 1_700_000_000_000,
            "rfc": "M0ABC",
        },
    )
    posts = s.recent_posts(5)
    reply = next(p for p in posts if p["ts"] == 1_700_000_001_000)
    assert reply["reply_ts"] == 1_700_000_000_000
    assert reply["reply_from"] == "M0ABC"
    s.close()


def test_reply_natural_key_dm() -> None:
    assert reply_natural_key("dm", {"reply_id": "100-M0ABC"}) == "100-M0ABC"
    assert reply_natural_key("dm", {"r": "100-M0ABC"}) == "100-M0ABC"
    assert reply_natural_key("dm", {}) is None


def test_reply_natural_key_post() -> None:
    assert reply_natural_key("ch", {"reply_ts": 1234}) == "1234"
    assert reply_natural_key("ch", {"rts": 5678}) == "5678"
    assert reply_natural_key("ch", {}) is None


def test_reply_call_for_dm_parses_id() -> None:
    # DM `r` is the parent's `_id` ({ts}-{fc}); the call is the suffix.
    assert reply_call_for("dm", {"reply_id": "100-M0ABC"}) == "M0ABC"
    assert reply_call_for("dm", {}) is None
    # Lower-case input is upper-cased on output for consistency with
    # the rest of the UI's call-rendering path.
    assert reply_call_for("dm", {"reply_id": "100-m0abc"}) == "M0ABC"


def test_reply_call_for_post_uses_rfc() -> None:
    assert reply_call_for("ch", {"reply_from": "g7xyz"}) == "G7XYZ"
    assert reply_call_for("ch", {}) is None


def test_resolve_reply_meta_dm_in_db(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_message(
        {"_id": "100-M0ABC", "fc": "M0ABC", "tc": "T3EST", "m": "parent body", "ts": 100}
    )
    reply_row = {"reply_id": "100-M0ABC"}
    meta = resolve_reply_meta(s, "dm", "M0ABC", reply_row)
    assert meta is not None
    assert meta["in_db"] is True
    assert meta["call"] == "M0ABC"
    # First 10 chars of body, then '...' (parent body is 11 chars).
    assert meta["snippet"] == "parent bod..."
    assert meta["parent"]["body"] == "parent body"
    s.close()


def test_resolve_reply_meta_short_body_has_no_ellipsis(tmp_path: Path) -> None:
    """Parent bodies with ``len(body) <= 10`` render verbatim — no `...`."""
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_message(
        {"_id": "100-M0ABC", "fc": "M0ABC", "tc": "T3EST", "m": "hi there", "ts": 100}
    )
    meta = resolve_reply_meta(s, "dm", "M0ABC", {"reply_id": "100-M0ABC"})
    assert meta is not None
    assert meta["snippet"] == "hi there"
    s.close()


def test_resolve_reply_meta_exactly_ten_chars_has_no_ellipsis(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_message(
        {"_id": "100-M0ABC", "fc": "M0ABC", "tc": "T3EST", "m": "1234567890", "ts": 100}
    )
    meta = resolve_reply_meta(s, "dm", "M0ABC", {"reply_id": "100-M0ABC"})
    assert meta is not None
    assert meta["snippet"] == "1234567890"
    s.close()


def test_resolve_reply_meta_dm_not_in_db(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    meta = resolve_reply_meta(s, "dm", "T3EST", {"reply_id": "999-M0ABC"})
    assert meta is not None
    assert meta["in_db"] is False
    # Call comes from the embedded fc in the parent's _id.
    assert meta["call"] == "M0ABC"
    assert meta["snippet"] is None
    s.close()


def test_resolve_reply_meta_post_in_db(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_post(5, {"ts": 1_700_000_000_000, "fc": "M0ABC", "p": "hello world here"})
    reply_row = {"reply_ts": 1_700_000_000_000, "reply_from": "M0ABC"}
    meta = resolve_reply_meta(s, "ch", "5", reply_row)
    assert meta is not None
    assert meta["in_db"] is True
    assert meta["call"] == "M0ABC"
    assert meta["snippet"] == "hello worl..."
    s.close()


def test_resolve_reply_meta_post_not_in_db(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    meta = resolve_reply_meta(
        s, "ch", "5", {"reply_ts": 1_700_000_000_000, "reply_from": "M0ABC"}
    )
    assert meta is not None
    assert meta["in_db"] is False
    assert meta["call"] == "M0ABC"
    s.close()


def test_resolve_reply_meta_returns_none_when_not_a_reply(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    assert resolve_reply_meta(s, "dm", "T3EST", {}) is None
    assert resolve_reply_meta(s, "ch", "5", {}) is None
    s.close()


def test_reply_prefix_text_in_db_with_call() -> None:
    meta = {"in_db": True, "call": "M0ABC", "snippet": "first ten ..."}
    assert reply_prefix_text(meta) == "[Reply To M0ABC: first ten ...] "


def test_reply_prefix_text_not_in_db_with_call() -> None:
    meta = {"in_db": False, "call": "M0ABC", "snippet": None}
    assert reply_prefix_text(meta) == "[Reply To M0ABC: <msg not in db>] "


def test_reply_prefix_text_not_in_db_no_call() -> None:
    meta = {"in_db": False, "call": None, "snippet": None}
    assert reply_prefix_text(meta) == "[Reply To: <msg not in db>] "


def test_reply_prefix_text_empty_when_no_meta() -> None:
    assert reply_prefix_text(None) == ""


# ---------------------------------------------------------------------------
# Renderer integration: prefix lands in the rendered line for each UI.
# ---------------------------------------------------------------------------


def test_line_ui_renders_reply_prefix_for_dm(tmp_path: Path, capsys) -> None:
    from types import SimpleNamespace
    from whatspyc.ui.line import LineUI
    from whatspyc.ui.options import SessionOptions

    store = SqliteStore(tmp_path / "state.sqlite3")
    store.upsert_message(
        {"_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "the parent here", "ts": 100}
    )
    store.upsert_message(
        {
            "_id": "200-M0ABC",
            "fc": "M0ABC",
            "tc": "M0FOO",
            "m": "answering",
            "ts": 200,
            "r": "100-M0FOO",
        },
    )
    client = SimpleNamespace(
        _store=store,
        paused_channels=lambda: {},
        auto_backfill_post_count=None,
        ham_name=lambda c: None,
        set_delivery_timeout_s=lambda v: None,
    )
    ui = LineUI(client, my_call="M0ABC", history_backfill=3, options=SessionOptions())
    ui._show_history(("dm", "M0FOO"), 2)
    out = capsys.readouterr().out
    # The reply line carries the prefix; the parent does not.
    assert "[Reply To M0FOO: the parent" in out
    assert "answering" in out
    store.close()


def test_textual_render_row_emits_yellow_reply_prefix() -> None:
    from whatspyc.ui.textual_ui import _render_row

    line = _render_row(
        kind="dm",
        from_call="M0ABC",
        body="hi back",
        ts=1_700_000_000_000,
        edit_ts=None,
        delivered_ts=1_700_000_000_500,
        received_ts=None,
        realtime=None,
        lid=1,
        my_call="M0ABC",
        verbose=False,
        ham_name=lambda c: None,
        delivery_timeout_s=30,
        reply_meta={
            "in_db": True,
            "call": "M0FOO",
            "snippet": "hello ther...",
            "parent": {"body": "hello there"},
        },
    )
    assert r"[yellow]\[Reply To M0FOO: hello ther...][/]" in line


def test_urwid_render_row_emits_reply_attr_span() -> None:
    from whatspyc.ui.urwid_ui import _render_row_markup

    parts = _render_row_markup(
        kind="ch",
        from_call="M0ABC",
        body="ack",
        ts=1_700_000_000_000,
        edit_ts=None,
        delivered_ts=1_700_000_000_500,
        received_ts=None,
        realtime=None,
        lid=1,
        my_call="M0ABC",
        verbose=False,
        ham_name=lambda c: None,
        delivery_timeout_s=30,
        reply_meta={
            "in_db": False,
            "call": "M0FOO",
            "snippet": None,
            "parent": None,
        },
    )
    assert any(
        attr == "reply" and "<msg not in db>" in text
        for attr, text in parts
        if isinstance(text, str)
    )
