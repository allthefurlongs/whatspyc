"""Multi-pane Textual TUI for whatspyc.

Layout::

    ┌─Header───────────────────────────────────────────┐
    ├Tabs───┬─Status pane (Ctrl+S, hidden by default)──┤
    │Ch DM  ├─Thread header (active channel / DM)──────┤
    ├───────┼───────────────────────────────────────────┤
    │ch list│ Per-target message ListView              │
    │       │  (arrow-key selectable, auto-loads older │
    │/dm    │   on cursor-at-top, in-place updates on  │
    │list   │   edit/ack)                              │
    ├───────┤                                           │
    │Online │                                           │
    │users  │                                           │
    ├───────┴───────────────────────────────────────────┤
    │ Input                                             │
    ├Footer─────────────────────────────────────────────┤
    └───────────────────────────────────────────────────┘

Public shape unchanged: ``TextualUI(client, ...).run()`` and
``render_event(obj)`` — so ``cli.py`` is untouched.
"""

from __future__ import annotations

import asyncio
import datetime
import re
import time
from typing import Any, Awaitable, Callable

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    ContentSwitcher,
    Footer,
    Header,
    Input,
    ListItem,
    ListView,
    RichLog,
    Static,
)
from textual.widgets._footer import FooterKey

from whatspyc import __version__
from whatspyc import log as log_mod
from whatspyc.config import ChannelInfo
from whatspyc.ui import emoji_for_display
from whatspyc.ui import help as help_data
from whatspyc.ui import ts_to_ms
from whatspyc.ui.emoji_catalog import (
    EmojiEntry,
    build_catalog,
    by_char,
    entries_in,
    groups as catalog_groups,
    search,
)
from whatspyc.ui.options import SessionOptions
from whatspyc.wps.client import WpsClient


TargetKey = tuple[str, str]  # ("ch", "5") or ("dm", "G7XYZ")
RowKey = tuple[str, str, str]  # (kind, target_key, natural_key)


class _TabBar(Horizontal):
    """Lightweight Tabs replacement: a horizontal strip of Buttons with
    an ``-active`` class on the currently-selected one.

    Textual's ``Tabs`` widget animates an underline, watches focus, and
    rebuilds bindings on every focus change — measurable cost on slow
    hardware where the visual flourish isn't useful. This widget keeps
    the visible affordance (a row of tab labels with the active one
    highlighted) while skipping the heavier machinery: no underline,
    no per-focus rebuild, no animations.

    The bar itself is focusable so the App's pane-focus cycle can
    target it as a single stop. ``on_focus`` forwards focus to the
    active Button so ←/→ feel like Tabs' built-in navigation; ←/→
    cycle the active tab and ``press()`` it (which posts the standard
    ``Button.Pressed`` event so existing handlers keep working).
    """

    DEFAULT_CSS = """
    _TabBar {
        height: auto;
        layout: horizontal;
    }
    _TabBar > Button {
        height: 3;
        min-width: 4;
        border: none;
        padding: 0 1;
        background: $boost;
        color: $text;
    }
    _TabBar > Button.-active {
        background: $accent;
        color: $text;
        text-style: bold;
    }
    """

    can_focus = True

    def __init__(
        self,
        *children: Any,
        active_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*children, **kwargs)
        self._active_id = active_id

    def on_mount(self) -> None:
        if self._active_id is not None:
            self.set_active(self._active_id)

    def set_active(self, btn_id: str) -> None:
        self._active_id = btn_id
        for child in self.children:
            if isinstance(child, Button):
                if child.id == btn_id:
                    child.add_class("-active")
                else:
                    child.remove_class("-active")

    def _buttons(self) -> list[Button]:
        return [c for c in self.children if isinstance(c, Button)]

    def on_focus(self) -> None:
        # Focus the active button so ←/→ work without an extra hop.
        for b in self._buttons():
            if b.id == self._active_id:
                b.focus()
                return
        bs = self._buttons()
        if bs:
            bs[0].focus()

    def on_key(self, event: events.Key) -> None:
        if event.key not in ("left", "right"):
            return
        bs = self._buttons()
        if not bs:
            return
        try:
            idx = next(i for i, b in enumerate(bs) if b.id == self._active_id)
        except StopIteration:
            idx = 0
        delta = -1 if event.key == "left" else 1
        new_btn = bs[(idx + delta) % len(bs)]
        if new_btn.id is not None:
            self.set_active(new_btn.id)
        new_btn.focus()
        new_btn.press()
        event.stop()


# ----------------------------------------------------------------------
# Top-level UI shell — mirrors the LineUI shape so cli.py is unchanged.
# ----------------------------------------------------------------------


class TextualUI:
    def __init__(
        self,
        client: WpsClient,
        *,
        my_call: str,
        channels: list[ChannelInfo] | None = None,
        history_backfill: int = 3,
        options: SessionOptions | None = None,
        offline: bool = False,
        show_clock: bool = True,
        cursor_blink: bool = True,
    ) -> None:
        self._client = client
        self._my_call = my_call.upper()
        self._channels = list(channels or [])
        self._history_backfill = max(0, int(history_backfill))
        self._options = options or SessionOptions()
        self._offline = offline
        self._show_clock = show_clock
        self._cursor_blink = cursor_blink
        self._target: TargetKey | None = None
        self._pending: list[dict] = []
        self._app: _WhatspycApp | None = None
        # Set to "terminal" when the link drops with no auto-reconnect or
        # after auto-reconnect gives up; the cli reads this after run()
        # returns to decide whether to offer a reconnect/quit prompt.
        self.exit_reason: str | None = None

    def render_event(self, obj: dict) -> None:
        if self._app is None or not self._app.is_mounted:
            self._pending.append(obj)
            return
        self._app.render_event(obj)

    async def run(self) -> None:
        self._app = _WhatspycApp(self)
        await self._app.run_async()


def launch(
    client: WpsClient,
    *,
    my_call: str,
    channels: list[ChannelInfo] | None = None,
    history_backfill: int = 3,
    options: SessionOptions | None = None,
) -> TextualUI:
    return TextualUI(
        client,
        my_call=my_call,
        channels=channels,
        history_backfill=history_backfill,
        options=options,
    )


# ----------------------------------------------------------------------
# Rendering helpers
# ----------------------------------------------------------------------


def _fmt_ts(ts: int | float | None) -> str:
    ms = ts_to_ms(ts)
    if ms is None:
        return "[gray]\\[--][/]"
    dt = datetime.datetime.fromtimestamp(ms / 1000)
    return f"[gray]\\[{dt.strftime('%Y-%m-%d %H:%M:%S')}][/]"


def _fmt_duration_ms(ms: int | float) -> str:
    s = max(0, round(ms / 1000))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m{s}s"


def _fmt_call(call: str | None, ham_name: Callable[[str | None], str | None]) -> str:
    if not call:
        return ""
    name = ham_name(call)
    inner = f"{name}, {call}" if name else call
    return f"[cornflowerblue]<{inner}>[/]"


def _fmt_user(call: str | None, ham_name: Callable[[str | None], str | None]) -> str:
    if not call:
        return ""
    name = ham_name(call)
    return f"{name}, {call}" if name else str(call)


def _verbose_status(
    *,
    from_call: str,
    my_call: str,
    ts: int | float | None,
    delivered_ts: int | None,
    received_ts: int | None,
    realtime: int | None,
    delivery_timeout_s: int,
) -> str | None:
    fc = (from_call or "").upper()
    ts_ms = ts_to_ms(ts)
    if fc == my_call:
        if delivered_ts is not None and ts_ms is not None:
            return f"Delivered to server in {_fmt_duration_ms(int(delivered_ts) - ts_ms)}"
        if ts_ms is None:
            return "Delivering..."
        age_ms = int(time.time() * 1000) - ts_ms
        if age_ms >= delivery_timeout_s * 1000:
            return "NOT DELIVERED"
        return "Delivering..."
    if realtime == 1 and received_ts is not None and ts_ms is not None:
        return f"Received real-time in {_fmt_duration_ms(int(received_ts) - ts_ms)}"
    return None


def _render_reactions(reactions: list[dict]) -> str:
    """Format the per-row reaction tail.

    User-facing form is ``[CALL EMOJI]`` (per the user's spec; one
    bracket per reactor+emoji), space-separated, prefixed with a
    leading space so it appends cleanly after the body or status
    suffix. Wrapped in cyan so reactions are easy to scan visually.
    Only the opening ``[`` needs to be escaped for Rich markup —
    ``]`` is only meaningful as a tag terminator and renders as a
    literal anywhere else; escaping it (``\\]``) leaks the backslash
    onto the screen as ``\\]``.
    """
    if not reactions:
        return ""
    parts: list[str] = []
    for r in reactions:
        e = r.get("emoji") or ""
        c = (r.get("callsign") or "").upper()
        if not e:
            continue
        # Wire form is a hex codepoint string (`"1f622"`); render as
        # the literal character so the user sees 😢 instead of `1f622`.
        e = emoji_for_display(e)
        if c:
            parts.append(rf"[cyan]\[{c} {e}][/]")
        else:
            parts.append(rf"[cyan]\[{e}][/]")
    if not parts:
        return ""
    return " " + " ".join(parts)


def _render_row(
    *,
    kind: str,
    from_call: str,
    body: str,
    ts: int | float | None,
    edit_ts: int | None,
    delivered_ts: int | None,
    received_ts: int | None,
    realtime: int | None,
    lid: int | None,
    my_call: str,
    verbose: bool,
    ham_name: Callable[[str | None], str | None],
    delivery_timeout_s: int,
    reactions: list[dict] | None = None,
) -> str:
    """Build a Rich-marked-up line for a single message/post.

    Compact: ``[ts] <Name, CALL>: body [EDITED if edited] [CALL EMOJI]...``
    Verbose: ``ID: lid - [ts] - <status> - <Name, CALL>: body [EDITED] [CALL EMOJI]...``

    Outbound rows we sent but haven't seen an ack for are dimmed; the dim
    clears once `delivered_ts` is set (live `mr`/`cpr` ack, or already
    persisted from a previous session). Rows from other people are never
    dimmed. Reactions render outside the dim wrap so a peer's reaction
    on a still-pending outbound row stays readable.
    """
    actor = _fmt_call(from_call, ham_name)
    is_mine = (from_call or "").upper() == my_call
    edit_marker = " [bold][EDITED][/]" if edit_ts else ""

    if verbose:
        head = f"ID: {lid} - {_fmt_ts(ts)}"
        status = _verbose_status(
            from_call=from_call,
            my_call=my_call,
            ts=ts,
            delivered_ts=delivered_ts,
            received_ts=received_ts,
            realtime=realtime,
            delivery_timeout_s=delivery_timeout_s,
        )
        if status:
            head = f"{head} - [gray]{status}[/]"
        line = f"{head} - {actor}: {body}{edit_marker}"
    else:
        line = f"{_fmt_ts(ts)} {actor}: {body}{edit_marker}"

    if is_mine and delivered_ts is None:
        line = f"[dim]{line}[/]"
    return line + _render_reactions(reactions or [])


# ----------------------------------------------------------------------
# MessageRow widget — holds row state, refreshes its label in place.
# ----------------------------------------------------------------------


class MessageRow(ListItem):
    """One message/post, mounted in a per-target ListView.

    Domain state (body, ts, edit_ts, delivered_ts, ...) lives here so the
    TUI can re-render in place when a `med`/`cped` rewrites the body, an
    `mr`/`cpr` records delivery, or `Ctrl+D` flips the verbose toggle.
    """

    DEFAULT_CSS = """
    MessageRow {
        padding: 0 1;
        height: auto;
    }
    """

    def __init__(
        self,
        *,
        kind: str,
        target_key: str,
        natural_key: str,
        from_call: str,
        body: str,
        ts: int | float | None,
        edit_ts: int | None = None,
        delivered_ts: int | None = None,
        received_ts: int | None = None,
        realtime: int | None = None,
        lid: int | None = None,
        reactions: list[dict] | None = None,
    ) -> None:
        # Keep a direct reference to the Static so refresh_label can
        # update it without going through query_one — query_one only
        # works once the row is fully mounted.
        self._static = Static("", markup=True)
        super().__init__(self._static)
        self.kind = kind
        self.tkey = target_key
        self.natural_key = natural_key
        self.from_call = from_call or ""
        self.body = body or ""
        self.ts = ts
        self.edit_ts = edit_ts
        self.delivered_ts = delivered_ts
        self.received_ts = received_ts
        self.realtime = realtime
        self.lid = lid
        # `[{emoji, callsign, emoji_ts}, ...]` — populated from the
        # store on mount and mutated in place by inbound mem/cpem
        # handlers. The outbound react path writes through to the
        # store first, then refreshes this from the store.
        self.reactions: list[dict] = list(reactions or [])
        # Cache key over inputs to ``_render_row`` so back-to-back
        # ``refresh_label`` calls with no observable change skip the
        # markup build + ``Static.update``. Ack flurries and the
        # post-reaction-batch handler hit this path repeatedly with
        # the same fields. ``None`` forces the first refresh through.
        self._render_key: tuple | None = None

    def refresh_label(
        self,
        *,
        my_call: str,
        verbose: bool,
        ham_name: Callable[[str | None], str | None],
        delivery_timeout_s: int,
    ) -> None:
        # Verbose-mode delivery status depends on wall-clock time
        # (the "Delivering... → NOT DELIVERED" flip) for outbound
        # rows we haven't seen an ack for. Including ``time.time()``
        # in the cache key would defeat the cache; instead, mark
        # those rows as uncacheable so the next refresh always
        # re-evaluates the threshold.
        is_pending_outbound = (
            verbose
            and (self.from_call or "").upper() == my_call
            and self.delivered_ts is None
            and self.ts is not None
        )
        if is_pending_outbound:
            key: tuple | None = None
        else:
            key = (
                self.body,
                self.ts,
                self.edit_ts,
                self.delivered_ts,
                self.received_ts,
                self.realtime,
                self.lid,
                verbose,
                my_call,
                delivery_timeout_s,
                tuple(
                    (r.get("emoji"), r.get("callsign"))
                    for r in self.reactions
                ),
            )
            if key == self._render_key:
                return
        text = _render_row(
            kind=self.kind,
            from_call=self.from_call,
            body=self.body,
            ts=self.ts,
            edit_ts=self.edit_ts,
            delivered_ts=self.delivered_ts,
            received_ts=self.received_ts,
            realtime=self.realtime,
            lid=self.lid,
            my_call=my_call,
            verbose=verbose,
            ham_name=ham_name,
            delivery_timeout_s=delivery_timeout_s,
            reactions=self.reactions,
        )
        self._render_key = key
        self._static.update(text)


# ----------------------------------------------------------------------
# Modal screens
# ----------------------------------------------------------------------


class HelpScreen(ModalScreen[None]):
    """F1 / ``/h`` — show key bindings and the slash-command list.

    With ``focus_command`` set, shows the detailed help for that one
    command instead of the full listing — used by ``/h <command>``.
    """

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-pane {
        width: 80%;
        height: 80%;
        border: round $accent;
        background: $surface;
    }
    #help-log {
        height: 1fr;
    }
    """

    def __init__(self, focus_command: str | None = None) -> None:
        super().__init__()
        self._focus_command = focus_command

    def compose(self) -> ComposeResult:
        with Vertical(id="help-pane"):
            yield RichLog(id="help-log", markup=True, wrap=True)
            yield Static("[dim]Esc to close[/]")

    def on_mount(self) -> None:
        log = self.query_one("#help-log", RichLog)
        if self._focus_command is not None:
            lines = help_data.detail_lines(self._focus_command)
            if lines is None:
                log.write(
                    f"[yellow]/h: unknown command {self._focus_command!r}. "
                    f"Try /h with no arguments for the full list.[/]"
                )
                return
            for line in lines:
                log.write(line)
            return
        log.write("[bold]Key bindings[/]")
        for line in _KEYBINDING_HELP_LINES:
            log.write(line)
        log.write("")
        log.write("[bold]Slash commands[/] (use /h <command> for details)")
        # Hide commands the TUI has replaced with GUI affordances.
        # /set still works (it opens the settings modal), so it stays.
        hide = {"/list", "/users"}
        for line in help_data.list_lines(hide=hide)[1:]:  # drop duplicated header
            log.write(line)


_KEYBINDING_HELP_LINES = [
    "  Tab / Shift+Tab    Cycle focus between panes",
    "  Esc                Return focus to the input box",
    "  ← / →              In tab strip: switch Channels / DMs",
    "  ↑ / ↓              In a list: navigate items",
    "  ↑ at top of msgs   Auto-load the next older page from the store",
    "  Enter (target)     Pin target as the send target, focus input",
    "  Enter (message)    Open action menu (Edit / Resend / React)",
    "  F1                 This help screen",
    "  Ctrl+D             Toggle detailed render (live)",
    "  Ctrl+S             Toggle the status pane (acks / edits log)",
    "  Ctrl+U             Unsubscribe highlighted channel (with confirm)",
    "  Ctrl+O             Open the Options (session settings) modal",
    "  Ctrl+E             Open searchable emoji picker, insert at cursor",
    "  Ctrl+C / Ctrl+Q    Quit",
]


class ActionMenu(ModalScreen[str | None]):
    """Enter on a message — Edit / Resend / React.

    Edit and Resend are disabled for rows we didn't send; React is
    always available. ``dismiss(action)`` returns one of
    ``"edit" | "resend" | "react" | None``.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ActionMenu {
        align: center middle;
    }
    #menu-pane {
        width: 30;
        border: round $accent;
        background: $surface;
        padding: 1;
    }
    #menu-list {
        height: auto;
    }
    """

    def __init__(self, *, allow_edit: bool, allow_resend: bool) -> None:
        super().__init__()
        self._allow_edit = allow_edit
        self._allow_resend = allow_resend

    def compose(self) -> ComposeResult:
        items: list[ListItem] = []
        if self._allow_edit:
            items.append(ListItem(Static("Edit"), id="action-edit"))
        if self._allow_resend:
            items.append(ListItem(Static("Resend"), id="action-resend"))
        items.append(ListItem(Static("React"), id="action-react"))
        with Vertical(id="menu-pane"):
            yield Static("[bold]Action[/]")
            yield ListView(*items, id="menu-list")
            yield Static("[dim]Enter to choose, Esc to cancel[/]")

    def on_mount(self) -> None:
        self.query_one("#menu-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Stop bubbling so the App-level on_list_view_selected can't react
        # to a modal item even if a future modal item id collides with one
        # of the App's prefixes (`target-`, `dm-add-call`, …).
        event.stop()
        item_id = event.item.id or ""
        if item_id.startswith("action-"):
            self.dismiss(item_id[len("action-"):])

    def action_cancel(self) -> None:
        self.dismiss(None)


QUICK_REACT_EMOJI: tuple[str, ...] = (
    "👍", "❤️", "😂", "🎉", "😮", "😢",
    "🔥", "🙏", "👏", "💯", "🤔", "👀",
    "🚀", "✅", "❌", "💀", "😎", "🤝",
    "👋", "😇", "😅", "🥳", "😴", "🙄",
)


# Tab id (kept short for stable Textual ids) → display label + group
# name. The group name is "" for the synthetic "quick" tab.
_GROUP_TABS: tuple[tuple[str, str, str], ...] = (
    ("quick", "★ Quick", ""),
    ("smileys", "Smileys", "Smileys & Emotion"),
    ("people", "People", "People & Body"),
    ("animals", "Animals", "Animals & Nature"),
    ("food", "Food", "Food & Drink"),
    ("travel", "Travel", "Travel & Places"),
    ("activities", "Activity", "Activities"),
    ("objects", "Objects", "Objects"),
    ("symbols", "Symbols", "Symbols"),
    ("flags", "Flags", "Flags"),
)
_GROUP_BY_TAB_ID: dict[str, str] = {tid: grp for tid, _, grp in _GROUP_TABS}
_TAB_ID_BY_GROUP: dict[str, str] = {grp: tid for tid, _, grp in _GROUP_TABS if grp}

# Group whose entries get a second-level subgroup tab strip — only
# People & Body is large enough (~386 entries spread over 16 subgroups)
# to need it. All other groups stay flat in a single scrollable grid.
_SUBGROUPED_GROUP = "People & Body"

_GRID_COLS = 8


class _EmojiButton(Button):
    """Button that carries its emoji string for `Button.Pressed` lookup."""

    def __init__(self, char: str, idx: int) -> None:
        super().__init__(char, id=f"emoji-btn-{idx}")
        self.emoji: str = char

    def set_emoji(self, char: str) -> None:
        """Mutate the button's emoji + label in place.

        Used by EmojiPrompt's grid update path: when a search refinement
        produces the same number of results, we update the existing
        buttons' contents instead of unmounting the entire grid and
        mounting a fresh batch (the original behaviour, which spent
        most of its time in mount/unmount on slow hardware).
        """
        self.emoji = char
        self.label = char


def _slug(s: str) -> str:
    """Stable Textual id from a CLDR subgroup name (alnum + dashes)."""
    out = []
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


class EmojiPrompt(ModalScreen[str | None]):
    """Searchable, tabbed emoji picker.

    Top-level Tabs strip selects the active CLDR group (or the curated
    "Quick" tab); for People & Body a second-level Tabs strip selects
    the subgroup. The grid lives inside a `VerticalScroll` so larger
    categories (Flags, Objects, Symbols) browse smoothly. The search
    Input at the top overrides the active tab when it has text — typing
    a query shows ranked matches across the whole catalogue, clearing
    the search reverts to the previously-active tab.

    Returns the chosen string verbatim (literal char or hex codepoint
    like ``1f44d``); both call sites and the wire helpers in
    ``whatspyc.ui`` accept either form, so no normalisation happens here.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    EmojiPrompt {
        align: center middle;
    }
    #emoji-pane {
        width: 100;
        max-height: 90%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #emoji-hint {
        margin-bottom: 1;
    }
    #emoji-search {
        margin-bottom: 1;
    }
    #emoji-group-tabs {
        margin-bottom: 1;
    }
    #emoji-subgroup-tabs {
        margin-bottom: 1;
    }
    #emoji-scroll {
        height: 12;
        border: tall $boost;
        margin-bottom: 1;
    }
    #emoji-grid {
        grid-size: 8;
        grid-gutter: 0 1;
        height: auto;
    }
    #emoji-grid Button {
        width: 1fr;
        min-width: 4;
        height: 1;
        border: none;
        background: $boost;
    }
    #emoji-grid Button:focus {
        background: $accent;
        text-style: bold;
    }
    #emoji-focused-name {
        color: $text-muted;
        height: 1;
        margin-bottom: 1;
    }
    """

    def __init__(self, *, debounce_ms: int = 0) -> None:
        super().__init__()
        # Active "view" — what the grid currently shows. Drives _render.
        # The subgroup tab id (when in People & Body), else "" for the
        # whole group.
        self._active_tab: str = "quick"
        self._active_subgroup: str = ""
        self._search_active: bool = False
        # Cache the buttons currently mounted, in DOM order.
        self._grid_buttons: list[_EmojiButton] = []
        # ms to wait after the last keystroke before re-rendering the
        # grid for a search update. 0 keeps the historic per-keystroke
        # behaviour. Tab clicks bypass this — they're a single user
        # gesture, not a stream of events.
        self._debounce_ms: int = max(0, debounce_ms)
        # Active debounce timer (Textual ``Timer``). Stopped + replaced
        # by each keystroke; cleared when the timer fires.
        self._render_timer: Any = None

    # ------------------------------------------------------------------
    # Compose / mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="emoji-pane"):
            yield Static(
                "Tabs select a CLDR group (or the curated quick-reacts). "
                "Type to search the full catalogue across every group. "
                "The bottom box accepts a literal character or a hex "
                "codepoint like [bold]1f44d[/].",
                id="emoji-hint",
            )
            yield Input(id="emoji-search", placeholder="search emoji…")
            yield _TabBar(
                *(Button(label, id=f"gtab-{tid}") for tid, label, _ in _GROUP_TABS),
                active_id="gtab-quick",
                id="emoji-group-tabs",
            )
            # Subgroup tab strip — populated lazily; hidden unless the
            # active group is People & Body.
            yield _TabBar(id="emoji-subgroup-tabs")
            with VerticalScroll(id="emoji-scroll"):
                yield Grid(id="emoji-grid")
            yield Static("", id="emoji-focused-name")
            yield Input(id="emoji-input", placeholder="emoji or hex codepoint")
            yield Static(
                "[dim]Type to search · ←→ tabs · ↑↓←→ grid · Tab cycles · Enter to send · Esc to cancel[/]"
            )

    async def on_mount(self) -> None:
        # Hide subgroup tabs until People & Body is selected.
        self.query_one("#emoji-subgroup-tabs", _TabBar).display = False
        await self._render_view()
        self.query_one("#emoji-search", Input).focus()

    # ------------------------------------------------------------------
    # Render — pick the entry list based on (search_active, active_tab,
    # active_subgroup) and remount the grid.
    # ------------------------------------------------------------------

    def _entries_for_view(self) -> list[str]:
        """Return the literal-char list to render in the grid."""
        if self._search_active:
            inp = self.query_one("#emoji-search", Input)
            return [e.char for e in search(inp.value, limit=200)]
        if self._active_tab == "quick":
            return list(QUICK_REACT_EMOJI)
        group = _GROUP_BY_TAB_ID.get(self._active_tab, "")
        if not group:
            return []
        if group == _SUBGROUPED_GROUP and self._active_subgroup:
            return [e.char for e in entries_in(group, self._active_subgroup)]
        if group == _SUBGROUPED_GROUP:
            # Defensive — should always have an active subgroup once
            # the People tab is selected.
            return [e.char for e in entries_in(group)]
        return [e.char for e in entries_in(group)]

    async def _render_view(self) -> None:
        chars = self._entries_for_view()
        grid = self.query_one("#emoji-grid", Grid)
        existing = self._grid_buttons
        # Fast path: same number of buttons → mutate labels in place
        # instead of unmounting + remounting the entire grid. The
        # original code path (and the slow path below) take the unmount/
        # mount cost for any change at all, which dominates EmojiPrompt
        # CPU during search typing.
        if existing and len(existing) == len(chars):
            for btn, char in zip(existing, chars):
                if btn.emoji != char:
                    btn.set_emoji(char)
        else:
            # Slow path: drop and rebuild. Triggered when search shrinks
            # / grows the result set or the active tab changes.
            await grid.remove_children()
            new_buttons = [_EmojiButton(c, i) for i, c in enumerate(chars)]
            if new_buttons:
                await grid.mount(*new_buttons)
            self._grid_buttons = new_buttons
        # Reset scroll to top and clear focused-name caption.
        try:
            self.query_one("#emoji-scroll", VerticalScroll).scroll_home(animate=False)
        except Exception:
            pass
        self._update_focused_name(None)

    # ------------------------------------------------------------------
    # Tab handling
    # ------------------------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        # Tab buttons live inside our two ``_TabBar``s; emoji-grid
        # buttons fire a different code path. Stop the event either
        # way so it can't bubble to the App.
        btn_id = event.button.id or ""
        if btn_id.startswith("gtab-"):
            event.stop()
            tid = btn_id.removeprefix("gtab-")
            if tid == self._active_tab and not self._search_active:
                return
            self._active_tab = tid
            group_bar = self.query_one("#emoji-group-tabs", _TabBar)
            group_bar.set_active(btn_id)
            # Clearing search if the user explicitly picked a tab.
            if self._search_active:
                self._search_active = False
                inp = self.query_one("#emoji-search", Input)
                if inp.value:
                    # Avoid re-firing on_input_changed.
                    with inp.prevent(Input.Changed):
                        inp.value = ""
            group = _GROUP_BY_TAB_ID.get(tid, "")
            sub_tabs = self.query_one("#emoji-subgroup-tabs", _TabBar)
            if group == _SUBGROUPED_GROUP:
                await self._populate_subgroup_tabs(group)
                sub_tabs.display = True
            else:
                self._active_subgroup = ""
                sub_tabs.display = False
            await self._render_view()
            return
        if btn_id.startswith("stab-"):
            event.stop()
            sub = btn_id.removeprefix("stab-")
            if sub == _slug(self._active_subgroup):
                return
            # Resolve the slug back to its real subgroup name.
            for grp_name, subs in catalog_groups():
                if grp_name == _SUBGROUPED_GROUP:
                    for s in subs:
                        if _slug(s) == sub:
                            self._active_subgroup = s
                            break
                    break
            self.query_one("#emoji-subgroup-tabs", _TabBar).set_active(btn_id)
            await self._render_view()
            return
        # Emoji grid buttons — pick that emoji and dismiss.
        if isinstance(event.button, _EmojiButton) and event.button.emoji:
            event.stop()
            self.dismiss(event.button.emoji)

    async def _populate_subgroup_tabs(self, group: str) -> None:
        """Rebuild the subgroup tab bar for ``group``."""
        sub_tabs = self.query_one("#emoji-subgroup-tabs", _TabBar)
        # Find the subgroups for this group.
        subs: list[str] = []
        for grp_name, sgs in catalog_groups():
            if grp_name == group:
                subs = sgs
                break
        await sub_tabs.remove_children()
        if not subs:
            return
        # Pick first by default if not already a valid sub.
        if self._active_subgroup not in subs:
            self._active_subgroup = subs[0]
        active_btn_id = f"stab-{_slug(self._active_subgroup)}"
        await sub_tabs.mount(
            *(Button(s, id=f"stab-{_slug(s)}") for s in subs)
        )
        sub_tabs.set_active(active_btn_id)

    # ------------------------------------------------------------------
    # Search wiring
    # ------------------------------------------------------------------

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "emoji-search":
            return
        event.stop()
        q = event.value
        self._search_active = bool(q.strip())
        # Re-render either way; tab strip stays where it was so clearing
        # the search box reverts to the active tab. Debounce so a burst
        # of keystrokes only causes one grid rebuild — the search loop
        # itself is fast (~ms in the catalogue) but the resulting
        # mount/unmount of up to 200 buttons isn't.
        if self._render_timer is not None:
            try:
                self._render_timer.stop()
            except Exception:
                pass
            self._render_timer = None
        if self._debounce_ms <= 0:
            await self._render_view()
            return

        def _fire() -> None:
            self._render_timer = None
            # ``set_timer`` callbacks are scheduled on the App's pump.
            # Spawn the actual render in a worker so we can ``await``
            # the asynchronous ``Grid.mount`` / ``Grid.remove_children``.
            self.run_worker(self._render_view(), exclusive=True, name="emoji-render")

        self._render_timer = self.set_timer(self._debounce_ms / 1000.0, _fire)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "emoji-search":
            for btn in self._grid_buttons:
                if btn.emoji:
                    self.dismiss(btn.emoji)
                    return
            text = event.value.strip()
            self.dismiss(text or None)
            return
        text = event.value.strip()
        self.dismiss(text or None)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        widget = event.widget
        if isinstance(widget, _EmojiButton):
            self._update_focused_name(widget.emoji)
            try:
                widget.scroll_visible(animate=False)
            except Exception:
                pass
        else:
            self._update_focused_name(None)

    # ------------------------------------------------------------------
    # Caption
    # ------------------------------------------------------------------

    def _update_focused_name(self, char: str | None) -> None:
        try:
            label = self.query_one("#emoji-focused-name", Static)
        except Exception:
            return
        if not char:
            label.update("")
            return
        entry = by_char(char)
        wire = format(ord(char), "x") if len(char) == 1 else "—"
        if entry is not None:
            shortcode = ":" + entry.name.replace(" ", "_") + ":"
            tail = f"  ({entry.group} · {entry.subgroup})" if entry.group else ""
            label.update(
                f"[bold]{char}[/]  {entry.name} · {shortcode} · {wire}{tail}"
            )
        else:
            label.update(f"[bold]{char}[/]  · {wire}")

    # ------------------------------------------------------------------
    # Arrow-key navigation + printable-key forwarding
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        focused = self.focused

        if isinstance(focused, _EmojiButton):
            buttons = self._grid_buttons
            if focused not in buttons:
                return
            idx = buttons.index(focused)
            target: int | None = None
            if event.key == "right" and idx + 1 < len(buttons):
                target = idx + 1
            elif event.key == "left" and idx - 1 >= 0:
                target = idx - 1
            elif event.key == "down" and idx + _GRID_COLS < len(buttons):
                target = idx + _GRID_COLS
            elif event.key == "up":
                if idx - _GRID_COLS >= 0:
                    target = idx - _GRID_COLS
                else:
                    event.stop()
                    event.prevent_default()
                    self.query_one("#emoji-search", Input).focus()
                    return
            elif event.key == "pagedown":
                target = min(idx + _GRID_COLS * 4, len(buttons) - 1)
            elif event.key == "pageup":
                target = max(idx - _GRID_COLS * 4, 0)
            elif event.key == "home":
                target = 0
            elif event.key == "end":
                target = len(buttons) - 1
            if target is not None and target != idx:
                event.stop()
                event.prevent_default()
                buttons[target].focus()
                return
            # Forward a plain printable keystroke into the search box.
            ch = event.character
            if ch and ch.isprintable() and len(ch) == 1 and not event.key.startswith("ctrl"):
                event.stop()
                event.prevent_default()
                inp = self.query_one("#emoji-search", Input)
                inp.value = inp.value + ch
                inp.cursor_position = len(inp.value)
                inp.focus()
                return

        if isinstance(focused, Input) and focused.id == "emoji-search":
            if event.key == "down":
                if self._grid_buttons:
                    event.stop()
                    event.prevent_default()
                    self._grid_buttons[0].focus()
            return

        if isinstance(focused, Input) and focused.id == "emoji-input":
            if event.key == "up":
                if self._grid_buttons:
                    event.stop()
                    event.prevent_default()
                    self._grid_buttons[-1].focus()
            return

    def action_cancel(self) -> None:
        self.dismiss(None)


class SubscribeModal(ModalScreen[int | None]):
    """Two-stage modal: confirm subscribe → ask post count.

    Stage 1 ("confirm"): Y subscribes (advances to stage 2), N/Esc closes
    without subscribing. Stage 2 ("count"): an `Input` for "how many of
    the {pc} historic posts to fetch?" — Enter on empty uses the default,
    Enter with a non-negative integer fetches that many, Esc closes with
    the default.

    Dismiss values:
    - ``None``     — user said no at the confirm stage; the caller should
                     leave the centre pane on its previous target.
    - ``int >= 0`` — user is now subscribed; the caller should switch
                     centre to the channel and, if ``> 0``, fire
                     ``request_post_batch(cid, n)``.
    """

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    SubscribeModal {
        align: center middle;
    }
    #sub-pane {
        width: 60;
        border: round $accent;
        background: $surface;
        padding: 1;
    }
    """

    def __init__(
        self,
        *,
        channel_ref: str,
        on_confirm: Callable[[], Awaitable[int]],
        default_count_for: Callable[[int], int],
        skip_confirm: bool = False,
    ) -> None:
        super().__init__()
        self._channel_ref = channel_ref
        self._on_confirm = on_confirm
        self._default_count_for = default_count_for
        self._skip_confirm = skip_confirm
        self._stage = "confirm"
        self._pc = 0
        self._default = 0

    def compose(self) -> ComposeResult:
        # The count-stage Input is mounted on demand in `_do_subscribe`.
        # Including it in the initial DOM (even hidden) makes Textual
        # auto-focus it, which steals the y/n keystrokes from the
        # screen-level bindings.
        with Vertical(id="sub-pane"):
            yield Static(
                f"Subscribe to [bold]{self._channel_ref}[/]?",
                id="sub-question",
            )
            yield Static(
                "[dim]Y to subscribe, N or Esc to cancel[/]",
                id="sub-hint",
            )

    def on_mount(self) -> None:
        # `skip_confirm` is set by callers who already obtained explicit
        # consent (e.g. the user typed `/sub CID`) — go straight to the
        # subscribe + count flow rather than asking Y/N again.
        if self._skip_confirm:
            self.run_worker(self._do_subscribe(), exclusive=False)

    def action_yes(self) -> None:
        if self._stage != "confirm":
            return
        self.run_worker(self._do_subscribe(), exclusive=False)

    def action_no(self) -> None:
        if self._stage == "confirm":
            self.dismiss(None)

    def action_cancel(self) -> None:
        # At the count stage the subscribe RPC has already landed; honour
        # that with the default count rather than throwing it away.
        if self._stage == "count":
            self.dismiss(self._default)
        else:
            self.dismiss(None)

    async def _do_subscribe(self) -> None:
        self._stage = "subscribing"
        self.query_one("#sub-question", Static).update(
            f"Subscribing to [bold]{self._channel_ref}[/]…"
        )
        self.query_one("#sub-hint", Static).update("[dim]Waiting for ack[/]")
        try:
            pc = await self._on_confirm()
        except asyncio.TimeoutError:
            self.query_one("#sub-question", Static).update(
                "[red]Timed out waiting for ack from server.[/]"
            )
            self.query_one("#sub-hint", Static).update("[dim]Esc to close[/]")
            self._stage = "error"
            return
        except Exception as exc:
            self.query_one("#sub-question", Static).update(
                f"[red]Subscribe failed:[/] {exc}"
            )
            self.query_one("#sub-hint", Static).update("[dim]Esc to close[/]")
            self._stage = "error"
            return
        self._pc = int(pc) if pc else 0
        if self._pc <= 0:
            # No history available — close right away and let the caller
            # switch the centre pane to the freshly-subscribed channel.
            self.dismiss(0)
            return
        self._default = max(0, self._default_count_for(self._pc))
        self._stage = "count"
        self.query_one("#sub-question", Static).update(
            f"Subscribed to [bold]{self._channel_ref}[/].\n"
            f"How many of the {self._pc} historic posts to fetch?"
        )
        self.query_one("#sub-hint", Static).update(
            f"[dim]Enter to fetch (default {self._default}); Esc to skip[/]"
        )
        pane = self.query_one("#sub-pane", Vertical)
        inp = Input(id="sub-input", placeholder=f"default {self._default}")
        await pane.mount(inp, before=self.query_one("#sub-hint", Static))
        inp.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Stop bubbling — otherwise the App-level on_input_submitted fires
        # next and sends the typed count as a chat message to whatever
        # target was active before the modal opened.
        event.stop()
        if self._stage != "count":
            return
        text = event.value.strip()
        if not text:
            n = self._default
        else:
            try:
                n = int(text)
            except ValueError:
                self.query_one("#sub-hint", Static).update(
                    f"[red]Expected integer, got {text!r}[/]"
                )
                return
            if n < 0:
                self.query_one("#sub-hint", Static).update(
                    "[red]Post count must be non-negative[/]"
                )
                return
        self.dismiss(min(n, self._pc))


class NewDmModal(ModalScreen[str | None]):
    """Tiny modal asking for a callsign to start (or switch to) a DM thread.

    Dismisses with the upper-cased callsign on Enter, or ``None`` on Esc /
    empty input. The caller decides whether to create a new thread or
    switch to an existing one.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    NewDmModal {
        align: center middle;
    }
    #dm-add-pane {
        width: 60;
        border: round $accent;
        background: $surface;
        padding: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dm-add-pane"):
            yield Static("Enter callsign for new DM:")
            yield Input(id="dm-add-input")
            yield Static("[dim]Enter to open, Esc to cancel[/]")

    def on_mount(self) -> None:
        self.query_one("#dm-add-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip().upper()
        self.dismiss(text or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class UnsubscribeModal(ModalScreen[bool]):
    """Y/N confirm for unsubscribing from a channel.

    Dismisses with ``True`` if the user said yes, ``False`` on N / Esc.
    The caller is responsible for actually issuing the unsubscribe RPC.
    """

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    UnsubscribeModal {
        align: center middle;
    }
    #unsub-pane {
        width: 60;
        border: round $accent;
        background: $surface;
        padding: 1;
    }
    """

    def __init__(self, *, channel_ref: str) -> None:
        super().__init__()
        self._channel_ref = channel_ref

    def compose(self) -> ComposeResult:
        with Vertical(id="unsub-pane"):
            yield Static(
                f"Unsubscribe from [bold]{self._channel_ref}[/]?",
                id="unsub-question",
            )
            yield Static(
                "[dim]Y to unsubscribe, N or Esc to cancel[/]",
                id="unsub-hint",
            )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class QuitConfirmModal(ModalScreen[bool]):
    """Y/N confirm before quitting the app.

    Dismisses with ``True`` to proceed with quit, ``False`` on N / Esc.
    Defaults the selection to ``No`` so an accidental Enter cancels.
    """

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    QuitConfirmModal {
        align: center middle;
    }
    #quit-pane {
        width: 50;
        border: round $accent;
        background: $surface;
        padding: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="quit-pane"):
            yield Static(
                "[bold]Are you sure you want to quit?[/]",
                id="quit-question",
            )
            yield ListView(
                ListItem(Static("No"), id="quit-no"),
                ListItem(Static("Yes"), id="quit-yes"),
                id="quit-list",
            )
            yield Static(
                "[dim]Y to quit, N or Esc to cancel[/]",
                id="quit-hint",
            )

    def on_mount(self) -> None:
        lv = self.query_one("#quit-list", ListView)
        lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        self.dismiss(event.item.id == "quit-yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class BoolSelectModal(ModalScreen[bool | None]):
    """On/Off picker for boolean settings.

    Arrow keys move between the two rows, Enter commits the highlighted
    choice. Numeric shortcuts ``1`` (On) and ``0`` (Off) commit
    immediately so power users don't need to navigate. Esc cancels.

    ``priority=True`` on the digit bindings stops the focused
    ``ListView`` from swallowing them.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("1", "pick_on", "On", show=False, priority=True),
        Binding("0", "pick_off", "Off", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    BoolSelectModal {
        align: center middle;
    }
    #boolsel-pane {
        width: 60;
        border: round $accent;
        background: $surface;
        padding: 1;
    }
    """

    def __init__(self, *, name: str, current: bool, description: str) -> None:
        super().__init__()
        self._name = name
        self._current = current
        self._description = description

    def compose(self) -> ComposeResult:
        with Vertical(id="boolsel-pane"):
            yield Static(f"[bold]{self._name}[/]")
            yield Static(f"[dim]{self._description}[/]")
            yield ListView(
                ListItem(Static("On"), id="boolsel-on"),
                ListItem(Static("Off"), id="boolsel-off"),
                id="boolsel-list",
            )
            yield Static("[dim]Enter to save, 1=On, 0=Off, Esc to cancel[/]")

    def on_mount(self) -> None:
        lv = self.query_one("#boolsel-list", ListView)
        # Highlight the current value by default so Enter == no change.
        lv.index = 0 if self._current else 1
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        if event.item.id == "boolsel-on":
            self.dismiss(True)
        elif event.item.id == "boolsel-off":
            self.dismiss(False)

    def action_pick_on(self) -> None:
        self.dismiss(True)

    def action_pick_off(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditValueModal(ModalScreen[str | None]):
    """Inline editor for a single setting's value. Pre-fills the input
    with the current rendered value; Enter submits, Esc cancels.

    Returns the raw string the user typed (caller parses + validates via
    ``SessionOptions.set``); ``None`` on cancel.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    EditValueModal {
        align: center middle;
    }
    #setval-pane {
        width: 60;
        border: round $accent;
        background: $surface;
        padding: 1;
    }
    """

    def __init__(self, *, name: str, current: str, description: str) -> None:
        super().__init__()
        self._name = name
        self._current = current
        self._description = description

    def compose(self) -> ComposeResult:
        with Vertical(id="setval-pane"):
            yield Static(f"[bold]{self._name}[/]")
            yield Static(f"[dim]{self._description}[/]")
            yield Input(value=self._current, id="setval-input")
            yield Static("[dim]Enter to save, Esc to cancel[/]", id="setval-hint")

    def on_mount(self) -> None:
        inp = self.query_one("#setval-input", Input)
        inp.focus()
        inp.cursor_position = len(self._current)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SettingsModal(ModalScreen[None]):
    """Settings browser — replaces the inline ``/set`` listing.

    Renders one row per ``SessionOptions`` entry showing
    ``name = value`` plus its description. Enter on a row pushes
    ``EditValueModal`` to edit the value; on a successful change the
    row label is refreshed and the optional ``on_change`` callback
    fires (used by the TUI to apply side-effects like
    ``set_delivery_timeout_s`` and the verbose-mode re-render).
    """

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    SettingsModal {
        align: center middle;
    }
    #settings-pane {
        width: 80;
        height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1;
    }
    #settings-list {
        height: 1fr;
    }
    """

    def __init__(
        self,
        *,
        options: Any,
        on_change: Callable[[str, Any, Any], None] | None = None,
    ) -> None:
        super().__init__()
        self._opts = options
        self._on_change = on_change

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-pane"):
            yield Static("[bold]Session settings[/] — Enter to edit, Esc to close")
            yield ListView(id="settings-list")
            yield Static("", id="settings-hint")

    def on_mount(self) -> None:
        lv = self.query_one("#settings-list", ListView)
        for n in self._opts.names():
            lv.append(self._row_for(n))
        lv.focus()

    def _row_for(self, name: str) -> ListItem:
        label = (
            f"[bold]{name}[/] = [green]{self._opts.format(name)}[/]\n"
            f"  [dim]{self._opts.describe(name)}[/]"
        )
        return ListItem(Static(label, markup=True), id=f"setting-{name}")

    def _refresh_row(self, name: str) -> None:
        try:
            item = self.query_one(f"#setting-{name}", ListItem)
        except Exception:
            return
        item.query_one(Static).update(
            f"[bold]{name}[/] = [green]{self._opts.format(name)}[/]\n"
            f"  [dim]{self._opts.describe(name)}[/]"
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        item_id = event.item.id or ""
        prefix = "setting-"
        if not item_id.startswith(prefix):
            return
        name = item_id[len(prefix):]
        self.run_worker(self._edit_setting(name), exclusive=False)

    async def _edit_setting(self, name: str) -> None:
        current_val = self._opts.get(name)
        if isinstance(current_val, bool):
            picked = await self.app.push_screen_wait(
                BoolSelectModal(
                    name=name,
                    current=current_val,
                    description=self._opts.describe(name),
                )
            )
            if picked is None:
                return
            # SessionOptions.set parses strings; "on"/"off" matches the
            # public /set surface and the canonical format for bools.
            raw = "on" if picked else "off"
        else:
            raw = await self.app.push_screen_wait(
                EditValueModal(
                    name=name,
                    current=self._opts.format(name),
                    description=self._opts.describe(name),
                )
            )
            if raw is None:
                return
        try:
            old, new = self._opts.set(name, raw)
        except ValueError as exc:
            self.query_one("#settings-hint", Static).update(
                f"[yellow]{name}: {exc}[/]"
            )
            return
        self._refresh_row(name)
        old_fmt = self._opts.format_value(name, old)
        new_fmt = self._opts.format_value(name, new)
        if old_fmt == new_fmt:
            self.query_one("#settings-hint", Static).update(
                f"[green]{name} = {new_fmt}[/] [dim](unchanged)[/]"
            )
        else:
            self.query_one("#settings-hint", Static).update(
                f"[green]{name} = {new_fmt}[/] [dim](was {old_fmt})[/]"
            )
        if self._on_change is not None:
            self._on_change(name, old, new)


# ----------------------------------------------------------------------
# Main app
# ----------------------------------------------------------------------


_FOCUS_CYCLE = ["input", "msg-active", "tab-strip", "target-active", "online"]


class StaticFooter(Footer):
    """``Footer`` subclass with a focus-independent binding order.

    The default ``Footer`` rebuilds from ``screen.active_bindings`` on
    every focus change, which reshuffles the entries because the
    focused widget's bindings move to the front of the chain. We
    override ``compose`` to iterate over the App's ``BINDINGS`` list
    in declaration order — same Textual styling, stable order.
    """

    def compose(self) -> ComposeResult:
        if not self._bindings_ready:
            return
        # Walk the App's BINDINGS in declaration order. De-dup by
        # action so two bindings on the same handler (e.g. ctrl+c and
        # ctrl+q both → quit_app) only render once.
        app = self.app
        # Conditional bindings: hide ctrl+u (unsub) unless the active
        # target is a subscribed channel — keeps the footer honest about
        # what's actually actionable right now. Recompose is triggered
        # from `_refresh_footer` whenever target / subscription state
        # changes.
        show_unsub = (
            hasattr(app, "_active_target_is_subscribed_channel")
            and app._active_target_is_subscribed_channel()
        )
        seen: set[str] = set()
        for binding in app.BINDINGS:
            if not getattr(binding, "show", False):
                continue
            if binding.action in seen:
                continue
            if binding.action == "unsub_channel" and not show_unsub:
                continue
            seen.add(binding.action)
            yield FooterKey(
                binding.key,
                app.get_key_display(binding),
                binding.description,
                binding.action,
            ).data_bind(compact=Footer.compact)


class _WhatspycApp(App):
    TITLE = f"Whatspyc (v{__version__}) text-only WhatsPac Client - Recommended GUI Client: http://whatspac.oarc.uk/"

    CSS = """
    Screen { layout: vertical; }
    #main { layout: horizontal; height: 1fr; }
    #left { width: 30; border-right: solid $accent; layout: vertical; }
    #target-switcher { height: 1fr; }
    #online-header { background: $boost; padding: 0 1; }
    #online { height: 10; border-top: solid $accent; }
    #right { layout: vertical; height: 1fr; }
    #status { height: 8; border: round $accent; display: none; }
    #thread-header { background: $boost; padding: 0 1; }
    #message-switcher { height: 1fr; border: round $accent; }
    #empty-msgs { padding: 1 2; color: $text-muted; }
    #input { dock: bottom; }
    """

    # Width budget per channel row: "☑ {cid} #{name} (100)" — that's
    # 2 (checkbox + space) + len(cid) + 2 (" #") + len(name) + 6 (" (100)").
    # We pick the widest channel from the directory + store and pin the
    # left pane so a 3-digit unread count never wraps.
    _LEFT_PANE_MIN_WIDTH = 22
    _LEFT_PANE_RESERVED_SUFFIX = 6  # " (100)"

    BINDINGS = [
        # F1 is the working help key. ctrl+h is kept as an aspirational
        # alias but won't fire on most terminals: Ctrl+H emits byte 0x08,
        # the same as Backspace, and Textual's ANSI parser normalises
        # that to Keys.Backspace — so a "ctrl+h" binding never matches.
        Binding("f1", "help", "Help", priority=True, key_display="F1"),
        Binding("ctrl+h", "help", show=False, priority=True),
        Binding("ctrl+q", "quit_app", "Quit"),
        Binding("ctrl+c", "quit_app", "Quit"),
        # priority=True so the focused widget (Input binds ctrl+d as
        # "delete-forward" by default; ListView swallows arrow/letter
        # keys) doesn't preempt the global toggle.
        Binding("ctrl+d", "toggle_verbose", "Detailed View", priority=True),
        Binding("ctrl+s", "toggle_status", "Status Pane", priority=True),
        # priority=True so the focused Input (which binds ctrl+u to
        # "delete to start of line") doesn't preempt the app binding.
        # Clicking a channel moves focus to the Input, so without
        # priority the very gesture used to pick a channel would
        # disable this shortcut for the next keypress.
        Binding("ctrl+u", "unsub_channel", "Unsub", priority=True),
        # priority=True so the focused Input doesn't swallow Ctrl+O.
        Binding("ctrl+o", "options", "Options", priority=True),
        # priority=True so the focused Input doesn't claim Ctrl+E for
        # its built-in "cursor to end of line" action.
        Binding("ctrl+e", "insert_emoji", "Emoji", priority=True),
        Binding("escape", "focus_input", "Focus input", show=False),
        Binding("tab", "focus_next_pane", "Next pane", show=False),
        Binding("shift+tab", "focus_prev_pane", "Prev pane", show=False),
    ]

    def __init__(self, ui: TextualUI) -> None:
        super().__init__()
        self._ui = ui
        self.is_mounted = False

        # Set on mount when the configured log_console mode is "pane".
        # Removed on unmount so reconnect cycles don't accumulate handlers.
        self._log_handler: Any = None

        # Lazy per-target message ListView ids.
        self._views: dict[TargetKey, str] = {}
        self._next_view_idx = 0

        # MessageRow widgets keyed by (kind, target_key, natural_key) so
        # `med`/`cped`/`mr`/`cpr` can find the row to update in place.
        self._rows: dict[RowKey, MessageRow] = {}

        # (kind, key) → unread count, displayed as " (N)" suffix in the
        # target list. Cleared on activation.
        self._unread: dict[TargetKey, int] = {}

        # (kind, key) → True once we've confirmed the store has nothing
        # older. Stops us hammering the store every time the cursor
        # bumps the top row.
        self._history_exhausted: dict[TargetKey, bool] = {}

        # When set, the next plain-text submit is interpreted as the new
        # body for an in-progress edit instead of a fresh send.
        self._pending_edit: dict | None = None

        # Re-entrancy guard for the quit-confirm modal so a second Ctrl+Q
        # while the prompt is already up is a no-op.
        self._quit_confirm_open = False

        # Suppress ListView.Highlighted handling during programmatic
        # mutation of a list (mounting rows, prepending older history).
        self._suppress_highlight = False

        # Inbound events from the WpsClient reader task land here and a
        # single drain worker pulls them in order. Required because
        # rendering an event may need to mount a new ListView, which is
        # async — and the reader task can't await directly from a sync
        # callback.
        self._event_queue: asyncio.Queue[dict] = asyncio.Queue()

        # Cached handles for static-DOM widgets — populated in on_mount,
        # consulted from hot handlers that would otherwise call
        # ``query_one`` on every event. The dynamic per-target message
        # ListViews still use ``query_one(f"#{view_id}")`` because their
        # ids are minted lazily.
        self._w_input: Input | None = None
        self._w_status: RichLog | None = None
        self._w_thread_header: Static | None = None
        self._w_online: ListView | None = None
        self._w_online_header: Static | None = None
        self._w_msg_switcher: ContentSwitcher | None = None
        self._w_target_switcher: ContentSwitcher | None = None
        self._w_tabs: _TabBar | None = None

        # Online pane incremental-diff state. Keys are callsigns; each
        # value is the ``ListItem`` currently mounted for that user.
        # ``_online_label_cache`` holds the last formatted label so we
        # only ``Static.update`` when the resolved name actually changed
        # (relevant after an ``he`` (ham name) event).
        self._online_items: dict[str, ListItem] = {}
        self._online_label_cache: dict[str, str] = {}

        # Targets whose mounted MessageRow widgets are stale w.r.t. the
        # current verbose_history setting. Populated by
        # ``action_toggle_verbose`` for every non-active target; cleared
        # lazily by ``_switch_centre_to`` when each target gets
        # activated. Avoids walking every row in every view on Ctrl+D.
        self._verbose_dirty: set[TargetKey] = set()

        # Memoised set of subscribed cids. Lazy-loaded on first
        # ``_is_subscribed`` call (which used to scan the full
        # ``store.list_channels()`` result on every check — hot path
        # via ``_target_label`` on every unread bump). Invalidated by
        # ``_handle_cs`` on subscribe/unsubscribe acks and on
        # ``_reconnected`` so a fresh link picks up any server-side
        # drift. ``None`` means "not yet loaded".
        self._subscribed_cids: set[int] | None = None

        # Coalescing timer for ``he`` (ham-enquiry) events. The connect
        # sequence emits a burst of these — each one fires a full
        # online-pane diff plus a DM-target-label relabel for every
        # mounted thread, which is quadratic in (hams × DMs) and
        # noticeable on slow CPUs. ``_schedule_he_refresh`` arms the
        # timer; subsequent ``he`` events while the timer is pending
        # are no-ops so the burst collapses into one refresh.
        self._he_refresh_pending: bool = False

    # ------------------------------------------------------------------
    # Compose / mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=self._ui._show_clock)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield _TabBar(
                    Button("Channels", id="tab-channels"),
                    Button("DMs", id="tab-dms"),
                    active_id="tab-channels",
                    id="tab-strip",
                )
                with ContentSwitcher(initial="channels", id="target-switcher"):
                    yield ListView(id="channels")
                    yield ListView(id="dms")
                yield Static("Online (0)", id="online-header")
                yield ListView(id="online")
            with Vertical(id="right"):
                yield RichLog(id="status", wrap=True, markup=True, highlight=False)
                yield Static("", id="thread-header")
                with ContentSwitcher(initial="empty-msgs", id="message-switcher"):
                    yield Static(
                        "[dim]Pick a channel or DM on the left to start. Use the mouse, or tab to change pane focus with the keyboard.[/]",
                        id="empty-msgs",
                    )
        # ``cursor_blink`` isn't a ctor kwarg on this Textual version —
        # it's a reactive on the class. Set it on the instance before
        # yield; the timer is built in Input.on_mount and reads the
        # reactive's current value, so a False here means the timer
        # starts paused (no per-tick refresh).
        inp = Input(
            placeholder=f"{self._ui._my_call}> ",
            id="input",
            select_on_focus=False,
        )
        inp.cursor_blink = self._ui._cursor_blink
        yield inp
        yield StaticFooter()

    async def on_mount(self) -> None:
        self.is_mounted = True
        # Resolve handles for static-DOM widgets once so hot handlers
        # (online refresh, status writes, focus management, ...) don't
        # call ``query_one`` per event.
        self._w_input = self.query_one("#input", Input)
        self._w_status = self.query_one("#status", RichLog)
        self._w_thread_header = self.query_one("#thread-header", Static)
        self._w_online = self.query_one("#online", ListView)
        self._w_online_header = self.query_one("#online-header", Static)
        self._w_msg_switcher = self.query_one("#message-switcher", ContentSwitcher)
        self._w_target_switcher = self.query_one("#target-switcher", ContentSwitcher)
        self._w_tabs = self.query_one("#tab-strip", _TabBar)
        self._apply_left_pane_width()
        self._populate_initial_target_lists()
        self._refresh_online_pane(self._ui._client.online_users())
        self._refresh_thread_header()
        if self._ui._offline:
            # Force the status pane visible and announce the mode so the
            # user always sees the read-only banner — `_status_error`
            # auto-shows the pane on first write.
            self._status_error(
                "[yellow][offline][/] read-only mode — browsing local "
                "store, no connection"
            )
        # Hook the root logger if log_console resolved to "pane" — otherwise
        # this is a no-op and returns None.
        self._log_handler = log_mod.install_pane_handler(
            self._status_write, self._status_error
        )
        # Drain worker — processes inbound events in order, async-safe.
        self.run_worker(self._drain_events(), exclusive=True, name="event-drain")
        for obj in self._ui._pending:
            self.render_event(obj)
        self._ui._pending.clear()
        self._w_input.focus()

    def on_unmount(self) -> None:
        log_mod.remove_pane_handler(self._log_handler)
        self._log_handler = None

    def render_event(self, obj: dict) -> None:
        # Called from the WpsClient reader task (sync). Push onto the
        # queue; the drain worker pulls and awaits the dispatch.
        self._event_queue.put_nowait(obj)

    async def _drain_events(self) -> None:
        while True:
            obj = await self._event_queue.get()
            try:
                await self._dispatch_event(obj)
            except Exception as exc:
                self._status_error(f"[red][error][/] dispatch: {exc}")

    def _apply_left_pane_width(self) -> None:
        """Size the left pane to fit the widest channel label plus room
        for a 3-digit unread suffix, so '#announcements (100)' never wraps."""
        widest = self._LEFT_PANE_MIN_WIDTH
        cids: dict[int, str | None] = {}
        for c in self._ui._channels:
            cids[c.cid] = c.name
        try:
            for r in self._ui._client._store.list_channels():  # type: ignore[attr-defined]
                cids.setdefault(r["cid"], None)
        except Exception:
            pass
        for cid, name in cids.items():
            label_len = 2 + len(str(cid))  # "☑ {cid}"
            if name:
                label_len += 2 + len(name)  # " #{name}"
            widest = max(widest, label_len + self._LEFT_PANE_RESERVED_SUFFIX)
        # +1 for the right border, +2 for breathing room.
        try:
            self.query_one("#left").styles.width = widest + 3
        except Exception:
            pass

    def _populate_initial_target_lists(self) -> None:
        # Channels: subscribed first (from the store), then directory
        # entries the store hasn't seen yet (with a (–) marker).
        try:
            channels = self._ui._client._store.list_channels()  # type: ignore[attr-defined]
        except Exception:
            channels = []
        seeded: set[int] = set()
        for ch in channels:
            if ch.get("subscribed"):
                self._add_target(("ch", str(ch["cid"])))
                seeded.add(ch["cid"])
        for c in sorted(self._ui._channels, key=lambda c: c.cid):
            if c.cid in seeded:
                continue
            self._add_target(("ch", str(c.cid)), unsubscribed=True)

        # "Add Call to DM" pinned row at the top of the DM list. Stays
        # in place because every later DM peer is appended below it.
        try:
            dms_lv = self.query_one("#dms", ListView)
            dms_lv.append(
                ListItem(
                    Static("[bold]+ Add Call to DM[/]", markup=True),
                    id="dm-add-call",
                )
            )
        except Exception:
            pass

        # DM peers from the store.
        try:
            peers = self._ui._client._store.list_dm_peers(self._ui._my_call)  # type: ignore[attr-defined]
        except Exception:
            peers = []
        for p in peers:
            self._add_target(("dm", p["peer"]))

    # ------------------------------------------------------------------
    # Target list — left pane
    # ------------------------------------------------------------------

    def _target_id(self, target: TargetKey) -> str:
        kind, key = target
        # Sanitize: ListItem ids must be alphanumeric / underscore /
        # hyphen and start with a letter.
        safe = re.sub(r"[^A-Za-z0-9]", "_", key)
        return f"target-{kind}-{safe}"

    def _target_label(self, target: TargetKey, *, unsubscribed: bool = False) -> str:
        kind, key = target
        unread = self._unread.get(target, 0)
        unread_suffix = f" [bold yellow]({unread})[/]" if unread else ""
        if kind == "ch":
            try:
                cid = int(key)
            except ValueError:
                return f"ch:{key}{unread_suffix}"
            subscribed = self._is_subscribed(cid) and not unsubscribed
            check = "☑" if subscribed else "☐"
            name = self._channel_name(cid)
            label = f"{check} {cid} #{name}" if name else f"{check} {cid}"
            return f"{label}{unread_suffix}"
        return f"{_fmt_user(key, self._ui._client.ham_name)}{unread_suffix}"

    def _add_target(self, target: TargetKey, *, unsubscribed: bool = False) -> None:
        kind, _ = target
        list_id = "channels" if kind == "ch" else "dms"
        try:
            lv = self.query_one(f"#{list_id}", ListView)
        except Exception:
            return
        wid = self._target_id(target)
        for child in lv.children:
            if child.id == wid:
                return
        label = self._target_label(target, unsubscribed=unsubscribed)
        lv.append(ListItem(Static(label, markup=True), id=wid))

    def _refresh_target_label(self, target: TargetKey) -> None:
        wid = self._target_id(target)
        try:
            item = self.query_one(f"#{wid}", ListItem)
        except Exception:
            return
        item.query_one(Static).update(self._target_label(target))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Tab-strip buttons swap the target ContentSwitcher. Other Button
        # presses (modal buttons, future widgets) are uninteresting here
        # — fall through silently rather than ``event.stop()``-ing.
        btn_id = event.button.id or ""
        if btn_id in ("tab-channels", "tab-dms"):
            if self._w_tabs is not None:
                self._w_tabs.set_active(btn_id)
            switcher = self._w_target_switcher
            if switcher is not None:
                switcher.current = "channels" if btn_id == "tab-channels" else "dms"

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if self._suppress_highlight:
            return
        if event.item is None:
            return
        lv = event.list_view
        # Target list highlight → live-preview the centre pane. Skip for
        # unsubscribed channels: nothing should change in the centre pane
        # until the user confirms the subscribe via the modal opened by
        # `on_list_view_selected`.
        if lv.id in ("channels", "dms") and event.item.id and event.item.id.startswith("target-"):
            target = self._target_from_id(event.item.id)
            if target is None:
                return
            if target[0] == "ch" and not self._ui._offline:
                try:
                    cid = int(target[1])
                except ValueError:
                    cid = None
                if cid is not None and not self._is_subscribed(cid) \
                        and not self._ui._client.paused_channels().get(cid):
                    return
            await self._switch_centre_to(target)
            return
        # Active message list, cursor at top → load older.
        active = self._active_view_id()
        if active and lv.id == active and lv.index == 0:
            await self._load_older()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        # "Add Call to DM" pinned row at the top of the DM list.
        if item_id == "dm-add-call":
            self._open_new_dm_modal()
            return
        # Target list: Enter pins target and refocuses the input.
        if item_id.startswith("target-"):
            target = self._target_from_id(item_id)
            if target is None:
                return
            if target[0] == "ch" and not self._ui._offline:
                try:
                    cid = int(target[1])
                except ValueError:
                    cid = None
                # Unsubscribed channel: open the subscribe modal and only
                # commit the target / centre switch on confirm. Paused
                # implies subscribed, so it falls through to the normal
                # path below. Offline mode skips this whole flow — there's
                # no subscribe to be done; just preview the local store.
                if cid is not None and not self._is_subscribed(cid) \
                        and not self._ui._client.paused_channels().get(cid):
                    self._open_subscribe_modal(cid, target=target)
                    return
            self._ui._target = target
            await self._switch_centre_to(target)
            self._refresh_prompt()
            self._refresh_footer()
            self.query_one("#input", Input).focus()
            if target[0] == "ch":
                cid = int(target[1])
                if self._ui._client.paused_channels().get(cid):
                    self._maybe_print_paused_hint(cid)
            return
        # Active message list: Enter opens the action menu.
        active = self._active_view_id()
        if active and event.list_view.id == active:
            row = event.item
            if isinstance(row, MessageRow):
                self._open_action_menu(row)

    def _target_from_id(self, item_id: str) -> TargetKey | None:
        # "target-{kind}-{safe_key}" — but sanitisation is lossy, so we
        # round-trip via the existing _add_target keys.
        for tkey in list(self._views.keys()) + self._all_target_keys():
            if self._target_id(tkey) == item_id:
                return tkey
        return None

    def _all_target_keys(self) -> list[TargetKey]:
        out: list[TargetKey] = []
        for c in self._ui._channels:
            out.append(("ch", str(c.cid)))
        try:
            for ch in self._ui._client._store.list_channels():  # type: ignore[attr-defined]
                out.append(("ch", str(ch["cid"])))
        except Exception:
            pass
        try:
            for p in self._ui._client._store.list_dm_peers(self._ui._my_call):  # type: ignore[attr-defined]
                out.append(("dm", p["peer"]))
        except Exception:
            pass
        return out

    # ------------------------------------------------------------------
    # Centre pane — per-target message ListViews
    # ------------------------------------------------------------------

    async def _ensure_message_view(self, target: TargetKey) -> ListView:
        """Get-or-create the per-target message ListView. Async because
        Textual requires the LV to finish mounting before its children
        can be appended."""
        if target in self._views:
            return self.query_one(f"#{self._views[target]}", ListView)
        view_id = f"msgs-{self._next_view_idx}"
        self._next_view_idx += 1
        self._views[target] = view_id
        switcher = self.query_one("#message-switcher", ContentSwitcher)
        new_lv = ListView(id=view_id)
        await switcher.mount(new_lv)
        # Seed initial backfill from the store now that the LV is live.
        self._mount_initial_history(target, new_lv)
        return new_lv

    async def _switch_centre_to(self, target: TargetKey) -> None:
        lv = await self._ensure_message_view(target)
        if self._w_msg_switcher is not None:
            self._w_msg_switcher.current = lv.id  # type: ignore[assignment]
        # If verbose_history was toggled while this target was inactive
        # its rows still hold the old render; refresh them now (lazy
        # paydown of the work the toggle handler deliberately deferred).
        if target in self._verbose_dirty:
            self._verbose_dirty.discard(target)
            for (k, t, _), row in self._rows.items():
                if (k, t) == target:
                    self._refresh_row_label(row)
        # Activating clears the unread count.
        if self._unread.pop(target, 0):
            self._refresh_target_label(target)
        self._refresh_thread_header(target)

    def _refresh_thread_header(self, target: TargetKey | None = None) -> None:
        hdr = self._w_thread_header
        if hdr is None:
            return
        if target is None:
            target = self._active_target()
        if target is None:
            hdr.update("[dim]No channel or DM selected[/]")
            return
        kind, key = target
        if kind == "ch":
            try:
                cid = int(key)
            except ValueError:
                hdr.update(f"[b]Channel {key}[/]")
                return
            name = self._channel_name(cid)
            label = f"[b]#{name}[/] (ch {cid})" if name else f"[b]Channel {cid}[/]"
            hdr.update(label)
        else:
            user = _fmt_user(key, self._ui._client.ham_name)
            hdr.update(f"[b]DM:[/] {user}" if user else f"[b]DM:[/] {key}")

    def _active_view_id(self) -> str | None:
        switcher = self._w_msg_switcher
        if switcher is None:
            return None
        cur = switcher.current
        return cur if cur and cur.startswith("msgs-") else None

    def _active_target(self) -> TargetKey | None:
        active = self._active_view_id()
        if active is None:
            return None
        for tkey, vid in self._views.items():
            if vid == active:
                return tkey
        return None

    def _initial_load_count(self) -> int:
        """How many rows to seed into a freshly-mounted message ListView.

        Use the centre pane's actual height when known (so the view
        opens with rows already filling the visible area) and fall back
        to the configured backfill count otherwise."""
        base = self._ui._history_backfill or 30
        switcher = self._w_msg_switcher
        if switcher is None:
            return base
        h = switcher.size.height
        if h <= 0:
            return base
        # Trim a couple of rows for the rounded border + status pane room.
        return max(base, h - 2)

    def _mount_initial_history(self, target: TargetKey, lv: ListView) -> None:
        kind, key = target
        store = self._ui._client._store  # type: ignore[attr-defined]
        n = self._initial_load_count()
        if kind == "dm":
            rows = store.recent_messages(key.upper(), limit=n)
        else:
            try:
                rows = store.recent_posts(int(key), limit=n)
            except ValueError:
                rows = []
        # Mount oldest first, since each `append` puts the row at the bottom.
        rows = list(reversed(rows))
        # Bulk-fetch reactions for the whole backfill in one query so the
        # per-row mount path doesn't spawn N SQLite round trips.
        reactions_by_key = self._bulk_reactions(target, rows)
        with self._batch_mutate():
            for r in rows:
                self._mount_row(
                    target,
                    lv,
                    r,
                    append=True,
                    reactions_by_key=reactions_by_key,
                    defer_scroll=True,
                )
        # The newest row should be visible on first activation. Layout
        # hasn't measured yet during the loop (max_scroll_y is still 0),
        # so schedule the scroll for after the next refresh — one job,
        # not one per row.
        if rows:
            self.call_after_refresh(lv.scroll_end, animate=False)

    def _bulk_reactions(
        self, target: TargetKey, rows: list[dict]
    ) -> dict | None:
        """Pre-fetch all reactions for ``rows`` in a single SQL query.

        Returns a dict shaped for ``_build_row(reactions_by_key=...)``,
        or ``None`` on any error (callers fall back to per-row lookup).
        For DMs the dict is keyed by ``msg_id`` strings; for channel
        posts by integer ``ts``.
        """
        if not rows:
            return {}
        store = self._ui._client._store  # type: ignore[attr-defined]
        kind, key = target
        try:
            if kind == "dm":
                ids = [self._natural_key_for_row(kind, r) for r in rows]
                ids = [i for i in ids if i]
                return store.list_message_emojis_for_ids(ids)
            if kind == "ch":
                ts_list: list[int] = []
                for r in rows:
                    nat = self._natural_key_for_row(kind, r)
                    if not nat:
                        continue
                    try:
                        ts_list.append(int(nat))
                    except ValueError:
                        continue
                return store.list_post_emojis_for_keys(int(key), ts_list)
        except Exception:
            return None
        return None

    async def _reset_message_view(self, target: TargetKey) -> None:
        """Drop every mounted item for ``target`` and re-seed from the
        store. Used on re-subscribe so stale system lines (the prior
        [unsubscribed] notice, /paused hints, etc.) don't linger on top
        of the fresh post history."""
        view_id = self._views.get(target)
        if not view_id:
            return
        try:
            lv = self.query_one(f"#{view_id}", ListView)
        except Exception:
            return
        with self._batch_mutate():
            await lv.clear()
        # `_rows` keys are (kind, target_key, natural_key) — drop every
        # entry for this target so future inbound mounts don't dedupe
        # against orphaned references and miss the row.
        kind, key = target
        for rk in [k for k in self._rows if k[0] == kind and k[1] == key]:
            self._rows.pop(rk, None)
        self._history_exhausted.pop(target, None)
        self._unread.pop(target, None)
        self._mount_initial_history(target, lv)

    async def _load_older(self) -> None:
        target = self._active_target()
        if target is None:
            return
        if self._history_exhausted.get(target):
            return
        kind, key = target
        lv = await self._ensure_message_view(target)
        if not lv.children:
            return
        # Find the oldest MessageRow — a ListView may also contain
        # plain ListItems for system hints (`_write_to_active`), and
        # those don't have a ts to anchor on.
        oldest: MessageRow | None = None
        for child in lv.children:
            if isinstance(child, MessageRow):
                oldest = child
                break
        if oldest is None:
            return
        anchor_natural_key = oldest.natural_key
        before_ts = oldest.ts
        if before_ts is None:
            return
        store = self._ui._client._store  # type: ignore[attr-defined]
        n = self._ui._history_backfill or 30
        if kind == "dm":
            rows = store.recent_messages(key.upper(), limit=n, before_ts=int(before_ts))
        else:
            try:
                rows = store.recent_posts(int(key), limit=n, before_ts=int(before_ts))
            except ValueError:
                rows = []
        if not rows:
            self._history_exhausted[target] = True
            return
        if len(rows) < n:
            self._history_exhausted[target] = True
        # Prepend in chronological order so the result reads
        # oldest-first at the top. ``rows`` came back newest-first; we
        # iterate as-is and mount each at ``before=0`` so each newer
        # row pushes the previous insertion down, leaving the oldest at
        # the top.
        reactions_by_key = self._bulk_reactions(target, rows)
        with self._batch_mutate():
            for r in rows:
                key_tuple = (kind, key, self._natural_key_for_row(kind, r))
                if key_tuple in self._rows:
                    # Defensive: a row we already have mounted shouldn't
                    # be re-prepended (would happen if the cursor query
                    # ever returned an overlap).
                    continue
                new_row = self._build_row(target, r, reactions_by_key=reactions_by_key)
                lv.mount(new_row, before=0)
                self._rows[key_tuple] = new_row
                self._refresh_row_label(new_row)
        # Keep the visual anchor: highlight the row that was at the top
        # before we prepended, so cursor-up at top doesn't slide off.
        for i, child in enumerate(lv.children):
            if isinstance(child, MessageRow) and child.natural_key == anchor_natural_key:
                lv.index = i
                break

    def _natural_key_for_row(self, kind: str, row: dict) -> str:
        if kind == "dm":
            return str(row.get("id") or row.get("_id") or "")
        return str(row.get("ts") or "")

    def _build_row(
        self,
        target: TargetKey,
        row: dict,
        *,
        reactions_by_key: dict | None = None,
    ) -> MessageRow:
        kind, key = target
        natural_key = self._natural_key_for_row(kind, row)
        if reactions_by_key is not None:
            # Bulk-prefetched: avoid the per-row store roundtrip that
            # ``_lookup_reactions`` would otherwise do. The key shape
            # mirrors what the bulk query returns: ``msg_id`` for DMs,
            # ``int(ts)`` for posts.
            if kind == "dm":
                reactions = reactions_by_key.get(natural_key, [])
            elif kind == "ch":
                try:
                    reactions = reactions_by_key.get(int(natural_key), [])
                except (TypeError, ValueError):
                    reactions = []
            else:
                reactions = []
        else:
            reactions = self._lookup_reactions(kind, key, natural_key)
        return MessageRow(
            kind=kind,
            target_key=key,
            natural_key=natural_key,
            from_call=row.get("from_call") or row.get("fc") or "",
            body=row.get("body") or row.get("m") or row.get("p") or "",
            ts=row.get("ts"),
            edit_ts=row.get("edit_ts"),
            delivered_ts=row.get("delivered_ts"),
            received_ts=row.get("received_ts"),
            realtime=row.get("realtime"),
            lid=row.get("lid"),
            reactions=reactions,
        )

    def _lookup_reactions(
        self, kind: str, target_key: str, natural_key: str
    ) -> list[dict]:
        """Pull current reactions from the store keyed by row identity.

        DM rows are keyed by ``msg_id`` (the natural_key); post rows by
        ``(cid, ts)``. Returns ``[]`` on any lookup error so a transient
        store hiccup never breaks row mounting.
        """
        store = self._ui._client._store  # type: ignore[attr-defined]
        try:
            if kind == "dm":
                return store.list_message_emojis(natural_key)
            if kind == "ch":
                return store.list_post_emojis(int(target_key), int(natural_key))
        except Exception:
            return []
        return []

    def _mount_row(
        self,
        target: TargetKey,
        lv: ListView,
        row: dict,
        *,
        append: bool = True,
        reactions_by_key: dict | None = None,
        defer_scroll: bool = False,
    ) -> MessageRow:
        kind, key = target
        new_row = self._build_row(target, row, reactions_by_key=reactions_by_key)
        existing = self._rows.get((kind, key, new_row.natural_key))
        if existing is not None:
            return existing
        if append:
            # Capture stickiness BEFORE the append so a fresh row doesn't
            # itself push us off the bottom and break the check.
            was_at_bottom = lv.is_vertical_scroll_end
            lv.append(new_row)
            # ``defer_scroll`` lets a caller batch-mounting many rows
            # (e.g. ``_mount_initial_history``) suppress per-row scroll
            # scheduling and emit one ``scroll_end`` at the end of the
            # loop. Otherwise N rows = N ``call_after_refresh`` jobs,
            # all racing to put the cursor at the bottom.
            if was_at_bottom and not defer_scroll:
                self.call_after_refresh(lv.scroll_end, animate=False)
        else:
            lv.mount(new_row, before=0)
        self._rows[(kind, key, new_row.natural_key)] = new_row
        self._refresh_row_label(new_row)
        return new_row

    def _refresh_row_label(self, row: MessageRow) -> None:
        row.refresh_label(
            my_call=self._ui._my_call,
            verbose=self._ui._options.verbose_history,
            ham_name=self._ui._client.ham_name,
            delivery_timeout_s=self._ui._options.delivery_timeout_s,
        )

    def _refresh_active_rows(self) -> None:
        """Refresh only rows belonging to the currently-active target.

        Inactive views are repainted lazily on activation — see
        ``_switch_centre_to``, which consults ``_verbose_dirty`` and
        plays catch-up there. Walking every row in every view on Ctrl+D
        was the original behaviour and dominated CPU on slow hardware
        once the user had bounced through several channels in a
        session.
        """
        target = self._active_target()
        if target is None:
            return
        kind, key = target
        for (k, t, _), row in self._rows.items():
            if k == kind and t == key:
                self._refresh_row_label(row)

    # ------------------------------------------------------------------
    # Online users
    # ------------------------------------------------------------------

    def _refresh_online_pane(self, users: list[str]) -> None:
        """Incrementally reconcile the online ListView with ``users``.

        Originally this cleared and rebuilt the whole ListView on every
        ``uc`` / ``ud`` / ``o`` / ``he`` event — fine with a few users,
        a major hot path with 100+ users + churn (one full unmount /
        remount per join or part).

        The diff here:
          * drops items for callsigns no longer in ``users``,
          * appends items for new callsigns at the end,
          * leaves retained callsigns mounted untouched, only updating
            their label when the resolved display name changed (this
            matters for ``he`` events, which can rewrite names without
            changing the roster).

        Callsigns are case-insensitive so we normalise to upper-case
        keys to avoid mounting a duplicate when the same call comes
        back with different casing.
        """
        lv = self._w_online
        header = self._w_online_header
        if lv is None or header is None:
            return
        wanted_norm: list[str] = []
        seen: set[str] = set()
        for call in users:
            up = call.upper()
            if up in seen:
                continue
            seen.add(up)
            wanted_norm.append(up)
        wanted_set = set(wanted_norm)
        current_set = set(self._online_items.keys())
        ham_name = self._ui._client.ham_name

        # Drop items for users who logged off.
        for call in current_set - wanted_set:
            item = self._online_items.pop(call, None)
            self._online_label_cache.pop(call, None)
            if item is not None:
                try:
                    item.remove()
                except Exception:
                    pass

        # Add items for fresh joins. Append in the order they appear in
        # ``wanted_norm`` so the visual order matches the server's.
        for call in wanted_norm:
            if call in self._online_items:
                continue
            label = _fmt_user(call, ham_name)
            self._online_label_cache[call] = label
            item = ListItem(Static(label, markup=True))
            self._online_items[call] = item
            lv.append(item)

        # Retained users: only refresh labels when name resolution
        # actually changed (an ``he`` event for that callsign).
        for call in (current_set & wanted_set):
            new_label = _fmt_user(call, ham_name)
            if new_label != self._online_label_cache.get(call):
                self._online_label_cache[call] = new_label
                item = self._online_items.get(call)
                if item is not None:
                    try:
                        item.query_one(Static).update(new_label)
                    except Exception:
                        pass

        header.update(f"Online ({len(wanted_norm)})")

    def _schedule_he_refresh(self) -> None:
        """Arm a short debounce timer for the post-``he`` repaint.

        Connect-time bursts of ``he`` events would otherwise drive one
        full online-pane diff + per-DM-target relabel + thread-header
        refresh per arrival. Collapsing into a single deferred refresh
        keeps the cost linear in (hams + DMs) instead of quadratic.
        Subsequent calls while a refresh is pending are no-ops.
        """
        if self._he_refresh_pending:
            return
        self._he_refresh_pending = True
        self.set_timer(0.05, self._do_he_refresh)

    def _do_he_refresh(self) -> None:
        self._he_refresh_pending = False
        self._refresh_online_pane(self._ui._client.online_users())
        seen: set[TargetKey] = set()
        for tkey in list(self._unread.keys()) + list(self._views.keys()):
            if tkey[0] == "dm" and tkey not in seen:
                seen.add(tkey)
                self._refresh_target_label(tkey)
        self._refresh_thread_header()

    # ------------------------------------------------------------------
    # Status pane (Ctrl+S)
    # ------------------------------------------------------------------

    def action_toggle_status(self) -> None:
        pane = self._w_status
        if pane is None:
            return
        pane.styles.display = "none" if pane.display else "block"

    def _status_visible(self) -> bool:
        pane = self._w_status
        return bool(pane.display) if pane is not None else False

    def _status_write(self, line: str) -> None:
        pane = self._w_status
        if pane is None:
            return
        pane.write(line)

    def _status_error(self, line: str) -> None:
        """Write a user-facing error / warning to the status pane and
        force it visible. Slash-command argument validation, async
        worker exceptions and other error paths funnel through here so
        the user notices without polluting their conversation thread."""
        pane = self._w_status
        if pane is None:
            return
        pane.write(line)
        if not pane.display:
            pane.styles.display = "block"

    def _refuse_offline(self, what: str) -> bool:
        """If running offline, surface a one-line warning and return ``True``.

        Used as an early-out guard in send paths and any slash command
        that needs the wire link. Read-only paths (history, listings,
        target switching) skip this check entirely.
        """
        if not self._ui._offline:
            return False
        self._status_error(
            f"[yellow][offline][/] {what} unavailable — read-only mode "
            f"(no connection)"
        )
        return True

    # ------------------------------------------------------------------
    # Verbose toggle (Ctrl+D) and help (Ctrl+H)
    # ------------------------------------------------------------------

    def action_toggle_verbose(self) -> None:
        opts = self._ui._options
        opts.verbose_history = not opts.verbose_history
        # Mark every other target as dirty so its rows get repainted
        # next time it's activated; refresh only the visible target's
        # rows now.
        active = self._active_target()
        self._verbose_dirty = {t for t in self._views if t != active}
        self._refresh_active_rows()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_options(self) -> None:
        self._open_settings_modal()

    def action_insert_emoji(self) -> None:
        debounce = self._ui._options.tui_emoji_search_debounce_ms

        async def _run() -> None:
            picked = await self.push_screen_wait(EmojiPrompt(debounce_ms=debounce))
            if not picked:
                return
            # Hex codepoint fallback (for terminals that can't render
            # the picker grid) — convert `1f44d` → 👍 before inserting
            # so the body carries a literal emoji, not the hex digits.
            if re.fullmatch(r"[0-9a-fA-F]{4,6}", picked):
                try:
                    picked = chr(int(picked, 16))
                except (ValueError, OverflowError):
                    pass
            try:
                inp = self.query_one("#input", Input)
            except Exception:
                return
            pos = inp.cursor_position
            inp.value = inp.value[:pos] + picked + inp.value[pos:]
            inp.cursor_position = pos + len(picked)
            inp.focus()

        self.run_worker(_run(), exclusive=False)

    # ------------------------------------------------------------------
    # Focus management
    # ------------------------------------------------------------------

    def action_focus_input(self) -> None:
        if self._w_input is not None:
            self._w_input.focus()

    def _focus_target_for_step(self, step: str) -> Any | None:
        if step == "input":
            return self._w_input
        if step == "msg-active":
            active = self._active_view_id()
            if active:
                return self.query_one(f"#{active}", ListView)
            return None
        if step == "tab-strip":
            return self._w_tabs
        if step == "target-active":
            switcher = self._w_target_switcher
            if switcher is None:
                return None
            current_id = switcher.current
            if current_id:
                return self.query_one(f"#{current_id}", ListView)
            return None
        if step == "online":
            return self._w_online
        return None

    def _focus_step(self, delta: int) -> None:
        focused = self.focused
        focused_id = focused.id if focused else None
        cycle = _FOCUS_CYCLE
        # Find current position; tab-strip / lists may be matched by id.
        active_msg = self._active_view_id()
        active_target = self._w_target_switcher.current if self._w_target_switcher is not None else None
        try:
            if focused_id == "input":
                idx = cycle.index("input")
            elif focused_id == active_msg:
                idx = cycle.index("msg-active")
            elif focused_id == "tab-strip":
                idx = cycle.index("tab-strip")
            elif focused_id == active_target:
                idx = cycle.index("target-active")
            elif focused_id == "online":
                idx = cycle.index("online")
            else:
                idx = -1
        except ValueError:
            idx = -1
        for offset in range(1, len(cycle) + 1):
            step = cycle[(idx + delta * offset) % len(cycle)]
            target = self._focus_target_for_step(step)
            if target is not None:
                target.focus()
                return

    def action_focus_next_pane(self) -> None:
        self._focus_step(+1)

    def action_focus_prev_pane(self) -> None:
        self._focus_step(-1)

    # ------------------------------------------------------------------
    # Quit / link teardown
    # ------------------------------------------------------------------

    def action_quit_app(self) -> None:
        if self._quit_confirm_open:
            return
        self._quit_confirm_open = True

        async def _run() -> None:
            try:
                confirmed = await self.push_screen_wait(QuitConfirmModal())
            finally:
                self._quit_confirm_open = False
            if not confirmed:
                return
            try:
                await self._ui._client.close()
            finally:
                self.exit()

        self.run_worker(_run(), exclusive=False)

    def _signal_terminal_link_loss(self) -> None:
        if self._ui.exit_reason is not None:
            return
        self._ui.exit_reason = "terminal"
        try:
            self.exit()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers — channel name lookup, prompt, paused/subscribe hints
    # ------------------------------------------------------------------

    def _channel_name(self, cid: int) -> str | None:
        for c in self._ui._channels:
            if c.cid == cid and c.name:
                return c.name
        return None

    def _channel_ref(self, cid: int) -> str:
        name = self._channel_name(cid)
        return f"ch {cid} #{name}" if name else f"ch {cid}"

    def _refresh_prompt(self) -> None:
        try:
            inp = self.query_one("#input", Input)
        except Exception:
            return
        if self._pending_edit:
            kind = self._pending_edit["kind"]
            inp.placeholder = f"editing {kind}> "
            return
        if self._ui._target is None:
            inp.placeholder = f"{self._ui._my_call}> "
        else:
            kind, key = self._ui._target
            if kind == "ch":
                try:
                    cid = int(key)
                except ValueError:
                    inp.placeholder = f"{key}> "
                else:
                    name = self._channel_name(cid)
                    inp.placeholder = f"{cid} #{name}> " if name else f"{cid}> "
            else:
                inp.placeholder = f"{kind}:{key}> "

    def _is_subscribed(self, cid: int) -> bool:
        cids = self._subscribed_cids
        if cids is None:
            try:
                cids = {
                    r["cid"]
                    for r in self._ui._client._store.list_channels()  # type: ignore[attr-defined]
                    if r.get("subscribed")
                }
            except Exception:
                cids = set()
            self._subscribed_cids = cids
        return cid in cids

    def _invalidate_subscribed_cids(self) -> None:
        self._subscribed_cids = None

    def _maybe_print_paused_hint(self, cid: int) -> None:
        paused = self._ui._client.paused_channels().get(cid)
        if not paused:
            return
        self._write_to_active(self._paused_hint(cid, paused))

    def _paused_hint(self, cid: int, paused: int) -> str:
        return (
            f"[yellow]{self._channel_ref(cid)} is paused — {paused} posts "
            f"waiting on the server. Run /unpause {cid} [N] to download "
            f"them. Posting is blocked until you unpause.[/]"
        )

    def _unsubscribed_send_hint(self, cid: int) -> str:
        return (
            f"[yellow][{self._channel_ref(cid)}][/] not subscribed — posting "
            f"is blocked (server only relays new posts to subscribers, so "
            f"you'd never see replies)."
        )

    def _open_subscribe_modal(
        self,
        cid: int,
        *,
        target: TargetKey,
        skip_confirm: bool = False,
    ) -> None:
        """Push the two-stage subscribe modal for an unsubscribed channel.

        On confirm + count answered, switches the centre pane and target
        to the freshly-subscribed channel. On dismiss-without-subscribe,
        the centre pane and target stay on whatever was previously
        active — the caller hasn't switched yet by the time this runs.

        ``skip_confirm`` skips the Y/N stage and goes straight to the
        subscribe + count flow — used by ``/sub CID`` since the user has
        already opted in by typing the command."""
        if self._refuse_offline("subscribing"):
            return
        c = self._ui._client

        async def on_confirm() -> int:
            return await c.subscribe_and_wait(cid)

        def default_count_for(pc: int) -> int:
            return min(c.auto_backfill_post_count or 10, pc)

        async def _run() -> None:
            result = await self.push_screen_wait(
                SubscribeModal(
                    channel_ref=self._channel_ref(cid),
                    on_confirm=on_confirm,
                    default_count_for=default_count_for,
                    skip_confirm=skip_confirm,
                )
            )
            if result is None:
                # User declined — leave centre pane / target untouched.
                return
            # Switch the centre pane to the new channel BEFORE requesting
            # the post batch. The cpb response races with the switch — if
            # posts arrive while the old target is still active,
            # `_handle_inbound_post` early-returns (bump unread, no mount)
            # and the user sees the unread count but no posts in the pane.
            # Switching first guarantees `active == target` when cpb lands.
            self._ui._target = target
            self._add_target(target)
            await self._switch_centre_to(target)
            self._refresh_prompt()
            self._refresh_footer()
            try:
                if result > 0:
                    await c.request_post_batch(cid, result)
            except Exception as exc:
                self._status_error(f"[red][error][/] {exc}")
            try:
                self.query_one("#input", Input).focus()
            except Exception:
                pass

        self.run_worker(_run(), exclusive=False)

    def _open_new_dm_modal(self) -> None:
        """Prompt for a callsign and open or switch to that DM thread.

        Esc / empty input is a no-op. Existing threads simply get
        activated; brand-new ones get a fresh (empty) message view so the
        user can start typing right away."""

        async def _run() -> None:
            call = await self.push_screen_wait(NewDmModal())
            if not call:
                return
            target: TargetKey = ("dm", call)
            self._ui._target = target
            self._add_target(target)
            await self._switch_centre_to(target)
            self._refresh_prompt()
            self._refresh_footer()
            try:
                self.query_one("#input", Input).focus()
            except Exception:
                pass

        self.run_worker(_run(), exclusive=False)

    def action_unsub_channel(self) -> None:
        """Ctrl+U — confirm-and-unsubscribe the active channel target.

        Operates on the centre-pane's active target regardless of focus
        — the only channel the user can plausibly mean is the one they're
        currently looking at. The footer hides this binding unless the
        active target is a subscribed channel, so reaching this handler
        with anything else is a defensive no-op. The server's ``cs`` ack
        flips the checkbox label back to ☐ via ``_handle_cs``."""
        cid = self._active_subscribed_channel_cid()
        if cid is None:
            return
        self._open_unsubscribe_modal(cid)

    def _active_subscribed_channel_cid(self) -> int | None:
        target = self._ui._target
        if target is None or target[0] != "ch":
            return None
        try:
            cid = int(target[1])
        except ValueError:
            return None
        if not self._is_subscribed(cid):
            return None
        return cid

    def _active_target_is_subscribed_channel(self) -> bool:
        return self._active_subscribed_channel_cid() is not None

    def _refresh_footer(self) -> None:
        """Recompose the footer so conditional bindings (ctrl+u) update.

        Call after any change that affects whether the active target is
        a subscribed channel: target switch, subscribe/unsubscribe ack."""
        try:
            footer = self.query_one(StaticFooter)
        except Exception:
            return
        footer.refresh(recompose=True)

    def _open_unsubscribe_modal(self, cid: int) -> None:
        if self._refuse_offline("unsubscribing"):
            return
        c = self._ui._client

        async def _run() -> None:
            confirmed = await self.push_screen_wait(
                UnsubscribeModal(channel_ref=self._channel_ref(cid))
            )
            if not confirmed:
                return
            try:
                await c.unsubscribe(cid)
            except Exception as exc:
                self._status_error(f"[red][error][/] unsubscribe: {exc}")

        self.run_worker(_run(), exclusive=False)

    # ------------------------------------------------------------------
    # Status / system messages — go into the active view as ListItems
    # ------------------------------------------------------------------

    def _write_to_active(self, line: str) -> None:
        """System / hint line → write into the active centre ListView as
        a non-message ListItem so it stays in the conversation context."""
        active = self._active_view_id()
        if active is None:
            return
        try:
            lv = self.query_one(f"#{active}", ListView)
        except Exception:
            return
        was_at_bottom = lv.is_vertical_scroll_end
        lv.append(ListItem(Static(line, markup=True)))
        if was_at_bottom:
            self.call_after_refresh(lv.scroll_end, animate=False)

    # ------------------------------------------------------------------
    # Event rendering — called from the client's reader task
    # ------------------------------------------------------------------

    async def _dispatch_event(self, obj: dict) -> None:
        t = obj.get("t")
        # DM / batched DM
        if t == "m":
            await self._handle_inbound_dm(obj, batched=False)
        elif t == "mb":
            await self._handle_inbound_dm_batch(list(obj.get("m", [])))
        # Post / batched post
        elif t == "cp":
            await self._handle_inbound_post(obj, batched=False)
        elif t == "cpb":
            cid = obj.get("cid")
            if cid is None:
                return
            await self._handle_inbound_post_batch(cid, list(obj.get("p", [])))
        # Acks
        elif t == "mr":
            self._handle_dm_ack(obj)
        elif t == "cpr":
            self._handle_post_ack(obj)
        # Edits
        elif t == "med":
            self._handle_dm_edit(obj)
        elif t == "cped":
            self._handle_post_edit(obj)
        # Reactions
        elif t == "mem":
            self._handle_dm_reaction(obj)
        elif t == "memb":
            self._handle_dm_reaction_batch(list(obj.get("mem", [])))
        elif t == "cpem":
            self._handle_post_reaction(obj)
        elif t == "cpemb":
            self._handle_post_reaction_groups(list(obj.get("e", [])))
        # Roster
        elif t == "uc":
            self._refresh_online_pane(self._ui._client.online_users())
            if self._status_visible():
                self._status_write(
                    f"[user] {_fmt_user(obj.get('c'), self._ui._client.ham_name)} connected"
                )
        elif t == "ud":
            self._refresh_online_pane(self._ui._client.online_users())
            if self._status_visible():
                self._status_write(
                    f"[user] {_fmt_user(obj.get('c'), self._ui._client.ham_name)} disconnected"
                )
        elif t == "o":
            self._refresh_online_pane(self._ui._client.online_users())
        elif t == "he":
            self._schedule_he_refresh()
        # Channel state / paused
        elif t == "cs":
            await self._handle_cs(obj)
        elif t == "pch":
            for ch in obj.get("ch", []):
                cid = ch.get("cid")
                ref = self._channel_ref(int(cid)) if cid is not None else "ch"
                self._write_to_active(
                    f"[yellow][paused {ref}][/] {ch.get('pt')} pending posts "
                    f"— /unpause {cid} [N] to download"
                )
        # Connect / disconnect (link state)
        elif t == "c" and "n" not in obj:
            self._write_to_active(
                f"[connect] mc={obj.get('mc', 0)} pc={obj.get('pc', 0)} v={obj.get('v')}"
            )
        elif t == "_disconnect":
            line = f"[red][link][/] disconnected ({obj.get('reason', '')})"
            self._write_to_active(line)
            if self._status_visible():
                self._status_write(line)
            if not self._ui._client.is_auto_reconnect:
                self._signal_terminal_link_loss()
        elif t == "_reconnecting":
            line = (
                f"[yellow][link][/] reconnect attempt {obj.get('attempt')} in "
                f"{obj.get('delay'):.1f}s"
            )
            self._write_to_active(line)
            if self._status_visible():
                self._status_write(line)
        elif t == "_reconnect_failed":
            line = (
                f"[yellow][link][/] reconnect attempt {obj.get('attempt')} failed: "
                f"{obj.get('exc')}"
            )
            self._write_to_active(line)
            if self._status_visible():
                self._status_write(line)
        elif t == "_reconnected":
            line = f"[green][link][/] reconnected (attempt {obj.get('attempt')})"
            self._write_to_active(line)
            if self._status_visible():
                self._status_write(line)
            # Server-side subscription state may have shifted across the
            # link drop; rebuild the cache lazily on next consult.
            self._invalidate_subscribed_cids()
        elif t == "_reconnect_giveup":
            line = (
                f"[red][link][/] giving up after {obj.get('attempts')} "
                "reconnect attempts"
            )
            self._write_to_active(line)
            if self._status_visible():
                self._status_write(line)
            self._signal_terminal_link_loss()
        elif t == "_error":
            self._status_error(f"[red][error][/] {obj.get('exc')}")
        elif t == "_delivery_timeout":
            self._handle_delivery_timeout(obj)

    async def _handle_inbound_dm_batch(self, items: list[dict]) -> None:
        """Process a wire ``mb`` (batch DM) payload.

        Groups items by peer, opens one ``_batch_mutate`` block per
        target, bulk-fetches reactions for the whole active-target
        group in a single SQL query, and coalesces unread bumps + label
        refreshes per target. Replaces the original loop-of-handlers
        approach which paid the LV-resolve + label-refresh cost per
        item, plus one SQLite round trip per row for reactions.
        """
        if not items:
            return
        by_peer: dict[TargetKey, list[dict]] = {}
        for m in items:
            fc = m.get("fc")
            tc = m.get("tc")
            peer = tc if fc == self._ui._my_call else fc
            if not peer:
                continue
            target: TargetKey = ("dm", peer)
            by_peer.setdefault(target, []).append(m)
        active = self._active_target()
        for target, group in by_peer.items():
            self._add_target(target)
            if active != target:
                # Inactive target: just bump unread by the group size and
                # refresh the label once. Rows page in from the store
                # on activation.
                self._unread[target] = self._unread.get(target, 0) + len(group)
                self._refresh_target_label(target)
                continue
            # Active target: resolve store rows for each item once, then
            # bulk-fetch reactions for the whole group in one query
            # before mounting.
            store_rows: list[dict] = []
            for m in group:
                msg_id = m.get("_id") or (
                    f"{m.get('ts')}-{m.get('fc')}"
                    if m.get("ts") and m.get("fc")
                    else None
                )
                row = None
                if msg_id:
                    try:
                        row = self._ui._client._store.lookup_message_by_id(msg_id)  # type: ignore[attr-defined]
                    except Exception:
                        row = None
                if row is None:
                    row = {
                        "id": msg_id,
                        "from_call": m.get("fc"),
                        "to_call": m.get("tc"),
                        "body": m.get("m"),
                        "ts": m.get("ts"),
                        "edit_ts": m.get("edts"),
                        "lid": None,
                    }
                store_rows.append(row)
            lv = await self._ensure_message_view(target)
            reactions_by_key = self._bulk_reactions(target, store_rows)
            with self._batch_mutate():
                for r in store_rows:
                    self._mount_row(
                        target, lv, r, append=True,
                        reactions_by_key=reactions_by_key,
                    )

    async def _handle_inbound_post_batch(self, cid: int, items: list[dict]) -> None:
        """Process a wire ``cpb`` (batch posts) payload for one channel.

        ``cpb`` carries one ``cid`` and a list of posts under ``p`` —
        already grouped by target, so we just resolve the LV once,
        bulk-prefetch reactions, and mount.
        """
        if not items:
            return
        target: TargetKey = ("ch", str(cid))
        self._add_target(target)
        active = self._active_target()
        if active != target:
            self._unread[target] = self._unread.get(target, 0) + len(items)
            self._refresh_target_label(target)
            return
        store_rows: list[dict] = []
        for p in items:
            ts = p.get("ts")
            row = None
            if isinstance(ts, int):
                try:
                    row = self._ui._client._store.lookup_post(int(cid), int(ts))  # type: ignore[attr-defined]
                except Exception:
                    row = None
            if row is None:
                row = {
                    "channel_id": cid,
                    "from_call": p.get("fc"),
                    "body": p.get("p"),
                    "ts": ts,
                    "edit_ts": p.get("edts"),
                    "lid": None,
                }
            store_rows.append(row)
        lv = await self._ensure_message_view(target)
        reactions_by_key = self._bulk_reactions(target, store_rows)
        with self._batch_mutate():
            for r in store_rows:
                self._mount_row(
                    target, lv, r, append=True,
                    reactions_by_key=reactions_by_key,
                )

    async def _handle_inbound_dm(self, m: dict, *, batched: bool) -> None:
        fc = m.get("fc")
        tc = m.get("tc")
        peer = tc if fc == self._ui._my_call else fc
        if not peer:
            return
        target: TargetKey = ("dm", peer)
        self._add_target(target)
        active = self._active_target()
        if active != target:
            # Inactive target → bump unread, do NOT mount the row. The
            # row will be paged in from the store on activation.
            self._unread[target] = self._unread.get(target, 0) + 1
            self._refresh_target_label(target)
            return
        # Active target → mount the row. Look up the persisted row from
        # the store (by id) so we get the same fields the verbose render
        # path expects.
        msg_id = m.get("_id") or (
            f"{m.get('ts')}-{m.get('fc')}" if m.get("ts") and m.get("fc") else None
        )
        row = None
        if msg_id:
            try:
                row = self._ui._client._store.lookup_message_by_id(msg_id)  # type: ignore[attr-defined]
            except Exception:
                row = None
        if row is None:
            row = {
                "id": msg_id,
                "from_call": fc,
                "to_call": tc,
                "body": m.get("m"),
                "ts": m.get("ts"),
                "edit_ts": m.get("edts"),
                "lid": None,
            }
        lv = await self._ensure_message_view(target)
        with self._batch_mutate():
            self._mount_row(target, lv, row, append=True)

    async def _handle_inbound_post(self, p: dict, *, batched: bool) -> None:
        cid = p.get("cid")
        if cid is None:
            return
        target: TargetKey = ("ch", str(cid))
        self._add_target(target)
        active = self._active_target()
        if active != target:
            self._unread[target] = self._unread.get(target, 0) + 1
            self._refresh_target_label(target)
            return
        ts = p.get("ts")
        row = None
        if isinstance(ts, int):
            try:
                row = self._ui._client._store.lookup_post(int(cid), int(ts))  # type: ignore[attr-defined]
            except Exception:
                row = None
        if row is None:
            row = {
                "channel_id": cid,
                "from_call": p.get("fc"),
                "body": p.get("p"),
                "ts": ts,
                "edit_ts": p.get("edts"),
                "lid": None,
            }
        lv = await self._ensure_message_view(target)
        with self._batch_mutate():
            self._mount_row(target, lv, row, append=True)

    def _handle_dm_ack(self, obj: dict) -> None:
        msg_id = obj.get("_id")
        if not isinstance(msg_id, str):
            return
        try:
            row = self._ui._client._store.lookup_message_by_id(msg_id)  # type: ignore[attr-defined]
        except Exception:
            row = None
        if row is None:
            return
        peer = row.get("to_call") or row.get("from_call") or ""
        target = ("dm", str(peer))
        rk = (target[0], target[1], msg_id)
        msg_row = self._rows.get(rk)
        if msg_row is not None:
            msg_row.delivered_ts = row.get("delivered_ts") or int(time.time() * 1000)
            self._refresh_row_label(msg_row)
        if self._status_visible():
            ts_ms = ts_to_ms(row.get("ts"))
            now = int(time.time() * 1000)
            duration = _fmt_duration_ms(now - ts_ms) if ts_ms else "?"
            label = self._fmt_target_label(target)
            self._status_write(
                f"[green][ack][/] {label} msg {row.get('lid')} delivered in {duration}"
            )

    def _handle_post_ack(self, obj: dict) -> None:
        ts = obj.get("ts")
        dts = obj.get("dts")
        if not isinstance(ts, int):
            return
        try:
            row = self._ui._client._store.lookup_post_by_from_ts(  # type: ignore[attr-defined]
                self._ui._my_call, ts
            )
        except Exception:
            row = None
        if row is None:
            return
        cid = row.get("channel_id")
        target = ("ch", str(cid))
        rk = (target[0], target[1], str(ts))
        msg_row = self._rows.get(rk)
        if msg_row is not None:
            msg_row.delivered_ts = (
                int(dts) if isinstance(dts, int) else int(time.time() * 1000)
            )
            self._refresh_row_label(msg_row)
        if self._status_visible():
            ts_ms = ts_to_ms(ts)
            end = ts_to_ms(dts) if isinstance(dts, int) else int(time.time() * 1000)
            duration = _fmt_duration_ms(end - ts_ms) if ts_ms else "?"
            label = self._fmt_target_label(target)
            self._status_write(
                f"[green][ack][/] {label} post {row.get('lid')} delivered in {duration}"
            )

    def _handle_dm_edit(self, obj: dict, *, clear_delivered: bool = False) -> None:
        msg_id = obj.get("_id")
        if not isinstance(msg_id, str):
            return
        try:
            row = self._ui._client._store.lookup_message_by_id(msg_id)  # type: ignore[attr-defined]
        except Exception:
            row = None
        if row is None:
            return
        peer = row.get("to_call") or row.get("from_call") or ""
        target = ("dm", str(peer))
        rk = (target[0], target[1], msg_id)
        msg_row = self._rows.get(rk)
        body = obj.get("m", row.get("body"))
        edts = obj.get("edts") or row.get("edit_ts")
        if msg_row is not None:
            msg_row.body = body or ""
            msg_row.edit_ts = edts
            # When *we* edit our own DM, dim the row until `mr` re-acks
            # the edit (the original send's delivered_ts was already
            # set, so clear it so `_render_row` re-applies the [dim]).
            if clear_delivered:
                msg_row.delivered_ts = None
            self._refresh_row_label(msg_row)
        if self._status_visible():
            self._status_write(
                f"[blue][edit][/] {self._fmt_target_label(target)} msg "
                f"{row.get('lid')} edited"
            )

    def _handle_post_edit(self, obj: dict, *, clear_delivered: bool = False) -> None:
        cid = obj.get("cid")
        ts = obj.get("ts")
        if not isinstance(cid, int) or not isinstance(ts, int):
            return
        try:
            row = self._ui._client._store.lookup_post(int(cid), int(ts))  # type: ignore[attr-defined]
        except Exception:
            row = None
        if row is None:
            return
        target = ("ch", str(cid))
        rk = (target[0], target[1], str(ts))
        msg_row = self._rows.get(rk)
        body = obj.get("p", row.get("body"))
        edts = obj.get("edts") or row.get("edit_ts")
        if msg_row is not None:
            msg_row.body = body or ""
            msg_row.edit_ts = edts
            # See comment in `_handle_dm_edit`: clear delivered_ts on
            # our own edits so the row dims until `cpr` re-acks.
            if clear_delivered:
                msg_row.delivered_ts = None
            self._refresh_row_label(msg_row)
        if self._status_visible():
            self._status_write(
                f"[blue][edit][/] {self._fmt_target_label(target)} post "
                f"{row.get('lid')} edited"
            )

    def _handle_dm_reaction(self, obj: dict) -> None:
        """Refresh the reaction tail on the matching DM row.

        The client-layer handler has already persisted the new state;
        this just re-pulls the per-row list and re-renders. ``mem``
        events for messages we don't have rendered (different active
        target, not yet paged in from the store) are no-ops — the
        reactions will be picked up when that row is mounted later.
        """
        msg_id = obj.get("_id")
        if not isinstance(msg_id, str):
            return
        try:
            row = self._ui._client._store.lookup_message_by_id(msg_id)  # type: ignore[attr-defined]
        except Exception:
            row = None
        if row is None:
            return
        peer = row.get("to_call") or row.get("from_call") or ""
        rk = ("dm", str(peer), msg_id)
        msg_row = self._rows.get(rk)
        if msg_row is None:
            return
        msg_row.reactions = self._lookup_reactions("dm", str(peer), msg_id)
        self._refresh_row_label(msg_row)

    def _handle_post_reaction(self, obj: dict) -> None:
        """Refresh the reaction tail on the matching post row."""
        cid = obj.get("cid")
        ts = obj.get("ts")
        if not isinstance(cid, int) or not isinstance(ts, int):
            return
        rk = ("ch", str(cid), str(ts))
        msg_row = self._rows.get(rk)
        if msg_row is None:
            return
        msg_row.reactions = self._lookup_reactions("ch", str(cid), str(ts))
        self._refresh_row_label(msg_row)

    def _handle_post_reaction_batch(self, group: dict) -> None:
        cid = group.get("cid")
        ts = group.get("ts")
        if not isinstance(cid, int) or not isinstance(ts, int):
            return
        rk = ("ch", str(cid), str(ts))
        msg_row = self._rows.get(rk)
        if msg_row is None:
            return
        msg_row.reactions = self._lookup_reactions("ch", str(cid), str(ts))
        self._refresh_row_label(msg_row)

    def _handle_dm_reaction_batch(self, items: list[dict]) -> None:
        """Apply a wire ``memb`` (batch DM reaction) payload.

        Resolves each entry's peer (one ``lookup_message_by_id`` per
        item) but bulk-fetches the reaction lists in a single SQL
        query for items whose row is currently mounted.
        """
        if not items:
            return
        # Resolve mounted rows + their peers up front so we know what
        # we'll actually need to refresh.
        targets: list[tuple[MessageRow, str, str]] = []
        msg_ids: list[str] = []
        for obj in items:
            msg_id = obj.get("_id")
            if not isinstance(msg_id, str):
                continue
            try:
                row = self._ui._client._store.lookup_message_by_id(msg_id)  # type: ignore[attr-defined]
            except Exception:
                row = None
            if row is None:
                continue
            peer = str(row.get("to_call") or row.get("from_call") or "")
            rk = ("dm", peer, msg_id)
            msg_row = self._rows.get(rk)
            if msg_row is None:
                continue
            targets.append((msg_row, peer, msg_id))
            msg_ids.append(msg_id)
        if not targets:
            return
        try:
            bulk = self._ui._client._store.list_message_emojis_for_ids(msg_ids)  # type: ignore[attr-defined]
        except Exception:
            bulk = None
        for msg_row, _peer, msg_id in targets:
            if bulk is not None:
                msg_row.reactions = list(bulk.get(msg_id, []))
            else:
                msg_row.reactions = self._lookup_reactions(
                    "dm", _peer, msg_id
                )
            self._refresh_row_label(msg_row)

    def _handle_post_reaction_groups(self, groups: list[dict]) -> None:
        """Apply a wire ``cpemb`` (batch post-reaction) payload.

        ``cpemb`` carries a list of ``{cid, ts, e:[...], ets}`` groups.
        We group by ``cid`` so that each channel's reactions are
        prefetched in a single SQL query.
        """
        if not groups:
            return
        by_cid: dict[int, list[int]] = {}
        valid: list[tuple[int, int]] = []
        for g in groups:
            cid = g.get("cid")
            ts = g.get("ts")
            if not isinstance(cid, int) or not isinstance(ts, int):
                continue
            valid.append((cid, ts))
            by_cid.setdefault(cid, []).append(ts)
        if not valid:
            return
        # Bulk-fetch reactions per cid.
        bulk_by_cid: dict[int, dict[int, list[dict]]] = {}
        for cid, ts_list in by_cid.items():
            try:
                bulk_by_cid[cid] = self._ui._client._store.list_post_emojis_for_keys(  # type: ignore[attr-defined]
                    cid, ts_list
                )
            except Exception:
                bulk_by_cid[cid] = {}
        for cid, ts in valid:
            rk = ("ch", str(cid), str(ts))
            msg_row = self._rows.get(rk)
            if msg_row is None:
                continue
            msg_row.reactions = list(bulk_by_cid.get(cid, {}).get(int(ts), []))
            self._refresh_row_label(msg_row)

    async def _handle_cs(self, obj: dict) -> None:
        cid = obj.get("cid")
        subscribed = bool(obj.get("s"))
        pc = obj.get("pc")
        # Server confirmed a subscription state change — drop the cache so
        # the next ``_is_subscribed`` reload picks up the new row.
        self._invalidate_subscribed_cids()
        if cid is not None:
            name = self._channel_name(int(cid))
            label = f"{cid} #{name}" if name else f"{cid}"
        else:
            label = "channel"
        verb = "Subscribed to" if subscribed else "Unsubscribed from"
        if cid is not None and subscribed:
            # Re-subscribe: drop any stale per-target system lines
            # (paused hints, send errors, etc.) and re-seed from the
            # store so the user lands on fresh history.
            await self._reset_message_view(("ch", str(cid)))
        if subscribed and isinstance(pc, int) and pc > 0:
            self._status_write(f"{verb} {label} ({pc} historic posts on server)")
        else:
            self._status_write(f"{verb} {label}")
        if cid is not None:
            target = ("ch", str(cid))
            if obj.get("s"):
                self._add_target(target)
            self._refresh_target_label(target)
            # Subscription state of this channel just changed — if it's
            # the active target, the ctrl+u footer entry needs to flip.
            if self._ui._target == target:
                self._refresh_footer()

    def _handle_delivery_timeout(self, obj: dict) -> None:
        kind = obj.get("kind")
        lid = obj.get("lid")
        ts = _fmt_ts(obj.get("ts")) if obj.get("ts") is not None else "[--]"
        edit_tag = " (edit)" if obj.get("is_edit") else ""
        if kind == "post":
            cid = obj.get("cid")
            ref = self._channel_ref(int(cid)) if isinstance(cid, int) else f"ch:{cid}"
            line = (
                f"[red][timeout][/] [{ref}] post {lid}{edit_tag} at {ts}. "
                f"To resend: /retrypost {lid}"
            )
        else:
            peer = obj.get("peer")
            line = (
                f"[red][timeout][/] [dm:{peer}] msg {lid}{edit_tag} at {ts}. "
                f"To resend: /retrydm {lid}"
            )
        self._status_error(line)

    def _fmt_target_label(self, target: TargetKey) -> str:
        kind, key = target
        if kind == "ch":
            try:
                cid = int(key)
            except ValueError:
                return f"ch:{key}"
            name = self._channel_name(cid)
            return f"ch:{cid} #{name}" if name else f"ch:{cid}"
        return f"dm:{key}"

    # ------------------------------------------------------------------
    # Action menu (Enter on a message row)
    # ------------------------------------------------------------------

    def _open_action_menu(self, row: MessageRow) -> None:
        is_mine = (row.from_call or "").upper() == self._ui._my_call

        async def _run() -> None:
            action = await self.push_screen_wait(
                ActionMenu(allow_edit=is_mine, allow_resend=is_mine)
            )
            if action is None:
                return
            if action == "edit":
                await self._begin_edit(row)
            elif action == "resend":
                await self._do_resend(row)
            elif action == "react":
                await self._do_react(row)

        self.run_worker(_run(), exclusive=False)

    async def _begin_edit(self, row: MessageRow) -> None:
        if self._refuse_offline("editing"):
            return
        self._pending_edit = {
            "kind": row.kind,
            "target_key": row.tkey,
            "natural_key": row.natural_key,
        }
        try:
            inp = self.query_one("#input", Input)
            inp.value = row.body
            inp.focus()
            inp.cursor_position = len(row.body)
            self._refresh_prompt()
        except Exception:
            pass

    async def _do_resend(self, row: MessageRow) -> None:
        if self._refuse_offline("resending"):
            return
        c = self._ui._client
        try:
            if row.kind == "dm":
                await c.resend_message(row.natural_key)
            else:
                cid = int(row.tkey)
                ts = int(row.natural_key)
                await c.resend_post(cid, ts)
        except ValueError as exc:
            self._status_error(f"[yellow][{exc}][/]")
            return

    async def _do_react(self, row: MessageRow) -> None:
        if self._refuse_offline("reacting"):
            return
        debounce = self._ui._options.tui_emoji_search_debounce_ms
        emoji = await self.push_screen_wait(EmojiPrompt(debounce_ms=debounce))
        if not emoji:
            return
        c = self._ui._client
        try:
            if row.kind == "dm":
                await c.react_message(row.natural_key, emoji)
            else:
                cid = int(row.tkey)
                ts = int(row.natural_key)
                await c.react_post(cid, ts, emoji)
        except Exception as exc:
            self._status_error(f"[red][error][/] {exc}")
            return
        # `react_message` / `react_post` write the reactor row to the
        # local store before returning — pull the fresh list so our
        # own reaction appears inline immediately, the same way it
        # would for an inbound peer reaction.
        row.reactions = self._lookup_reactions(row.kind, row.tkey, row.natural_key)
        self._refresh_row_label(row)

    # ------------------------------------------------------------------
    # Input submission — slash commands, plain text, pending-question
    # answers, in-progress edits.
    # ------------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        inp = self.query_one("#input", Input)
        inp.value = ""
        if self._pending_edit is not None:
            await self._consume_pending_edit(text)
            return
        if not text:
            return
        try:
            if text.startswith("/"):
                await self._handle_command(text)
            elif self._refuse_offline("sending"):
                return
            elif self._ui._target is None:
                self._status_error(
                    "[yellow]hint:[/] no target — /dm CALL or /ch N|#NAME"
                )
            else:
                kind, key = self._ui._target
                c = self._ui._client
                if kind == "dm":
                    msg_id = await c.send_message(key, text)
                    # Server only sends back an `mr` ack — never echoes the
                    # `m` frame to the sender — so mount the row locally.
                    try:
                        ts_seconds = int(msg_id.split("-", 1)[0]) // 1000
                    except (ValueError, AttributeError):
                        ts_seconds = int(time.time())
                    await self._handle_inbound_dm(
                        {
                            "_id": msg_id,
                            "fc": self._ui._my_call,
                            "tc": key.upper(),
                            "m": text,
                            "ts": ts_seconds,
                        },
                        batched=False,
                    )
                else:
                    cid = int(key)
                    name = self._channel_name(cid)
                    if name and name.lower() == "announcements":
                        self._write_to_active("[Users cannot post to #announcements]")
                        return
                    if not self._is_subscribed(cid):
                        self._write_to_active(self._unsubscribed_send_hint(cid))
                        return
                    paused = c.paused_channels().get(cid)
                    if paused:
                        self._write_to_active(self._paused_hint(cid, paused))
                        return
                    ts = await c.post(cid, text)
                    # Server only sends back a `cpr` ack — never echoes the
                    # `cp` frame to the sender — so mount the row locally.
                    await self._handle_inbound_post(
                        {
                            "cid": cid,
                            "fc": self._ui._my_call,
                            "ts": ts,
                            "p": text,
                        },
                        batched=False,
                    )
        except Exception as exc:
            self._status_error(f"[red][error][/] {exc}")

    async def _consume_pending_edit(self, text: str) -> None:
        edit = self._pending_edit
        self._pending_edit = None
        self._refresh_prompt()
        if edit is None or not text:
            return
        if self._refuse_offline("editing"):
            return
        c = self._ui._client
        edts = int(time.time() * 1000)
        try:
            if edit["kind"] == "dm":
                await c.edit_message(edit["natural_key"], text)
                # Server only acks via `mr` — the `med` edit frame goes to
                # the recipient, never echoed to us — so refresh the row
                # locally and dim it until the `mr` ack arrives.
                self._handle_dm_edit(
                    {"_id": edit["natural_key"], "m": text, "edts": edts},
                    clear_delivered=True,
                )
            else:
                cid = int(edit["target_key"])
                ts = int(edit["natural_key"])
                await c.edit_post(cid, ts, text)
                self._handle_post_edit(
                    {"cid": cid, "ts": ts, "p": text, "edts": edts},
                    clear_delivered=True,
                )
        except ValueError as exc:
            self._status_error(f"[yellow][{exc}][/]")

    # ------------------------------------------------------------------
    # Slash-command dispatch — full LineUI parity
    # ------------------------------------------------------------------

    def _known_cids(self) -> set[int]:
        cids = {c.cid for c in self._ui._channels}
        try:
            cids.update(
                r["cid"] for r in self._ui._client._store.list_channels()  # type: ignore[attr-defined]
            )
        except Exception:
            pass
        return cids

    def _resolve_channel(self, arg: str, *, allow_unknown_cid: bool = False) -> int | None:
        if arg.startswith("#"):
            wanted = arg[1:].lower()
        else:
            try:
                cid = int(arg)
            except ValueError:
                wanted = arg.lower()
            else:
                if allow_unknown_cid or cid in self._known_cids():
                    return cid
                return None
        for c in self._ui._channels:
            if c.name and c.name.lower() == wanted:
                return c.cid
        return None

    async def _handle_command(self, line: str) -> None:
        parts = line.split()
        cmd, args = parts[0], parts[1:]
        c = self._ui._client
        if cmd == "/quit":
            self.action_quit_app()
        elif cmd == "/h" and len(args) <= 1:
            self._handle_help(args)
        elif cmd == "/sub" and 1 <= len(args) <= 2:
            await self._handle_sub(args)
        elif cmd == "/unsub" and len(args) == 1:
            if self._refuse_offline("/unsub"):
                return
            cid = self._resolve_channel(args[0], allow_unknown_cid=True)
            if cid is None:
                self._status_error(
                    f"[yellow]/unsub: unknown channel {args[0]!r} (use cid or #name)[/]"
                )
                return
            await c.unsubscribe(cid)
        elif cmd == "/unpause" and 1 <= len(args) <= 2:
            await self._handle_unpause(args)
        elif cmd == "/editdm" and len(args) >= 2:
            if self._refuse_offline("/editdm"):
                return
            try:
                lid = int(args[0])
            except ValueError:
                self._status_error(f"[yellow]/editdm: LID must be an integer (got {args[0]!r})[/]")
                return
            row = c._store.lookup_message_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                self._status_error(f"[yellow]/editdm: no local message with lid {lid}[/]")
                return
            new_body = " ".join(args[1:])
            edts = int(time.time() * 1000)
            try:
                await c.edit_message(row["id"], new_body)
            except ValueError as exc:
                self._status_error(f"[yellow][{exc}][/]")
                return
            self._handle_dm_edit(
                {"_id": row["id"], "m": new_body, "edts": edts},
                clear_delivered=True,
            )
        elif cmd == "/editpost" and len(args) >= 2:
            if self._refuse_offline("/editpost"):
                return
            try:
                lid = int(args[0])
            except ValueError:
                self._status_error(f"[yellow]/editpost: LID must be an integer (got {args[0]!r})[/]")
                return
            row = c._store.lookup_post_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                self._status_error(f"[yellow]/editpost: no local post with lid {lid}[/]")
                return
            new_body = " ".join(args[1:])
            edts = int(time.time() * 1000)
            try:
                await c.edit_post(row["channel_id"], row["ts"], new_body)
            except ValueError as exc:
                self._status_error(f"[yellow][{exc}][/]")
                return
            self._handle_post_edit(
                {
                    "cid": int(row["channel_id"]),
                    "ts": int(row["ts"]),
                    "p": new_body,
                    "edts": edts,
                },
                clear_delivered=True,
            )
        elif cmd == "/retrydm" and len(args) == 1:
            if self._refuse_offline("/retrydm"):
                return
            try:
                lid = int(args[0])
            except ValueError:
                self._status_error(f"[yellow]/retrydm: LID must be an integer (got {args[0]!r})[/]")
                return
            row = c._store.lookup_message_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                self._status_error(f"[yellow]/retrydm: no local message with lid {lid}[/]")
                return
            try:
                await c.resend_message(row["id"])
            except ValueError as exc:
                self._status_error(f"[yellow][{exc}][/]")
                return
            self._write_to_active(f"[green][retrydm][/] resent lid {lid}")
        elif cmd == "/retrypost" and len(args) == 1:
            if self._refuse_offline("/retrypost"):
                return
            try:
                lid = int(args[0])
            except ValueError:
                self._status_error(f"[yellow]/retrypost: LID must be an integer (got {args[0]!r})[/]")
                return
            row = c._store.lookup_post_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                self._status_error(f"[yellow]/retrypost: no local post with lid {lid}[/]")
                return
            try:
                await c.resend_post(row["channel_id"], row["ts"])
            except ValueError as exc:
                self._status_error(f"[yellow][{exc}][/]")
                return
            self._write_to_active(f"[green][retrypost][/] resent lid {lid}")
        elif cmd == "/react" and len(args) == 2:
            if self._refuse_offline("/react"):
                return
            target = self._ui._target
            if target is None:
                self._status_error(
                    "[yellow]/react: no current target. /dm CALL or /ch N|#NAME first[/]"
                )
                return
            try:
                lid = int(args[0])
            except ValueError:
                self._status_error(f"[yellow]/react: ID must be an integer (got {args[0]!r})[/]")
                return
            kind, _ = target
            if kind == "dm":
                row = c._store.lookup_message_by_lid(lid)  # type: ignore[attr-defined]
                if row is None:
                    self._status_error(f"[yellow]/react: no local message with lid {lid}[/]")
                    return
                await c.react_message(row["id"], args[1])
            else:
                row = c._store.lookup_post_by_lid(lid)  # type: ignore[attr-defined]
                if row is None:
                    self._status_error(f"[yellow]/react: no local post with lid {lid}[/]")
                    return
                await c.react_post(row["channel_id"], row["ts"], args[1])
        elif cmd == "/dm" and len(args) == 1:
            call = args[0].upper()
            target = ("dm", call)
            self._ui._target = target
            self._add_target(target)
            await self._switch_centre_to(target)
            self._refresh_prompt()
            self._refresh_footer()
        elif cmd == "/ch" and len(args) == 1:
            cid = self._resolve_channel(args[0])
            if cid is None:
                self._status_error(f"[yellow]/ch: unknown channel {args[0]!r} (use cid or #name)[/]")
                return
            target = ("ch", str(cid))
            # Offline mode skips the subscribe prompt entirely — switching
            # to an unsubscribed channel just shows whatever local history
            # we have for it, with no opportunity to subscribe.
            if not self._ui._offline \
                    and not self._is_subscribed(cid) \
                    and not self._ui._client.paused_channels().get(cid):
                self._open_subscribe_modal(cid, target=target)
                return
            self._ui._target = target
            self._add_target(target)
            await self._switch_centre_to(target)
            self._refresh_prompt()
            self._refresh_footer()
            if self._ui._client.paused_channels().get(cid):
                self._maybe_print_paused_hint(cid)
        elif cmd == "/set":
            # The TUI replaces the inline /set listing with a modal;
            # any extra args are ignored — the modal is the editor.
            self._open_settings_modal()
        elif cmd == "/history":
            self._handle_history_toggle(args, verbose=False)
        elif cmd == "/vhistory":
            self._handle_history_toggle(args, verbose=True)
        else:
            self._status_error(f"[yellow]unknown or malformed:[/] {line}")

    def _handle_history_toggle(self, args: list[str], *, verbose: bool) -> None:
        # The TUI centre pane already shows mounted MessageRows; older
        # pages auto-load on cursor-up. So /history and /vhistory don't
        # *replay* — they just set the verbose mode and re-render every
        # mounted row in place. This matches Ctrl+D (which toggles).
        # Any [N] arg is silently ignored — the line-UI semantics
        # don't apply here.
        del args
        self._ui._options.verbose_history = verbose
        # Same dirty-set mechanic as Ctrl+D — see ``action_toggle_verbose``.
        active = self._active_target()
        self._verbose_dirty = {t for t in self._views if t != active}
        self._refresh_active_rows()

    def _handle_help(self, args: list[str]) -> None:
        focus = args[0] if args else None
        self.push_screen(HelpScreen(focus_command=focus))

    def _open_settings_modal(self) -> None:
        """Push the SettingsModal — the GUI replacement for ``/set``.
        ``on_change`` carries the side-effects the inline ``/set``
        handler used to do directly (live timer update, in-place row
        re-render on verbose-mode flip)."""
        def on_change(name: str, old: Any, new: Any) -> None:
            if name == "delivery_timeout_s":
                self._ui._client.set_delivery_timeout_s(new)
            if name == "verbose_history":
                active = self._active_target()
                self._verbose_dirty = {t for t in self._views if t != active}
                self._refresh_active_rows()
        self.push_screen(SettingsModal(options=self._ui._options, on_change=on_change))

    async def _handle_sub(self, args: list[str]) -> None:
        if self._refuse_offline("/sub"):
            return
        cid = self._resolve_channel(args[0], allow_unknown_cid=True)
        if cid is None:
            self._status_error(
                f"[yellow]/sub: unknown channel {args[0]!r} (use cid or #name)[/]"
            )
            return
        # Explicit count: skip the modal entirely, just subscribe and
        # pull. The modal is for the interactive "how many?" prompt.
        if len(args) == 2:
            try:
                explicit_n = int(args[1])
            except ValueError:
                self._status_error(
                    f"[yellow]/sub: post count must be an integer, got {args[1]!r}[/]"
                )
                return
            if explicit_n < 0:
                self._status_error("[yellow]/sub: post count must be non-negative[/]")
                return
            try:
                pc = await self._ui._client.subscribe_and_wait(cid)
            except asyncio.TimeoutError:
                self._status_error(
                    f"[yellow]/sub: timed out waiting for ack for "
                    f"{self._channel_ref(cid)}[/]"
                )
                return
            if pc > 0 and explicit_n > 0:
                await self._ui._client.request_post_batch(cid, min(explicit_n, pc))
            return
        # No explicit count → reuse the SubscribeModal with skip_confirm
        # (the user already opted in by typing /sub).
        target: TargetKey = ("ch", str(cid))
        self._open_subscribe_modal(cid, target=target, skip_confirm=True)

    async def _handle_unpause(self, args: list[str]) -> None:
        if self._refuse_offline("/unpause"):
            return
        cid = self._resolve_channel(args[0])
        if cid is None:
            self._status_error(
                f"[yellow]/unpause: unknown channel {args[0]!r} (use cid or #name)[/]"
            )
            return
        if len(args) == 2:
            try:
                n = int(args[1])
            except ValueError:
                self._status_error(
                    f"[yellow]/unpause: post count must be an integer, got {args[1]!r}[/]"
                )
                return
            if n <= 0:
                self._status_error("[yellow]/unpause: post count must be positive[/]")
                return
        else:
            n = self._ui._client.paused_channels().get(cid, 0)
            if n <= 0:
                self._status_error(
                    f"[yellow]/unpause cid={cid}: no pending count from pch "
                    f"headers; pass /unpause {cid} N[/]"
                )
                return
        await self._ui._client.unpause_channel(cid, post_count=n)
        self._write_to_active(
            f"[green][unpause][/] requested {n} post(s) for "
            f"{self._channel_ref(cid)}"
        )

    # ------------------------------------------------------------------
    # Misc utilities
    # ------------------------------------------------------------------

    def _batch_mutate(self):
        """Context manager that suppresses ListView.Highlighted handling
        during programmatic mutation (mounting/prepending rows)."""
        app = self

        class _Ctx:
            def __enter__(self_inner):
                app._suppress_highlight = True
            def __exit__(self_inner, exc_type, exc, tb):
                app._suppress_highlight = False
                return False

        return _Ctx()
