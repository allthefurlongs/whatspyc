"""Smoke tests for the urwid UI.

We can't run ``urwid.MainLoop.run()`` from pytest — it expects a real
terminal — so these tests build the widget tree, dispatch events
synchronously, and assert state. The goal is the same as
``test_tui_perf.py`` for the Textual backend: lock in the wiring
contracts (online-pane diff, target list population, message-row mount,
event dispatch) so a future refactor that quietly breaks them fails
loudly here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from whatspyc.config import ChannelInfo
from whatspyc.store.store import SqliteStore
from whatspyc.ui.options import SessionOptions
from whatspyc.ui.urwid_ui import (
    EmojiPrompt,
    HelpScreen,
    QuitConfirmModal,
    SubscribeModal,
    UrwidUI,
    _InputEdit,
    _MessageRow,
    _UrwidApp,
)


def _make_app(
    tmp_path: Path,
    *,
    options: SessionOptions | None = None,
    channels: list[ChannelInfo] | None = None,
    offline: bool = True,
) -> tuple[UrwidUI, _UrwidApp, SqliteStore]:
    """Build a UrwidUI + _UrwidApp wrapping a stub WpsClient + on-disk store.

    Mirrors ``test_tui_perf.py``'s helper. The stub mimics the
    ``WpsClient`` surface that the urwid app actually touches —
    ``_store``, ``ham_name``, ``paused_channels``, plus a few async
    methods called from slash-command paths but never reached in the
    smoke tests.
    """
    store = SqliteStore(tmp_path / "state.sqlite3")
    online: list[str] = []
    client = SimpleNamespace(
        _store=store,
        _name="Tester",
        ham_name=lambda call: (
            (store.lookup_ham(call) or {}).get("name") or None
        ),
        online_users=lambda: list(online),
        paused_channels=lambda: {},
        is_auto_reconnect=False,
        set_delivery_timeout_s=lambda v: None,
        auto_backfill_post_count=10,
        _online_list=online,
        close=lambda: asyncio.sleep(0),
    )
    ui = UrwidUI(
        client,
        my_call="M0ABC",
        channels=channels or [
            ChannelInfo(cid=5, name="lounge"),
            ChannelInfo(cid=7, name="space"),
        ],
        history_backfill=3,
        options=options or SessionOptions(),
        offline=offline,
    )
    app = _UrwidApp(ui)
    ui._app = app
    app._build_widgets()
    return ui, app, store


# ---------------------------------------------------------------------
# Construction + initial state
# ---------------------------------------------------------------------


def test_widget_tree_builds(tmp_path: Path) -> None:
    """The full widget tree comes up without error and has the right
    shape — a Frame holding a body Columns plus footer Pile."""
    ui, app, store = _make_app(tmp_path)
    try:
        assert app._frame is not None
        assert app._frame_holder is not None
        # Header has the version string.
        assert app._header_text is not None
        assert "whatspyc" in str(app._header_text.text)
        # Two channels seeded from directory.
        assert len(app._channels_walker) == 2
        # Pinned "+ Add DM call…" row at the top of DMs.
        assert len(app._dms_walker) == 1
        # No active target on startup.
        assert ui._target is None
    finally:
        store.close()


def test_open_action_menu_moves_frame_focus_to_messages(tmp_path: Path) -> None:
    """``_MessageRow.mouse_event`` (click) opens the action menu, but
    the click doesn't move ``Frame.focus_position`` to ``body`` —
    that stays on ``footer`` (input) from when the target was
    activated. So after the menu dismisses, Up/Down arrows go to
    the input instead of the message list.

    Fix: ``_open_action_menu`` calls ``_set_focus_step("messages")``
    so Frame focus lands on the centre listbox before the menu
    appears, and arrow scrolling Just Works after dismissal."""
    ui, app, store = _make_app(tmp_path)
    try:
        # Stub the loop so _show_modal pushes onto the stack.
        app._loop = SimpleNamespace(
            set_alarm_in=lambda *a, **k: None,
            remove_alarm=lambda *a: None,
            draw_screen=lambda: None,
        )
        store.set_subscription(5, True)
        store.upsert_post(5, {"ts": 1_700_000_000, "fc": "G7XYZ", "p": "hi"},
                          realtime=True)

        async def _run() -> None:
            ui._target = ("ch", "5")
            await app._switch_centre_to(("ch", "5"))
            # Pretend the user activated by click — Frame focus left
            # on footer.
            app._frame.focus_position = "footer"
            assert app._current_focus_step() == "input"

            # Now simulate a mouse click on the row → action menu.
            row = app._walkers[("ch", "5")][0]
            assert isinstance(row, _MessageRow)
            app._open_action_menu(row)
            await asyncio.sleep(0)
            # The click should have moved Frame focus to the messages
            # list so subsequent arrow scrolling works.
            assert app._current_focus_step() == "messages"
            # Tear down the modal.
            if app._modal_stack:
                app._modal_stack[-1][0].dismiss(None)

        asyncio.run(_run())
    finally:
        store.close()


def test_enter_with_empty_input_activates_focused_message_row(tmp_path: Path) -> None:
    """When the user has arrow-scrolled the message list (focus
    visually on a row, but ``Frame.focus_position`` still on the
    input because the arrow-fall-through doesn't move it), Enter
    should open the action menu — NOT submit a blank input. Only
    when the input has typed text does Enter mean "send"."""
    from whatspyc.ui.urwid_ui import ActionMenu

    ui, app, store = _make_app(tmp_path)
    try:
        # Stub the loop so _show_modal pushes onto the stack.
        app._loop = SimpleNamespace(
            set_alarm_in=lambda *a, **k: None,
            remove_alarm=lambda *a: None,
            draw_screen=lambda: None,
        )
        store.set_subscription(5, True)
        store.upsert_post(5, {"ts": 1_700_000_000, "fc": "G7XYZ", "p": "hi"},
                          realtime=True)

        async def _run() -> None:
            ui._target = ("ch", "5")
            await app._switch_centre_to(("ch", "5"))
            lv = app._views[("ch", "5")]
            app._frame_holder.render((100, 30), focus=True)
            # Frame focus stays on input, listbox focus is on the row.
            assert app._current_focus_step() == "input"
            assert app._input.edit_text == ""

            before = len(app._modal_stack)
            handled = app._on_unhandled_input("enter")
            assert handled is True
            await asyncio.sleep(0)
            assert len(app._modal_stack) == before + 1, (
                "Enter with empty input + focused row should open ActionMenu"
            )
            assert isinstance(app._modal_stack[-1][0], ActionMenu)
            app._modal_stack[-1][0].dismiss(None)

            # Now type something into the input — Enter should submit
            # (no menu).
            app._input.set_edit_text("hello")
            before = len(app._modal_stack)
            app._on_unhandled_input("enter")
            await asyncio.sleep(0)
            assert len(app._modal_stack) == before, (
                "Enter with non-empty input must submit, not open the menu"
            )

        asyncio.run(_run())
    finally:
        store.close()


def test_arrows_from_input_scroll_active_message_list(tmp_path: Path) -> None:
    """Up/Down/PgUp/PgDn pressed while focus is on the input box
    must scroll the active message list — otherwise the user has to
    Tab through 3 panes every time they want to look back at older
    messages. The handler lives in ``_on_unhandled_input`` and
    forwards the key to the active target's listbox."""
    ui, app, store = _make_app(tmp_path)
    try:
        store.set_subscription(5, True)
        for ts in range(1_700_000_001, 1_700_000_005):
            store.upsert_post(5, {"ts": ts, "fc": "G7XYZ", "p": f"post {ts}"},
                              realtime=True)

        async def _run() -> None:
            ui._target = ("ch", "5")
            await app._switch_centre_to(("ch", "5"))
            lb = app._views[("ch", "5")]
            # Render so the listbox has a viewport.
            app._frame_holder.render((100, 30), focus=True)
            # Focus is on input (default).
            assert app._current_focus_step() == "input"
            initial = lb.focus_position
            # Press Up via unhandled_input — should advance the
            # listbox even though Frame.focus is on footer.
            handled = app._on_unhandled_input("up")
            assert handled is True
            assert lb.focus_position != initial, (
                "Up arrow from input didn't scroll the message list"
            )

        asyncio.run(_run())
    finally:
        store.close()


def test_button_label_clips_at_narrow_widths(tmp_path: Path) -> None:
    """``_Button``'s inner ``Text`` must be ``wrap="clip"``. With the
    default ``"space"`` wrap, a long label in a narrow column wraps
    onto multiple lines, which makes the parent ``Columns`` render
    taller than the parent ``Pile`` allotted — and urwid raises
    ``WidgetError: rendered (W x N) canvas when passed size (W, M)``
    deep inside ``Pile.render``. This bites in the EmojiPrompt's
    group tab strip (10 group names share a modal-wide row) and in
    the left-pane Channels/DMs tab strip when the terminal is small.
    """
    from whatspyc.ui.urwid_ui import _Button

    btn = _Button("People & Body", on_press=lambda: None)
    # Render at a width too narrow to fit the label. Must produce
    # exactly 1 row regardless.
    canv = btn.render((4,), focus=False)
    assert canv.rows() == 1, (
        f"_Button rendered {canv.rows()} rows at width 4 — should be "
        f"1 (clipped). Wrap mode regressed to 'space'?"
    )


def test_modals_render_at_small_sizes(tmp_path: Path) -> None:
    """Every modal must render cleanly even at the minimum overlay
    size urwid will give it (small terminal, ``min_height=4``,
    ``LineBox`` borders eat 2 of those leaving 2 rows for content).

    The bug: ``LineBox`` wraps the body in an internal Pile whose
    middle is a 3-item ``Columns([lline, body, rline])``. When the
    body is a flow widget (``BoxAdapter`` for ActionMenu, raw
    ``Pile`` for EmojiPrompt), the Columns inherits flow sizing but
    the parent Pile asks for box rendering, so the Columns renders
    at the flow widget's natural height instead of the allotted
    height — and ``validate_size`` raises
    ``WidgetError: rendered (W x N) canvas when passed size (W, H)``.
    The fix is in ``_ModalShell.__init__``: any body whose
    ``sizing()`` doesn't include ``"box"`` is wrapped in a Filler.
    """
    from whatspyc.ui.urwid_ui import (
        ActionMenu,
        BoolSelectModal,
        EditValueModal,
        EmojiPrompt,
        HelpScreen,
        NewDmModal,
        QuitConfirmModal,
        SettingsModal,
        SubscribeModal,
        UnsubscribeModal,
    )
    ui, app, store = _make_app(tmp_path)
    try:
        async def do_sub() -> int:
            return 5

        modals = [
            ActionMenu(allow_edit=True, allow_resend=True),
            HelpScreen(),
            EmojiPrompt(),
            SubscribeModal(
                cid=5, ref="#lounge", do_subscribe=do_sub,
                default_count_for=lambda pc: min(10, pc),
                skip_confirm=False,
            ),
            UnsubscribeModal(channel_ref="#lounge (ch:5)"),
            NewDmModal(),
            QuitConfirmModal(),
            BoolSelectModal(name="show_acks", current=True, description="d"),
            EditValueModal(name="delivery_timeout_s", current="60", description="d"),
            SettingsModal(options=ui._options, on_change=lambda *a: None),
        ]
        # 41 cols × 4 rows: this is the size urwid passes through to
        # the modal in the user's reported scenario (60% of a small
        # terminal, with the LineBox eating 2 rows leaving 2 for the
        # body Columns). All ten modals must render without raising
        # ``WidgetError``.
        for m in modals:
            m.attach(app)
            assert m.shell is not None
            canv = m.shell.render((41, 4), focus=True)
            assert canv.rows() == 4, (
                f"{type(m).__name__} render returned {canv.rows()} "
                f"rows for size (41, 4) — body widget likely a flow "
                f"widget that wasn't wrapped in Filler."
            )
    finally:
        store.close()


def test_emoji_prompt_renders_at_modest_overlay(tmp_path: Path) -> None:
    """Exercise the exact ``Pile`` → ``Columns`` render chain that was
    raising ``WidgetError`` for a user. Build an EmojiPrompt, attach
    it, switch the active group to People & Body (which previously
    triggered a 3-item subgroup tab bar), and render the shell."""
    ui, app, store = _make_app(tmp_path)
    try:
        modal = EmojiPrompt()
        modal.attach(app)
        # Trigger subgroup tab strip by switching to People & Body.
        modal._on_top_tab_change("People & Body")
        # Render at a width / height representative of a small modal.
        # Pre-fix this raised because the group tab bar's Buttons
        # wrapped to multiple rows.
        canv = modal.shell.render((50, 25), focus=True)
        assert canv.rows() == 25
    finally:
        store.close()


def test_widget_tree_renders(tmp_path: Path) -> None:
    """Render the frame at a representative terminal size. Catches
    Pile/Columns sizing-spec mismatches that the unit-construction
    test misses (e.g., a flow widget given a ``("weight", n, ...)``
    spec, which raises ``ValueError: too many values to unpack`` from
    deep inside ``BoxAdapter.render`` only at draw time)."""
    ui, app, store = _make_app(tmp_path)
    try:
        canvas = app._frame_holder.render((100, 30), focus=True)
        assert canvas.rows() == 30
    finally:
        store.close()


def test_modals_render(tmp_path: Path) -> None:
    """Each modal's shell renders at a representative size — same
    rationale as ``test_widget_tree_renders``: catch layout-spec bugs
    that bypass the build-only path."""
    from whatspyc.ui.urwid_ui import (
        BoolSelectModal,
        EditValueModal,
        NewDmModal,
        QuitConfirmModal,
        SettingsModal,
        UnsubscribeModal,
    )
    ui, app, store = _make_app(tmp_path)
    try:
        async def do_sub() -> int:
            return 5

        modals = [
            HelpScreen(),
            EmojiPrompt(),
            SubscribeModal(
                cid=5, ref="#lounge",
                do_subscribe=do_sub,
                default_count_for=lambda pc: min(10, pc),
                skip_confirm=False,
            ),
            UnsubscribeModal(channel_ref="#lounge (ch:5)"),
            NewDmModal(),
            QuitConfirmModal(),
            BoolSelectModal(name="show_acks", current=True, description="d"),
            EditValueModal(name="delivery_timeout_s", current="60", description="d"),
            SettingsModal(options=ui._options, on_change=lambda *a: None),
        ]
        for m in modals:
            m.attach(app)
            assert m.shell is not None
            canvas = m.shell.render((80, 25), focus=True)
            assert canvas.rows() == 25, f"{type(m).__name__} render returned {canvas.rows()} rows"
    finally:
        store.close()


# ---------------------------------------------------------------------
# Online pane incremental diff
# ---------------------------------------------------------------------


def test_online_pane_initial_population(tmp_path: Path) -> None:
    """A type-`o` event seeds the online pane and updates the count label."""
    ui, app, store = _make_app(tmp_path)
    try:
        async def _run() -> None:
            await app._dispatch_event({"t": "o", "o": ["M0AAA", "M0BBB", "M0CCC"]})
        asyncio.run(_run())
        assert set(app._online_items.keys()) == {"M0AAA", "M0BBB", "M0CCC"}
        assert "(3)" in app._online_count_label.text
    finally:
        store.close()


def test_online_pane_uc_adds_one(tmp_path: Path) -> None:
    """A `uc` event adds one entry; existing items keep their identity."""
    ui, app, store = _make_app(tmp_path)
    try:
        async def _run() -> None:
            await app._dispatch_event({"t": "o", "o": ["M0AAA", "M0BBB"]})
            kept = dict(app._online_items)
            await app._dispatch_event({"t": "uc", "call": "M0CCC"})
            return kept
        kept = asyncio.run(_run())
        assert set(app._online_items.keys()) == {"M0AAA", "M0BBB", "M0CCC"}
        # Identity check: retained entries kept the same widget instance.
        assert app._online_items["M0AAA"] is kept["M0AAA"]
        assert app._online_items["M0BBB"] is kept["M0BBB"]
    finally:
        store.close()


def test_online_pane_ud_removes_one(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path)
    try:
        async def _run() -> None:
            await app._dispatch_event({"t": "o", "o": ["M0AAA", "M0BBB", "M0CCC"]})
            kept = dict(app._online_items)
            await app._dispatch_event({"t": "ud", "call": "M0BBB"})
            return kept
        kept = asyncio.run(_run())
        assert set(app._online_items.keys()) == {"M0AAA", "M0CCC"}
        assert app._online_items["M0AAA"] is kept["M0AAA"]
        assert app._online_items["M0CCC"] is kept["M0CCC"]
    finally:
        store.close()


def test_online_pane_he_relabels_only_when_name_changes(tmp_path: Path) -> None:
    """`he` events refresh online labels only where the resolved name
    actually changed."""
    ui, app, store = _make_app(tmp_path)
    try:
        async def _run() -> None:
            await app._dispatch_event({"t": "o", "o": ["M0AAA"]})
            first = app._online_label_cache["M0AAA"]
            store.upsert_ham("M0AAA", "Alice", 1)
            # Direct call — the `he` debounce alarm uses urwid.MainLoop
            # which isn't running in this smoke test.
            app._do_he_refresh()
            return first
        first = asyncio.run(_run())
        assert app._online_label_cache["M0AAA"] == "Alice, M0AAA"
        assert app._online_label_cache["M0AAA"] != first
    finally:
        store.close()


# ---------------------------------------------------------------------
# Inbound message dispatch
# ---------------------------------------------------------------------


def test_inbound_dm_to_active_target_mounts_row(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path)
    try:
        async def _run() -> None:
            ui._target = ("dm", "M0FOO")
            await app._switch_centre_to(("dm", "M0FOO"))
            await app._dispatch_event(
                {"t": "m", "_id": "1234567890-M0FOO", "fc": "M0FOO",
                 "tc": "M0ABC", "ts": 1234567890, "m": "Hello there"}
            )
        asyncio.run(_run())
        walker = app._walkers[("dm", "M0FOO")]
        rows = [w for w in walker if isinstance(w, _MessageRow)]
        assert len(rows) == 1
        assert rows[0].body == "Hello there"
        assert rows[0].from_call == "M0FOO"
    finally:
        store.close()


def test_unread_seeded_from_store_at_session_start(tmp_path: Path) -> None:
    """Persistent unread cursors should drive per-target badges on
    first start. Seed + populate means a channel with stored unread
    posts shows a (N) suffix in the channels list immediately, before
    any live event arrives."""
    db = tmp_path / "state.sqlite3"
    s = SqliteStore(db)
    s.set_subscription(5, True)
    s.upsert_post(5, {"ts": 100, "fc": "G7BAR", "p": "a"})
    s.upsert_post(5, {"ts": 200, "fc": "G7BAR", "p": "b"})
    s.upsert_post(5, {"ts": 300, "fc": "G7BAR", "p": "c"})
    s.close()

    ui, app, store = _make_app(tmp_path)
    try:
        target = ("ch", "5")
        assert app._unread.get(target) == 3
        # Per-target row label includes the count.
        assert any(
            "(3)" in str(getattr(w._w, "text", "")) or
            "(3)" in str(w._w)
            for w in app._channels_walker
        )
    finally:
        store.close()


def test_unread_seeded_in_session_driven_path(tmp_path: Path) -> None:
    """Session-driven mode: client is None at _build_widgets time, then
    set in _on_client_ready (which seeds), then _post_connect_setup
    populates the lists. Ensures the seed reaches _unread before the
    channel row is rendered."""
    from whatspyc.config import ConnectProfile
    from whatspyc.ui.urwid_ui import UrwidUI as _UrwidUI
    from whatspyc.ui.urwid_ui import _UrwidApp as _UrwidApp_

    db = tmp_path / "state.sqlite3"
    s = SqliteStore(db)
    s.set_subscription(5, True)
    s.upsert_post(5, {"ts": 100, "fc": "G7BAR", "p": "a"})
    s.upsert_post(5, {"ts": 200, "fc": "G7BAR", "p": "b"})
    s.upsert_post(5, {"ts": 300, "fc": "G7BAR", "p": "c"})
    s.close()

    s2 = SqliteStore(db)
    online: list[str] = []

    def make_client():
        return SimpleNamespace(
            _store=s2,
            _name="Tester",
            ham_name=lambda call: None,
            online_users=lambda: list(online),
            paused_channels=lambda: {},
            is_auto_reconnect=False,
            set_delivery_timeout_s=lambda v: None,
            auto_backfill_post_count=10,
            _online_list=online,
            close=lambda: asyncio.sleep(0),
        )

    async def opener(profile, *, progress, on_event, on_client_ready=None):
        c = make_client()
        if on_client_ready:
            on_client_ready(c)
        return c, None

    ui = _UrwidUI(
        None,
        my_call="M0ABC",
        channels=[ChannelInfo(cid=5, name="lounge")],
        history_backfill=3,
        options=SessionOptions(),
        offline=False,
        connection_opener=opener,
        available_profiles=[ConnectProfile(name="X")],
    )
    app = _UrwidApp_(ui)
    ui._app = app
    app._build_widgets()  # client None → populate skipped
    assert app._unread == {}

    # Simulate the session-driven connect: opener calls
    # _on_client_ready (which sets client + seeds), then we run
    # _post_connect_setup as the bootstrap would after a successful
    # connect.
    asyncio.run(opener(None, progress=lambda *a, **k: None,
                       on_event=lambda obj: asyncio.sleep(0),
                       on_client_ready=lambda c: (
                           setattr(ui, "_client", c),
                           app._seed_unread_from_store(),
                       )))
    app._post_connect_setup()

    target = ("ch", "5")
    assert app._unread.get(target) == 3
    # Per-target row label includes the count.
    label_str = "".join(
        seg if isinstance(seg, str) else seg[1]
        for seg in app._target_label(target)
    )
    assert "(3)" in label_str
    s2.close()


def test_unread_no_double_count_when_cpb_arrives_after_seed(
    tmp_path: Path,
) -> None:
    """Regression: the client persists rows *before* dispatching, so
    if seeding ran *after* an inbound batch, the seed query would scan
    the just-persisted rows and add to ``_unread`` what the live
    increment already added. Seeding once, ahead of any wire events,
    keeps the count honest."""
    ui, app, store = _make_app(tmp_path)
    try:
        # _make_app's offline-style construction has already seeded
        # (via _build_widgets → _seed_unread_from_store on the empty
        # store). Now simulate cpb landing for a fresh subscribed
        # channel: 3 posts, persisted, then dispatched.
        store.set_subscription(11, True)
        for ts in (1000, 2000, 3000):
            store.upsert_post(
                11, {"ts": ts, "fc": "G7BAR", "p": f"p{ts}"},
                realtime=False,
            )

        async def _run() -> None:
            await app._dispatch_event(
                {"t": "cpb", "cid": 11, "p": [
                    {"ts": 1000, "fc": "G7BAR", "p": "p1000"},
                    {"ts": 2000, "fc": "G7BAR", "p": "p2000"},
                    {"ts": 3000, "fc": "G7BAR", "p": "p3000"},
                ]}
            )
        asyncio.run(_run())
        target = ("ch", "11")
        # Seed already done with 0 (no rows existed at seed time);
        # live increment adds exactly 3 — not 6.
        assert app._unread.get(target) == 3
    finally:
        store.close()


def test_inbound_dm_to_inactive_target_bumps_unread(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path)
    try:
        async def _run() -> None:
            ui._target = ("ch", "5")  # active is a channel, not the DM peer
            await app._switch_centre_to(("ch", "5"))
            await app._dispatch_event(
                {"t": "m", "_id": "x", "fc": "M0FOO", "tc": "M0ABC",
                 "ts": 1, "m": "ping"}
            )
        asyncio.run(_run())
        target = ("dm", "M0FOO")
        assert app._unread.get(target) == 1
        # No view created yet (lazy mount on activate).
        assert target not in app._walkers
    finally:
        store.close()


def test_inbound_post_to_active_channel_mounts_row(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path)
    try:
        async def _run() -> None:
            ui._target = ("ch", "5")
            await app._switch_centre_to(("ch", "5"))
            await app._dispatch_event(
                {"t": "cp", "cid": 5, "ts": 9999, "fc": "M0FOO",
                 "p": "post body"}
            )
        asyncio.run(_run())
        walker = app._walkers[("ch", "5")]
        rows = [w for w in walker if isinstance(w, _MessageRow)]
        assert len(rows) == 1
        assert rows[0].body == "post body"
    finally:
        store.close()


# ---------------------------------------------------------------------
# Chronological insertion of late arrivals
# ---------------------------------------------------------------------


def test_late_arriving_dm_inserts_in_timestamp_order(tmp_path: Path) -> None:
    """A DM whose ts is older than rows already mounted must slot
    into the correct chronological position, not append at the end."""
    ui, app, store = _make_app(tmp_path)
    try:
        async def _run() -> None:
            ui._target = ("dm", "M0FOO")
            await app._switch_centre_to(("dm", "M0FOO"))
            # Mount three DMs in order: ts=100, 200, 300.
            for ts in (100, 200, 300):
                await app._dispatch_event(
                    {"t": "m", "_id": f"{ts}-M0FOO", "fc": "M0FOO",
                     "tc": "M0ABC", "ts": ts, "m": f"msg{ts}"}
                )
            # Now a late arrival with ts=150 — must land between
            # ts=100 and ts=200.
            await app._dispatch_event(
                {"t": "m", "_id": "150-M0FOO", "fc": "M0FOO",
                 "tc": "M0ABC", "ts": 150, "m": "late"}
            )
            # And one older than everything (ts=50) — must land at the top.
            await app._dispatch_event(
                {"t": "m", "_id": "50-M0FOO", "fc": "M0FOO",
                 "tc": "M0ABC", "ts": 50, "m": "very late"}
            )
        asyncio.run(_run())
        walker = app._walkers[("dm", "M0FOO")]
        rows = [w for w in walker if isinstance(w, _MessageRow)]
        assert [r.ts for r in rows] == [50, 100, 150, 200, 300]
        assert [r.body for r in rows] == [
            "very late", "msg100", "late", "msg200", "msg300",
        ]
    finally:
        store.close()


def test_late_arriving_post_inserts_in_timestamp_order(tmp_path: Path) -> None:
    """Symmetric to the DM case but for channel posts."""
    ui, app, store = _make_app(tmp_path)
    try:
        async def _run() -> None:
            ui._target = ("ch", "5")
            await app._switch_centre_to(("ch", "5"))
            for ts in (1000, 2000, 3000):
                await app._dispatch_event(
                    {"t": "cp", "cid": 5, "ts": ts, "fc": "M0FOO",
                     "p": f"post{ts}"}
                )
            # Late arrival between 1000 and 2000.
            await app._dispatch_event(
                {"t": "cp", "cid": 5, "ts": 1500, "fc": "M0FOO",
                 "p": "late"}
            )
        asyncio.run(_run())
        walker = app._walkers[("ch", "5")]
        rows = [w for w in walker if isinstance(w, _MessageRow)]
        assert [r.ts for r in rows] == [1000, 1500, 2000, 3000]
    finally:
        store.close()


def test_in_order_dm_still_appends(tmp_path: Path) -> None:
    """Common case: when the new DM's ts >= the current last row's
    ts, it appends. (Sanity check the fast path.)"""
    ui, app, store = _make_app(tmp_path)
    try:
        async def _run() -> None:
            ui._target = ("dm", "M0FOO")
            await app._switch_centre_to(("dm", "M0FOO"))
            for ts in (100, 200, 300):
                await app._dispatch_event(
                    {"t": "m", "_id": f"{ts}-M0FOO", "fc": "M0FOO",
                     "tc": "M0ABC", "ts": ts, "m": f"msg{ts}"}
                )
        asyncio.run(_run())
        walker = app._walkers[("dm", "M0FOO")]
        rows = [w for w in walker if isinstance(w, _MessageRow)]
        assert [r.ts for r in rows] == [100, 200, 300]
    finally:
        store.close()


# ---------------------------------------------------------------------
# Edit + ack flows
# ---------------------------------------------------------------------


def test_dm_edit_updates_existing_row(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path)
    try:
        # In production, WpsClient persists `m` to the store before
        # invoking `on_event`. The test pipes events directly into
        # `_dispatch_event`, so we have to do the upsert ourselves to
        # give the edit handler a row to look up.
        store.upsert_message(
            {"_id": "abc-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
             "ts": 1, "m": "original body"},
            realtime=True,
        )

        async def _run() -> None:
            ui._target = ("dm", "M0FOO")
            await app._switch_centre_to(("dm", "M0FOO"))
            await app._dispatch_event(
                {"t": "m", "_id": "abc-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
                 "ts": 1, "m": "original body"}
            )
            store.apply_message_edit("abc-M0FOO", "edited body", 999)
            await app._dispatch_event(
                {"t": "med", "_id": "abc-M0FOO", "m": "edited body", "edts": 999}
            )
        asyncio.run(_run())
        row = app._rows[("dm", "M0FOO", "abc-M0FOO")]
        assert row.body == "edited body"
        assert row.edit_ts == 999
    finally:
        store.close()


def test_dm_ack_marks_delivered(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path)
    try:
        # See `test_dm_edit_updates_existing_row` — the ack handler
        # looks up the store row to recover lid + ts for the status
        # line, so the test has to upsert before dispatch.
        store.upsert_message(
            {"_id": "abc-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
             "ts": 1, "m": "hi"},
            realtime=True,
        )

        async def _run() -> None:
            ui._target = ("dm", "M0FOO")
            await app._switch_centre_to(("dm", "M0FOO"))
            await app._dispatch_event(
                {"t": "m", "_id": "abc-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
                 "ts": 1, "m": "hi"}
            )
            store.mark_message_delivered("abc-M0FOO", 12345)
            await app._dispatch_event(
                {"t": "mr", "_id": "abc-M0FOO", "dts": 12345}
            )
        asyncio.run(_run())
        row = app._rows[("dm", "M0FOO", "abc-M0FOO")]
        assert row.delivered_ts == 12345
    finally:
        store.close()


# ---------------------------------------------------------------------
# Disconnect → terminal exit signal
# ---------------------------------------------------------------------


def test_disconnect_without_auto_reconnect_signals_terminal(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path, offline=False)
    try:
        # Need an exit_future so _signal_terminal_link_loss can resolve.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app._exit_future = loop.create_future()
        try:
            loop.run_until_complete(
                app._dispatch_event(
                    {"t": "_disconnect", "auto_reconnect": False, "reason": "test"}
                )
            )
            assert ui.exit_reason == "terminal"
            assert app._exit_future.done()
        finally:
            loop.close()
            asyncio.set_event_loop(asyncio.new_event_loop())
    finally:
        store.close()


# ---------------------------------------------------------------------
# Modal construction
# ---------------------------------------------------------------------


def test_help_screen_builds(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path)
    try:
        modal = HelpScreen()
        body = modal.attach(app)
        assert body is not None
        assert modal.shell is not None
    finally:
        store.close()


def test_help_screen_focused_on_unknown_command(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path)
    try:
        modal = HelpScreen(focus_command="bogus")
        body = modal.attach(app)
        assert body is not None
        # Unknown command → modal still builds, with a "unknown command:"
        # line baked into the listbox.
    finally:
        store.close()


def test_emoji_prompt_initial_view_is_quick_picks(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path)
    try:
        modal = EmojiPrompt()
        body = modal.attach(app)
        assert body is not None
        # Quick-picks tab is active by default and entries are populated.
        assert modal._active_group == "__quick__"
        assert modal._entries  # non-empty
        assert modal._entries[0].char in "👍🙏❤️😂😢😡🎉🔥👀✅❌😀😎🤔🙌😉👋"
    finally:
        store.close()


def test_emoji_prompt_enter_picks_focused_button_not_first(tmp_path: Path) -> None:
    """Enter on a focused emoji button must dismiss with that button's
    char — not always the first entry on the page. Regression: the
    modal-level keypress used to intercept Enter unconditionally and
    dismiss with ``entries[0]``, so navigating with arrows had no
    effect on what got picked.
    """
    import asyncio as _asyncio

    from whatspyc.ui.urwid_ui import _EmojiButton

    ui, app, store = _make_app(tmp_path)
    try:
        loop = _asyncio.new_event_loop()
        try:
            modal = EmojiPrompt()
            modal.future = loop.create_future()
            modal._app = app
            modal.build()
            assert modal._pile is not None
            assert modal._grid_row_index >= 0
            assert len(modal._entries) >= 2

            # Move focus into the grid row, then to the second button
            # in the first row of the grid.
            modal._pile.focus_position = modal._grid_row_index
            grid_row = modal._pile.contents[modal._grid_row_index][0]
            # BoxAdapter -> ListBox -> walker[0] = grid_pile (Pile of Columns)
            listbox = grid_row.original_widget
            grid_pile = listbox.body[0]
            first_columns = grid_pile.contents[0][0]
            # Find the second _EmojiButton in the row.
            buttons = [w for (w, _opts) in first_columns.contents if isinstance(w, _EmojiButton)]
            assert len(buttons) >= 2
            target_char = buttons[1].char
            assert target_char != modal._entries[0].char
            # Set focus on the second button via the column index.
            for idx, (w, _opts) in enumerate(first_columns.contents):
                if w is buttons[1]:
                    first_columns.focus_position = idx
                    break

            # Modal-level keypress on Enter should fall through (return
            # the key) when the grid row is focused, so the body can
            # dispatch to _EmojiButton.keypress.
            assert modal.keypress((40, 20), "enter") == "enter"

            # _EmojiButton.keypress dismisses with its own char.
            buttons[1].keypress((4,), "enter")
            assert modal.future.done()
            assert modal.future.result() == target_char
        finally:
            loop.close()
    finally:
        store.close()


def test_emoji_button_mouse_click_dismisses_with_own_char(tmp_path: Path) -> None:
    """A left-click on an _EmojiButton must activate that button (not
    a no-op via the wrapped Text). Regression: _EmojiButton had no
    ``mouse_event`` override, so clicks fell through and did nothing.
    """
    import asyncio as _asyncio

    from whatspyc.ui.urwid_ui import _EmojiButton

    ui, app, store = _make_app(tmp_path)
    try:
        loop = _asyncio.new_event_loop()
        try:
            modal = EmojiPrompt()
            modal.future = loop.create_future()
            modal._app = app
            modal.build()
            assert len(modal._entries) >= 3

            grid_row = modal._pile.contents[modal._grid_row_index][0]
            listbox = grid_row.original_widget
            grid_pile = listbox.body[0]
            first_columns = grid_pile.contents[0][0]
            buttons = [w for (w, _opts) in first_columns.contents if isinstance(w, _EmojiButton)]
            target = buttons[2]

            handled = target.mouse_event((4,), "mouse press", 1, 0, 0, True)
            assert handled is True
            assert modal.future.done()
            assert modal.future.result() == target.char
        finally:
            loop.close()
    finally:
        store.close()


def test_subscribe_modal_confirm_stage(tmp_path: Path) -> None:
    """SubscribeModal opens at the confirm stage when ``skip_confirm`` is
    False and shows the channel reference text."""
    ui, app, store = _make_app(tmp_path)
    try:
        async def do_subscribe() -> int:
            return 5

        modal = SubscribeModal(
            cid=5,
            ref="#lounge (ch:5)",
            do_subscribe=do_subscribe,
            default_count_for=lambda pc: min(10, pc),
            skip_confirm=False,
        )
        body = modal.attach(app)
        assert body is not None
        assert modal._stage == "confirm"
    finally:
        store.close()


def test_subscribe_modal_count_stage_accepts_typing(tmp_path: Path) -> None:
    """Once the subscribe ack lands and the modal switches to the
    count-prompt stage, digits typed at the shell must reach the Edit.

    Regression: LineBox builds a Pile/Columns wrapper that caches
    ``_selectable`` at construct time. Our body Pile starts empty
    (non-selectable), so the cache latches False — and ``Pile.keypress``
    early-returns the key unchanged when not selectable, swallowing
    every digit before the Edit could see it.
    """
    ui, app, store = _make_app(tmp_path)
    try:
        async def do_subscribe() -> int:
            return 7

        async def drive() -> None:
            modal = SubscribeModal(
                cid=5,
                ref="#lounge",
                do_subscribe=do_subscribe,
                default_count_for=lambda pc: min(10, pc),
                skip_confirm=True,
            )
            modal.attach(app)
            # Let _kick_off_subscribe complete and re-render.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert modal._stage == "count"
            size = (60, 20)
            modal.shell.render(size, focus=True)
            assert modal.shell.keypress(size, "5") is None
            assert modal.shell.keypress(size, "0") is None
            assert modal._count_input is not None
            assert modal._count_input.edit_text == "50"
            assert modal.shell.keypress(size, "enter") is None
            assert modal.future.done()
            # Result is the typed value, capped at pc=7.
            assert modal.future.result() == 7

        asyncio.run(drive())
    finally:
        store.close()


# ---------------------------------------------------------------------
# Offline gating
# ---------------------------------------------------------------------


def test_offline_refuse_helper_blocks_send(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path, offline=True)
    try:
        # _refuse_offline returns True (refused) when offline; writes a
        # banner to the status pane.
        before = len(app._status_walker)
        assert app._refuse_offline("send") is True
        assert len(app._status_walker) > before
    finally:
        store.close()


def test_online_refuse_helper_allows_send(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path, offline=False)
    try:
        assert app._refuse_offline("send") is False
    finally:
        store.close()


# ---------------------------------------------------------------------
# Channel resolution
# ---------------------------------------------------------------------


def test_resolve_channel_by_cid_in_directory(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path)
    try:
        assert app._resolve_channel("5") == 5
        assert app._resolve_channel("7") == 7
    finally:
        store.close()


def test_resolve_channel_by_name(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path)
    try:
        assert app._resolve_channel("lounge") == 5
        assert app._resolve_channel("#space") == 7
        # Case-insensitive.
        assert app._resolve_channel("LOUNGE") == 5
    finally:
        store.close()


def test_resolve_channel_unknown_returns_none(tmp_path: Path) -> None:
    ui, app, store = _make_app(tmp_path)
    try:
        assert app._resolve_channel("nope") is None
        # Unknown cid is rejected unless ``allow_unknown_cid``.
        assert app._resolve_channel("999") is None
        assert app._resolve_channel("999", allow_unknown_cid=True) == 999
    finally:
        store.close()


# ---------------------------------------------------------------------
# Keypress dispatch — guard against the regression where the input
# Edit silently consumed every Ctrl-binding and Tab did nothing.
# ---------------------------------------------------------------------


def test_input_edit_lets_global_ctrl_keys_bubble(tmp_path: Path) -> None:
    """``urwid.Edit``'s default command map maps ``ctrl e`` /
    ``ctrl a`` / ``ctrl d`` etc. to line-editing commands and
    consumes them. The App needs those keys to reach
    ``unhandled_input``, so the input is a ``_InputEdit`` subclass
    that returns the global Ctrl-bindings unchanged.

    The bindings deliberately avoid keys the terminal/tty layer
    intercepts (``ctrl s`` / ``ctrl q`` / ``ctrl h``) — those should
    not appear in this list."""
    ui, app, store = _make_app(tmp_path)
    try:
        inp = app._input
        assert isinstance(inp, _InputEdit)
        # All the App's global Ctrl-bindings should pass through verbatim.
        for key in ("ctrl c", "ctrl x", "ctrl l", "ctrl d", "ctrl e",
                    "ctrl o", "ctrl u",
                    "tab", "shift tab", "f1", "esc"):
            result = inp.keypress((40,), key)
            assert result == key, f"{key!r} was consumed by Edit"
        # The dropped bindings are NOT in the global set — make sure
        # we don't accidentally re-add them.
        for dropped in ("ctrl s", "ctrl q", "ctrl h"):
            assert dropped not in _InputEdit._GLOBAL_KEYS, (
                f"{dropped!r} re-added; it collides with a tty/terminal "
                f"binding (XON/XOFF flow control or backspace)"
            )
        # Ordinary letters still get inserted.
        inp.set_edit_text("")
        result = inp.keypress((40,), "h")
        assert result is None
        assert inp.edit_text == "h"
    finally:
        store.close()


def test_initial_focus_is_input(tmp_path: Path) -> None:
    """Frame focus starts on ``footer`` so the input ``Edit`` is active
    immediately; otherwise users land on the tab strip and can't type
    anything. ``_focus_input`` is the canonical accessor."""
    ui, app, store = _make_app(tmp_path)
    try:
        assert app._frame.focus_position == "footer"
        assert app._current_focus_step() == "input"
    finally:
        store.close()


def test_focus_step_cycles_all_stops(tmp_path: Path) -> None:
    """Tab cycles input → tabs → targets → online → messages → input."""
    ui, app, store = _make_app(tmp_path)
    try:
        seen = []
        for _ in range(len(app._FOCUS_ORDER) + 1):
            seen.append(app._current_focus_step())
            app._focus_step(1)
        # First and last (after wrap) are both "input".
        assert seen[0] == "input"
        assert seen[-1] == "input"
        # Each stop appears once before wrapping.
        assert set(seen[:-1]) == set(app._FOCUS_ORDER)
    finally:
        store.close()


def test_modal_shell_is_selectable(tmp_path: Path) -> None:
    """``urwid.Overlay`` only forwards keypresses to its top widget if
    the top widget reports ``selectable() == True``. The modal body
    cascade (LineBox → Filler → Pile → Text) usually isn't
    selectable, so without an explicit ``selectable()`` override on
    ``_ModalShell`` urwid silently drops every Y/N press on a confirm
    modal — the symptom the user hit on Ctrl-Q showing the modal but
    Y/N doing nothing."""
    ui, app, store = _make_app(tmp_path)
    try:
        modal = QuitConfirmModal()
        modal.attach(app)
        assert modal.shell is not None
        assert modal.shell.selectable() is True
    finally:
        store.close()


def test_modal_shell_dispatches_to_modal_keypress(tmp_path: Path) -> None:
    """``_ModalShell.keypress`` invokes the modal's bespoke handler so
    Y/N/Esc bindings actually fire. Without this hook the modal's
    ``keypress`` method was never reached and modals couldn't be
    dismissed by the keys their docstrings advertise."""

    async def _run() -> None:
        ui, app, store = _make_app(tmp_path)
        try:
            modal = QuitConfirmModal()
            modal.attach(app)
            assert modal.shell is not None
            # Pressing Y on a QuitConfirmModal should consume the key
            # and resolve the future to True.
            result = modal.shell.keypress((40, 6), "y")
            assert result is None
            assert modal.future is not None
            assert modal.future.done()
            assert modal.future.result() is True
        finally:
            store.close()

    asyncio.run(_run())


def test_switch_to_channel_mounts_history_from_store(tmp_path: Path) -> None:
    """When the user runs ``/ch 5`` (or clicks a channel row), the
    centre pane must show whatever posts are already in the local
    store. The Textual UI does this via ``_mount_initial_history``;
    the urwid UI mirrors it. A regression where the store accessor was
    called with the wrong keyword (``recent_posts(cid=...)`` instead of
    the actual ``channel_id`` first positional) silently returned no
    rows because the broad ``except Exception`` swallowed the
    ``TypeError``."""
    ui, app, store = _make_app(tmp_path)
    try:
        # Seed three posts in channel 5.
        for ts in (1_000_000_000_001, 1_000_000_000_002, 1_000_000_000_003):
            store.upsert_post(
                5,
                {"ts": ts, "fc": "G7XYZ", "p": f"hello at {ts}"},
                realtime=True,
            )

        async def _run() -> None:
            ui._target = ("ch", "5")
            await app._switch_centre_to(("ch", "5"))

        asyncio.run(_run())
        walker = app._walkers[("ch", "5")]
        rows = [w for w in walker if isinstance(w, _MessageRow)]
        assert len(rows) == 3, f"expected 3 mounted post rows, got {len(rows)}"
        assert rows[0].body == "hello at 1000000000001"  # oldest first
    finally:
        store.close()


def test_switch_to_dm_mounts_history_from_store(tmp_path: Path) -> None:
    """Same contract as ``test_switch_to_channel_mounts_history_from_store``
    for DM peers."""
    ui, app, store = _make_app(tmp_path)
    try:
        for ts in (1_700_000_001, 1_700_000_002):
            store.upsert_message(
                {
                    "_id": f"{ts}-G7XYZ",
                    "ts": ts,
                    "fc": "G7XYZ",
                    "tc": "M0ABC",
                    "m": f"hi at {ts}",
                },
                realtime=True,
            )

        async def _run() -> None:
            ui._target = ("dm", "G7XYZ")
            await app._switch_centre_to(("dm", "G7XYZ"))

        asyncio.run(_run())
        walker = app._walkers[("dm", "G7XYZ")]
        rows = [w for w in walker if isinstance(w, _MessageRow)]
        assert len(rows) == 2, f"expected 2 mounted DM rows, got {len(rows)}"
    finally:
        store.close()


def test_help_and_settings_modals_open_synchronously(tmp_path: Path) -> None:
    """``_handle_help`` and ``_open_settings_modal`` are called from
    sync keypress handlers (Ctrl-H, Ctrl-O). They must NOT wrap
    ``_show_modal`` in ``asyncio.create_task`` — ``_show_modal`` is a
    sync method that returns a Future, and ``create_task(future)``
    raises ``TypeError: a coroutine was expected, got <Future
    pending>``. The helpers must work in both async and sync
    invocation contexts since urwid's keypress dispatch is sync."""
    ui, app, store = _make_app(tmp_path)
    try:
        async def _run() -> None:
            # No exceptions — that's the contract. The original bug
            # raised ``TypeError`` here.
            app._handle_help([])
            app._open_settings_modal()

        asyncio.run(_run())
    finally:
        store.close()


def test_custom_widgets_report_selectable(tmp_path: Path) -> None:
    """``_FocusableText``, ``_Button``, ``_EmojiButton`` and
    ``_MessageRow`` all subclass ``urwid.WidgetWrap``, which by default
    forwards ``selectable()`` to the wrapped widget. Since each wraps a
    non-selectable widget (``Text`` / ``AttrMap`` over ``Text``), the
    default ``selectable()`` returns ``False`` and the ``ListBox``
    can't put cursor focus on the row — keyboard navigation past the
    first item silently fails. Each widget must override
    ``selectable()`` explicitly. Mouse clicks happened to work because
    ``ListBox.mouse_event`` sets focus directly without consulting
    ``selectable()``."""
    from whatspyc.ui.urwid_ui import _Button, _EmojiButton, _FocusableText

    assert _FocusableText("hi", on_activate=lambda: None).selectable() is True
    assert _Button("Hi", on_press=lambda: None).selectable() is True
    assert _EmojiButton(
        "👍", on_activate=lambda c: None, on_focus=lambda c: None
    ).selectable() is True
    row = _MessageRow(
        kind="dm", target_key="X", natural_key="1",
        from_call="X", body="hi", ts=1,
    )
    assert row.selectable() is True


def test_keyboard_scrolls_channels_listbox(tmp_path: Path) -> None:
    """Pressing Down on the channels ``ListBox`` advances cursor focus
    through the rows. Without ``_FocusableText`` overriding
    ``selectable()``, ``ListBox`` would refuse to put focus on any row
    and Down would do nothing — the symptom the user reported as
    'cannot scroll past the first channel'."""
    ui, app, store = _make_app(
        tmp_path,
        channels=[
            ChannelInfo(cid=5, name="lounge"),
            ChannelInfo(cid=7, name="space"),
            ChannelInfo(cid=9, name="news"),
        ],
    )
    try:
        lb = app._channels_listbox
        # Render once so the ListBox knows its viewport size.
        app._frame_holder.render((100, 30), focus=True)
        assert lb.focus_position == 0
        assert lb.keypress((30, 20), "down") is None
        assert lb.focus_position == 1
        assert lb.keypress((30, 20), "down") is None
        assert lb.focus_position == 2
    finally:
        store.close()


def test_dms_seeded_from_store_at_startup(tmp_path: Path) -> None:
    """DM peers stored from previous sessions must show up in the DM
    list as soon as the App constructs its widgets — otherwise users
    see an empty DM list and assume the connection is broken. The
    accessor is ``store.list_dm_peers(my_call)``; an earlier version
    of ``_populate_initial_target_lists`` called the non-existent
    ``list_message_peers()``, which raised ``AttributeError`` that a
    broad ``except`` swallowed."""

    # Seed the store with two DM threads BEFORE constructing the app.
    store = SqliteStore(tmp_path / "state.sqlite3")
    store.upsert_message(
        {"_id": "1-G7XYZ", "ts": 1, "fc": "G7XYZ", "tc": "M0ABC", "m": "hi"},
        realtime=True,
    )
    store.upsert_message(
        {"_id": "2-K1ABC", "ts": 2, "fc": "M0ABC", "tc": "K1ABC", "m": "hello back"},
        realtime=False,
    )

    online: list[str] = []
    client = SimpleNamespace(
        _store=store, _name="Tester",
        ham_name=lambda c: None,
        online_users=lambda: list(online),
        paused_channels=lambda: {},
        is_auto_reconnect=False,
        set_delivery_timeout_s=lambda v: None,
        auto_backfill_post_count=10,
        _online_list=online,
        close=lambda: asyncio.sleep(0),
    )
    ui = UrwidUI(
        client, my_call="M0ABC",
        channels=[], history_backfill=3,
        options=SessionOptions(), offline=True,
    )
    app = _UrwidApp(ui)
    ui._app = app
    try:
        app._build_widgets()
        # The DMs walker has the pinned "+ Add DM call…" plus two
        # peers from the store.
        labels = [
            (w._text.text if hasattr(w, "_text") else "") for w in app._dms_walker
        ]
        # Normalize — first row is the pinned add-call entry, then peers.
        peer_keys = [t for t in app._target_items.keys() if t[0] == "dm"]
        assert ("dm", "G7XYZ") in peer_keys
        assert ("dm", "K1ABC") in peer_keys
    finally:
        store.close()


def test_is_subscribed_filters_by_subscribed_column(tmp_path: Path) -> None:
    """``store.list_channels()`` returns every channel row, including
    those we've explicitly unsubscribed from (``subscribed=0``). The
    UI must filter by the ``subscribed`` column or it'll treat
    everything in the table as subscribed — and then the click-to-
    subscribe modal flow never triggers because every known channel
    is reported as 'already subscribed'."""
    ui, app, store = _make_app(tmp_path)
    try:
        store.set_subscription(5, True)
        store.set_subscription(7, False)  # unsubscribed but row exists
        # Force lazy reload so the new rows are picked up.
        app._invalidate_subscribed_cids()
        assert app._is_subscribed(5) is True
        assert app._is_subscribed(7) is False
    finally:
        store.close()


def test_click_unsubscribed_channel_opens_subscribe_modal(tmp_path: Path) -> None:
    """When the user picks an unsubscribed channel from the target
    list (Enter or click), the App must open a ``SubscribeModal``
    rather than silently switching the centre pane to a read-only
    view. This was broken before the ``_is_subscribed`` fix above
    because every directory channel was misreported as subscribed."""
    ui, app, store = _make_app(
        tmp_path,
        offline=False,
        channels=[
            ChannelInfo(cid=5, name="lounge"),
            ChannelInfo(cid=7, name="space"),
        ],
    )
    try:
        # Mark ch 5 subscribed in the store; ch 7 has no row at all.
        store.set_subscription(5, True)
        app._invalidate_subscribed_cids()

        # Stub the "live" loop bits so ``_show_modal`` actually pushes
        # to the modal_stack instead of early-returning.
        app._loop = SimpleNamespace(
            set_alarm_in=lambda *a, **k: None,
            remove_alarm=lambda *a: None,
            draw_screen=lambda: None,
        )
        # Stub subscribe_and_wait so the modal can advance past stage 1.
        async def fake_subwait(cid):
            return 3
        ui._client.subscribe_and_wait = fake_subwait  # type: ignore[attr-defined]

        async def _run() -> None:
            before = len(app._modal_stack)
            app._on_target_activate(("ch", "7"))
            # The actual modal-show happens inside an asyncio task —
            # let it run.
            await asyncio.sleep(0)
            assert len(app._modal_stack) == before + 1, (
                "expected SubscribeModal on stack after clicking unsubscribed channel"
            )
            modal = app._modal_stack[-1][0]
            from whatspyc.ui.urwid_ui import SubscribeModal
            assert isinstance(modal, SubscribeModal)
            # Tear down so other tests aren't affected.
            modal.dismiss(None)

        asyncio.run(_run())
    finally:
        store.close()


def test_outbound_post_is_mounted_optimistically_and_dimmed(tmp_path: Path) -> None:
    """The WPS server only acks the sender's outbound traffic with
    ``mr`` / ``cpr`` — it never echoes the ``m`` / ``cp`` frame back
    to the sender. So the UI has to mount the row optimistically on
    the way out, otherwise the centre pane never updates when the
    user sends something. The row stays dimmed (``delivered_ts ==
    None``) until the ack handler fires and clears the dim."""

    ui, app, store = _make_app(tmp_path, offline=False)
    try:
        # Replace the client with one whose ``post`` returns a ts and
        # tracks the call. The bare fake from ``_make_app`` doesn't
        # have ``post`` / ``send_message`` since smoke tests usually
        # run offline.
        sent: list[tuple] = []

        async def fake_post(cid, text):
            ts = 1_700_000_000_000
            sent.append((cid, text, ts))
            return ts

        ui._client.post = fake_post  # type: ignore[attr-defined]
        # Subscribe the channel so the post path doesn't refuse.
        store.set_subscription(5, True)

        async def _run() -> None:
            ui._target = ("ch", "5")
            await app._switch_centre_to(("ch", "5"))
            # Invalidate the lazy subscribed-cids cache that loaded
            # before we upserted.
            app._invalidate_subscribed_cids()
            await app._send_to_target("hello channel")

        asyncio.run(_run())

        # The post handler ran.
        assert sent and sent[0][1] == "hello channel"
        # And a row landed in the centre pane.
        walker = app._walkers[("ch", "5")]
        rows = [w for w in walker if isinstance(w, _MessageRow)]
        assert len(rows) == 1
        assert rows[0].body == "hello channel"
        assert rows[0].from_call == "M0ABC"  # our call
        # Row is in the pending-outbound state (dimmed).
        assert rows[0].delivered_ts is None

        # In production WpsClient.post persists the outbound post to
        # the store before returning; the fake `fake_post` here
        # doesn't, so we mirror that step by hand. The ack handler
        # looks up the row via `lookup_post_by_from_ts` to recover
        # the lid, so the upsert needs to be in place before `cpr`
        # is dispatched.
        store.upsert_post(
            5,
            {"ts": 1_700_000_000_000, "fc": "M0ABC", "p": "hello channel"},
        )

        # When the server's ``cpr`` ack arrives, the row's
        # ``delivered_ts`` should be set and the dim cleared.
        async def _ack() -> None:
            store.mark_post_delivered(
                from_call="M0ABC",
                ts=1_700_000_000_000,
                delivered_ts=1_700_000_000_500,
            )
            await app._dispatch_event(
                {"t": "cpr", "cid": 5, "ts": 1_700_000_000_000,
                 "fc": "M0ABC", "dts": 1_700_000_000_500}
            )
        asyncio.run(_ack())
        assert rows[0].delivered_ts == 1_700_000_000_500
    finally:
        store.close()


def test_enter_on_message_row_opens_action_menu(tmp_path: Path) -> None:
    """Pressing Enter on a focused message row in the centre pane
    must open the Edit/Resend/React modal — that's the only way to
    get to those actions from the keyboard. The fix involves both
    ``_MessageListBox.keypress`` (intercepting Enter) and the centre
    Pile's ``focus_position`` being set to the message-list slot
    (otherwise focus lands on the hidden status pane and Enter goes
    nowhere)."""
    from whatspyc.ui.urwid_ui import ActionMenu

    ui, app, store = _make_app(tmp_path)
    try:
        store.set_subscription(5, True)
        store.upsert_post(5, {"ts": 1_700_000_000_001, "fc": "G7XYZ", "p": "hi"},
                          realtime=True)
        # Stub ``_loop`` so ``_show_modal`` actually pushes onto the stack.
        app._loop = SimpleNamespace(
            set_alarm_in=lambda *a, **k: None,
            remove_alarm=lambda *a: None,
            draw_screen=lambda: None,
        )

        async def _run() -> None:
            ui._target = ("ch", "5")
            await app._switch_centre_to(("ch", "5"))
            listbox = app._views[("ch", "5")]
            # Render so the listbox knows its viewport.
            app._frame_holder.render((100, 30), focus=True)
            listbox.focus_position = 0
            assert isinstance(listbox.body[0], _MessageRow)

            before = len(app._modal_stack)
            result = listbox.keypress((30, 20), "enter")
            assert result is None  # consumed
            await asyncio.sleep(0)
            assert len(app._modal_stack) == before + 1
            modal = app._modal_stack[-1][0]
            assert isinstance(modal, ActionMenu)
            modal.dismiss(None)

        asyncio.run(_run())
    finally:
        store.close()


def test_message_row_left_click_opens_action_menu(tmp_path: Path) -> None:
    """Left-clicking a message row also opens the Edit/Resend/React
    modal — the more discoverable interaction. Without
    ``_MessageRow.mouse_event``, the click would only set focus on
    the row without doing anything."""
    from whatspyc.ui.urwid_ui import _MessageRow

    fired: list[_MessageRow] = []
    row = _MessageRow(
        kind="ch", target_key="5", natural_key="1",
        from_call="G7XYZ", body="hi", ts=1,
    )
    row._mouse_activate = lambda r: fired.append(r)
    handled = row.mouse_event((40,), "mouse press", 1, 0, 0, focus=True)
    assert handled is True
    assert fired == [row]


def test_focus_cycle_excludes_online_users(tmp_path: Path) -> None:
    """The online-users list is informational only (no actions),
    so Tab cycling skips it: input → tabs → targets → messages →
    input. An earlier version included an ``online`` stop, making
    Tab take an extra hop with no payoff."""
    ui, app, store = _make_app(tmp_path)
    try:
        # Online not in the cycle.
        assert "online" not in app._FOCUS_ORDER
        # All four expected stops present.
        assert app._FOCUS_ORDER == ("input", "tabs", "targets", "messages")
    finally:
        store.close()


def test_message_row_does_not_render_edited_marker(tmp_path: Path) -> None:
    """Edited posts/DMs intentionally do NOT render an ``[EDITED]``
    suffix in the urwid backend (the Textual backend does). The
    body simply shows the current text and (in verbose mode) the
    new edit_ts is reflected in the timestamp."""
    from whatspyc.ui.urwid_ui import _render_row_markup

    parts = _render_row_markup(
        kind="ch", from_call="G7XYZ", body="updated body",
        ts=1, edit_ts=999, delivered_ts=None, received_ts=None,
        realtime=1, lid=42, my_call="M0ABC", verbose=False,
        ham_name=lambda c: None, delivery_timeout_s=60,
        reactions=None,
    )
    flat = "".join(p[1] if isinstance(p, tuple) else p for p in parts)
    assert "[EDITED]" not in flat
    assert "updated body" in flat


def test_focusable_text_mouse_click_activates(tmp_path: Path) -> None:
    """Clicking a target row should fire its on_activate (same as Enter).
    Without ``_FocusableText.mouse_event`` users couldn't switch
    targets with the mouse — only the keyboard worked."""
    from whatspyc.ui.urwid_ui import _FocusableText

    fired = []
    item = _FocusableText("hello", on_activate=lambda: fired.append(1))
    # Simulate a left-button press at row=0, col=0.
    handled = item.mouse_event((20,), "mouse press", 1, 0, 0, focus=True)
    assert handled is True
    assert fired == [1]


def test_ctrl_l_is_not_intercepted_by_redraw_command(tmp_path: Path) -> None:
    """``urwid.command_map`` ships with ``"ctrl l": REDRAW_SCREEN``,
    and ``MainLoop.process_input`` short-circuits on REDRAW_SCREEN
    before reaching ``unhandled_input``. We use Ctrl-L (mnemonic
    "log") for the status-pane toggle, so ``run_async`` clears that
    mapping. If a future change re-introduces it, Ctrl-L silently
    stops working and the bug is hard to spot — assert here that
    the App's invariant is preserved."""
    import urwid as _urwid

    ui, app, store = _make_app(tmp_path)
    try:
        # Simulate the bit of run_async that disables the mapping.
        # (Calling the full run_async needs a real terminal.)
        _urwid.command_map["ctrl l"] = None  # type: ignore[index]
        try:
            assert _urwid.command_map["ctrl l"] is None
            # Status pane toggles when Ctrl-L lands in unhandled_input.
            before = app._status_visible
            handled = app._on_unhandled_input("ctrl l")
            assert handled is True
            assert app._status_visible is not before
        finally:
            # Restore so other tests aren't affected.
            _urwid.command_map["ctrl l"] = _urwid.command_map._command_defaults.get(
                "ctrl l"
            )
    finally:
        store.close()


def test_unhandled_input_swallows_global_keys_when_modal_open(tmp_path: Path) -> None:
    """When a modal is layered on top, the App's global Ctrl-bindings
    must NOT fire underneath — otherwise pressing Ctrl-L inside the
    EmojiPrompt would toggle the log pane behind it."""
    ui, app, store = _make_app(tmp_path)
    try:
        # Push a sentinel modal entry. _on_unhandled_input only checks
        # truthiness, not the entry shape.
        app._modal_stack.append(("dummy", "prev", None))
        assert app._on_unhandled_input("ctrl s") is True
        # No status visibility flip — the key was eaten, not dispatched.
        assert app._status_visible is True  # default for offline mode
    finally:
        store.close()
