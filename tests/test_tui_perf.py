"""Targeted coverage for the TUI performance optimisations.

These tests use Textual's ``App.run_test()`` Pilot harness so the actual
widget tree is mounted and we can assert on real DOM state. The goal is
to lock in the *behavioural* contracts — incremental online-pane diff,
verbose-toggle scope, EmojiPrompt search debounce — so a future refactor
that quietly regresses to the clear+rebuild model fails loudly here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Input, ListView

from whatspyc.store.store import SqliteStore
from whatspyc.ui.options import SessionOptions
from whatspyc.ui.textual_ui import (
    EmojiPrompt,
    MessageRow,
    TextualUI,
    _WhatspycApp,
)


def _make_ui(tmp_path: Path, *, options: SessionOptions | None = None) -> tuple[TextualUI, SqliteStore]:
    """Build a TextualUI wrapping a stub WpsClient + on-disk store.

    The stub mirrors the surface ``_WhatspycApp`` actually touches —
    ``_store``, ``ham_name``, ``online_users``, ``paused_channels``,
    ``is_auto_reconnect``, ``set_delivery_timeout_s`` — and nothing else.
    """
    store = SqliteStore(tmp_path / "state.sqlite3")
    online: list[str] = []
    client = SimpleNamespace(
        _store=store,
        ham_name=lambda call: (
            (store.lookup_ham(call) or {}).get("name") or None
        ),
        online_users=lambda: list(online),
        paused_channels=lambda: {},
        is_auto_reconnect=False,
        set_delivery_timeout_s=lambda v: None,
        _online_list=online,  # exposed for test mutation
    )
    ui = TextualUI(
        client,
        my_call="M0ABC",
        options=options or SessionOptions(),
    )
    return ui, store


# ----------------------------------------------------------------------
# B.1 — Online pane incremental diff
# ----------------------------------------------------------------------


def test_refresh_online_pane_adds_one_item_for_one_join(tmp_path: Path) -> None:
    """Driving ``_refresh_online_pane`` with one extra user adds one
    ListItem and reuses the existing ones — no full clear+rebuild."""

    async def _run() -> None:
        ui, store = _make_ui(tmp_path)
        try:
            app = _WhatspycApp(ui)
            ui._app = app
            async with app.run_test() as pilot:
                # Seed the pane with three users.
                ui._client._online_list[:] = ["M0AAA", "M0BBB", "M0CCC"]
                app._refresh_online_pane(ui._client.online_users())
                await pilot.pause()
                assert set(app._online_items.keys()) == {"M0AAA", "M0BBB", "M0CCC"}
                seeded_items = dict(app._online_items)

                # Now add one user. The diff path must keep the same
                # ListItem objects for retained users.
                ui._client._online_list.append("M0DDD")
                app._refresh_online_pane(ui._client.online_users())
                await pilot.pause()
                assert set(app._online_items.keys()) == {
                    "M0AAA", "M0BBB", "M0CCC", "M0DDD"
                }
                # Identity check: retained users keep the same ListItem
                # instance — the proof that we didn't clear+rebuild.
                for call in ("M0AAA", "M0BBB", "M0CCC"):
                    assert app._online_items[call] is seeded_items[call]
        finally:
            store.close()

    asyncio.run(_run())


def test_refresh_online_pane_removes_one_item_for_one_part(tmp_path: Path) -> None:
    """Symmetric to the join case — one departure should drop one item."""

    async def _run() -> None:
        ui, store = _make_ui(tmp_path)
        try:
            app = _WhatspycApp(ui)
            ui._app = app
            async with app.run_test() as pilot:
                ui._client._online_list[:] = ["M0AAA", "M0BBB", "M0CCC"]
                app._refresh_online_pane(ui._client.online_users())
                await pilot.pause()
                kept_aaa = app._online_items["M0AAA"]
                kept_ccc = app._online_items["M0CCC"]

                ui._client._online_list.remove("M0BBB")
                app._refresh_online_pane(ui._client.online_users())
                await pilot.pause()
                assert set(app._online_items.keys()) == {"M0AAA", "M0CCC"}
                # Retained items must be the same instances.
                assert app._online_items["M0AAA"] is kept_aaa
                assert app._online_items["M0CCC"] is kept_ccc
        finally:
            store.close()

    asyncio.run(_run())


def test_refresh_online_pane_relabels_only_when_name_changes(tmp_path: Path) -> None:
    """An ``he`` event re-runs ``_refresh_online_pane`` with the same
    roster but possibly-different display names. Items shouldn't be
    rebuilt; their labels should only update where the resolved name
    changed."""

    async def _run() -> None:
        ui, store = _make_ui(tmp_path)
        try:
            app = _WhatspycApp(ui)
            ui._app = app
            async with app.run_test() as pilot:
                ui._client._online_list[:] = ["M0AAA"]
                app._refresh_online_pane(ui._client.online_users())
                await pilot.pause()
                first = app._online_label_cache["M0AAA"]
                # Repeat with no changes — label cache stays intact.
                app._refresh_online_pane(ui._client.online_users())
                await pilot.pause()
                assert app._online_label_cache["M0AAA"] == first

                # Now seed a ham row — name resolution changes for M0AAA.
                store.upsert_ham("M0AAA", "Alice", 1)
                app._refresh_online_pane(ui._client.online_users())
                await pilot.pause()
                assert app._online_label_cache["M0AAA"] == "Alice, M0AAA"
                assert app._online_label_cache["M0AAA"] != first
        finally:
            store.close()

    asyncio.run(_run())


# ----------------------------------------------------------------------
# Persistent unread cursors
# ----------------------------------------------------------------------


def test_unread_seeded_into_channel_label_at_session_start(tmp_path: Path) -> None:
    """Channels with stored unread posts left from a previous session
    must render with the (N) suffix in the channels list pane on first
    start — without any wire events firing."""

    async def _run() -> None:
        # Previous session: subscribe + 3 inbound posts in #5; quit
        # without reading them.
        s = SqliteStore(tmp_path / "state.sqlite3")
        s.set_subscription(5, True)
        s.upsert_post(5, {"ts": 100, "fc": "G7BAR", "p": "a"})
        s.upsert_post(5, {"ts": 200, "fc": "G7BAR", "p": "b"})
        s.upsert_post(5, {"ts": 300, "fc": "G7BAR", "p": "c"})
        s.close()

        ui, store = _make_ui(tmp_path)
        try:
            app = _WhatspycApp(ui)
            ui._app = app
            async with app.run_test() as pilot:
                await pilot.pause()
                # Seed populated _unread from the stored cursor.
                target = ("ch", "5")
                assert app._unread.get(target) == 3
                # Channel list item shows the (3) suffix in its label.
                lv = app.query_one("#channels", ListView)
                labels = [
                    str(item.query_one("Static").content)
                    for item in lv.children
                ]
                assert any("(3)" in label for label in labels), (
                    f"channel labels should contain '(3)' but were {labels!r}"
                )
        finally:
            store.close()

    asyncio.run(_run())


def test_unread_seeded_in_session_driven_mode(tmp_path: Path) -> None:
    """Reproduce the user's actual flow: client is None at on_mount,
    set later in _on_client_ready, and _post_connect_setup runs from
    the bootstrap. Per-channel unread (N) suffix should show on first
    start with no wire events."""
    from whatspyc.config import ConnectProfile
    from whatspyc.ui.textual_ui import TextualUI

    async def _run() -> None:
        # Pre-seed the store as if a previous session left unread posts.
        s = SqliteStore(tmp_path / "state.sqlite3")
        s.set_subscription(5, True)
        s.upsert_post(5, {"ts": 100, "fc": "G7BAR", "p": "a"})
        s.upsert_post(5, {"ts": 200, "fc": "G7BAR", "p": "b"})
        s.upsert_post(5, {"ts": 300, "fc": "G7BAR", "p": "c"})
        s.close()

        s2 = SqliteStore(tmp_path / "state.sqlite3")
        online: list[str] = []

        def make_client():
            return SimpleNamespace(
                _store=s2,
                ham_name=lambda call: None,
                online_users=lambda: list(online),
                paused_channels=lambda: {},
                is_auto_reconnect=False,
                set_delivery_timeout_s=lambda v: None,
                _online_list=online,
            )

        async def opener(profile, *, progress, on_event,
                         on_client_ready=None):
            c = make_client()
            if on_client_ready:
                on_client_ready(c)  # synchronous; sets client + seeds
            return c, None

        ui = TextualUI(
            client=None,
            my_call="M0ABC",
            options=SessionOptions(),
            connection_opener=opener,
            available_profiles=[ConnectProfile(name="X")],
            initial_profile=ConnectProfile(name="X"),
            is_offline_profile=lambda p: True,  # take the offline branch
        )
        try:
            app = _WhatspycApp(ui)
            ui._app = app
            async with app.run_test() as pilot:
                # Wait long enough for the bootstrap worker to run and
                # _post_connect_setup to populate the channels list.
                await pilot.pause()
                await pilot.pause()
                target = ("ch", "5")
                assert app._unread.get(target) == 3
                lv = app.query_one("#channels", ListView)
                labels = [
                    str(item.query_one("Static").content)
                    for item in lv.children
                ]
                assert any("(3)" in label for label in labels), (
                    f"channel labels should contain '(3)' but were {labels!r}"
                )
        finally:
            s2.close()

    asyncio.run(_run())


# ----------------------------------------------------------------------
# B.2 — Verbose-toggle scope
# ----------------------------------------------------------------------


def test_verbose_toggle_only_refreshes_active_target(tmp_path: Path) -> None:
    """Ctrl+D must repaint only rows in the active target. Inactive
    targets get marked dirty for lazy paydown on next activation."""

    async def _run() -> None:
        ui, store = _make_ui(tmp_path)
        try:
            app = _WhatspycApp(ui)
            ui._app = app
            async with app.run_test() as pilot:
                # Build two MessageRows for two different targets and
                # park them in ``_rows`` directly. Avoids needing the
                # full inbound-DM flow for a unit-style assertion.
                target_a: tuple[str, str] = ("dm", "M0FOO")
                target_b: tuple[str, str] = ("dm", "M0BAR")
                row_a = MessageRow(
                    kind="dm", target_key="M0FOO", natural_key="1-M0FOO",
                    from_call="M0FOO", body="hello a", ts=1000,
                )
                row_b = MessageRow(
                    kind="dm", target_key="M0BAR", natural_key="1-M0BAR",
                    from_call="M0BAR", body="hello b", ts=1000,
                )
                app._rows[("dm", "M0FOO", "1-M0FOO")] = row_a
                app._rows[("dm", "M0BAR", "1-M0BAR")] = row_b
                # Pretend both views exist so the dirty set has two
                # candidates to choose from. Active is target_a.
                app._views[target_a] = "msgs-test-a"
                app._views[target_b] = "msgs-test-b"

                # Stub ``_active_target`` to return target_a so we don't
                # need a real ContentSwitcher state.
                app._active_target = lambda: target_a  # type: ignore[assignment]

                # Toggle. Active row is refreshed (its render_key is
                # populated); inactive row's render_key stays None.
                app.action_toggle_verbose()
                assert row_a._render_key is not None
                assert row_b._render_key is None
                # Inactive target is in _verbose_dirty for lazy paydown.
                assert target_b in app._verbose_dirty
                assert target_a not in app._verbose_dirty
        finally:
            store.close()

    asyncio.run(_run())


# ----------------------------------------------------------------------
# B.4 — EmojiPrompt search debounce
# ----------------------------------------------------------------------


def test_emoji_prompt_debounce_coalesces_rapid_keystrokes(tmp_path: Path) -> None:
    """Several keystrokes in quick succession arm + replace the same
    timer; only one ``_render_view`` actually runs after the debounce
    window elapses."""

    async def _run() -> None:
        ui, store = _make_ui(tmp_path)
        try:
            app = _WhatspycApp(ui)
            ui._app = app
            async with app.run_test() as pilot:
                # 60 ms debounce — small enough that the test stays
                # quick, large enough that successive ``set_value``
                # calls clearly land inside the window.
                prompt = EmojiPrompt()
                prompt._debounce_ms = 60
                await app.push_screen(prompt)
                await pilot.pause()

                render_calls: list[int] = []
                # Wrap the original render view so the test counts actual
                # invocations rather than introspecting the timer.
                original = prompt._render_view

                async def _counting_render() -> None:
                    render_calls.append(1)
                    await original()

                prompt._render_view = _counting_render  # type: ignore[assignment]

                # Type five characters in rapid succession via the
                # search Input. Each Input.Changed re-arms the timer.
                inp = prompt.query_one("#emoji-search", Input)
                for ch in "smile":
                    inp.value = inp.value + ch
                    await pilot.pause()  # let the Changed event flow

                # Wait long enough for the debounce to fire (60 ms +
                # some headroom for the worker scheduler).
                await asyncio.sleep(0.3)
                await pilot.pause()
                # Exactly one render after the debounce window — the
                # timer fired once, regardless of how many keystrokes
                # arrived.
                assert len(render_calls) == 1, (
                    f"expected 1 debounced render, got {len(render_calls)} "
                    f"({render_calls})"
                )
        finally:
            store.close()

    asyncio.run(_run())


# ----------------------------------------------------------------------
# Chronological insertion of late arrivals
# ----------------------------------------------------------------------


def test_late_arriving_dm_inserts_in_timestamp_order(tmp_path: Path) -> None:
    """A DM whose ts is older than rows already mounted must slot
    into the correct chronological position, not append at the end."""

    async def _run() -> None:
        ui, store = _make_ui(tmp_path)
        try:
            app = _WhatspycApp(ui)
            ui._app = app
            async with app.run_test() as pilot:
                target = ("dm", "M0FOO")
                await app._switch_centre_to(target)
                await pilot.pause()

                for ts in (100, 200, 300):
                    await app._dispatch_event(
                        {"t": "m", "_id": f"{ts}-M0FOO", "fc": "M0FOO",
                         "tc": "M0ABC", "ts": ts, "m": f"msg{ts}"}
                    )
                await pilot.pause()
                # Late arrival between 100 and 200.
                await app._dispatch_event(
                    {"t": "m", "_id": "150-M0FOO", "fc": "M0FOO",
                     "tc": "M0ABC", "ts": 150, "m": "late"}
                )
                # Older than everything.
                await app._dispatch_event(
                    {"t": "m", "_id": "50-M0FOO", "fc": "M0FOO",
                     "tc": "M0ABC", "ts": 50, "m": "very late"}
                )
                await pilot.pause()

                lv = app.query_one(f"#{app._views[target]}", ListView)
                rows = [c for c in lv.children if isinstance(c, MessageRow)]
                assert [r.ts for r in rows] == [50, 100, 150, 200, 300]
                assert [r.body for r in rows] == [
                    "very late", "msg100", "late", "msg200", "msg300",
                ]
        finally:
            store.close()

    asyncio.run(_run())


def test_late_arriving_post_inserts_in_timestamp_order(tmp_path: Path) -> None:
    """Symmetric to the DM case but for channel posts."""

    async def _run() -> None:
        ui, store = _make_ui(tmp_path)
        try:
            app = _WhatspycApp(ui)
            ui._app = app
            async with app.run_test() as pilot:
                target = ("ch", "5")
                await app._switch_centre_to(target)
                await pilot.pause()

                for ts in (1000, 2000, 3000):
                    await app._dispatch_event(
                        {"t": "cp", "cid": 5, "ts": ts, "fc": "M0FOO",
                         "p": f"post{ts}"}
                    )
                await app._dispatch_event(
                    {"t": "cp", "cid": 5, "ts": 1500, "fc": "M0FOO",
                     "p": "late"}
                )
                await pilot.pause()

                lv = app.query_one(f"#{app._views[target]}", ListView)
                rows = [c for c in lv.children if isinstance(c, MessageRow)]
                assert [r.ts for r in rows] == [1000, 1500, 2000, 3000]
        finally:
            store.close()

    asyncio.run(_run())
