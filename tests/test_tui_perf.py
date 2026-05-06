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
from whatspyc.ui.tui import (
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
                prompt = EmojiPrompt(debounce_ms=60)
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
