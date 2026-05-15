"""Urwid-based TUI — a parallel backend to ``ui.textual_ui.TextualUI``.

The Textual UI is feature-rich but heavy: virtual DOM, CSS matching,
animation timers, cursor-blink redraws. Several of those costs dominate
on slow CPUs even after the ``low_power_mode`` work. urwid is older and
simpler — event-driven, no compositor, redraw on keypress / explicit
``draw_screen()``. This module mirrors as much of ``TextualUI``'s
user-visible surface as urwid permits: same panes, same slash commands,
same modals, same key bindings, same event dispatch.

Public shape matches ``TextualUI``: the constructor takes the same
kwargs (minus the Textual-only ``cursor_blink``); ``render_event(obj)``
queues an event from the WpsClient reader task; ``async run()`` builds
the widget tree, drives ``urwid.MainLoop`` over the running asyncio
loop, and returns when the user quits or the link drops without
recovery (``exit_reason="terminal"``).

The slash-command, event-dispatch and helper-method shapes deliberately
parallel ``ui.textual_ui.TextualUI._WhatspycApp`` so a grep for ``_handle_cs``
or ``_open_subscribe_modal`` lands in both backends. Read ``textual_ui.py``'s
docstrings for the deeper "why" of each handler — most of the comments
there apply unchanged.
"""

from __future__ import annotations

import asyncio
import curses
import logging
import re
import sys
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Optional

import urwid

from whatspyc import __version__
from whatspyc import log as log_mod

_log = logging.getLogger(__name__)
from whatspyc.config import ChannelInfo, ConnectProfile
from whatspyc.ui import (
    at_calls_from_row,
    emoji_for_display,
    emoji_to_wire,
    parse_post_mentions,
    resolve_reply_meta,
    ts_to_ms,
)
from whatspyc.ui import help as help_data
from whatspyc.ui.emoji_catalog import (
    EmojiEntry,
    by_char,
    entries_in,
    groups as catalog_groups,
    search as catalog_search,
)
from whatspyc.ui.options import SessionOptions
from whatspyc.wps.client import WpsClient
from whatspyc.wps.connect_seq import ConnectSummary


TargetKey = tuple[str, str]
RowKey = tuple[str, str, str]


_ALTSCREEN_SEQS_CACHE: tuple[str, str] | None = None


def _altscreen_seqs() -> tuple[str, str]:
    """Return ``(smcup, rmcup)`` strings derived from the current
    terminal's terminfo entry, falling back to the xterm DEC
    private-mode sequences when terminfo is unavailable. Cached so
    repeated calls don't re-run ``setupterm``.

    Going through terminfo (rather than hard-coding ``\\x1b[?1049h/l``)
    lets screen and tmux route the buffer-switch through their own
    altscreen handling when their config has it enabled.
    """
    global _ALTSCREEN_SEQS_CACHE
    if _ALTSCREEN_SEQS_CACHE is not None:
        return _ALTSCREEN_SEQS_CACHE
    smcup = "\x1b[?1049h"
    rmcup = "\x1b[?1049l"
    try:
        curses.setupterm()
        ti_smcup = curses.tigetstr("smcup")
        ti_rmcup = curses.tigetstr("rmcup")
        if ti_smcup is not None and ti_rmcup is not None:
            smcup = ti_smcup.decode("ascii", "replace")
            rmcup = ti_rmcup.decode("ascii", "replace")
    except Exception:
        pass
    _ALTSCREEN_SEQS_CACHE = (smcup, rmcup)
    return _ALTSCREEN_SEQS_CACHE

# ---------------------------------------------------------------------
# Palette — named colour roles used throughout the file.
# ---------------------------------------------------------------------

PALETTE: list[tuple[str, ...]] = [
    ("default", "white", ""),
    ("dim", "dark gray", ""),
    ("bold", "white,bold", ""),
    ("accent", "light cyan", ""),
    ("red", "light red", ""),
    ("green", "light green", ""),
    ("yellow", "yellow", ""),
    ("cyan", "dark cyan", ""),
    ("ham", "light blue", ""),
    ("ts", "dark gray", ""),
    ("system", "yellow", ""),
    ("error", "white", "dark red"),
    ("system_dim", "dark gray", ""),
    ("offline_banner", "yellow,bold", ""),
    ("active_tab", "white,bold", "dark blue"),
    ("inactive_tab", "white", ""),
    ("subscribe_check", "light green", ""),
    ("unread_badge", "yellow", ""),
    ("focus", "black", "light gray"),
    ("focus_button", "black", "light cyan"),
    # ----- Header / Footer variants with the bar bg baked in -----
    # ``urwid.AttrMap(widget, "header")`` only applies the "header"
    # attribute to spans with NO attr; spans already carrying their
    # own attr keep their default bg. So the markup that fills the
    # header has to use these ``header_*`` variants explicitly to
    # paint white-bold/dim/yellow text on the dark-blue background.
    ("header", "white,bold", "dark blue"),
    ("header_dim", "light gray", "dark blue"),
    ("header_yellow", "yellow,bold", "dark blue"),
    ("footer", "white", "dark blue"),
    ("footer_dim", "light gray", "dark blue"),
    ("status_pane", "yellow", ""),
    ("ack_line", "dark cyan", ""),
    ("connect_line", "light green", ""),
    ("disconnect_line", "light red", ""),
    ("reconnect_line", "yellow", ""),
    ("border", "dark gray", ""),
    # MessageRow palette extras: dim variants for outbound-pending render.
    ("dim_ham", "dark gray", ""),
    ("dim_ts", "dark gray", ""),
    ("dim_default", "dark gray", ""),
    # `[Edited <ts>]` suffix. Uses a separate, non-dim shade so the
    # marker stays visible on outbound-pending rows where every other
    # span collapses to dark gray (`dim_default`/`dim_ts`/`dim_ham` all
    # resolve to the same colour and would otherwise hide the marker).
    ("edited", "light gray", ""),
    # `[Reply To CALL: ...]` prefix on rows that are replies. urwid's
    # `yellow` is the bright/light yellow on most terminals; `brown`
    # would be the duller variant.
    ("reply", "yellow", ""),
    # `[@CALL]` mention tags. urwid's 16-colour palette has no
    # "purple", but `light magenta` renders as a vivid purple on
    # virtually every modern terminal (xterm, iTerm, Windows Terminal,
    # tmux, etc. — the conventional ANSI mapping for code 13). A
    # bolded variant covers self-mentions so the user can spot their
    # own call without a second colour.
    ("mention", "light magenta", ""),
    ("mention_self", "light magenta,bold", ""),
    # ----- Focus variants for every attr that can appear in a row -----
    # Same trick as the header bar: the row's outer ``AttrMap`` has a
    # ``focus_map`` dict that re-maps each named attr to its
    # ``focus_*`` variant, so the white-on-dark highlight covers the
    # whole row regardless of what attrs the markup uses.
    ("focus_default", "black", "light gray"),
    ("focus_bold", "black,bold", "light gray"),
    ("focus_dim", "dark gray", "light gray"),
    ("focus_ham", "dark blue", "light gray"),
    ("focus_ts", "dark gray", "light gray"),
    ("focus_subscribe_check", "dark green", "light gray"),
    ("focus_unread_badge", "brown", "light gray"),
    ("focus_yellow", "brown", "light gray"),
    ("focus_cyan", "dark cyan", "light gray"),
    ("focus_red", "dark red", "light gray"),
    ("focus_green", "dark green", "light gray"),
    ("focus_dim_ham", "dark gray", "light gray"),
    ("focus_dim_ts", "dark gray", "light gray"),
    ("focus_dim_default", "dark gray", "light gray"),
    # focus bg is `light gray`, so the non-focus `light gray` fg would
    # vanish — flip to `dark gray` for the focused variant.
    ("focus_edited", "dark gray", "light gray"),
    ("focus_reply", "brown", "light gray"),
    # On the focused-row light-gray background, the bright
    # `light magenta` would stay legible but blend with the highlight;
    # `dark magenta` keeps the same hue family and reads cleanly.
    ("focus_mention", "dark magenta", "light gray"),
    ("focus_mention_self", "dark magenta,bold", "light gray"),
]


# Map every named markup attribute to its focused-row equivalent. Used
# as the ``focus_map`` argument on the ``AttrMap`` wrapping a row, so
# the highlight covers the whole row instead of just the gaps between
# attribute spans. ``None`` (the default-attr key) covers spans the
# markup didn't tag.
FOCUS_MAP: dict[Any, str] = {
    None: "focus_default",
    "default": "focus_default",
    "bold": "focus_bold",
    "dim": "focus_dim",
    "ham": "focus_ham",
    "ts": "focus_ts",
    "subscribe_check": "focus_subscribe_check",
    "unread_badge": "focus_unread_badge",
    "yellow": "focus_yellow",
    "cyan": "focus_cyan",
    "red": "focus_red",
    "green": "focus_green",
    "dim_ham": "focus_dim_ham",
    "dim_ts": "focus_dim_ts",
    "dim_default": "focus_dim_default",
    "edited": "focus_edited",
    "reply": "focus_reply",
    "mention": "focus_mention",
    "mention_self": "focus_mention_self",
}


# ---------------------------------------------------------------------
# Format helpers — produce urwid markup lists (lists of (attr, text) tuples).
# ---------------------------------------------------------------------


def _format_connected_line(summary: ConnectSummary) -> str:
    """Compose the one-line summary shown when the link is up. Same
    text as cli.py's pre-UI ``[Connected]`` echo."""
    parts = [
        f"{summary.received_message_count} new DMs",
        f"{summary.server_post_count} new posts",
    ]
    if summary.paused_channels:
        n = len(summary.paused_channels)
        parts.append(f"{n} paused channel(s)")
    return "[Connected] " + ", ".join(parts)


def _format_open_error(exc: BaseException) -> str:
    """Same shape as cli.py's ``_format_connect_error``. Duplicated here
    so the modal can label connect failures without an import cycle."""
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused — is the host/port correct and the service running?"
    if isinstance(exc, asyncio.IncompleteReadError):
        return "link closed unexpectedly during connect"
    if isinstance(exc, TimeoutError):
        return "connection timed out"
    if isinstance(exc, OSError):
        msg = str(exc) or exc.__class__.__name__
        return f"network error: {msg}"
    return f"{type(exc).__name__}: {exc}"


def _ts_text(ts: int | float | None, *, dim: bool = False) -> tuple[str, str]:
    ms = ts_to_ms(ts)
    attr = "dim_ts" if dim else "ts"
    if ms is None:
        return (attr, "[--]")
    dt = datetime.fromtimestamp(ms / 1000)
    return (attr, f"[{dt.strftime('%Y-%m-%d %H:%M:%S')}]")


def _fmt_duration_ms(ms: int | float) -> str:
    s = max(0, round(ms / 1000))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m{s}s"


def _fmt_ts_str(ts: int | float | None) -> str:
    ms = ts_to_ms(ts)
    if ms is None:
        return "[--]"
    dt = datetime.fromtimestamp(ms / 1000)
    return f"[{dt.strftime('%Y-%m-%d %H:%M:%S')}]"


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


def _call_markup(
    call: str | None,
    ham_name: Callable[[str | None], str | None],
    *,
    dim: bool = False,
) -> tuple[str, str]:
    if not call:
        return ("default", "")
    name = ham_name(call)
    inner = f"{name}, {call}" if name else call
    return (("dim_ham" if dim else "ham"), f"<{inner}>")


def _user_label(
    call: str | None,
    ham_name: Callable[[str | None], str | None],
) -> str:
    if not call:
        return ""
    name = ham_name(call)
    return f"{name}, {call}" if name else str(call)


def _reactions_markup(reactions: list[dict] | None) -> list:
    """Return the urwid markup tail for reactions, or [] if none."""
    if not reactions:
        return []
    parts: list = []
    for r in reactions:
        e = r.get("emoji") or ""
        c = (r.get("callsign") or "").upper()
        if not e:
            continue
        e = emoji_for_display(e)
        if c:
            parts.append(("cyan", f" [{c} {e}]"))
        else:
            parts.append(("cyan", f" [{e}]"))
    return parts


def _reply_prefix_text(meta: dict | None) -> str:
    """Plain-text reply prefix — same content as the line UI's, used as
    the body of the urwid ``("reply", ...)`` markup span. Includes the
    trailing space so the body text is visually separated."""
    if not meta:
        return ""
    call = meta.get("call")
    if meta.get("in_db") and meta.get("snippet"):
        body = meta["snippet"]
        inner = f"Reply To {call}: {body}" if call else f"Reply To: {body}"
    else:
        inner = (
            f"Reply To {call}: <msg not in db>"
            if call
            else "Reply To: <msg not in db>"
        )
    return f"[{inner}] "


def _render_row_markup(
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
    show_edits: bool = True,
    reactions: list[dict] | None = None,
    reply_meta: dict | None = None,
    at_calls: list[str] | None = None,
) -> list:
    """Build a urwid markup list for a single message/post.

    Same content as ``ui.textual_ui._render_row``, returned as a list of
    ``(attr, text)`` tuples (urwid's native markup form). Outbound rows
    we sent but haven't seen an ack for are styled with ``dim_*``
    attribute variants; the dim clears once ``delivered_ts`` is set.
    Reactions render with their own ``cyan`` attr regardless of the
    pending-outbound state, mirroring the Textual backend.
    """
    is_mine = (from_call or "").upper() == my_call
    pending = is_mine and delivered_ts is None
    actor = _call_markup(from_call, ham_name, dim=pending)
    body_attr = "dim_default" if pending else "default"
    ts_attr_dim = pending
    parts: list = []
    reply_text = _reply_prefix_text(reply_meta)
    # Pre-build the mention markup so both branches can splice it in
    # the same spot (between ``actor: `` and the reply / body). All
    # tags use the `mention` (purple/light-magenta) attr; self-
    # mentions get the bolded variant so the user can spot their own
    # call at a glance. The attrs map to focus_* equivalents via the
    # row's FOCUS_MAP so the highlight covers them too.
    at_markup: list = []
    for c in at_calls or []:
        c_up = c.upper()
        attr = "mention_self" if c_up == my_call else "mention"
        at_markup.append((attr, f"[@{c_up}] "))
    if verbose:
        head: list = [(body_attr, f"ID: {lid} - "), _ts_text(ts, dim=ts_attr_dim)]
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
            head.extend([(body_attr, " - "), ("dim", status)])
        parts.extend(head)
        parts.append((body_attr, " - "))
        parts.append(actor)
        parts.append((body_attr, ": "))
        parts.extend(at_markup)
        if reply_text:
            parts.append(("reply", reply_text))
        parts.append((body_attr, body))
    else:
        parts.append(_ts_text(ts, dim=ts_attr_dim))
        parts.append((body_attr, " "))
        parts.append(actor)
        parts.append((body_attr, ": "))
        parts.extend(at_markup)
        if reply_text:
            parts.append(("reply", reply_text))
        parts.append((body_attr, body))
    if show_edits and edit_ts:
        edts_ms = ts_to_ms(edit_ts)
        if edts_ms is not None:
            edts_str = datetime.fromtimestamp(edts_ms / 1000).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            # Always `edited` (light gray); never the row's dim variant.
            # On a pending outbound row every other span collapses to
            # `dark gray`, so a dim marker would be invisible against
            # the dim body — see palette comment on `("edited", ...)`.
            parts.append(("edited", f" [Edited {edts_str}]"))
    parts.extend(_reactions_markup(reactions or []))
    return parts


# ---------------------------------------------------------------------
# Custom widgets
# ---------------------------------------------------------------------


class _InputEdit(urwid.Edit):
    """Single-line ``Edit`` that lets the App's global key bindings
    pass through.

    ``urwid.Edit`` registers a default command map that consumes
    ``ctrl a`` / ``ctrl e`` / ``ctrl d`` / ``ctrl b`` / ``ctrl f``
    (line-edit navigation), which means our app-level Ctrl-bindings
    (Ctrl-E for the emoji picker, Ctrl-D for verbose toggle, etc.)
    never reach ``unhandled_input`` while the input is focused — and
    the input is focused by default. This subclass returns those keys
    unchanged so they bubble up through Pile → Frame →
    ``MainLoop.unhandled_input``.

    Bindings deliberately avoid keys the terminal layer can intercept:
    no Ctrl-S / Ctrl-Q (XOFF / XON flow control), no Ctrl-H (backspace
    on most terminals). The remaining Ctrl-letters and the F-keys are
    delivered cleanly by every terminal we care about.

    ``tab`` / ``shift tab`` / ``f1`` / ``esc`` aren't consumed by
    ``Edit`` to begin with, but we list them here for clarity.
    """

    _GLOBAL_KEYS = frozenset(
        {
            "ctrl c", "ctrl x",        # quit
            "ctrl l",                  # log / status pane
            "ctrl d",                  # verbose
            "ctrl e",                  # emoji
            "ctrl o",                  # options
            "ctrl u",                  # unsubscribe
            "tab", "shift tab",        # focus cycle
            "f1",                      # help
            "esc",                     # focus input
        }
    )

    def keypress(self, size, key):  # type: ignore[override]
        if key in self._GLOBAL_KEYS:
            return key
        return super().keypress(size, key)


class _FocusableText(urwid.WidgetWrap):
    """A Text widget that's focusable, selectable, and click-activated.

    urwid's ``Text`` is non-selectable by default. We need selectable
    rows for the various ListBoxes (target list, message list, online
    list) so the cursor can land on them. ``WidgetWrap`` over an
    ``AttrMap('default','focus')`` gives us the highlight on focus.

    ``urwid.WidgetWrap.selectable()`` defers to the *wrapped* widget,
    so a class-level ``_selectable = True`` is silently ignored — we
    have to override ``selectable()`` explicitly. Without this the
    ListBox can't put cursor focus on the row, so Down arrow won't
    advance past the first row (mouse clicks still land via
    ``ListBox.mouse_event`` which sets focus directly without going
    through ``selectable()``, which is why the bug only manifests on
    keyboard navigation).

    Pass an ``on_activate`` callback for ``enter`` handling. The same
    callback fires for left-click, since users will instinctively click
    a target row to switch to it.
    """

    def __init__(
        self,
        markup: Any,
        *,
        on_activate: Callable[[], None] | None = None,
        focus_attr: str | dict | None = None,
        wrap: str = "space",
    ) -> None:
        self._text = urwid.Text(markup, wrap=wrap)
        self._on_activate = on_activate
        # ``focus_map`` is a dict by default so every named attribute
        # in the markup gets its own focused variant; passing a bare
        # string leaves attribute-tagged spans unhighlighted.
        focus_map = focus_attr if focus_attr is not None else FOCUS_MAP
        super().__init__(urwid.AttrMap(self._text, None, focus_map=focus_map))

    def selectable(self) -> bool:  # type: ignore[override]
        return True

    def set_markup(self, markup: Any) -> None:
        self._text.set_text(markup)

    def keypress(self, size, key):  # type: ignore[override]
        if key == "enter" and self._on_activate is not None:
            self._on_activate()
            return None
        return key

    def mouse_event(self, size, event, button, col, row, focus):  # type: ignore[override]
        if (
            urwid.util.is_mouse_press(event)
            and button == 1
            and self._on_activate is not None
        ):
            self._on_activate()
            return True
        return False


class _Button(urwid.WidgetWrap):
    """A focusable, click/Enter-activated button rendered as ``[ Label ]``.

    urwid ships its own ``Button`` but its rendering (``< label >``) is
    a bit dated; we keep our own so we can style it via the palette
    (``inactive_tab`` / ``active_tab`` / ``focus_button``). See
    ``_FocusableText`` for the explanation of why we override
    ``selectable()`` instead of relying on ``_selectable``.
    """

    def __init__(
        self,
        label: str,
        *,
        on_press: Callable[[], None] | None = None,
        attr: str = "inactive_tab",
        focus_attr: str = "focus_button",
    ) -> None:
        # ``wrap="clip"`` is critical: ``Text``'s default ``"space"`` wrap
        # mode breaks long labels onto multiple lines when the column
        # gets narrow, which makes ``Columns.rows()`` report a height >
        # 1. A ``Pile`` that gave the Columns 1 row then sees the
        # rendered canvas exceed it and raises
        # ``WidgetError: rendered (W x N) canvas when passed size (W, 1)``.
        # Clipping keeps each button at exactly one row regardless of
        # label length and modal width.
        self._text = urwid.Text(label, align="center", wrap="clip")
        self._on_press = on_press
        self._attr_normal = attr
        self._attr_focus = focus_attr
        self._wrap = urwid.AttrMap(
            urwid.Padding(self._text, left=1, right=1), attr, focus_map=focus_attr,
        )
        super().__init__(self._wrap)

    def selectable(self) -> bool:  # type: ignore[override]
        return True

    def set_label(self, label: str) -> None:
        self._text.set_text(label)

    def set_attr(self, attr: str) -> None:
        self._attr_normal = attr
        self._wrap.set_attr_map({None: attr})

    def keypress(self, size, key):  # type: ignore[override]
        if key in ("enter", " "):
            if self._on_press:
                self._on_press()
            return None
        return key

    def mouse_event(self, size, event, button, col, row, focus):  # type: ignore[override]
        if urwid.util.is_mouse_press(event) and button == 1:
            if self._on_press:
                self._on_press()
            return True
        return False


class _TabBar(urwid.WidgetWrap):
    """Horizontal row of buttons with one styled as the active tab.

    Mirrors the Textual ``_TabBar``: lightweight, focusable as one stop,
    ←/→ cycle the active button. The ``on_change`` callback fires when
    the user presses Enter or arrows past the active tab.
    """

    _selectable = True

    def __init__(
        self,
        tabs: list[tuple[str, str]],
        *,
        active_id: str | None = None,
        on_change: Callable[[str], None] | None = None,
    ) -> None:
        # tabs: list of (id, label)
        self._tabs = list(tabs)
        self._on_change = on_change
        self._active_id = active_id or (tabs[0][0] if tabs else "")
        self._buttons: dict[str, _Button] = {}
        cols = self._build_columns()
        super().__init__(cols)

    def _build_columns(self) -> urwid.Columns:
        widgets: list = []
        self._buttons.clear()
        for tid, label in self._tabs:
            attr = "active_tab" if tid == self._active_id else "inactive_tab"
            btn = _Button(
                label,
                on_press=lambda tid=tid: self._activate(tid),
                attr=attr,
            )
            self._buttons[tid] = btn
            widgets.append(("pack", btn))
        # Pad with empty Text so the row fills available width.
        widgets.append(urwid.Text(""))
        return urwid.Columns(widgets, dividechars=1, focus_column=0)

    def _activate(self, tid: str) -> None:
        if tid == self._active_id:
            return
        old = self._active_id
        self._active_id = tid
        if old in self._buttons:
            self._buttons[old].set_attr("inactive_tab")
        if tid in self._buttons:
            self._buttons[tid].set_attr("active_tab")
        if self._on_change:
            self._on_change(tid)

    def set_tabs(self, tabs: list[tuple[str, str]], *, active_id: str | None = None) -> None:
        self._tabs = list(tabs)
        if active_id is not None:
            self._active_id = active_id
        elif self._tabs and self._active_id not in {t[0] for t in self._tabs}:
            self._active_id = self._tabs[0][0]
        self._w = self._build_columns()

    @property
    def active_id(self) -> str:
        return self._active_id

    def keypress(self, size, key):  # type: ignore[override]
        if key in ("left", "right"):
            ids = [t[0] for t in self._tabs]
            if not ids:
                return key
            try:
                idx = ids.index(self._active_id)
            except ValueError:
                idx = 0
            delta = -1 if key == "left" else 1
            new_id = ids[(idx + delta) % len(ids)]
            self._activate(new_id)
            return None
        return super().keypress(size, key)


class _MessageRow(urwid.WidgetWrap):
    """One message/post row, mounted in a per-target ``ListBox``.

    Same role and domain state as ``ui.textual_ui.MessageRow``: holds row state
    (body, ts, edit_ts, delivered_ts, received_ts, realtime, lid,
    reactions) so the UI can re-render in place when an edit / ack /
    reaction lands. ``refresh_label`` rebuilds the markup from current
    state and pushes it through the inner ``urwid.Text`` widget. The
    ``_render_key`` cache short-circuits no-op refreshes.
    """

    _selectable = True

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
        reply_id: str | None = None,
        reply_ts: int | None = None,
        reply_from: str | None = None,
        at_calls: list[str] | None = None,
    ) -> None:
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
        self.reactions: list[dict] = list(reactions or [])
        self.reply_id = reply_id
        self.reply_ts = reply_ts
        self.reply_from = reply_from
        # Mention list — set on the original `cp` and immutable across
        # edits (the `cped` wire frame doesn't carry `at`), so this
        # never changes after construction.
        self.at_calls: list[str] = list(at_calls or [])
        self._render_key: tuple | None = None
        self._text = urwid.Text("", wrap="space")
        # ``focus_map`` is a dict so the highlight covers attribute-
        # tagged spans (ham/ts/bold/dim/etc.) too — see ``FOCUS_MAP``.
        super().__init__(urwid.AttrMap(self._text, None, focus_map=FOCUS_MAP))

    def selectable(self) -> bool:  # type: ignore[override]
        return True

    def refresh_label(
        self,
        *,
        my_call: str,
        verbose: bool,
        ham_name: Callable[[str | None], str | None],
        delivery_timeout_s: int,
        show_edits: bool = True,
        reply_meta: dict | None = None,
    ) -> None:
        is_pending_outbound = (
            verbose
            and (self.from_call or "").upper() == my_call
            and self.delivered_ts is None
            and self.ts is not None
        )
        reactions_signature = tuple(
            (r.get("emoji"), r.get("callsign")) for r in self.reactions
        )
        reply_sig = (
            (
                bool(reply_meta.get("in_db")),
                reply_meta.get("call"),
                reply_meta.get("snippet"),
            )
            if reply_meta
            else None
        )
        key: tuple | None = None
        if not is_pending_outbound:
            key = (
                verbose,
                show_edits,
                self.body,
                self.ts,
                self.edit_ts,
                self.delivered_ts,
                self.received_ts,
                self.realtime,
                self.lid,
                self.from_call.upper(),
                reactions_signature,
                bool(self.from_call and self.from_call.upper() == my_call and self.delivered_ts is None),
                reply_sig,
                tuple(self.at_calls),
            )
            if key == self._render_key:
                return
        markup = _render_row_markup(
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
            show_edits=show_edits,
            reactions=self.reactions,
            reply_meta=reply_meta,
            at_calls=self.at_calls,
        )
        self._text.set_text(markup)
        self._render_key = key  # None if uncacheable

    def keypress(self, size, key):  # type: ignore[override]
        # Enter is forwarded to the App via the parent ``_MessageListBox``,
        # which intercepts it before the row sees it; the row itself
        # is a passive selectable widget for keyboard navigation.
        return key

    def mouse_event(self, size, event, button, col, row, focus):  # type: ignore[override]
        # Left-click opens the same Edit/Resend/React menu as Enter.
        # The App-wide ``mouse_event`` doesn't reach us by default in
        # all urwid versions, so handle the press locally and
        # surface it via a class attribute hook the App installs at
        # mount time. Falling back to ``False`` when no hook is
        # installed lets the default ListBox.mouse_event still set
        # focus on the clicked row.
        if (
            urwid.util.is_mouse_press(event)
            and button == 1
            and self._mouse_activate is not None
        ):
            self._mouse_activate(self)
            return True
        return False

    # Set by the App once the message ListBox is constructed; called
    # by ``mouse_event`` above on left-click. ``None`` if not wired
    # (e.g. unit tests that don't go through ``_UrwidApp``).
    _mouse_activate: Callable[["_MessageRow"], None] | None = None


# ---------------------------------------------------------------------
# Modal infrastructure
# ---------------------------------------------------------------------


class _ModalShell(urwid.WidgetWrap):
    """Wraps a modal body in a LineBox + AttrMap for visual contrast.

    Modals layer on top of the main widget via ``urwid.Overlay`` —
    ``UrwidUI._show_modal()`` does the wrapping. The modal itself
    (``HelpScreen`` / ``EmojiPrompt`` / ...) returns its body widget;
    ``_ModalShell`` decorates it with a title bar + border, and routes
    keypresses through the modal's ``keypress`` override before
    delegating to the body widget. urwid only invokes ``keypress`` on
    the rendered widget tree — without this hook the bespoke modal
    handlers (Y/N on confirms, Esc to cancel, etc.) would never fire.
    """

    def __init__(
        self,
        title: str,
        body: urwid.Widget,
        modal: "_Modal | None" = None,
    ) -> None:
        self._body = body
        self._modal = modal
        # ``urwid.LineBox`` wraps the body in an internal Pile whose
        # middle is a 3-item Columns ``[lline, body, rline]``. If the
        # body is a *flow* widget (e.g., ``BoxAdapter`` or a bare
        # ``Pile``) the Columns becomes flow too, but the parent Pile
        # treats it as box and asks for ``(W, H)`` rendering — and
        # urwid then renders the flow widget at its natural height
        # rather than the requested H. When the modal's overlay is
        # smaller than that natural height (small terminal, narrow
        # split), the result is a canvas taller than the allocated
        # row count and ``validate_size`` raises:
        #   ``rendered (W x N) canvas when passed size (W, H)``.
        # Wrapping flow bodies in ``Filler`` turns them into box
        # widgets so LineBox's middle Columns is consistently box and
        # the size flow stays correct.
        if "box" not in body.sizing():
            body = urwid.Filler(body, valign="top")
        boxed = urwid.LineBox(body, title=title, title_align="left")
        super().__init__(urwid.AttrMap(boxed, None, focus_map=None))

    def keypress(self, size, key):  # type: ignore[override]
        # Modal-bespoke handler runs first. ``None`` = consumed; any
        # other return value falls through to the body for navigation
        # (arrow keys in lists, typing in Edits, etc.) — modals return
        # ``key`` unchanged for everything they don't care about.
        if self._modal is not None:
            result = self._modal.keypress(size, key)
            if result is None:
                return None
            key = result
        # The wrapped widget might not itself be selectable (e.g. a
        # ``QuitConfirmModal`` body is just a Text + Divider + Text;
        # all flow widgets, none selectable). ``WidgetWrap.keypress``
        # would refuse to forward the key in that case. Try, but
        # silently swallow if the body has nothing to do with it —
        # the modal's bespoke handler is the source of truth here.
        try:
            return super().keypress(size, key)
        except AttributeError:
            return key

    def selectable(self) -> bool:  # type: ignore[override]
        # The wrapped body cascade (LineBox → Filler → Pile → Text)
        # often returns False because Text is non-selectable. urwid's
        # ``Overlay`` won't dispatch keypresses to a non-selectable
        # top widget — so without this override, Y/N/Esc presses on a
        # modal would be silently dropped before ``keypress`` was
        # even called. Modals are *always* selectable as far as the
        # input pipeline is concerned; their bespoke ``keypress`` is
        # the one that decides what to do with each key.
        return True


class _Modal:
    """Base class for modals.

    A modal is an opt-in context: ``UrwidUI._show_modal(modal)`` swaps
    the screen's top widget to ``urwid.Overlay(modal.shell, prev,
    ...)`` and returns the ``asyncio.Future`` the modal will resolve
    via ``self.dismiss(value)``. Each modal subclass implements
    ``build()`` to return its body widget; the shell wrapper is added
    by the App.
    """

    title: str = ""
    # Hard floor passed to ``urwid.Overlay(min_height=...)``. The
    # default 4 fits a confirm-dialog body (one prompt line plus the
    # LineBox border). Modals with fixed-size lists override this so
    # all rows stay visible regardless of terminal height — without
    # the override, ``overlay_size``'s relative-percent height
    # collapses to ``min_height`` on small terminals and clips the
    # tail of the list.
    overlay_min_height: int = 4

    def __init__(self) -> None:
        # Future is lazy: created at ``attach`` time when we know a
        # running event loop is available. Constructing the modal can
        # happen outside an async context (e.g., in tests building the
        # widget tree synchronously), so eager creation would either
        # fail (``get_running_loop()``) or trigger a ``DeprecationWarning``
        # (``get_event_loop()`` without a running loop).
        self.future: asyncio.Future | None = None
        self.shell: urwid.Widget | None = None
        self._app: _UrwidApp | None = None

    def attach(self, app: "_UrwidApp") -> "urwid.Widget":
        self._app = app
        if self.future is None:
            try:
                self.future = asyncio.get_running_loop().create_future()
            except RuntimeError:
                # No running loop (synchronous construction in tests).
                # Build the body anyway so ``attach`` can return its
                # widget; the caller won't await ``self.future``.
                self.future = None  # type: ignore[assignment]
        body = self.build()
        self.shell = _ModalShell(self.title, body, modal=self)
        return self.shell

    def build(self) -> urwid.Widget:
        raise NotImplementedError

    def dismiss(self, value: Any) -> None:
        if self.future is not None and not self.future.done():
            self.future.set_result(value)
        if self._app is not None:
            self._app._dismiss_modal(self)

    def keypress(self, size: tuple[int, ...], key: str) -> str | None:
        # Default: Esc cancels (returns None as the dismiss value).
        if key == "esc":
            self.dismiss(None)
            return None
        return key

    @property
    def overlay_size(self) -> tuple[int, str, int]:
        """(width, height) hints for ``urwid.Overlay``.

        Returns ``(cols, valign, rows)`` so the overlay can position
        the modal centred. Default is 60% wide × 60% tall.
        """
        return 60, "middle", 60


def _safe_id(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)


# ---------------------------------------------------------------------
# Concrete modals
# ---------------------------------------------------------------------


class HelpScreen(_Modal):
    title = "Help"

    def __init__(self, focus_command: str | None = None) -> None:
        super().__init__()
        self._focus = focus_command

    def build(self) -> urwid.Widget:
        rows: list[urwid.Widget] = []
        if self._focus is None:
            rows.append(urwid.Text(("bold", "Key bindings")))
            for line in _KEYBINDING_HELP_LINES:
                rows.append(urwid.Text(line))
            rows.append(urwid.Divider())
            rows.append(urwid.Text(("bold", "Slash commands")))
            for line in help_data.list_lines(hide={"/list", "/users", "/target", "/back"}):
                rows.append(urwid.Text(line))
        else:
            detail = help_data.detail_lines(self._focus)
            if detail is None:
                rows.append(
                    urwid.Text(("yellow", f"unknown command: {self._focus}"))
                )
            else:
                for line in detail:
                    rows.append(urwid.Text(line))
        rows.append(urwid.Divider())
        rows.append(urwid.Text(("dim", "Esc to close")))
        listbox = urwid.ListBox(urwid.SimpleFocusListWalker(rows))
        return listbox

    def keypress(self, size, key):
        if key == "esc":
            self.dismiss(None)
            return None
        # Allow scrolling
        return key


_KEYBINDING_HELP_LINES = [
    "  Tab / Shift-Tab    cycle focus: input → tab strip → target list → message list → log (when shown)",
    "  Esc                focus the input box",
    "  Enter (input)      send / submit slash command / submit pending edit",
    "  Enter (target row) pin as send target, focus input",
    "  Enter (message)    open Edit/Resend/React menu",
    "  Up at top of list  load older messages from local store",
    "  Up/Dn/PgUp/PgDn    in the log pane (when focused): scroll through entries",
    "  F1                 open this help screen",
    "  Ctrl-X / Ctrl-C    quit (with confirm)",
    "  Ctrl-L             toggle the log pane (above the message log)",
    "  Ctrl-D             toggle verbose history (id, timestamps, delivery state)",
    "  Ctrl-E             open the Emoji picker, insert at the input cursor",
    "  Ctrl-O             open the Settings modal (live /set replacement)",
    "  Ctrl-U             unsubscribe from the active channel (with confirm)",
]


class ActionMenu(_Modal):
    title = "Action"
    # 3 menu items + 2-row LineBox border + 1 row of slack inside the
    # BoxAdapter = 6. Without this override the modal collapses to
    # ``min_height=4`` on small terminals (the 6% relative height in
    # ``overlay_size`` rounds to 1 on a 24-row terminal) and the
    # bottom-most row — React — falls outside the visible window.
    overlay_min_height = 7

    def __init__(
        self,
        *,
        allow_edit: bool,
        allow_resend: bool,
        allow_view_reply: bool = False,
        allow_reply: bool = True,
    ) -> None:
        super().__init__()
        self._allow_edit = allow_edit
        self._allow_resend = allow_resend
        self._allow_view_reply = allow_view_reply
        self._allow_reply = allow_reply

    def build(self) -> urwid.Widget:
        rows: list[urwid.Widget] = []

        def make(label: str, value: str, *, enabled: bool) -> urwid.Widget:
            if enabled:
                return _FocusableText(
                    label,
                    on_activate=lambda v=value: self.dismiss(v),
                )
            return urwid.AttrMap(urwid.Text(("dim", label)), None)

        rows.append(make("Edit",   "edit",   enabled=self._allow_edit))
        rows.append(make("Resend", "resend", enabled=self._allow_resend))
        rows.append(make("React",  "react",  enabled=True))
        if self._allow_reply:
            rows.append(make("Reply", "reply", enabled=True))
        if self._allow_view_reply:
            rows.append(
                make("View Full Reply-To", "view-reply", enabled=True)
            )
        listbox = urwid.ListBox(urwid.SimpleFocusListWalker(rows))
        return urwid.BoxAdapter(listbox, height=len(rows) + 1)

    @property
    def overlay_size(self) -> tuple[int, str, int]:
        # Width 30 fits "View Full Reply-To"; height grows with the
        # number of optional rows currently present.
        height = 6
        if self._allow_reply:
            height += 1
        if self._allow_view_reply:
            height += 1
        return 30, "middle", height


class ReplyToModal(_Modal):
    """Read-only modal showing the parent of a reply row.

    Mirrors :class:`ui.textual_ui.ReplyToModal`: a single rendered line for
    the parent post/DM, presented in the same format the user would
    see in the thread (timestamp, name + call, body, edit marker,
    reactions). Esc dismisses.
    """

    title = "Reply-to"

    def __init__(self, *, header: str, body_markup: list) -> None:
        super().__init__()
        self._header = header
        self._body_markup = body_markup

    def build(self) -> urwid.Widget:
        rows: list[urwid.Widget] = [
            urwid.Text(self._header),
            urwid.Divider(),
            urwid.Text(self._body_markup),
            urwid.Divider(),
            urwid.Text(("dim", "Esc to close")),
        ]
        listbox = urwid.ListBox(urwid.SimpleFocusListWalker(rows))
        return listbox

    def keypress(self, size, key):
        if key == "esc":
            self.dismiss(None)
            return None
        return key


class SubscribeModal(_Modal):
    """Two-stage subscribe flow.

    Stage 1: "Subscribe to #foo? [y/N]". Stage 2 (after y + ack arrives):
    "How many of {pc} historic posts? [0-pc]". Dismisses with ``None``
    on cancel and ``int >= 0`` on success.
    """

    title = "Subscribe"

    def __init__(
        self,
        *,
        cid: int,
        ref: str,
        do_subscribe: Callable[[], Awaitable[int]],
        default_count_for: Callable[[int], int],
        skip_confirm: bool = False,
    ) -> None:
        super().__init__()
        self._cid = cid
        self._ref = ref
        self._do_subscribe = do_subscribe
        self._default_count_for = default_count_for
        self._skip_confirm = skip_confirm
        self._stage = "subscribing" if skip_confirm else "confirm"
        self._pc: int = 0
        self._count_input: urwid.Edit | None = None
        self._body_pile: urwid.Pile | None = None
        # Set True once the server's ``cs`` ack lands (i.e. we are now
        # subscribed on the server side). The caller reads this after
        # the modal dismisses to decide whether a cancel needs to send
        # an undo ``cs s=0``.
        self.subscribed_on_server = False
        self.kickoff_task: asyncio.Task | None = None

    def build(self) -> urwid.Widget:
        self._body_pile = urwid.Pile([])
        self._render_stage()
        if self._skip_confirm:
            self.kickoff_task = asyncio.create_task(self._kick_off_subscribe())
        return urwid.Filler(self._body_pile, valign="top")

    def _render_stage(self) -> None:
        assert self._body_pile is not None
        widgets: list[urwid.Widget] = []
        if self._stage == "confirm":
            widgets.append(urwid.Text([("bold", f"Subscribe to {self._ref}?")]))
            widgets.append(urwid.Divider())
            widgets.append(urwid.Text([("yellow", "  y"), ("default", " → subscribe   "), ("yellow", "n"), ("default", " or "), ("yellow", "Esc"), ("default", " → cancel")]))
        elif self._stage == "subscribing":
            widgets.append(urwid.Text(("yellow", f"Subscribing to {self._ref}…")))
        elif self._stage == "count":
            widgets.append(
                urwid.Text(
                    f"Subscribed. How many of {self._pc} historic posts to fetch?"
                )
            )
            default = self._default_count_for(self._pc)
            self._count_input = urwid.Edit(
                f"  count [Enter = {default}]: ",
            )
            widgets.append(self._count_input)
            widgets.append(urwid.Divider())
            widgets.append(urwid.Text(("dim", "  Enter to submit · Esc to cancel")))
        elif self._stage == "error":
            widgets.append(urwid.Text(("red", "Subscribe failed; press Esc to close.")))
        self._body_pile.contents = [(w, ("pack", None)) for w in widgets]
        # Focus the input on stage=count
        if self._stage == "count" and self._count_input is not None:
            self._body_pile.focus_position = len(widgets) - 3  # the Edit position
        self._refresh_shell_selectability()

    def _refresh_shell_selectability(self) -> None:
        # ``LineBox.__init__`` builds a ``Pile([top, middle, bottom])``
        # where ``middle = Columns([lline, body, rline])``. Both that
        # ``Pile`` and ``Columns`` cache ``_selectable`` from their
        # contents at construction time and only recompute it when
        # their own contents list is mutated. Our body Pile starts
        # empty, so both caches latch to ``False`` — and
        # ``Pile.keypress`` early-returns the key unchanged when
        # ``self.selectable()`` is ``False``, so once we transition to
        # ``stage=count`` the new ``Edit`` never sees keystrokes.
        # Nudge them to recompute.
        if self.shell is None:
            return
        try:
            linebox = self.shell._w.original_widget  # AttrMap → LineBox
            inner_pile = linebox._w  # Pile([top, middle, bottom])
            for w, _opts in inner_pile.contents:
                if isinstance(w, urwid.Columns):
                    w._contents_modified()
            inner_pile._contents_modified()
        except (AttributeError, IndexError):
            pass

    async def _kick_off_subscribe(self) -> None:
        try:
            self._pc = await self._do_subscribe()
        except asyncio.TimeoutError:
            self._stage = "error"
            self._render_stage()
            return
        except Exception:
            self._stage = "error"
            self._render_stage()
            return
        # The server has now accepted our ``cs s=1`` — we're subscribed
        # regardless of what happens to the modal. The caller uses this
        # to undo the subscription if the user cancels the count prompt.
        self.subscribed_on_server = True
        self._stage = "count"
        self._render_stage()
        if self._app is not None and self._app._loop is not None:
            self._app._loop.draw_screen()

    def keypress(self, size, key):
        if key == "esc":
            self.dismiss(None)
            return None
        if self._stage == "confirm":
            if key in ("y", "Y"):
                self._stage = "subscribing"
                self._render_stage()
                self.kickoff_task = asyncio.create_task(self._kick_off_subscribe())
                return None
            if key in ("n", "N"):
                self.dismiss(None)
                return None
            return key
        if self._stage == "count":
            if key == "enter" and self._count_input is not None:
                raw = self._count_input.edit_text.strip()
                if not raw:
                    n = self._default_count_for(self._pc)
                else:
                    try:
                        n = int(raw)
                    except ValueError:
                        return None
                    if n < 0:
                        return None
                    if n > self._pc:
                        n = self._pc
                self.dismiss(n)
                return None
            # Forward other keys (typing) to the Pile so the Edit gets them.
            return key
        return key


class UnpauseModal(_Modal):
    """Two-stage unpause flow.

    Stage 1: "{ref} paused — {pending} new posts. Unpause? [y/N]".
    Stage 2: "How many of {pending} to fetch? [Enter = default]".
    Dismisses with ``None`` on cancel and ``int > 0`` on confirm.

    Unlike :class:`SubscribeModal` there is no waiting-for-ack stage:
    the pending count comes from the cached ``pch`` headers, so y jumps
    straight to the count prompt.
    """

    title = "Unpause"

    def __init__(
        self,
        *,
        cid: int,
        ref: str,
        pending: int,
        default_count_for: Callable[[int], int],
    ) -> None:
        super().__init__()
        self._cid = cid
        self._ref = ref
        self._pending = pending
        self._default_count_for = default_count_for
        self._stage = "confirm"
        self._count_input: urwid.Edit | None = None
        self._body_pile: urwid.Pile | None = None

    def build(self) -> urwid.Widget:
        self._body_pile = urwid.Pile([])
        self._render_stage()
        return urwid.Filler(self._body_pile, valign="top")

    def _render_stage(self) -> None:
        assert self._body_pile is not None
        widgets: list[urwid.Widget] = []
        if self._stage == "confirm":
            widgets.append(
                urwid.Text(
                    [("bold", f"{self._ref} paused — {self._pending} new posts")]
                )
            )
            widgets.append(urwid.Divider())
            widgets.append(urwid.Text("Unpause?"))
            widgets.append(urwid.Divider())
            widgets.append(
                urwid.Text(
                    [
                        ("yellow", "  y"), ("default", " → unpause   "),
                        ("yellow", "n"), ("default", " or "),
                        ("yellow", "Esc"), ("default", " → cancel"),
                    ]
                )
            )
        elif self._stage == "count":
            widgets.append(
                urwid.Text(
                    f"How many of {self._pending} pending posts to fetch?"
                )
            )
            default = self._default_count_for(self._pending)
            self._count_input = urwid.Edit(
                f"  count [Enter = {default}]: ",
            )
            widgets.append(self._count_input)
            widgets.append(urwid.Divider())
            widgets.append(urwid.Text(("dim", "  Enter to submit · Esc to cancel")))
        self._body_pile.contents = [(w, ("pack", None)) for w in widgets]
        if self._stage == "count" and self._count_input is not None:
            self._body_pile.focus_position = len(widgets) - 3
        self._refresh_shell_selectability()

    def _refresh_shell_selectability(self) -> None:
        # See SubscribeModal._refresh_shell_selectability for why this
        # nudge is required — LineBox caches _selectable on an empty Pile.
        if self.shell is None:
            return
        try:
            linebox = self.shell._w.original_widget
            inner_pile = linebox._w
            for w, _opts in inner_pile.contents:
                if isinstance(w, urwid.Columns):
                    w._contents_modified()
            inner_pile._contents_modified()
        except (AttributeError, IndexError):
            pass

    def keypress(self, size, key):
        if key == "esc":
            self.dismiss(None)
            return None
        if self._stage == "confirm":
            if key in ("y", "Y"):
                self._stage = "count"
                self._render_stage()
                return None
            if key in ("n", "N"):
                self.dismiss(None)
                return None
            return key
        if self._stage == "count":
            if key == "enter" and self._count_input is not None:
                raw = self._count_input.edit_text.strip()
                if not raw:
                    n = self._default_count_for(self._pending)
                else:
                    try:
                        n = int(raw)
                    except ValueError:
                        return None
                    if n <= 0:
                        return None
                    if n > self._pending:
                        n = self._pending
                self.dismiss(n)
                return None
            return key
        return key


class NewDmModal(_Modal):
    title = "New DM"

    def build(self) -> urwid.Widget:
        self._input = urwid.Edit("Callsign: ")
        pile = urwid.Pile(
            [
                self._input,
                urwid.Divider(),
                urwid.Text(("dim", "Enter to add, Esc to cancel")),
            ]
        )
        return urwid.Filler(pile, valign="top")

    def keypress(self, size, key):
        if key == "esc":
            self.dismiss(None)
            return None
        if key == "enter":
            text = self._input.edit_text.strip().upper()
            if not text:
                self.dismiss(None)
            else:
                self.dismiss(text)
            return None
        return key

    @property
    def overlay_size(self) -> tuple[int, str, int]:
        return 40, "middle", 6


class UnsubscribeModal(_Modal):
    title = "Unsubscribe"

    def __init__(self, *, channel_ref: str) -> None:
        super().__init__()
        self._ref = channel_ref

    def build(self) -> urwid.Widget:
        rows = [
            urwid.Text([("bold", f"Unsubscribe from {self._ref}?")]),
            urwid.Divider(),
            urwid.Text([("yellow", "  y"), ("default", " → confirm    "), ("yellow", "n"), ("default", " or "), ("yellow", "Esc"), ("default", " → cancel")]),
        ]
        return urwid.Filler(urwid.Pile(rows), valign="top")

    def keypress(self, size, key):
        if key in ("y", "Y", "enter"):
            self.dismiss(True)
            return None
        if key in ("n", "N", "esc"):
            self.dismiss(False)
            return None
        return key

    @property
    def overlay_size(self) -> tuple[int, str, int]:
        return 50, "middle", 6


class QuitConfirmModal(_Modal):
    title = "Quit"

    def build(self) -> urwid.Widget:
        rows = [
            urwid.Text(("bold", "Quit whatspyc? y/n")),
            urwid.Divider(),
            urwid.Text([("yellow", "  y"), ("default", " → quit          ")]),
            urwid.Text([("yellow", "  n"), ("default", " or "), ("yellow", "Esc"), ("default", " → cancel  (default = no)")]),
        ]
        return urwid.Filler(urwid.Pile(rows), valign="top")

    def keypress(self, size, key):
        if key in ("y", "Y"):
            self.dismiss(True)
            return None
        if key in ("n", "N", "esc", "enter"):
            self.dismiss(False)
            return None
        return key

    @property
    def overlay_size(self) -> tuple[int, str, int]:
        return 40, "middle", 6


class BoolSelectModal(_Modal):
    title = "Edit setting"

    def __init__(self, *, name: str, current: bool, description: str) -> None:
        super().__init__()
        self._name = name
        self._current = current
        self._description = description

    def build(self) -> urwid.Widget:
        rows = [
            urwid.Text(("bold", self._name)),
            urwid.Text(("dim", self._description)),
            urwid.Divider(),
            _FocusableText("On",  on_activate=lambda: self.dismiss(True)),
            _FocusableText("Off", on_activate=lambda: self.dismiss(False)),
            urwid.Divider(),
            urwid.Text(("dim", "  Enter to pick · 1 = On · 0 = Off · Esc to cancel")),
        ]
        listbox = urwid.ListBox(urwid.SimpleFocusListWalker(rows))
        return listbox

    def keypress(self, size, key):
        if key == "1":
            self.dismiss(True)
            return None
        if key == "0":
            self.dismiss(False)
            return None
        if key == "esc":
            self.dismiss(None)
            return None
        return key


class EditValueModal(_Modal):
    title = "Edit setting"

    def __init__(self, *, name: str, current: str, description: str) -> None:
        super().__init__()
        self._name = name
        self._description = description
        self._input = urwid.Edit("  value: ", current)

    def build(self) -> urwid.Widget:
        rows = [
            urwid.Text(("bold", self._name)),
            urwid.Text(("dim", self._description)),
            urwid.Divider(),
            self._input,
            urwid.Divider(),
            urwid.Text(("dim", "  Enter to save · Esc to cancel")),
        ]
        return urwid.Filler(urwid.Pile(rows), valign="top")

    def keypress(self, size, key):
        if key == "esc":
            self.dismiss(None)
            return None
        if key == "enter":
            self.dismiss(self._input.edit_text)
            return None
        return key


class SettingsModal(_Modal):
    title = "Settings"

    def __init__(
        self,
        *,
        options: SessionOptions,
        on_change: Callable[[str, Any, Any], None],
    ) -> None:
        super().__init__()
        self._options = options
        self._on_change = on_change
        self._rows: dict[str, _FocusableText] = {}
        self._listbox: urwid.ListBox | None = None

    def build(self) -> urwid.Widget:
        items: list[urwid.Widget] = []
        # Skip line-UI-only options (e.g. notify_new_dms) — they have no
        # effect in the urwid backend, so showing them in this modal
        # would just confuse users.
        for name in self._options.names(include_line_only=False):
            row = _FocusableText(
                self._row_markup(name),
                on_activate=lambda n=name: self._edit(n),
            )
            self._rows[name] = row
            items.append(row)
        items.append(urwid.Divider())
        items.append(urwid.Text(("dim", "Enter to edit · Esc to close")))
        self._listbox = urwid.ListBox(urwid.SimpleFocusListWalker(items))
        return self._listbox

    def _row_markup(self, name: str) -> list:
        value = self._options.format(name)
        desc = self._options.describe(name)
        return [
            ("bold", name),
            ("default", " = "),
            ("green", value),
            ("default", "\n  "),
            ("dim", desc),
        ]

    def _edit(self, name: str) -> None:
        if self._app is None:
            return
        spec_value = self._options.get(name)
        if isinstance(spec_value, bool):
            modal: _Modal = BoolSelectModal(
                name=name,
                current=spec_value,
                description=self._options.describe(name),
            )
        else:
            modal = EditValueModal(
                name=name,
                current=str(spec_value),
                description=self._options.describe(name),
            )

        async def _wait() -> None:
            assert self._app is not None
            new = await self._app._show_modal(modal)
            if new is None:
                return
            try:
                if isinstance(spec_value, bool):
                    old, new = self._options.set(name, "on" if new else "off")
                else:
                    old, new = self._options.set(name, str(new))
            except (ValueError, KeyError):
                return
            if old != new:
                self._on_change(name, old, new)
            if name in self._rows:
                self._rows[name].set_markup(self._row_markup(name))

        asyncio.create_task(_wait())

    def keypress(self, size, key):
        if key == "esc":
            self.dismiss(None)
            return None
        return key


# ---------------------------------------------------------------------
# Emoji picker — searchable, tabbed, in-place grid rebuild.
# ---------------------------------------------------------------------


class _EmojiButton(urwid.WidgetWrap):
    """Single emoji button used in the EmojiPrompt grid."""

    def __init__(
        self,
        char: str,
        *,
        on_activate: Callable[[str], None],
        on_focus: Callable[[str], None],
    ) -> None:
        self._char = char
        self._on_activate = on_activate
        self._on_focus = on_focus
        self._text = urwid.Text(char, align="center")
        super().__init__(urwid.AttrMap(self._text, None, focus_map="focus_button"))

    def selectable(self) -> bool:  # type: ignore[override]
        return True

    @property
    def char(self) -> str:
        return self._char

    def set_emoji(self, char: str) -> None:
        self._char = char
        self._text.set_text(char)

    def keypress(self, size, key):  # type: ignore[override]
        if key in ("enter", " "):
            self._on_activate(self._char)
            return None
        return key

    def mouse_event(self, size, event, button, col, row, focus):  # type: ignore[override]
        if urwid.util.is_mouse_press(event) and button == 1:
            self._on_activate(self._char)
            return True
        return False


class EmojiPrompt(_Modal):
    """Searchable emoji picker.

    Layout: search Edit → top-level group tabs → optional subgroup tabs
    (only for People & Body) → grid (8-col) → focused-emoji caption →
    hex/literal fallback Edit. Search is debounced via
    ``MainLoop.set_alarm_in``.
    """

    title = "Emoji"

    def __init__(self) -> None:
        super().__init__()
        self._debounce_ms = 200
        self._search_input = urwid.Edit("search: ")
        self._fallback_input = urwid.Edit("hex/literal: ")
        self._caption = urwid.Text("")
        self._grid_pile = urwid.Pile([])
        self._tabs_top: _TabBar | None = None
        self._tabs_sub: _TabBar | None = None
        self._tabs_sub_holder = urwid.WidgetPlaceholder(urwid.Text(""))
        self._search_alarm: Any = None
        self._buttons: list[_EmojiButton] = []
        self._entries: list[EmojiEntry] = []
        self._active_group: str = ""
        self._active_subgroup: str | None = None
        self._pile: urwid.Pile | None = None
        self._grid_row_index: int = -1

    def build(self) -> urwid.Widget:
        # Top-level groups: ★ Quick + the nine CLDR groups.
        all_groups = catalog_groups()
        top_tabs = [("__quick__", "★ Quick")]
        top_tabs.extend([(g, g) for g, _ in all_groups])
        self._active_group = "__quick__"
        self._tabs_top = _TabBar(
            top_tabs, active_id="__quick__", on_change=self._on_top_tab_change
        )
        urwid.connect_signal(self._search_input, "change", self._on_search_change)

        grid_row = urwid.BoxAdapter(
            urwid.ListBox(urwid.SimpleFocusListWalker([self._grid_pile])),
            height=12,
        )
        body_rows: list[urwid.Widget] = [
            self._search_input,
            self._tabs_top,
            self._tabs_sub_holder,
            urwid.Divider("─"),
            grid_row,
            urwid.Divider("─"),
            self._caption,
            self._fallback_input,
            urwid.Text(("dim", "Type to search · ↑↓←→ to navigate · Enter to pick · Esc to cancel")),
        ]
        self._grid_row_index = body_rows.index(grid_row)
        self._pile = urwid.Pile(body_rows)
        self._render_view()
        return self._pile

    @property
    def overlay_size(self) -> tuple[int, str, int]:
        # The emoji picker has wide tab strips (10 CLDR groups + a
        # filler), a search input, and an 8-column grid. 90% of the
        # terminal in both dimensions keeps the tabs and grid usable
        # even on smaller xterms. The default 60×60 used elsewhere is
        # too cramped for this picker.
        return 90, "middle", 90

    # --- view rebuild ----------------------------------------------------

    def _entries_for_view(self) -> list[EmojiEntry]:
        query = self._search_input.edit_text.strip()
        if query:
            return list(catalog_search(query, limit=200))
        if self._active_group == "__quick__":
            return list(_QUICK_PICKS)
        if self._active_group:
            return list(
                entries_in(self._active_group, self._active_subgroup)
            )
        return []

    def _on_search_change(self, _input: urwid.Edit, new: str) -> None:
        if self._app is None or self._app._loop is None:
            self._render_view()
            return
        if self._debounce_ms <= 0:
            self._render_view()
            return
        if self._search_alarm is not None:
            self._app._loop.remove_alarm(self._search_alarm)
        self._search_alarm = self._app._loop.set_alarm_in(
            self._debounce_ms / 1000.0, lambda *a: self._render_view()
        )

    def _on_top_tab_change(self, tid: str) -> None:
        self._active_group = tid
        self._active_subgroup = None
        # Surface subgroup tabs only for People & Body.
        if tid == "People & Body":
            subs = next((s for g, s in catalog_groups() if g == tid), [])
            sub_tabs = [("__all__", "All")] + [(s, s) for s in subs]
            self._tabs_sub = _TabBar(
                sub_tabs,
                active_id="__all__",
                on_change=self._on_sub_tab_change,
            )
            self._tabs_sub_holder.original_widget = self._tabs_sub
        else:
            self._tabs_sub = None
            self._tabs_sub_holder.original_widget = urwid.Text("")
        # Clear search on tab change so what the user expects to see is
        # what they actually see.
        if self._search_input.edit_text:
            urwid.disconnect_signal(self._search_input, "change", self._on_search_change)
            self._search_input.set_edit_text("")
            urwid.connect_signal(self._search_input, "change", self._on_search_change)
        self._render_view()

    def _on_sub_tab_change(self, tid: str) -> None:
        self._active_subgroup = None if tid == "__all__" else tid
        self._render_view()

    def _render_view(self) -> None:
        entries = self._entries_for_view()
        self._entries = entries
        cols_per_row = 8
        rows: list[urwid.Widget] = []
        if not entries:
            rows.append(urwid.Text(("dim", "  no matches")))
        else:
            for i in range(0, len(entries), cols_per_row):
                chunk = entries[i : i + cols_per_row]
                cols: list = []
                for e in chunk:
                    btn = _EmojiButton(
                        e.char,
                        on_activate=self._pick,
                        on_focus=self._on_emoji_focus,
                    )
                    cols.append(("weight", 1, btn))
                while len(cols) < cols_per_row:
                    cols.append(("weight", 1, urwid.Text("")))
                rows.append(urwid.Columns(cols, dividechars=1))
        self._grid_pile.contents = [(r, ("pack", None)) for r in rows]
        if entries:
            self._update_caption(entries[0].char)
        else:
            self._caption.set_text("")

    def _update_caption(self, char: str) -> None:
        e = by_char(char)
        if e is None:
            self._caption.set_text(char)
            return
        cp = "+".join(f"{ord(c):x}" for c in char)
        sub = f" · {e.subgroup}" if e.subgroup else ""
        self._caption.set_text(
            f"{char} · {e.name} · U+{cp} · ({e.group}{sub})"
        )

    def _on_emoji_focus(self, char: str) -> None:
        self._update_caption(char)

    def _pick(self, char: str) -> None:
        self.dismiss(char)

    # --- key handling ---------------------------------------------------

    def keypress(self, size, key):
        if key == "esc":
            self.dismiss(None)
            return None
        if key == "enter":
            # When the grid is focused, fall through so the focused
            # _EmojiButton's keypress dismisses with its own char.
            # Intercepting here would always pick entries[0] regardless
            # of which button the user navigated to.
            if (
                self._pile is not None
                and self._pile.focus_position == self._grid_row_index
            ):
                return key
            if self._fallback_input.edit_text.strip():
                self.dismiss(self._fallback_input.edit_text.strip())
                return None
            if self._entries:
                self.dismiss(self._entries[0].char)
                return None
            return None
        return key


# Quick-picks tab content. Mirrors the textual UI's first-class
# affordances: a curated set of common emoji that show up first.
_QUICK_PICKS_RAW = "👍🙏❤️😂😢😡🎉🔥👀✅❌😀😎🤔🙌😉👋"
_QUICK_PICKS: list[EmojiEntry] = [
    e
    for c in _QUICK_PICKS_RAW
    if (e := by_char(c)) is not None
]


# ---------------------------------------------------------------------
# Connect / picker modals — the "session-driven" entry path.
# ---------------------------------------------------------------------


class ConnectProgressModal(_Modal):
    """Streaming connect-progress display.

    Used for both the initial connect and auto-reconnect cycles. Lines
    are appended via ``add_line``; the modal sits on top of the main
    widget tree until the caller calls ``dismiss(value)``. Pressing
    ``q``/``Esc`` cancels: it cancels the bound ``cancel_task`` (the
    in-flight connect coroutine, if any) and dismisses with ``None``,
    which the bootstrap reads as "user bailed; show picker".
    """

    title = "Connecting"
    overlay_min_height = 8

    def __init__(self, *, header: str, banner: str | None = None) -> None:
        super().__init__()
        self._header = header
        self._banner = banner
        self._walker: urwid.SimpleFocusListWalker | None = None
        self._listbox: urwid.ListBox | None = None
        self.cancel_task: asyncio.Task | None = None

    def build(self) -> urwid.Widget:
        # Frame so the progress ListBox grows to fill whatever vertical
        # space the overlay gives us — a fixed BoxAdapter height clipped
        # progress lines off the top once a multi-hop connect ran past
        # ~10 lines, even when the modal had room to spare.
        header_rows: list[urwid.Widget] = []
        header_rows.append(urwid.Text(("dim", "Press q or Esc to cancel")))
        header_rows.append(urwid.Divider())
        if self._banner:
            header_rows.append(urwid.Text(("yellow,bold", self._banner)))
            header_rows.append(urwid.Divider())
        header_rows.append(urwid.Text(("bold", self._header)))
        header_rows.append(urwid.Divider())
        self._walker = urwid.SimpleFocusListWalker([])
        self._listbox = urwid.ListBox(self._walker)
        return urwid.Frame(self._listbox, header=urwid.Pile(header_rows))

    def add_line(self, text: Any) -> None:
        """Append a progress line. ``text`` may be a str or urwid markup."""
        if self._walker is None:
            return
        self._walker.append(urwid.Text(text))
        if self._listbox is not None and len(self._walker) > 0:
            try:
                self._listbox.set_focus(len(self._walker) - 1)
            except (IndexError, ValueError):
                pass
        if self._app is not None and self._app._loop is not None:
            try:
                self._app._loop.draw_screen()
            except Exception:
                pass

    def update_header(self, header: str) -> None:
        self._header = header

    def keypress(self, size, key):
        if key in ("esc", "q", "Q"):
            if self.cancel_task is not None and not self.cancel_task.done():
                self.cancel_task.cancel()
            self.dismiss(None)
            return None
        return key

    @property
    def overlay_size(self) -> tuple[int, str, int]:
        return 70, "middle", 60


class ProfilePickerModal(_Modal):
    """Pick a profile from the list (offline + configured profiles).

    Surfaced at startup when no ``--profile``/ad-hoc flags were given,
    and on terminal disconnect / ``_reconnect_giveup``. Optional
    ``banner`` is a one-line explanation shown above the list (e.g.
    "Disconnected" or "Max reconnect retries reached").

    Dismisses with the chosen ``ConnectProfile`` or ``None`` for quit.
    """

    title = "Select Profile"
    overlay_min_height = 8

    def __init__(
        self,
        *,
        profiles: list[ConnectProfile],
        default_name: str | None = None,
        banner: str | None = None,
    ) -> None:
        super().__init__()
        self._profiles = profiles
        self._default_name = default_name
        self._banner = banner
        self._listbox: urwid.ListBox | None = None
        self._row_for_idx: list[_FocusableText] = []

    def build(self) -> urwid.Widget:
        rows: list[urwid.Widget] = []
        if self._banner:
            rows.append(urwid.Text(("yellow,bold", self._banner)))
            rows.append(urwid.Divider())
        rows.append(urwid.Text(("bold", "Select a connect profile")))
        rows.append(urwid.Divider())
        default_idx: int | None = None
        for i, p in enumerate(self._profiles):
            label = self._format_label(i, p)
            row = _FocusableText(
                label,
                on_activate=lambda prof=p: self.dismiss(prof),
            )
            self._row_for_idx.append(row)
            rows.append(row)
            if self._default_name and p.name == self._default_name:
                default_idx = len(rows) - 1
        rows.append(urwid.Divider())
        rows.append(
            urwid.Text(("dim", "Enter to pick · digit to jump · q/Esc to quit"))
        )
        walker = urwid.SimpleFocusListWalker(rows)
        self._listbox = urwid.ListBox(walker)
        if default_idx is not None:
            try:
                self._listbox.set_focus(default_idx)
            except (IndexError, ValueError):
                pass
        return self._listbox

    def _format_label(self, i: int, p: ConnectProfile) -> list:
        if p.name.startswith("<") and p.name.endswith(">"):
            return [
                ("bold", f"{i}. "),
                ("yellow", p.name),
                ("dim", "  browse local database (no connection)"),
            ]
        user_hops = [s for s in p.connect_script if s.cmd]
        suffix = f"  {len(user_hops)}-hop" if user_hops else "  direct"
        # urwid 4.0.0 truncates a Text canvas at any zero-length markup
        # span — the row stops painting at that span, falls short of
        # ``maxcol``, and the surrounding LineBox loses its right border
        # on that line. Only emit the marker span when there's something
        # to show.
        markup: list = [
            ("bold", f"{i}. "),
            ("default", p.name),
        ]
        if self._default_name and p.name == self._default_name:
            markup.append(("yellow", " (default)"))
        markup.append(("dim", suffix))
        return markup

    def keypress(self, size, key):
        if key in ("esc", "q", "Q"):
            self.dismiss(None)
            return None
        if key.isdigit():
            idx = int(key)
            if 0 <= idx < len(self._profiles):
                self.dismiss(self._profiles[idx])
                return None
        return key

    @property
    def overlay_size(self) -> tuple[int, str, int]:
        return 70, "middle", 70


# ---------------------------------------------------------------------
# Main App — owns the widget tree and runs urwid.MainLoop.
# ---------------------------------------------------------------------


_ADD_DM_KEY = "__add_dm__"


class _UrwidApp:
    """Holds the widget tree, dispatches events, owns the MainLoop.

    Parallel to ``ui.textual_ui._WhatspycApp``. The same dicts (``_views``,
    ``_rows``, ``_unread``, ``_history_exhausted``, ``_subscribed_cids``,
    ``_online_items``) are kept and have the same meaning. Method names
    mirror the Textual side wherever possible so the two backends are
    grep-comparable.
    """

    def __init__(self, ui: "UrwidUI") -> None:
        self._ui = ui
        self._loop: urwid.MainLoop | None = None
        self._screen: urwid.BaseScreen | None = None
        self._exit_future: asyncio.Future | None = None
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._drain_task: asyncio.Task | None = None

        # Per-target message ListBox + walker. Lazy-mounted on first
        # activation; persisted across switches so the cursor / scroll
        # position survive.
        self._views: dict[TargetKey, urwid.ListBox] = {}
        self._walkers: dict[TargetKey, urwid.SimpleFocusListWalker] = {}
        self._rows: dict[RowKey, _MessageRow] = {}
        self._unread: dict[TargetKey, int] = {}
        # Set once after ``_seed_unread_from_store`` populates _unread
        # from the persistent cursors. Guards against re-seeding on
        # later post_connect_setup runs that might overlap with live
        # arrivals — re-seeding would double-count any rows that the
        # live path already incremented.
        self._unread_seeded = False
        self._history_exhausted: dict[TargetKey, bool] = {}
        self._verbose_dirty: set[TargetKey] = set()

        # Target list left pane state.
        self._channels_walker: urwid.SimpleFocusListWalker | None = None
        self._dms_walker: urwid.SimpleFocusListWalker | None = None
        self._target_items: dict[TargetKey, _FocusableText] = {}

        # Online pane state — the incremental-diff dict.
        self._online_walker: urwid.SimpleFocusListWalker | None = None
        self._online_items: dict[str, _FocusableText] = {}
        self._online_label_cache: dict[str, str] = {}
        self._online_count_label: urwid.Text | None = None

        # Status pane (RichLog-equivalent). Visible by default — Ctrl-L
        # toggles it off. Forced visible while offline regardless.
        self._status_walker: urwid.SimpleFocusListWalker | None = None
        self._status_visible: bool = True

        # Centre pane content switcher and thread header.
        self._centre_placeholder: urwid.WidgetPlaceholder | None = None
        self._messages_box: urwid.LineBox | None = None
        self._centre_pane: urwid.Pile | None = None
        self._thread_header: urwid.Text | None = None

        # Input + footer.
        self._input: urwid.Edit | None = None
        self._footer_text: urwid.Text | None = None
        self._header_text: urwid.Text | None = None

        # Modal state.
        self._modal_stack: list[tuple[_Modal, urwid.Widget, str | None]] = []

        # Subscribed channel cid cache (lazy).
        self._subscribed_cids: set[int] | None = None

        # Pending edit dict (mirrors textual): {kind: "dm"|"post", id: ..., body: ...}
        self._pending_edit: dict | None = None

        # Pending reply dict — mirrors the textual backend. Same shape
        # as ``_pending_edit`` plus a ``parent_call`` field used to
        # build the input caption.
        self._pending_reply: dict | None = None

        # he debounce.
        self._he_alarm: Any = None

        # Frame outer widget.
        self._frame: urwid.Frame | None = None
        self._frame_holder: urwid.WidgetPlaceholder | None = None

        # Session-driven state. ``_active_connect_modal`` points at the
        # currently-mounted ConnectProgressModal (during initial
        # connect or auto-reconnect) so _disconnect / _reconnecting /
        # _reconnected events can update it in place.
        self._active_connect_modal: ConnectProgressModal | None = None
        self._bootstrap_task: asyncio.Task | None = None
        # Re-entrancy guard for the in-event-handler reconnect modal —
        # we'd otherwise stack a new modal for each `_disconnect` while
        # the previous reconnect cycle is still settling.
        self._reconnect_handling: bool = False
        # Set by the `_reconnected` synthetic-event handler so the next
        # wire type-`c` reply can emit a `[Connected] N new DMs, M new
        # posts` line — the auto-reconnect path skips the
        # `_run_connect_modal` summary, so without this we'd silently
        # drop back into the session with no recap.
        self._reconnect_summary_pending: bool = False

    # ------------------------------------------------------------------
    # Public surface — called by UrwidUI.
    # ------------------------------------------------------------------

    def render_event(self, obj: dict) -> None:
        try:
            self._event_queue.put_nowait(obj)
        except asyncio.QueueFull:
            pass

    async def run_async(self) -> None:
        loop = asyncio.get_running_loop()
        self._exit_future = loop.create_future()
        self._build_widgets()
        # urwid's default ``command_map`` binds ``ctrl l`` to
        # ``REDRAW_SCREEN``, and ``MainLoop.process_input`` intercepts
        # those frames itself before ``unhandled_input`` ever sees them.
        # We use ``ctrl l`` (mnemonic "log") to toggle the status pane,
        # so that mapping has to go. urwid will redraw on widget
        # changes regardless — losing the manual force-redraw key is a
        # non-issue.
        try:
            urwid.command_map["ctrl l"] = None  # type: ignore[index]
        except Exception:
            pass
        evloop = urwid.AsyncioEventLoop(loop=loop)
        self._loop = urwid.MainLoop(
            self._frame_holder,
            palette=PALETTE,
            event_loop=evloop,
            unhandled_input=self._on_unhandled_input,
            handle_mouse=True,
        )
        # Drain pending events after the screen mounts.
        for ev in self._ui._pending:
            self.render_event(ev)
        self._ui._pending.clear()
        self._drain_task = asyncio.create_task(self._drain_events())

        # Switch into the terminal's alternate-screen buffer so quitting
        # restores whatever was on the screen before the app started,
        # instead of leaving the shell prompt sitting in the middle of
        # the urwid render. urwid's raw_display.Screen doesn't do this
        # itself. We pull smcup from terminfo rather than hard-coding
        # the xterm DEC private-mode sequence so screen/tmux can route
        # it through their own buffer-switch handling when altscreen is
        # enabled.
        smcup, _rmcup = _altscreen_seqs()
        if smcup:
            sys.stdout.write(smcup)
            sys.stdout.flush()
        self._loop.start()
        try:
            if self._ui.session_driven:
                # Drive the picker → connect modal → main session flow
                # inside the running loop. The bootstrap sets
                # ``self._ui._client`` on success and signals exit on
                # quit/cancel.
                self._bootstrap_task = asyncio.create_task(self._bootstrap_session())
            await self._exit_future
        finally:
            if self._bootstrap_task is not None:
                self._bootstrap_task.cancel()
                try:
                    await self._bootstrap_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._drain_task is not None:
                self._drain_task.cancel()
                try:
                    await self._drain_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._loop.stop()
            # Park the cursor on the bottom row and clear that line
            # *before* rmcup. If altscreen actually switched buffers
            # (xterm, screen with ``altscreen on``, tmux) this happens
            # on the alt-buffer that's about to be discarded — harmless.
            # If altscreen got silently dropped (screen with altscreen
            # off, dumb terminals) rmcup is a no-op and the urwid
            # render stays on screen, but the shell prompt at least
            # lands on a clean bottom line instead of stomping the
            # last urwid row — same end-state as ncurses' ``endwin()``
            # (what htop relies on).
            _smcup, rmcup = _altscreen_seqs()
            try:
                rows = self._loop.screen.get_cols_rows()[1]
            except Exception:
                rows = 24
            sys.stdout.write(f"\x1b[{rows};1H\x1b[2K")
            if rmcup:
                sys.stdout.write(rmcup)
            sys.stdout.flush()

    # ------------------------------------------------------------------
    # Session bootstrap (session-driven mode only).
    # ------------------------------------------------------------------

    async def _bootstrap_session(self) -> None:
        """Drive the picker → connect cycle until a client is connected
        (or the user quits). Runs inside the urwid main loop.
        """
        profile: ConnectProfile | None = self._ui._initial_profile
        banner: str | None = None
        while True:
            if profile is None:
                picker = ProfilePickerModal(
                    profiles=self._ui._available_profiles,
                    default_name=self._ui._default_profile_name,
                    banner=banner,
                )
                choice = await self._show_modal(picker)
                if choice is None:
                    self._signal_clean_exit()
                    return
                profile = choice
                banner = None
            if self._ui._is_offline_profile_fn(profile):
                # Offline doesn't open a link — connector returns the
                # offline-shaped client immediately.
                client, _ = await self._open_offline(profile)
                self._ui._client = client
                self._enter_offline_mode()
                self._post_connect_setup()
                return
            ok = await self._run_connect_modal(profile, banner=banner)
            if ok:
                self._post_connect_setup()
                return
            # Connect cancelled or errored — show the picker again.
            banner = "Disconnected"
            profile = None

    def _post_connect_setup(self) -> None:
        """Re-run client-dependent widget setup once a client is up.

        ``_build_widgets`` runs *before* the bootstrap connects, so in
        session-driven mode it skips the target-list population (the
        store call would crash on a ``None`` client). Once the
        connector returns a live client, the target list needs to
        catch up: subscribed channels should render with the
        BALLOT-BOX-WITH-CHECK box rather than the empty BALLOT-BOX
        they'd otherwise inherit from the
        directory-only fill. The header also references the client's
        display name and needs a fresh draw.
        """
        # Clear any rows that ``_build_widgets`` may have populated
        # from the directory (channels.toml) so the merged
        # subscribed-from-store + directory list ends up consistent.
        if self._channels_walker is not None:
            self._channels_walker.clear()
        if self._dms_walker is not None:
            self._dms_walker.clear()
        self._target_items.clear()
        # Seed the per-target unread tally from the persistent cursors
        # before populate computes per-target labels. Idempotent — a
        # session-driven flow may have already seeded inside
        # ``_on_client_ready``, ahead of any wire events arriving.
        self._seed_unread_from_store()
        self._populate_initial_target_lists()
        if self._loop is not None:
            try:
                self._loop.draw_screen()
            except Exception:
                pass

    async def _open_offline(self, profile: ConnectProfile) -> tuple[WpsClient, ConnectSummary]:
        """Open the connector for an offline profile (no link, no modal)."""
        async def _on_event(_obj: dict) -> None:
            return

        def _on_client_ready(c: WpsClient) -> None:
            self._ui._client = c
            # Snapshot per-target unread state from the persistent
            # cursors before any wire events flow. Doing it here (rather
            # than in _post_connect_setup, after the connect window) is
            # what avoids double-counting cpb-during-connect rows: the
            # client persists each row before dispatching the event, so
            # a later seed would scan the store and re-add the same
            # rows the live increment paths have already counted.
            self._seed_unread_from_store()

        client, summary = await self._ui._connection_opener(  # type: ignore[misc]
            profile,
            progress=lambda _line: None,
            on_event=_on_event,
            on_client_ready=_on_client_ready,
        )
        return client, summary

    def _enter_offline_mode(self) -> None:
        """Flip the running UI into offline (read-only) mode after the
        user picked the ``<offline>`` sentinel from the in-UI picker."""
        self._ui._offline = True
        self._status_visible = True
        self._refresh_status_pane()
        self._status_error(
            ("yellow", "[offline] read-only mode — browsing local store, no connection")
        )

    async def _run_connect_modal(
        self, profile: ConnectProfile, *, banner: str | None
    ) -> bool:
        """Open the ConnectProgressModal and run the connection_opener.

        Returns True when the link is up (self._ui._client is set);
        False when the user cancelled or the opener raised. The picker
        loop in ``_bootstrap_session`` decides what happens next.
        """
        modal = ConnectProgressModal(
            header=f"Connecting via '{profile.name}'…",
            banner=banner,
        )
        self._active_connect_modal = modal
        modal.add_line(("dim", f"Connecting with '{profile.name}' profile..."))

        async def _on_event(obj: dict) -> None:
            self.render_event(obj)

        def _on_client_ready(c: WpsClient) -> None:
            # Set the UI's client *before* ``client.open()`` returns so
            # events that arrive during the type-`c` settle (mb / cpb /
            # pch / o / he / ...) reach handlers with a valid
            # ``self._ui._client`` rather than crashing on ``None``.
            self._ui._client = c
            # Snapshot per-target unread state from the persistent
            # cursors before any wire events arrive. The client
            # persists each row before dispatching, so seeding *after*
            # the connect window would double-count anything the live
            # path has already incremented. See ``_seed_unread_from_store``.
            self._seed_unread_from_store()

        async def _runner() -> None:
            try:
                _, summary = await self._ui._connection_opener(  # type: ignore[misc]
                    profile,
                    progress=modal.add_line,
                    on_event=_on_event,
                    on_client_ready=_on_client_ready,
                )
            except asyncio.CancelledError:
                # User pressed q/Esc; partial client is closed by the
                # connector's own cleanup. Reset the UI's reference so
                # the picker that follows starts from a clean slate.
                self._ui._client = None
                return
            except Exception as exc:
                # Connect failed mid-handshake — the connector closed
                # the partial client. Drop the dangling UI reference.
                self._ui._client = None
                modal.add_line(("red", f"Could not connect: {_format_open_error(exc)}"))
                # Briefly leave the error visible so the user sees why.
                try:
                    await asyncio.sleep(2.0)
                except asyncio.CancelledError:
                    pass
                modal.dismiss(("error", exc))
                return
            connected_line = _format_connected_line(summary)
            modal.add_line(("connect_line", connected_line))
            # Also write the summary into the status pane so the user
            # has a record after the modal disappears. Don't force the
            # pane visible — the user explicitly asked for this not to
            # toggle on visibility (use Ctrl-L if you want to see it).
            self._status_write(("connect_line", connected_line))
            modal.dismiss(("ok", summary))

        runner_task = asyncio.create_task(_runner())
        modal.cancel_task = runner_task
        try:
            result = await self._show_modal(modal)
        finally:
            self._active_connect_modal = None
            if not runner_task.done():
                runner_task.cancel()
                try:
                    await runner_task
                except (asyncio.CancelledError, Exception):
                    pass
        return isinstance(result, tuple) and result and result[0] == "ok"

    def _on_link_dropped(self) -> None:
        """Schedule the in-UI reconnect / picker flow on `_disconnect`.

        Called by the event-dispatch handler. If a connect modal is
        already up (initial bootstrap, or a prior reconnect cycle),
        do nothing — events will keep updating it.
        """
        if not self._ui.session_driven:
            return
        if self._reconnect_handling or self._active_connect_modal is not None:
            return
        self._reconnect_handling = True
        asyncio.create_task(self._handle_link_drop())

    async def _handle_link_drop(self) -> None:
        try:
            client = self._ui._client
            if client is not None and client.is_auto_reconnect:
                # Auto-reconnect on. Show progress modal; the
                # `_reconnecting` / `_reconnected` / `_reconnect_giveup`
                # handlers update / dismiss it.
                modal = ConnectProgressModal(
                    header="Reconnecting…",
                    banner="Disconnected",
                )
                modal.add_line(("yellow", "Link dropped, reconnecting..."))
                self._active_connect_modal = modal
                # No cancel_task: pressing q closes the running client
                # via the modal-dismiss-watcher below.
                result = await self._show_modal(modal)
                self._active_connect_modal = None
                if isinstance(result, tuple) and result and result[0] == "reconnected":
                    return  # back to normal session
                if isinstance(result, tuple) and result and result[0] == "giveup":
                    await self._reopen_picker(banner="Max reconnect retries reached")
                    return
                # Modal dismissed with None == user pressed q/Esc.
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        pass
                self._signal_clean_exit()
                return
            # No auto-reconnect. Straight to picker.
            await self._reopen_picker(banner="Disconnected")
        finally:
            self._reconnect_handling = False

    async def _reopen_picker(self, *, banner: str) -> None:
        """Close the current client (if any) and re-run the
        picker → connect cycle. Mirrors ``_bootstrap_session``'s loop
        but starts from a known disconnected state."""
        client = self._ui._client
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
            self._ui._client = None
        cur_banner = banner
        profile: ConnectProfile | None = None
        while True:
            if profile is None:
                picker = ProfilePickerModal(
                    profiles=self._ui._available_profiles,
                    default_name=self._ui._default_profile_name,
                    banner=cur_banner,
                )
                choice = await self._show_modal(picker)
                if choice is None:
                    self._signal_clean_exit()
                    return
                profile = choice
                cur_banner = None
            if self._ui._is_offline_profile_fn(profile):
                offline_client, _ = await self._open_offline(profile)
                self._ui._client = offline_client
                self._enter_offline_mode()
                self._post_connect_setup()
                return
            ok = await self._run_connect_modal(profile, banner=None)
            if ok:
                self._post_connect_setup()
                return
            cur_banner = "Disconnected"
            profile = None

    def _signal_clean_exit(self) -> None:
        if self._ui.exit_reason is None:
            self._ui.exit_reason = None  # explicit clean exit
        if self._exit_future is not None and not self._exit_future.done():
            self._exit_future.set_result(None)

    # ------------------------------------------------------------------
    # Widget construction.
    # ------------------------------------------------------------------

    def _build_widgets(self) -> None:
        # ----- Header -----
        self._header_text = urwid.Text(self._header_markup(), align="left")
        header = urwid.AttrMap(
            urwid.Padding(self._header_text, left=1, right=1), "header"
        )

        # ----- Tab strip (Channels | DMs) -----
        self._left_tab_bar = _TabBar(
            [("ch", "Channels"), ("dm", "DMs")],
            active_id="ch",
            on_change=self._on_left_tab_change,
        )

        # ----- Left pane: target list (channels or dms) + online -----
        self._channels_walker = urwid.SimpleFocusListWalker([])
        self._dms_walker = urwid.SimpleFocusListWalker([])
        self._channels_listbox = urwid.ListBox(self._channels_walker)
        self._dms_listbox = urwid.ListBox(self._dms_walker)
        self._target_switcher = urwid.WidgetPlaceholder(self._channels_listbox)

        self._online_walker = urwid.SimpleFocusListWalker([])
        self._online_listbox = urwid.ListBox(self._online_walker)
        self._online_count_label = urwid.Text("Online (0)")

        # Pile semantics: bare widgets auto-detect (box → weight 1,
        # flow → pack); ``("weight", n, w)`` requires a box widget;
        # ``("pack", w)`` requires a flow widget. ``BoxAdapter`` wraps
        # a box widget into a flow widget with a fixed pixel-row height,
        # so it has to be a pack item — using ``("weight", 1,
        # BoxAdapter(...))`` raises at render time because urwid asks
        # the BoxAdapter for a box-render and the inner widget hands
        # back a flow render. We use ``"given"`` for the target switcher
        # so the box ListBox gets remaining vertical space deterministically.
        left_pane = urwid.Pile(
            [
                ("pack", self._left_tab_bar),
                ("weight", 1, self._target_switcher),
                ("pack", urwid.Divider("─")),
                ("pack", urwid.AttrMap(self._online_count_label, "bold")),
                ("pack", urwid.BoxAdapter(self._online_listbox, height=8)),
            ]
        )
        # Client-dependent setup. In session-driven mode the client is
        # ``None`` until the bootstrap finishes; without this guard
        # ``_populate_initial_target_lists`` would run with an empty
        # store result, leaving every channel rendered as
        # unsubscribed (BALLOT-BOX) even after the connect ack lands. The
        # bootstrap re-runs ``_post_connect_setup`` once the client
        # is up.
        if self._ui._client is not None:
            # Seed unread before populate so per-target labels carry
            # the (N) suffix on first render. In session-driven mode
            # the seed happened earlier, in ``_on_client_ready``.
            self._seed_unread_from_store()
            self._populate_initial_target_lists()

        # ----- Centre pane: status pane + thread header + per-target ListBox -----
        self._status_walker = urwid.SimpleFocusListWalker([])
        self._status_listbox = urwid.ListBox(self._status_walker)
        # Status pane is held in a placeholder so we can show/hide it
        # without re-laying out the whole centre Pile.
        empty = urwid.Filler(urwid.Text(""), valign="top")
        self._status_holder = urwid.WidgetPlaceholder(empty)
        self._refresh_status_pane()

        self._thread_header = urwid.Text("(no target)", align="left")
        self._centre_placeholder = urwid.WidgetPlaceholder(
            urwid.Filler(urwid.Text(("dim", "(no target — pick one from the left)")), valign="top")
        )
        # LineBox frames the messages area so it visually matches the
        # Textual sibling's ``#message-switcher { border: round $accent }``.
        # Kept on ``self`` so ``_set_focus_step`` can find it by identity
        # in ``_centre_pane.contents`` (the Pile holds the LineBox, not
        # the placeholder, after wrapping).
        # Bottom border is suppressed (``bline``/corners empty) so the
        # box's left and right sides extend all the way down to the
        # full-width divider below the body Columns — the divider then
        # visually closes the messages frame *and* caps the vertical
        # separator on the same row, instead of the LineBox bottom
        # ending one row above it.
        self._messages_box = urwid.LineBox(
            self._centre_placeholder,
            bline="",
            blcorner="",
            brcorner="",
        )

        centre_pane = urwid.Pile(
            [
                ("weight", 1, self._status_holder),
                ("pack", urwid.AttrMap(self._thread_header, "bold")),
                ("weight", 5, self._messages_box),
            ]
        )
        # Capture so the focus-step machinery can set
        # ``centre_pane.focus_position = 2`` when the user Tabs to
        # "messages". Without that, Pile.focus_position defaults to 0
        # (the status holder, which is non-selectable while hidden) and
        # Enter on a message row never fires because focus actually
        # lands on the wrong child of the Pile.
        self._centre_pane = centre_pane
        # The first ``_refresh_status_pane`` call ran before
        # ``_centre_pane`` was assigned, so the Pile-mutation branch
        # was a no-op. Re-run it now to drop the status holder when
        # it starts hidden.
        self._refresh_status_pane()
        if self._ui._offline:
            self._status_write(
                ("yellow", "[offline] read-only mode — browsing local store, no connection")
            )

        # ----- Main split: left + separator + centre -----
        # Continuous vertical line column mirrors the Textual sibling's
        # ``#left { border-right: solid $accent }``. SolidFill is non-
        # selectable so keyboard focus skips over it; the explicit column
        # means dividechars stays 0 (the separator IS the divider).
        # NOTE: separator at index 1 shifts the centre column to index 2 —
        # ``_get_focus_step`` / ``_set_focus_step`` reference position 2
        # for "messages".
        left_width = self._compute_left_pane_width()
        body_columns = urwid.Columns(
            [
                (left_width, urwid.AttrMap(left_pane, None)),
                (1, urwid.AttrMap(urwid.SolidFill("│"), "border")),
                ("weight", 1, urwid.AttrMap(centre_pane, None)),
            ],
            dividechars=0,
        )
        # Full-width horizontal divider one row below the body Columns.
        # Caps the vertical separator and the messages-LineBox bottom
        # border at the same row — without it the SolidFill ``│`` (which
        # paints its full cell height) reads as extending past the lower
        # divider on the left pane, since ``─`` only paints the cell's
        # vertical middle.
        body = urwid.Pile(
            [
                ("weight", 1, body_columns),
                ("pack", urwid.AttrMap(urwid.Divider("─"), "border")),
            ]
        )

        # ----- Input -----
        # ``_InputEdit`` rather than ``urwid.Edit`` so the App's global
        # Ctrl-bindings reach ``unhandled_input`` instead of being
        # swallowed by Edit's default command map.
        self._input = _InputEdit(self._input_caption(), "")
        urwid.connect_signal(self._input, "postchange", lambda *a: None)
        # We intercept Enter on the input widget so submission goes
        # through our handler.

        # ----- Footer -----
        self._footer_text = urwid.Text(self._footer_markup(), align="left")
        footer = urwid.AttrMap(self._footer_text, "footer")

        # Wrap the input in a custom widget so Enter triggers submission
        # before urwid.Edit's default handling. Pile [input, footer].
        bottom_pile = urwid.Pile(
            [
                ("pack", urwid.AttrMap(self._input, None)),
                ("pack", footer),
            ]
        )

        self._frame = urwid.Frame(body=body, header=header, footer=bottom_pile)
        # Default focus to the footer so the input is active on
        # startup. Without this, urwid.Frame defaults to "body", which
        # lands focus on the left pane's tab strip — pressing keys
        # there has no obvious effect and the user can't type.
        try:
            self._frame.focus_position = "footer"
        except (IndexError, KeyError):
            pass
        self._frame_holder = urwid.WidgetPlaceholder(self._frame)

    def _header_markup(self) -> list:
        # Single span on the dark-blue bar — matches the textual UI's
        # one-line title. Earlier versions interpolated the user's
        # callsign / name / an OFFLINE marker after the version, which
        # rendered fine in plain xterms but corrupted under screen
        # (stale attrs leaking past the trailing span). The status
        # pane already announces offline mode, so nothing here needs
        # to vary at runtime.
        return [
            (
                "header",
                f"whatspyc (v{__version__}) text-only WhatsPac Client"
                " - Recommended GUI Client: http://whatspac.oarc.uk/",
            ),
        ]

    def _input_caption(self) -> list:
        target = self._ui._target
        prefix = "(offline) " if self._ui._offline else ""
        if self._pending_edit is not None:
            return [("yellow", f"{prefix}edit (Esc cancel)> ")]
        # Reply-mode caption sandwiches a parent-call hint inside the
        # normal label so the user sees both the active thread and
        # what they're replying to. Esc cancels — see
        # ``_cancel_pending_reply``.
        if target is None:
            base = ""
        else:
            kind, key = target
            if kind == "dm":
                base = f"dm:{key}"
            else:
                try:
                    cid = int(key)
                except ValueError:
                    cid = -1
                name = self._channel_name(cid)
                base = f"ch:{cid} #{name}" if name else f"ch:{cid}"
        if self._pending_reply is not None:
            parent_call = self._pending_reply.get("parent_call") or ""
            return [
                ("yellow", f"{prefix}{base} (REPLY TO: {parent_call}, "),
                ("yellow", "Esc cancel reply)> "),
            ]
        if not base:
            return [("default", f"{prefix}> ")]
        return [("default", f"{prefix}{base}> ")]

    def _cancel_pending_reply(self) -> None:
        """Drop reply mode and restore the standard input caption.

        Called from the global Esc handler when ``_pending_reply`` is
        set; otherwise Esc keeps its normal "focus the input" meaning.
        Idempotent — a no-op when there's nothing to cancel.
        """
        if self._pending_reply is None:
            return
        self._pending_reply = None
        if self._input is not None:
            self._input.set_edit_text("")
        self._refresh_input_caption()

    def _cancel_pending_edit(self) -> None:
        """Drop edit mode and restore the standard input caption.

        Called from the global Esc handler when ``_pending_edit`` is
        set. The input was pre-filled with the row body in
        ``_begin_edit``; clear it so the user isn't left with stale
        text in a normal prompt. Idempotent.
        """
        if self._pending_edit is None:
            return
        self._pending_edit = None
        if self._input is not None:
            self._input.set_edit_text("")
        self._refresh_input_caption()

    def _refresh_input_caption(self) -> None:
        if self._input is not None:
            self._input.set_caption(self._input_caption())

    def _footer_markup(self) -> list:
        # ``footer`` / ``footer_dim`` paint on the dark-blue bar bg.
        # See the equivalent comment on ``_header_markup``.
        bits: list[tuple[str, str]] = [
            ("footer", " Tab"),
            ("footer_dim", "·cycle "),
            ("footer", "F1"),
            ("footer_dim", "·help "),
            ("footer", "Ctrl-E"),
            ("footer_dim", "·emoji "),
            ("footer", "Ctrl-O"),
            ("footer_dim", "·settings "),
            ("footer", "Ctrl-L"),
            ("footer_dim", "·log "),
            ("footer", "Ctrl-D"),
            ("footer_dim", "·verbose "),
        ]
        if self._active_target_is_subscribed_channel():
            bits.extend([("footer", "Ctrl-U"), ("footer_dim", "·unsub ")])
        bits.extend([("footer", "Ctrl-X"), ("footer_dim", "·quit")])
        return bits

    def _refresh_footer(self) -> None:
        if self._footer_text is not None:
            self._footer_text.set_text(self._footer_markup())

    def _compute_left_pane_width(self) -> int:
        """Same logic as Textual's ``_apply_left_pane_width``.

        Picks the widest channel/DM label + 6 chars for ``(100)`` unread
        suffix, with a sensible floor.
        """
        widest = 0
        # Directory and store-known channels.
        for ch in self._ui._channels:
            label = f"☐ {ch.cid} #{ch.name}"
            widest = max(widest, len(label))
        # _build_widgets runs before the bootstrap connects, so the
        # client (and its store) may not exist yet on first call.
        client = self._ui._client
        store = getattr(client, "_store", None) if client is not None else None
        if store is not None:
            try:
                for row in store.list_channels():
                    cid = int(row["cid"])
                    name = row.get("name") or ""
                    label = f"☑ {cid} #{name}"
                    widest = max(widest, len(label))
            except Exception:
                pass
            # DM peers from the store: ``2E0XYZ Matt`` style. Method is
            # ``list_dm_peers(my_call)``; rows look like
            # ``{"peer": CALL, "last_ts": ms, "count": N}``.
            try:
                for row in store.list_dm_peers(self._ui._my_call):
                    widest = max(widest, len(row.get("peer", "") or "") + 16)
            except Exception as e:
                _log.warning("_compute_left_pane_width DM peers: %s", e)
        return max(24, widest + 6)

    # ------------------------------------------------------------------
    # Target list
    # ------------------------------------------------------------------

    def _populate_initial_target_lists(self) -> None:
        # Channels: subscribed (from store, with ``subscribed=1``)
        # first, then directory entries that aren't already subscribed.
        # Mark each row with the (un)checked box so the user can see at
        # a glance which ones are subscribed. The store may have rows
        # for channels we *unsubscribed* from (subscribed=0); those
        # should appear as unsubscribed (☐), not subscribed (☑).
        try:
            store_channels = list(self._ui._client._store.list_channels())  # type: ignore[attr-defined]
        except Exception:
            store_channels = []
        subscribed_cids: set[int] = set()
        for row in store_channels:
            cid = int(row["cid"])
            if row.get("subscribed"):
                subscribed_cids.add(cid)
                self._add_target(("ch", str(cid)))
        # Directory entries — anything in the bundled channels.toml or
        # user's directory that isn't already shown as subscribed.
        for ch in self._ui._channels:
            if ch.cid in subscribed_cids:
                continue
            self._add_target(("ch", str(ch.cid)), unsubscribed=True)
        # Store rows that aren't subscribed and aren't in the directory
        # either (e.g. a channel we unsubscribed from). Show them as
        # unsubscribed so the user can re-subscribe via the click flow.
        directory_cids = {ch.cid for ch in self._ui._channels}
        for row in store_channels:
            cid = int(row["cid"])
            if cid in subscribed_cids or cid in directory_cids:
                continue
            self._add_target(("ch", str(cid)), unsubscribed=True)
        # DMs: distinct peers from the store. The accessor is
        # ``list_dm_peers(my_call)`` (not ``list_message_peers()``);
        # each row's callsign is in the ``"peer"`` field. An earlier
        # version of this method called the wrong name with a broad
        # ``except`` that swallowed the ``AttributeError``, so DM
        # threads from a populated store never showed up at startup.
        try:
            peers = self._ui._client._store.list_dm_peers(self._ui._my_call)  # type: ignore[attr-defined]
        except Exception as e:
            _log.warning("list_dm_peers: %s", e)
            peers = []
        for row in peers:
            call = row.get("peer")
            if call:
                self._add_target(("dm", str(call).upper()))
        # Pinned "Add Call to DM" row at the top of the DMs list.
        if self._dms_walker is not None:
            self._dms_walker.insert(
                0,
                _FocusableText(
                    [("dim", "+ Add DM call…")],
                    on_activate=self._open_new_dm_modal,
                ),
            )
        # Tab badges sum across `_unread`, which `_add_target` may have
        # just seeded from the persistent cursor. Refresh once after the
        # whole bootstrap so badges reflect that initial state — without
        # this, the (N) suffix on the inactive tab would only appear on
        # the first tab switch.
        self._refresh_tab_labels()

    def _target_id(self, target: TargetKey) -> str:
        kind, key = target
        return f"{kind}-{_safe_id(str(key))}"

    def _target_label(self, target: TargetKey, *, unsubscribed: bool = False) -> list:
        kind, key = target
        unread = self._unread.get(target, 0)
        suffix = f" ({unread})" if unread else ""
        if kind == "ch":
            try:
                cid = int(key)
            except ValueError:
                cid = -1
            name = self._channel_name(cid) or ""
            paused = 0
            if not unsubscribed and self._ui._client is not None:
                paused = self._ui._client.paused_channels().get(cid, 0)
            if paused:
                box = "⏸"
                box_attr = "yellow"
            elif unsubscribed:
                box = "☐"
                box_attr = "dim"
            else:
                box = "☑"
                box_attr = "subscribe_check"
            return [
                (box_attr, f"{box} "),
                ("default", f"{cid} #{name}"),
                ("unread_badge", suffix),
            ]
        # DM.
        ham = self._ui._client.ham_name(key)
        label = f"{key}" if not ham else f"{key} ({ham})"
        return [
            ("default", label),
            ("unread_badge", suffix),
        ]

    def _add_target(
        self, target: TargetKey, *, unsubscribed: bool = False
    ) -> None:
        if target in self._target_items:
            return
        kind, _ = target
        # ``_target_label`` reads ``_unread`` for the (N) suffix;
        # ``_seed_unread_from_store`` runs once at session start
        # (before any wire events flow) and pre-fills counts left over
        # from the previous session, so the label here picks them up.
        # ``wrap="clip"``: target-list rows must stay single-line — a
        # wrapped row would break the left pane's vertical alignment
        # against the message pane (the ListBox treats each row as one
        # cell tall). Overlong labels get clipped at the column edge.
        item = _FocusableText(
            self._target_label(target, unsubscribed=unsubscribed),
            on_activate=lambda t=target: self._on_target_activate(t),
            wrap="clip",
        )
        self._target_items[target] = item
        if kind == "ch" and self._channels_walker is not None:
            self._channels_walker.append(item)
        elif kind == "dm" and self._dms_walker is not None:
            # Append after the pinned "+ Add DM call…" row.
            self._dms_walker.append(item)

    def _seed_unread_from_store(self) -> None:
        """Pre-fill ``_unread`` from the persistent cursors. Called once
        per session, *before* any wire events are dispatched, so the
        baseline reflects unread state left over from earlier sessions
        without double-counting fresh arrivals (which the live
        increment paths handle separately).

        Idempotent: only fills entries that aren't already present.
        """
        if self._unread_seeded:
            return
        client = self._ui._client
        if client is None or getattr(client, "_store", None) is None:
            return
        store = client._store  # type: ignore[attr-defined]
        me = self._ui._my_call
        try:
            posts = store.unread_post_counts_all(me)
            dms = store.unread_dm_counts_all(me)
        except Exception:
            return
        for cid, n in posts.items():
            self._unread.setdefault(("ch", str(cid)), n)
        for peer, n in dms.items():
            self._unread.setdefault(("dm", peer), n)
        self._unread_seeded = True

    def _refresh_target_label(self, target: TargetKey) -> None:
        if target not in self._target_items:
            return
        kind, key = target
        unsubscribed = False
        if kind == "ch":
            try:
                cid = int(key)
                unsubscribed = not self._is_subscribed(cid)
            except ValueError:
                pass
        self._target_items[target].set_markup(
            self._target_label(target, unsubscribed=unsubscribed)
        )

    def _refresh_all_target_labels(self) -> None:
        for t in list(self._target_items.keys()):
            self._refresh_target_label(t)

    def _refresh_tab_labels(self) -> None:
        # Sum unread per kind and badge both tabs regardless of which is
        # active — the count ticks down (and the suffix disappears at 0)
        # as the user reads individual channels / DMs.
        bar = self._left_tab_bar
        if bar is None:
            return
        ch_unread = sum(n for (k, _), n in self._unread.items() if k == "ch" and n)
        dm_unread = sum(n for (k, _), n in self._unread.items() if k == "dm" and n)
        ch_btn = bar._buttons.get("ch")
        dm_btn = bar._buttons.get("dm")
        if ch_btn is not None:
            ch_btn.set_label(f"Channels ({ch_unread})" if ch_unread else "Channels")
        if dm_btn is not None:
            dm_btn.set_label(f"DMs ({dm_unread})" if dm_unread else "DMs")

    def _on_target_activate(self, target: TargetKey) -> None:
        kind, key = target
        if kind == "ch":
            try:
                cid = int(key)
            except ValueError:
                cid = -1
            if cid >= 0 and not self._ui._offline:
                paused = self._ui._client.paused_channels().get(cid, 0)
                if paused:
                    self._open_unpause_modal(cid, target=target, pending=paused)
                    return
                if not self._is_subscribed(cid):
                    self._open_subscribe_modal(cid, target=target)
                    return
        self._ui._target = target
        asyncio.create_task(self._switch_centre_to(target))
        self._refresh_input_caption()
        self._refresh_footer()
        self._refresh_thread_header(target)
        if self._input is not None:
            self._frame.focus_position = "footer"

    def _on_left_tab_change(self, tid: str) -> None:
        if tid == "ch":
            self._target_switcher.original_widget = self._channels_listbox
        else:
            self._target_switcher.original_widget = self._dms_listbox
        self._refresh_tab_labels()

    # ------------------------------------------------------------------
    # Centre pane / per-target message ListBox
    # ------------------------------------------------------------------

    async def _ensure_message_view(self, target: TargetKey) -> urwid.ListBox:
        if target in self._views:
            return self._views[target]
        walker = urwid.SimpleFocusListWalker([])
        listbox = _MessageListBox(walker, app=self, target=target)
        self._walkers[target] = walker
        self._views[target] = listbox
        self._mount_initial_history(target, walker)
        return listbox

    async def _switch_centre_to(self, target: TargetKey) -> None:
        lv = await self._ensure_message_view(target)
        # If verbose mode flipped while we were elsewhere, refresh now.
        if target in self._verbose_dirty:
            self._refresh_target_rows(target)
            self._verbose_dirty.discard(target)
        if self._centre_placeholder is not None:
            self._centre_placeholder.original_widget = lv
        # Activating clears the in-memory unread count and advances the
        # persistent read cursor so the badge stays at 0 across restart
        # rather than coming back from the store.
        kind, key = target
        store = self._ui._client._store  # type: ignore[attr-defined]
        if kind == "dm":
            store.mark_dm_read(key)
        else:
            try:
                store.mark_channel_read(int(key))
            except ValueError:
                pass
        if self._unread.get(target):
            self._unread[target] = 0
            self._refresh_target_label(target)
            self._refresh_tab_labels()

    def _refresh_thread_header(self, target: TargetKey | None = None) -> None:
        if self._thread_header is None:
            return
        if target is None:
            target = self._ui._target
        if target is None:
            self._thread_header.set_text("(no target)")
            return
        kind, key = target
        if kind == "dm":
            ham = self._ui._client.ham_name(key)
            label = f"DM: {key}" + (f" ({ham})" if ham else "")
        else:
            try:
                cid = int(key)
            except ValueError:
                cid = -1
            name = self._channel_name(cid) or ""
            label = f"# {name} (ch:{cid})" if name else f"ch:{cid}"
        self._thread_header.set_text(label)

    def _active_target(self) -> TargetKey | None:
        return self._ui._target

    def _initial_load_count(self) -> int:
        # Floor only — enough to avoid a blank pane between mount and
        # the first render. Once the listbox renders, its real height
        # is captured and ``_fill_pane_initial`` tops up to match.
        return max(self._ui._history_backfill, 10)

    def _mount_initial_history(
        self, target: TargetKey, walker: urwid.SimpleFocusListWalker
    ) -> None:
        n = self._initial_load_count()
        if n <= 0:
            return
        kind, key = target
        rows: list[dict] = []
        store = self._ui._client._store  # type: ignore[attr-defined]
        try:
            if kind == "dm":
                # ``recent_messages(peer, limit, *, before_ts)`` — first
                # positional is ``peer``.
                rows = list(store.recent_messages(key, limit=n))
            else:
                try:
                    cid = int(key)
                except ValueError:
                    return
                # ``recent_posts(channel_id, limit, *, before_ts)`` —
                # the first positional is ``channel_id``, NOT ``cid``.
                # Calling it with ``cid=`` raises ``TypeError`` and was
                # being swallowed by the broad except below, so post
                # history silently never loaded into the centre pane.
                rows = list(store.recent_posts(cid, limit=n))
        except Exception as e:
            _log.warning(
                "_mount_initial_history(%s): %s", target, e
            )
            return
        rows.reverse()  # store returns newest-first; mount oldest-first
        bulk = self._bulk_reactions(target, rows)
        for row in rows:
            self._mount_row(target, walker, row, append=True, reactions_by_key=bulk, defer_scroll=True)
        # Scroll to bottom
        if walker:
            walker.set_focus(len(walker) - 1)

    def _bulk_reactions(
        self, target: TargetKey, rows: list[dict]
    ) -> dict | None:
        kind, key = target
        store = self._ui._client._store  # type: ignore[attr-defined]
        try:
            if kind == "dm":
                ids = [r.get("id") for r in rows if r.get("id")]
                if hasattr(store, "list_message_emojis_for_ids"):
                    return store.list_message_emojis_for_ids(ids)
            else:
                try:
                    cid = int(key)
                except ValueError:
                    return None
                ts_list = [r.get("ts") for r in rows if r.get("ts") is not None]
                if hasattr(store, "list_post_emojis_for_keys"):
                    return store.list_post_emojis_for_keys(cid, ts_list)
        except Exception:
            return None
        return None

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
    ) -> _MessageRow:
        kind, target_key = target
        natural = self._natural_key_for_row(kind, row)
        if reactions_by_key is not None:
            rkey = (
                row.get("id") if kind == "dm" else row.get("ts")
            )
            reactions = reactions_by_key.get(rkey, []) if rkey is not None else []
        else:
            reactions = self._lookup_reactions(kind, target_key, natural)
        msg_row = _MessageRow(
            kind=kind,
            target_key=target_key,
            natural_key=natural,
            from_call=row.get("from_call") or row.get("fc") or "",
            body=row.get("body") or row.get("m") or row.get("p") or "",
            ts=row.get("ts"),
            edit_ts=row.get("edit_ts") or row.get("edts"),
            delivered_ts=row.get("delivered_ts") or row.get("dts"),
            received_ts=row.get("received_ts"),
            realtime=row.get("realtime"),
            lid=row.get("lid"),
            reactions=reactions,
            reply_id=row.get("reply_id") or row.get("r"),
            reply_ts=row.get("reply_ts") or row.get("rts"),
            reply_from=row.get("reply_from") or row.get("rfc"),
            at_calls=at_calls_from_row(row),
        )
        # Wire mouse-click → action menu so clicking a row opens the
        # Edit/Resend/React menu (same as Enter on the keyboard).
        msg_row._mouse_activate = self._open_action_menu  # type: ignore[assignment]
        return msg_row

    def _lookup_reactions(
        self, kind: str, target_key: str, natural_key: str
    ) -> list[dict]:
        store = self._ui._client._store  # type: ignore[attr-defined]
        try:
            if kind == "dm":
                return list(store.list_message_emojis(natural_key))
            try:
                cid = int(target_key)
                ts = int(natural_key)
            except ValueError:
                return []
            return list(store.list_post_emojis(cid, ts))
        except Exception:
            return []

    def _chronological_insert_index(
        self,
        walker: urwid.SimpleFocusListWalker,
        new_row: _MessageRow,
    ) -> int | None:
        """Index at which to insert ``new_row`` to keep MessageRows
        sorted by ``ts``, or ``None`` to append at the end.

        Edits don't reach this path — ``_mount_row`` early-returns on
        natural-key collision — so the sort key is always the
        original ``ts``. Non-MessageRow walker entries (if any) keep
        their relative position; same-``ts`` arrivals tie-break by
        arrival order via ``<=``.
        """
        if new_row.ts is None:
            return None
        last_dated = -1
        for i in range(len(walker) - 1, -1, -1):
            w = walker[i]
            if isinstance(w, _MessageRow) and w.ts is not None:
                last_dated = i
                break
        if last_dated == -1:
            return None
        if walker[last_dated].ts <= new_row.ts:
            return None
        for i in range(last_dated - 1, -1, -1):
            w = walker[i]
            if isinstance(w, _MessageRow) and w.ts is not None and w.ts <= new_row.ts:
                return i + 1
        return 0

    def _mount_row(
        self,
        target: TargetKey,
        walker: urwid.SimpleFocusListWalker,
        row: dict,
        *,
        append: bool,
        reactions_by_key: dict | None = None,
        defer_scroll: bool = False,
    ) -> _MessageRow | None:
        kind, target_key = target
        natural = self._natural_key_for_row(kind, row)
        rkey = (kind, target_key, natural)
        if rkey in self._rows:
            return None
        msg_row = self._build_row(target, row, reactions_by_key=reactions_by_key)
        msg_row.refresh_label(
            my_call=self._ui._my_call,
            verbose=self._ui._options.verbose_history,
            ham_name=self._ui._client.ham_name,
            delivery_timeout_s=self._ui._options.delivery_timeout_s,
            show_edits=self._ui._options.show_edits,
            reply_meta=self._reply_meta_for_row(msg_row),
        )
        self._rows[rkey] = msg_row
        if append:
            insert_at = self._chronological_insert_index(walker, msg_row)
            if insert_at is None:
                walker.append(msg_row)
            else:
                walker.insert(insert_at, msg_row)
        else:
            walker.insert(0, msg_row)
        if not defer_scroll and append and walker:
            walker.set_focus(len(walker) - 1)
        return msg_row

    def _refresh_row_label(self, row: _MessageRow) -> None:
        row.refresh_label(
            my_call=self._ui._my_call,
            verbose=self._ui._options.verbose_history,
            ham_name=self._ui._client.ham_name,
            delivery_timeout_s=self._ui._options.delivery_timeout_s,
            show_edits=self._ui._options.show_edits,
            reply_meta=self._reply_meta_for_row(row),
        )

    def _reply_meta_for_row(self, row: _MessageRow) -> dict | None:
        """Resolve a row's reply metadata against the live store.

        Mirrors :meth:`ui.textual_ui._WhatspycApp._reply_meta_for_row`. Returns
        ``None`` for non-reply rows so they pay no DB cost.
        """
        if (
            row.reply_id is None
            and row.reply_ts is None
            and row.reply_from is None
        ):
            return None
        store = self._ui._client._store  # type: ignore[attr-defined]
        proxy = {
            "reply_id": row.reply_id,
            "reply_ts": row.reply_ts,
            "reply_from": row.reply_from,
        }
        return resolve_reply_meta(store, row.kind, row.tkey, proxy)

    def _refresh_target_rows(self, target: TargetKey) -> None:
        walker = self._walkers.get(target)
        if walker is None:
            return
        for w in walker:
            if isinstance(w, _MessageRow):
                self._refresh_row_label(w)

    def _refresh_active_rows(self) -> None:
        target = self._active_target()
        if target is None:
            return
        self._refresh_target_rows(target)

    async def _load_older(
        self, target: TargetKey, *, n: int | None = None
    ) -> None:
        if self._history_exhausted.get(target):
            return
        walker = self._walkers.get(target)
        if walker is None:
            return
        # Find oldest mounted row's ts.
        oldest_ts: int | None = None
        for w in walker:
            if isinstance(w, _MessageRow) and w.ts is not None:
                oldest_ts = int(ts_to_ms(w.ts) or 0)
                break
        kind, key = target
        store = self._ui._client._store  # type: ignore[attr-defined]
        rows: list[dict] = []
        if n is None:
            n = self._ui._history_backfill or 10
        try:
            if kind == "dm":
                rows = list(
                    store.recent_messages(key, limit=n, before_ts=oldest_ts)
                )
            else:
                try:
                    cid = int(key)
                except ValueError:
                    return
                # Positional ``channel_id`` — see ``_mount_initial_history``.
                rows = list(
                    store.recent_posts(cid, limit=n, before_ts=oldest_ts)
                )
        except Exception as e:
            _log.warning(
                "_load_older(%s): %s", target, e
            )
            return
        if not rows:
            self._history_exhausted[target] = True
            return
        # rows are newest-first; we need to insert oldest-first at the top.
        rows.reverse()
        bulk = self._bulk_reactions(target, rows)
        # Anchor: keep the previously-top row in view after prepend.
        prev_top_pos = walker.focus
        prev_top_row: _MessageRow | None = None
        if prev_top_pos is not None and 0 <= prev_top_pos < len(walker):
            prev_top_row = walker[prev_top_pos] if isinstance(walker[prev_top_pos], _MessageRow) else None
        # Insert in reverse order so each one ends up at the top in correct order.
        for row in reversed(rows):
            kind_, target_key = target
            natural = self._natural_key_for_row(kind_, row)
            rkey = (kind_, target_key, natural)
            if rkey in self._rows:
                continue
            msg_row = self._build_row(target, row, reactions_by_key=bulk)
            msg_row.refresh_label(
                my_call=self._ui._my_call,
                verbose=self._ui._options.verbose_history,
                ham_name=self._ui._client.ham_name,
                delivery_timeout_s=self._ui._options.delivery_timeout_s,
                show_edits=self._ui._options.show_edits,
                reply_meta=self._reply_meta_for_row(msg_row),
            )
            self._rows[rkey] = msg_row
            walker.insert(0, msg_row)
        # Restore anchor.
        if prev_top_row is not None:
            try:
                walker.set_focus(walker.index(prev_top_row))
            except ValueError:
                pass

    async def _fill_pane_initial(self, target: TargetKey, height: int) -> None:
        # Called once per target from _MessageListBox.render after the
        # first paint, when the rendered (maxcol, maxrow) is known. Tops
        # up the walker to the visible height so the pane opens already
        # full when the local store has enough history.
        if self._history_exhausted.get(target):
            return
        walker = self._walkers.get(target)
        if walker is None:
            return
        deficit = height - len(walker)
        if deficit <= 0:
            return
        await self._load_older(target, n=deficit)

    # ------------------------------------------------------------------
    # Online pane (incremental diff).
    # ------------------------------------------------------------------

    def _refresh_online_pane(self, users: list[str]) -> None:
        if self._online_walker is None:
            return
        users_upper = [u.upper() for u in users]
        new_set = set(users_upper)
        # Drop logged-off users.
        for u in list(self._online_items.keys()):
            if u not in new_set:
                w = self._online_items.pop(u)
                self._online_label_cache.pop(u, None)
                try:
                    self._online_walker.remove(w)
                except ValueError:
                    pass
        # Append new joins; relabel existing only if name changed.
        ham_name = self._ui._client.ham_name
        for u in users_upper:
            label_text = _user_label(u, ham_name)
            if u in self._online_items:
                if self._online_label_cache.get(u) != label_text:
                    self._online_items[u].set_markup(label_text)
                    self._online_label_cache[u] = label_text
                continue
            item = _FocusableText(label_text)
            self._online_items[u] = item
            self._online_label_cache[u] = label_text
            self._online_walker.append(item)
        if self._online_count_label is not None:
            self._online_count_label.set_text(f"Online ({len(self._online_items)})")

    def _schedule_he_refresh(self) -> None:
        if self._loop is None:
            return
        if self._he_alarm is not None:
            return
        self._he_alarm = self._loop.set_alarm_in(0.05, self._do_he_refresh)

    def _do_he_refresh(self, *_args: Any) -> None:
        self._he_alarm = None
        self._refresh_online_pane(list(self._online_items.keys()))
        self._refresh_all_target_labels()
        self._refresh_thread_header()

    # ------------------------------------------------------------------
    # Status pane.
    # ------------------------------------------------------------------

    def _refresh_status_pane(self) -> None:
        if self._status_holder is None:
            return
        if self._status_visible:
            self._status_holder.original_widget = urwid.AttrMap(
                urwid.LineBox(self._status_listbox, title="Log"),
                None,
            )
        else:
            self._status_holder.original_widget = urwid.Filler(urwid.Text(""))
        # Add/remove the holder from the centre Pile so the message
        # ListBox reclaims the space when status is hidden, and yields
        # it back when status is shown.
        if self._centre_pane is not None:
            contents = self._centre_pane.contents
            idx = next(
                (i for i, (w, _) in enumerate(contents) if w is self._status_holder),
                None,
            )
            if self._status_visible and idx is None:
                contents.insert(
                    0, (self._status_holder, self._centre_pane.options("weight", 1))
                )
            elif not self._status_visible and idx is not None:
                del contents[idx]

    def _status_write(self, markup: Any) -> None:
        if self._status_walker is None:
            return
        # Selectable wrapper so the Log ListBox can navigate / scroll
        # via the keyboard once the user Tabs to it. urwid.Text alone
        # is non-selectable, leaving ListBox unable to move its focus
        # in response to Up/Down/PgUp/PgDn — the keys would fall
        # through to the unhandled-input fallback that scrolls the
        # *message* list, which is the wrong pane.
        self._status_walker.append(_FocusableText(markup))
        if self._status_walker:
            self._status_walker.set_focus(len(self._status_walker) - 1)

    def _status_error(self, markup: Any) -> None:
        # Auto-show pane on error.
        if not self._status_visible:
            self._status_visible = True
            self._refresh_status_pane()
        self._status_write(markup)

    def action_toggle_status(self) -> None:
        # If the user is currently focused on the Log pane, hiding it
        # would leave focus pointing at a removed Pile child. Move
        # focus back to the input first so the cycle remains valid.
        was_focused_on_log = (
            self._status_visible and self._current_focus_step() == "log"
        )
        self._status_visible = not self._status_visible
        self._refresh_status_pane()
        if was_focused_on_log and not self._status_visible:
            self._focus_input()

    # ------------------------------------------------------------------
    # Refusal helper for offline mode.
    # ------------------------------------------------------------------

    def _refuse_offline(self, what: str) -> bool:
        if not self._ui._offline:
            return False
        self._status_error(("yellow", f"[offline] {what} unavailable — read-only mode (no connection)"))
        return True

    # ------------------------------------------------------------------
    # Channel resolution helpers (mirrors LineUI / TextualUI).
    # ------------------------------------------------------------------

    def _channel_name(self, cid: int) -> str | None:
        for ch in self._ui._channels:
            if ch.cid == cid:
                return ch.name or None
        try:
            for row in self._ui._client._store.list_channels():  # type: ignore[attr-defined]
                if int(row["cid"]) == cid:
                    return row.get("name") or None
        except Exception:
            pass
        return None

    def _channel_ref(self, cid: int) -> str:
        name = self._channel_name(cid)
        return f"#{name} (ch:{cid})" if name else f"ch:{cid}"

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

    def _write_to_active(self, markup: Any) -> None:
        """Append a system / hint line to the currently active message
        ListBox. Mirrors ``TextualUI._write_to_active``: the line stays
        with the conversation context (visible in the message log)
        rather than being routed to the toggleable status pane.
        """
        target = self._active_target()
        if target is None:
            return
        walker = self._walkers.get(target)
        if walker is None:
            return
        walker.append(urwid.Text(markup))
        # Auto-scroll to the new line if the user is already at the
        # bottom (focus on the previous last row). Skip when they've
        # scrolled up to read older history so we don't yank the view.
        if walker:
            try:
                if walker.focus is None or walker.focus >= len(walker) - 2:
                    walker.set_focus(len(walker) - 1)
            except (IndexError, TypeError):
                pass

    def _known_cids(self) -> set[int]:
        cids: set[int] = {ch.cid for ch in self._ui._channels}
        try:
            for row in self._ui._client._store.list_channels():  # type: ignore[attr-defined]
                cids.add(int(row["cid"]))
        except Exception:
            pass
        return cids

    def _resolve_channel(
        self, arg: str, *, allow_unknown_cid: bool = False
    ) -> int | None:
        s = arg.lstrip("#")
        try:
            cid = int(s)
        except ValueError:
            cid = None
        if cid is not None:
            if allow_unknown_cid or cid in self._known_cids():
                return cid
            return None
        # Bare name lookup.
        for ch in self._ui._channels:
            if ch.name and ch.name.lower() == s.lower():
                return ch.cid
        try:
            for row in self._ui._client._store.list_channels():  # type: ignore[attr-defined]
                name = row.get("name") or ""
                if name.lower() == s.lower():
                    return int(row["cid"])
        except Exception:
            pass
        return None

    def _is_subscribed(self, cid: int) -> bool:
        if self._subscribed_cids is None:
            try:
                # ``store.list_channels()`` returns every row in the
                # channels table — subscribed or not. The
                # ``subscribed`` column is the source of truth. An
                # earlier version of this method treated "any row in
                # the table" as subscribed, which made the click-to-
                # subscribe modal flow never trigger because every
                # known channel was reported as already subscribed.
                self._subscribed_cids = {
                    int(r["cid"])
                    for r in self._ui._client._store.list_channels()  # type: ignore[attr-defined]
                    if r.get("subscribed")
                }
            except Exception:
                self._subscribed_cids = set()
        return cid in self._subscribed_cids

    def _invalidate_subscribed_cids(self) -> None:
        self._subscribed_cids = None

    def _active_subscribed_channel_cid(self) -> int | None:
        target = self._active_target()
        if target is None:
            return None
        kind, key = target
        if kind != "ch":
            return None
        try:
            cid = int(key)
        except ValueError:
            return None
        return cid if self._is_subscribed(cid) else None

    def _active_target_is_subscribed_channel(self) -> bool:
        return self._active_subscribed_channel_cid() is not None

    # ------------------------------------------------------------------
    # Modal infrastructure
    # ------------------------------------------------------------------

    def _show_modal(self, modal: _Modal) -> asyncio.Future:
        """Layer ``modal`` on top of the current frame and return a Future
        that resolves when the modal dismisses.

        Always called from an async context (slash-command handlers,
        action callbacks). ``modal.attach`` lazy-creates the future
        from the running loop, so by the time we return it the caller
        can ``await`` cleanly.
        """
        body = modal.attach(self)
        if modal.future is None:
            # Defensive fallback if attach couldn't get a running loop.
            modal.future = asyncio.get_running_loop().create_future()
        if self._loop is None or self._frame_holder is None:
            modal.future.set_result(None)
            return modal.future
        cols, valign, rows_pct = modal.overlay_size
        prev_top = self._frame_holder.original_widget
        overlay = urwid.Overlay(
            body,
            prev_top,
            align="center",
            width=("relative", cols),
            valign=valign,
            height=("relative", rows_pct),
            min_width=20,
            min_height=modal.overlay_min_height,
        )
        self._modal_stack.append((modal, prev_top, None))
        self._frame_holder.original_widget = overlay
        # urwid's AsyncioEventLoop only auto-redraws after input events /
        # alarms (via the ``_also_call_idle`` wrapper). When ``_show_modal``
        # is reached from a chain of asyncio tasks deeper than the one the
        # input handler started — e.g. ``Enter`` → ``_handle_input_submit``
        # → ``_handle_command`` → ``action_quit_app`` → ``_wait`` — the
        # idle redraw fires before the inner task gets to set the overlay,
        # and the modal is invisible until the next keypress. Direct paths
        # (``ctrl x`` → ``action_quit_app`` → ``_wait``) happen to win the
        # race because the inner task runs before the idle. Force a draw
        # here so every entry path renders the modal at push time.
        self._loop.draw_screen()
        return modal.future

    def _dismiss_modal(self, modal: _Modal) -> None:
        if not self._modal_stack:
            return
        # Pop until we drop the requested modal.
        while self._modal_stack:
            top_modal, prev_top, _ = self._modal_stack.pop()
            if top_modal is modal:
                if self._frame_holder is not None:
                    self._frame_holder.original_widget = prev_top
                # Mirror ``_show_modal``: the AsyncioEventLoop only
                # auto-redraws on input events / alarms, so without a
                # forced draw the modal frame visibly lingers (until
                # the next keypress) even after the dismiss future has
                # resolved. Most painful on the connect-progress modal,
                # which dismisses programmatically after the connect
                # completes — the user sees the modal still up until
                # they nudge a key.
                if self._loop is not None:
                    try:
                        self._loop.draw_screen()
                    except Exception:
                        pass
                return
        # Should never reach here; defensive.
        if self._frame_holder is not None and self._frame is not None:
            self._frame_holder.original_widget = self._frame
            if self._loop is not None:
                try:
                    self._loop.draw_screen()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Modal opens (mirroring TextualUI helpers)
    # ------------------------------------------------------------------

    def _open_subscribe_modal(
        self, cid: int, *, target: TargetKey, skip_confirm: bool = False
    ) -> None:
        ref = self._channel_ref(cid)
        client = self._ui._client

        async def do_subscribe() -> int:
            return await client.subscribe_and_wait(cid)

        def default_count_for(pc: int) -> int:
            cap = client.auto_backfill_post_count or 10
            return min(cap, pc)

        modal = SubscribeModal(
            cid=cid,
            ref=ref,
            do_subscribe=do_subscribe,
            default_count_for=default_count_for,
            skip_confirm=skip_confirm,
        )

        async def _wait() -> None:
            n = await self._show_modal(modal)
            if n is None:
                # User pressed Esc. ``subscribe_and_wait`` does the cs
                # round-trip during the ``subscribing`` stage, so by the
                # time the count prompt appears we are already
                # subscribed on the server (and the local store has
                # been updated by ``_on_subscribe_ack``). Honour the
                # cancel by undoing the subscription. If the cancel
                # came mid-flight, give the kickoff task a chance to
                # finish so we know whether the ack landed.
                if modal.kickoff_task is not None and not modal.kickoff_task.done():
                    try:
                        await modal.kickoff_task
                    except Exception:
                        pass
                if modal.subscribed_on_server:
                    try:
                        await client.unsubscribe(cid)
                    except Exception as e:
                        self._status_error(("red", f"[unsubscribe] {e}"))
                    self._invalidate_subscribed_cids()
                    self._refresh_target_label(target)
                return
            self._invalidate_subscribed_cids()
            self._refresh_target_label(target)
            self._ui._target = target
            self._add_target(target)
            await self._switch_centre_to(target)
            self._refresh_input_caption()
            self._refresh_footer()
            self._refresh_thread_header(target)
            if int(n) > 0:
                try:
                    await client.request_post_batch(cid, int(n))
                except Exception as e:
                    self._status_error(("red", f"[fetch] {e}"))

        asyncio.create_task(_wait())

    def _open_unpause_modal(
        self, cid: int, *, target: TargetKey, pending: int
    ) -> None:
        ref = self._channel_ref(cid)
        client = self._ui._client

        def default_count_for(n: int) -> int:
            cap = client.auto_backfill_post_count or 10
            return min(cap, n)

        modal = UnpauseModal(
            cid=cid,
            ref=ref,
            pending=pending,
            default_count_for=default_count_for,
        )

        async def _wait() -> None:
            n = await self._show_modal(modal)
            if n is None:
                return
            try:
                await client.unpause_channel(cid, post_count=int(n))
            except Exception as e:
                self._status_error(("red", f"[unpause] {e}"))
                return
            self._refresh_target_label(target)
            self._ui._target = target
            self._add_target(target)
            await self._switch_centre_to(target)
            self._refresh_input_caption()
            self._refresh_footer()
            self._refresh_thread_header(target)

        asyncio.create_task(_wait())

    def _open_unsubscribe_modal(self, cid: int) -> None:
        ref = self._channel_ref(cid)
        modal = UnsubscribeModal(channel_ref=ref)

        async def _wait() -> None:
            ok = await self._show_modal(modal)
            if not ok:
                return
            try:
                await self._ui._client.unsubscribe(cid)
            except Exception as e:
                self._status_error(("red", f"[unsubscribe] {e}"))

        asyncio.create_task(_wait())

    def _open_new_dm_modal(self) -> None:
        modal = NewDmModal()

        async def _wait() -> None:
            call = await self._show_modal(modal)
            if not call:
                return
            target = ("dm", call)
            self._add_target(target)
            self._ui._target = target
            await self._switch_centre_to(target)
            self._refresh_input_caption()
            self._refresh_footer()
            self._refresh_thread_header(target)

        asyncio.create_task(_wait())

    def _open_settings_modal(self) -> None:
        def on_change(name: str, old: Any, new: Any) -> None:
            if name in ("verbose_history", "show_edits"):
                # Same dirty-set mechanic as the textual backend.
                active = self._active_target()
                self._verbose_dirty = {t for t in self._views if t != active}
                self._refresh_active_rows()
            elif name == "delivery_timeout_s":
                try:
                    self._ui._client.set_delivery_timeout_s(int(new))
                except Exception:
                    pass

        modal = SettingsModal(options=self._ui._options, on_change=on_change)
        # ``_show_modal`` is sync (returns the dismiss Future). The
        # SettingsModal does its work via the ``on_change`` callback,
        # so we don't need to await the future — fire and forget.
        self._show_modal(modal)

    def _open_action_menu(self, row: _MessageRow) -> None:
        # Mouse click on a row sets ListBox focus to the clicked row,
        # but doesn't update Frame.focus_position — that's still on
        # the footer (input) from the last target activation. Move
        # Frame focus over too so Up/Down arrows reach the message
        # list after the user dismisses the menu.
        self._set_focus_step("messages")
        is_mine = (row.from_call or "").upper() == self._ui._my_call
        reply_meta = self._reply_meta_for_row(row)
        allow_view_reply = bool(reply_meta and reply_meta.get("in_db"))
        modal = ActionMenu(
            allow_edit=is_mine,
            allow_resend=is_mine,
            allow_view_reply=allow_view_reply,
        )

        async def _wait() -> None:
            choice = await self._show_modal(modal)
            if choice == "edit":
                await self._begin_edit(row)
            elif choice == "resend":
                await self._do_resend(row)
            elif choice == "react":
                await self._do_react(row)
            elif choice == "reply":
                await self._begin_reply(row)
            elif choice == "view-reply":
                await self._do_view_reply(row, reply_meta)

        asyncio.create_task(_wait())

    async def _begin_reply(self, row: _MessageRow) -> None:
        """Enter reply mode (urwid). Mirrors the textual backend.

        Stashes the parent's identity on ``self._pending_reply``, swaps
        the input caption to surface the parent's call + the cancel
        hint, and focuses the input. Esc cancels via
        :meth:`_cancel_pending_reply` (driven from
        :meth:`_on_unhandled_input`).
        """
        if self._refuse_offline("replying"):
            return
        target = self._ui._target
        if target is None or target != (row.kind, row.tkey):
            self._status_error(
                ("yellow", "[hint] reply target must match the active thread")
            )
            return
        if row.kind == "ch":
            try:
                cid = int(row.tkey)
            except ValueError:
                return
            name = self._channel_name(cid)
            if name and name.lower() == "announcements":
                self._status_error(("yellow", "[Users cannot post to #announcements]"))
                return
            if not self._is_subscribed(cid):
                self._status_error(("yellow", self._unsubscribed_send_hint(cid)))
                return
            paused = self._ui._client.paused_channels().get(cid)
            if paused:
                self._status_error(("yellow", self._paused_hint(cid, paused)))
                return
        self._pending_reply = {
            "kind": row.kind,
            "target_key": row.tkey,
            "natural_key": row.natural_key,
            "parent_call": (row.from_call or "").upper(),
            "parent_ts": row.ts,
        }
        if self._input is not None:
            self._input.set_edit_text("")
        self._refresh_input_caption()
        self._set_focus_step("input")

    async def _do_view_reply(
        self, row: _MessageRow, reply_meta: dict | None
    ) -> None:
        if not reply_meta or not reply_meta.get("parent"):
            return
        parent = reply_meta["parent"]
        parent_kind = row.kind
        store = self._ui._client._store  # type: ignore[attr-defined]
        try:
            if parent_kind == "dm":
                reactions = list(
                    store.list_message_emojis(parent.get("id") or "")
                )
            else:
                reactions = list(
                    store.list_post_emojis(
                        int(parent.get("channel_id") or 0),
                        int(parent.get("ts") or 0),
                    )
                )
        except Exception:
            reactions = []
        markup = _render_row_markup(
            kind=parent_kind,
            from_call=parent.get("from_call") or "",
            body=parent.get("body") or "",
            ts=parent.get("ts"),
            edit_ts=parent.get("edit_ts"),
            delivered_ts=parent.get("delivered_ts"),
            received_ts=parent.get("received_ts"),
            realtime=parent.get("realtime"),
            lid=parent.get("lid"),
            my_call=self._ui._my_call,
            verbose=self._ui._options.verbose_history,
            ham_name=self._ui._client.ham_name,
            delivery_timeout_s=self._ui._options.delivery_timeout_s,
            show_edits=self._ui._options.show_edits,
            reactions=reactions,
            reply_meta=None,
        )
        if parent_kind == "dm":
            header = ("bold", f"Reply-to message from {parent.get('from_call') or ''}")
        else:
            cid = parent.get("channel_id")
            name = self._channel_name(int(cid)) if isinstance(cid, int) else None
            label = f"ch:{cid} #{name}" if name else f"ch:{cid}"
            header = ("bold", f"Reply-to post in {label}")
        await self._show_modal(ReplyToModal(header=header, body_markup=markup))

    async def _begin_edit(self, row: _MessageRow) -> None:
        if self._refuse_offline("editing"):
            return
        if (row.from_call or "").upper() != self._ui._my_call:
            return
        kind = row.kind
        natural = row.natural_key
        self._pending_edit = {"kind": kind, "natural_key": natural, "tkey": row.tkey, "lid": row.lid}
        if self._input is not None:
            self._input.set_edit_text(row.body)
            self._input.set_edit_pos(len(row.body))
        self._refresh_input_caption()
        if self._frame is not None:
            self._frame.focus_position = "footer"

    async def _do_resend(self, row: _MessageRow) -> None:
        if self._refuse_offline("resending"):
            return
        if (row.from_call or "").upper() != self._ui._my_call:
            return
        c = self._ui._client
        try:
            if row.kind == "dm":
                await c.resend_message(row.natural_key)
            else:
                cid = int(row.tkey)
                await c.resend_post(cid, int(row.natural_key))
        except ValueError as e:
            self._status_error(("yellow", f"[{e}]"))
            return
        self._status_write(("green", f"[resend] resent lid {row.lid}"))

    async def _do_react(self, row: _MessageRow) -> None:
        if self._refuse_offline("reacting"):
            return
        modal = EmojiPrompt()
        emoji = await self._show_modal(modal)
        if not emoji:
            return
        c = self._ui._client
        try:
            if row.kind == "dm":
                await c.react_message(row.natural_key, emoji)
            else:
                cid = int(row.tkey)
                await c.react_post(cid, int(row.natural_key), emoji)
        except Exception as e:
            self._status_error(("red", f"[react] {e}"))
            return
        # Re-pull reactions and refresh.
        row.reactions = self._lookup_reactions(row.kind, row.tkey, row.natural_key)
        self._refresh_row_label(row)

    # ------------------------------------------------------------------
    # Slash-command + send dispatch.
    # ------------------------------------------------------------------

    async def _handle_input_submit(self) -> None:
        if self._input is None:
            return
        text = self._input.edit_text
        self._input.set_edit_text("")
        text = text.rstrip("\n")
        if not text:
            return
        if self._pending_edit is not None:
            await self._consume_pending_edit(text)
            return
        if self._pending_reply is not None:
            await self._consume_pending_reply(text)
            return
        if text.startswith("/"):
            try:
                await self._handle_command(text)
            except Exception as e:
                self._status_error(("red", f"[error] {e}"))
            return
        await self._send_to_target(text)

    async def _consume_pending_reply(self, text: str) -> None:
        """Send a reply for ``self._pending_reply``, then exit reply mode.

        Mirrors the textual backend. After the wire send, the row is
        mounted locally — the server only echoes acks, never the
        original send, so without a local mount the user wouldn't see
        their own reply until reconnect. The mounted row carries the
        wire reply fields (``r`` for DMs, ``rts``/``rfc`` for posts)
        so the inline ``[Reply To …]`` prefix renders immediately.
        """
        reply = self._pending_reply
        self._pending_reply = None
        self._refresh_input_caption()
        if reply is None:
            return
        if self._refuse_offline("replying"):
            return
        c = self._ui._client
        try:
            if reply["kind"] == "dm":
                msg_id = await c.send_message(
                    reply["target_key"], text, reply_id=reply["natural_key"]
                )
                try:
                    ts_seconds = int(msg_id.split("-", 1)[0]) // 1000
                except (ValueError, AttributeError):
                    ts_seconds = int(time.time())
                await self._handle_inbound_dm(
                    {
                        "_id": msg_id,
                        "fc": self._ui._my_call,
                        "tc": reply["target_key"].upper(),
                        "m": text,
                        "ts": ts_seconds,
                        "r": reply["natural_key"],
                    },
                    batched=False,
                )
            else:
                cid = int(reply["target_key"])
                parent_ts = int(reply["natural_key"])
                body, at_calls = parse_post_mentions(text)
                extra: dict = {"at_calls": at_calls} if at_calls else {}
                ts = await c.post(
                    cid,
                    body,
                    reply_ts=parent_ts,
                    reply_from=reply["parent_call"],
                    **extra,
                )
                local_frame: dict = {
                    "cid": cid,
                    "fc": self._ui._my_call,
                    "ts": ts,
                    "p": body,
                    "rts": parent_ts,
                    "rfc": reply["parent_call"],
                }
                if at_calls:
                    local_frame["at"] = at_calls
                await self._handle_inbound_post(local_frame, batched=False)
        except ValueError as exc:
            self._status_error(("yellow", f"[{exc}]"))

    async def _consume_pending_edit(self, text: str) -> None:
        pe = self._pending_edit
        self._pending_edit = None
        self._refresh_input_caption()
        if pe is None:
            return
        if self._refuse_offline("editing"):
            return
        c = self._ui._client
        try:
            if pe["kind"] == "dm":
                await c.edit_message(pe["natural_key"], text)
                edts = int(time.time() * 1000)
                self._handle_dm_edit(
                    {"_id": pe["natural_key"], "m": text, "edts": edts},
                    clear_delivered=True,
                )
            else:
                cid = int(pe["tkey"])
                ts = int(pe["natural_key"])
                await c.edit_post(cid, ts, text)
                edts = int(time.time() * 1000)
                self._handle_post_edit(
                    {"cid": cid, "ts": ts, "p": text, "edts": edts},
                    clear_delivered=True,
                )
        except ValueError as e:
            self._status_error(("yellow", f"[{e}]"))

    async def _send_to_target(self, text: str) -> None:
        if self._refuse_offline("send"):
            return
        target = self._ui._target
        if target is None:
            self._status_error(("yellow", "no current target. /dm CALL or /ch N first"))
            return
        kind, key = target
        c = self._ui._client
        try:
            if kind == "dm":
                msg_id = await c.send_message(key, text)
                # Server only sends back an ``mr`` ack — never echoes
                # the ``m`` frame back to the sender — so mount the
                # row locally. Without this the centre pane wouldn't
                # update at all when you send something. The row is
                # rendered dimmed because ``delivered_ts`` is None,
                # and the dim clears when ``mr`` lands and
                # ``_handle_dm_ack`` sets ``delivered_ts`` on the row.
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
                if not self._is_subscribed(cid):
                    self._status_error(
                        ("yellow", f"can't post to ch:{cid}: not subscribed (use /sub first)")
                    )
                    return
                body, at_calls = parse_post_mentions(text)
                extra: dict = {"at_calls": at_calls} if at_calls else {}
                ts = await c.post(cid, body, **extra)
                # Same optimistic-mount trick as DMs — server only
                # acks via ``cpr``, doesn't echo ``cp`` back to sender.
                # Mount with the stripped body + at so the local row
                # matches what was sent and what the store now holds.
                local_frame: dict = {
                    "cid": cid,
                    "fc": self._ui._my_call,
                    "ts": ts,
                    "p": body,
                }
                if at_calls:
                    local_frame["at"] = at_calls
                await self._handle_inbound_post(local_frame, batched=False)
        except Exception as e:
            self._status_error(("red", f"[send] {e}"))

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
                self._status_error(("yellow", f"/unsub: unknown channel {args[0]!r}"))
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
                self._status_error(("yellow", f"/editdm: LID must be int (got {args[0]!r})"))
                return
            row = c._store.lookup_message_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                self._status_error(("yellow", f"/editdm: no local message lid {lid}"))
                return
            new_body = " ".join(args[1:])
            edts = int(time.time() * 1000)
            try:
                await c.edit_message(row["id"], new_body)
            except ValueError as e:
                self._status_error(("yellow", f"[{e}]"))
                return
            self._handle_dm_edit(
                {"_id": row["id"], "m": new_body, "edts": edts}, clear_delivered=True
            )
        elif cmd == "/editpost" and len(args) >= 2:
            if self._refuse_offline("/editpost"):
                return
            try:
                lid = int(args[0])
            except ValueError:
                self._status_error(("yellow", f"/editpost: LID must be int (got {args[0]!r})"))
                return
            row = c._store.lookup_post_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                self._status_error(("yellow", f"/editpost: no local post lid {lid}"))
                return
            new_body = " ".join(args[1:])
            edts = int(time.time() * 1000)
            try:
                await c.edit_post(row["channel_id"], row["ts"], new_body)
            except ValueError as e:
                self._status_error(("yellow", f"[{e}]"))
                return
            self._handle_post_edit(
                {"cid": int(row["channel_id"]), "ts": int(row["ts"]), "p": new_body, "edts": edts},
                clear_delivered=True,
            )
        elif cmd == "/retrydm" and len(args) == 1:
            if self._refuse_offline("/retrydm"):
                return
            try:
                lid = int(args[0])
            except ValueError:
                self._status_error(("yellow", f"/retrydm: LID must be int"))
                return
            row = c._store.lookup_message_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                self._status_error(("yellow", f"/retrydm: no local message lid {lid}"))
                return
            try:
                await c.resend_message(row["id"])
            except ValueError as e:
                self._status_error(("yellow", f"[{e}]"))
                return
            self._status_write(("green", f"[retrydm] resent lid {lid}"))
        elif cmd == "/retrypost" and len(args) == 1:
            if self._refuse_offline("/retrypost"):
                return
            try:
                lid = int(args[0])
            except ValueError:
                self._status_error(("yellow", f"/retrypost: LID must be int"))
                return
            row = c._store.lookup_post_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                self._status_error(("yellow", f"/retrypost: no local post lid {lid}"))
                return
            try:
                await c.resend_post(row["channel_id"], row["ts"])
            except ValueError as e:
                self._status_error(("yellow", f"[{e}]"))
                return
            self._status_write(("green", f"[retrypost] resent lid {lid}"))
        elif cmd == "/react" and len(args) == 2:
            if self._refuse_offline("/react"):
                return
            target = self._ui._target
            if target is None:
                self._status_error(("yellow", "/react: no current target"))
                return
            try:
                lid = int(args[0])
            except ValueError:
                self._status_error(("yellow", "/react: LID must be int"))
                return
            kind, _ = target
            if kind == "dm":
                row = c._store.lookup_message_by_lid(lid)  # type: ignore[attr-defined]
                if row is None:
                    self._status_error(("yellow", f"/react: no local message lid {lid}"))
                    return
                await c.react_message(row["id"], args[1])
            else:
                row = c._store.lookup_post_by_lid(lid)  # type: ignore[attr-defined]
                if row is None:
                    self._status_error(("yellow", f"/react: no local post lid {lid}"))
                    return
                await c.react_post(row["channel_id"], row["ts"], args[1])
        elif cmd == "/dm" and len(args) == 1:
            call = args[0].upper()
            target = ("dm", call)
            self._ui._target = target
            self._add_target(target)
            await self._switch_centre_to(target)
            self._refresh_input_caption()
            self._refresh_footer()
            self._refresh_thread_header(target)
        elif cmd == "/ch" and len(args) == 1:
            cid = self._resolve_channel(args[0])
            if cid is None:
                self._status_error(("yellow", f"/ch: unknown channel {args[0]!r}"))
                return
            target = ("ch", str(cid))
            if not self._ui._offline:
                paused = self._ui._client.paused_channels().get(cid, 0)
                if paused:
                    self._open_unpause_modal(cid, target=target, pending=paused)
                    return
                if not self._is_subscribed(cid):
                    self._open_subscribe_modal(cid, target=target)
                    return
            self._ui._target = target
            self._add_target(target)
            await self._switch_centre_to(target)
            self._refresh_input_caption()
            self._refresh_footer()
            self._refresh_thread_header(target)
        elif cmd == "/set":
            self._open_settings_modal()
        elif cmd == "/history":
            self._handle_history_toggle(args, verbose=False)
        elif cmd == "/vhistory":
            self._handle_history_toggle(args, verbose=True)
        elif cmd == "/list":
            # The target list pane already shows subscribed channels and
            # DM peers, so dumping the same info into the Log pane is
            # just noise. Silently swallow the command.
            pass
        elif cmd == "/users":
            self._handle_users()
        else:
            self._status_error(("yellow", f"unknown or malformed: {line}"))

    def _handle_help(self, args: list[str]) -> None:
        focus = args[0] if args else None
        modal = HelpScreen(focus_command=focus)
        # ``_show_modal`` returns the dismiss Future synchronously;
        # HelpScreen's only outcome is "close it" so we don't await.
        self._show_modal(modal)

    def _handle_history_toggle(self, args: list[str], *, verbose: bool) -> None:
        del args
        self._ui._options.verbose_history = verbose
        active = self._active_target()
        self._verbose_dirty = {t for t in self._views if t != active}
        self._refresh_active_rows()

    async def _handle_sub(self, args: list[str]) -> None:
        if self._refuse_offline("/sub"):
            return
        cid = self._resolve_channel(args[0], allow_unknown_cid=True)
        if cid is None:
            self._status_error(("yellow", f"/sub: unknown channel {args[0]!r}"))
            return
        target = ("ch", str(cid))
        if len(args) == 2:
            try:
                n = int(args[1])
            except ValueError:
                self._status_error(("yellow", f"/sub: count must be int"))
                return
            try:
                pc = await self._ui._client.subscribe_and_wait(cid)
            except Exception as e:
                self._status_error(("red", f"/sub: {e}"))
                return
            self._invalidate_subscribed_cids()
            self._refresh_target_label(target)
            self._ui._target = target
            self._add_target(target)
            await self._switch_centre_to(target)
            self._refresh_input_caption()
            self._refresh_footer()
            self._refresh_thread_header(target)
            if n > 0:
                fetch = min(n, pc)
                if fetch > 0:
                    try:
                        await self._ui._client.request_post_batch(cid, fetch)
                    except Exception as e:
                        self._status_error(("red", f"[fetch] {e}"))
        else:
            self._open_subscribe_modal(cid, target=target, skip_confirm=True)

    async def _handle_unpause(self, args: list[str]) -> None:
        if self._refuse_offline("/unpause"):
            return
        cid = self._resolve_channel(args[0], allow_unknown_cid=True)
        if cid is None:
            self._status_error(("yellow", f"/unpause: unknown channel {args[0]!r}"))
            return
        if len(args) == 2:
            try:
                n = int(args[1])
            except ValueError:
                self._status_error(("yellow", "/unpause: count must be int"))
                return
            if n <= 0:
                self._status_error(("yellow", "/unpause: count must be positive"))
                return
        else:
            n = self._ui._client.paused_channels().get(cid, 0)
            if n <= 0:
                self._status_error(
                    (
                        "yellow",
                        f"/unpause cid={cid}: no pending count from pch "
                        f"headers; pass /unpause {cid} N",
                    )
                )
                return
        try:
            await self._ui._client.unpause_channel(cid, post_count=n)
        except Exception as e:
            self._status_error(("red", f"/unpause: {e}"))
            return
        self._refresh_target_label(("ch", str(cid)))
        self._status_write(
            ("green", f"[unpause] requested {n} post(s) for {self._channel_ref(cid)}")
        )

    def _handle_users(self) -> None:
        # Online users live in the always-visible left pane — re-render
        # from the client's authoritative snapshot rather than dumping
        # the same list into the Log pane.
        self._refresh_online_pane(self._ui._client.online_users())

    # ------------------------------------------------------------------
    # Event dispatch (invoked by drain worker).
    # ------------------------------------------------------------------

    async def _drain_events(self) -> None:
        while True:
            obj = await self._event_queue.get()
            try:
                await self._dispatch_event(obj)
            except Exception as e:
                self._status_error(("red", f"[dispatch] {e}"))
            if self._loop is not None:
                self._loop.draw_screen()

    async def _dispatch_event(self, obj: dict) -> None:
        # Wire-field reminder (matches WpsClient's per-type handlers):
        #   mb     → o["m"]   (DM batch)
        #   cpb    → o["p"]   (post batch)
        #   medb   → o["med"] (DM edit batch)
        #   cpedb  → o["ed"]  (post edit batch)
        #   memb   → o["mem"] (DM reaction batch)
        #   cpemb  → o["e"]   (post reaction-group batch)
        # An earlier version of this dispatcher read ``obj.get("o")``
        # everywhere, which was wrong: ``o`` is the type-`c` online
        # roster field and isn't present on these batch frames at all,
        # so the loops never executed and historic posts didn't render.
        t = obj.get("t")
        if t == "m":
            await self._handle_inbound_dm(obj, batched=False)
            self._maybe_bell()
        elif t == "mb":
            await self._handle_inbound_dm_batch(obj.get("m") or [])
        elif t == "cp":
            await self._handle_inbound_post(obj, batched=False)
            self._maybe_bell()
        elif t == "cpb":
            await self._handle_inbound_post_batch(int(obj.get("cid")), obj.get("p") or [])
        elif t == "mr":
            self._handle_dm_ack(obj)
        elif t == "cpr":
            self._handle_post_ack(obj)
        elif t == "med":
            self._handle_dm_edit(obj)
        elif t == "cped":
            self._handle_post_edit(obj)
        elif t == "medb":
            for entry in obj.get("med") or []:
                self._handle_dm_edit(entry)
        elif t == "cpedb":
            for entry in obj.get("ed") or []:
                self._handle_post_edit(entry)
        elif t == "mem":
            self._handle_dm_reaction(obj)
        elif t == "memb":
            for entry in obj.get("mem") or []:
                self._handle_dm_reaction(entry)
        elif t == "cpem":
            self._handle_post_reaction(obj)
        elif t == "cpemb":
            for grp in obj.get("e") or []:
                self._handle_post_reaction_group(grp)
        elif t == "cs":
            await self._handle_cs(obj)
        elif t == "pch":
            self._handle_pch(obj)
        elif t == "uc":
            self._handle_user_connect(obj)
        elif t == "ud":
            self._handle_user_disconnect(obj)
        elif t == "o":
            self._refresh_online_pane(list(obj.get("o") or []))
        elif t == "he":
            self._schedule_he_refresh()
        elif t == "c" and "n" not in obj:
            # Initial connect's `[Connected]` summary is emitted by the
            # connect-modal runner with the full ConnectSequence-derived
            # counts; only print here for auto-reconnect, where no such
            # runner is involved.
            if self._reconnect_summary_pending:
                self._reconnect_summary_pending = False
                summary = ConnectSummary(
                    server_post_count=obj.get("pc", 0),
                    received_message_count=obj.get("mc", 0),
                    paused_channels=[],
                    online_users=[],
                )
                self._status_write(
                    ("connect_line", _format_connected_line(summary))
                )
        elif t == "_disconnect":
            line = [
                ("disconnect_line", "[link]"),
                f" disconnected ({obj.get('reason') or ''})",
            ]
            self._status_error(line)
            if self._ui.session_driven:
                self._on_link_dropped()
            elif not self._ui._client.is_auto_reconnect:
                self._signal_terminal_link_loss()
        elif t == "_reconnecting":
            attempt = obj.get("attempt", 0)
            delay = obj.get("delay", 0)
            line = [
                ("reconnect_line", "[link]"),
                f" reconnect attempt {attempt} in {delay:.1f}s",
            ]
            self._status_error(line)
            if self._active_connect_modal is not None:
                self._active_connect_modal.add_line(
                    ("reconnect_line", f"Reconnect attempt {attempt} in {delay:.1f}s")
                )
        elif t == "_reconnect_failed":
            line = [
                ("reconnect_line", "[link]"),
                f" reconnect attempt {obj.get('attempt')} failed: {obj.get('error') or obj.get('exc') or ''}",
            ]
            self._status_error(line)
            if self._active_connect_modal is not None:
                self._active_connect_modal.add_line(
                    (
                        "reconnect_line",
                        f"Attempt {obj.get('attempt')} failed: "
                        f"{obj.get('error') or obj.get('exc') or ''}",
                    )
                )
        elif t == "_reconnected":
            line = [
                ("green", "[link]"),
                f" reconnected (attempt {obj.get('attempt')})",
            ]
            self._status_error(line)
            self._reconnect_summary_pending = True
            # Server-side subscription state may have shifted across
            # the link drop; rebuild the cache lazily on next consult.
            self._invalidate_subscribed_cids()
            if self._active_connect_modal is not None:
                self._active_connect_modal.add_line(
                    ("connect_line", f"Reconnected (attempt {obj.get('attempt')})")
                )
                # Brief pause so the success line is visible, then dismiss.
                modal = self._active_connect_modal
                attempt = obj.get("attempt")

                async def _close_after_brief_pause(m=modal, a=attempt) -> None:
                    try:
                        await asyncio.sleep(0.4)
                    except asyncio.CancelledError:
                        pass
                    if m.future is not None and not m.future.done():
                        m.dismiss(("reconnected", a))

                asyncio.create_task(_close_after_brief_pause())
        elif t == "_reconnect_giveup":
            line = [
                ("disconnect_line", "[link]"),
                f" giving up after {obj.get('attempts')} reconnect attempts",
            ]
            self._status_error(line)
            if self._ui.session_driven:
                if self._active_connect_modal is not None:
                    self._active_connect_modal.add_line(
                        (
                            "disconnect_line",
                            f"Giving up after {obj.get('attempts')} attempts",
                        )
                    )
                    if (
                        self._active_connect_modal.future is not None
                        and not self._active_connect_modal.future.done()
                    ):
                        self._active_connect_modal.dismiss(
                            ("giveup", obj.get("attempts"))
                        )
            else:
                self._signal_terminal_link_loss()
        elif t == "_silence_disconnect":
            line = [
                ("disconnect_line", "[link]"),
                " silence-disconnect — no traffic for too long",
            ]
            self._status_error(line)
            if self._ui.session_driven:
                self._on_link_dropped()
            else:
                self._signal_terminal_link_loss()
        elif t == "_delivery_timeout":
            self._handle_delivery_timeout(obj)
        elif t == "_error":
            self._status_error(("red", f"[error] {obj.get('msg') or obj.get('exc') or ''}"))

    def _signal_terminal_link_loss(self) -> None:
        self._ui.exit_reason = "terminal"
        if self._exit_future is not None and not self._exit_future.done():
            self._exit_future.set_result(None)

    def _maybe_bell(self) -> None:
        """Ring the terminal bell when ``bell_on_activity`` is on.

        urwid has no built-in bell, so we write the BEL byte through
        the screen's output file directly. Real-time DM/post arrivals
        only — batch frames (mb/cpb) bypass this so a fresh connect
        doesn't fire a flurry of beeps for the backlog.
        """
        if not self._ui._options.bell_on_activity:
            return
        if self._loop is None:
            return
        try:
            self._loop.screen.write("\a")
            flush = getattr(self._loop.screen, "flush", None)
            if flush is not None:
                flush()
        except Exception:
            pass

    # --- Inbound DM / post handlers ---

    async def _handle_inbound_dm(self, m: dict, *, batched: bool) -> None:
        fc = (m.get("fc") or "").upper()
        tc = (m.get("tc") or "").upper()
        peer = tc if fc == self._ui._my_call else fc
        target = ("dm", peer)
        self._add_target(target)
        active = self._active_target()
        if active == target:
            walker = self._walkers.get(target)
            if walker is None:
                lv = await self._ensure_message_view(target)
                walker = self._walkers.get(target)
            if walker is not None:
                row = self._build_row_from_wire("dm", target, m)
                self._mount_row_obj(target, walker, row, append=True)
            # Active-target arrival counts as read on landing — advance
            # the persistent cursor so this row isn't recounted as unread
            # next session.
            self._ui._client._store.mark_dm_read(peer)  # type: ignore[attr-defined]
        elif fc != self._ui._my_call:
            # Self-sent echoes (from another session) don't count toward
            # unread — match the [Connected] line's "from someone else"
            # semantics.
            self._unread[target] = self._unread.get(target, 0) + 1
            self._refresh_target_label(target)
            self._refresh_tab_labels()

    async def _handle_inbound_post(self, p: dict, *, batched: bool) -> None:
        cid = int(p.get("cid", 0))
        target = ("ch", str(cid))
        self._add_target(target)
        active = self._active_target()
        if active == target:
            walker = self._walkers.get(target)
            if walker is None:
                await self._ensure_message_view(target)
                walker = self._walkers.get(target)
            if walker is not None:
                row = self._build_row_from_wire("post", target, p)
                self._mount_row_obj(target, walker, row, append=True)
            self._ui._client._store.mark_channel_read(cid)  # type: ignore[attr-defined]
        else:
            self._unread[target] = self._unread.get(target, 0) + 1
            self._refresh_target_label(target)
            self._refresh_tab_labels()

    async def _handle_inbound_dm_batch(self, items: list[dict]) -> None:
        # Group by peer; track per-target inbound (non-self) count so
        # unread badges match the [Connected] line's "from someone else"
        # count.
        by_peer: dict[str, list[dict]] = {}
        inbound_counts: dict[str, int] = {}
        my_call = self._ui._my_call
        for m in items:
            fc = (m.get("fc") or "").upper()
            tc = (m.get("tc") or "").upper()
            peer = tc if fc == my_call else fc
            by_peer.setdefault(peer, []).append(m)
            if fc != my_call:
                inbound_counts[peer] = inbound_counts.get(peer, 0) + 1
        active = self._active_target()
        for peer, msgs in by_peer.items():
            target = ("dm", peer)
            self._add_target(target)
            if active == target:
                if target not in self._walkers:
                    await self._ensure_message_view(target)
                walker = self._walkers[target]
                bulk = self._bulk_reactions(target, msgs)
                for m in msgs:
                    row = self._build_row_from_wire("dm", target, m, bulk=bulk)
                    self._mount_row_obj(target, walker, row, append=True)
                self._ui._client._store.mark_dm_read(peer)  # type: ignore[attr-defined]
            else:
                inbound = inbound_counts.get(peer, 0)
                if inbound:
                    self._unread[target] = self._unread.get(target, 0) + inbound
                    self._refresh_target_label(target)
                    self._refresh_tab_labels()

    async def _handle_inbound_post_batch(self, cid: int, items: list[dict]) -> None:
        target = ("ch", str(cid))
        self._add_target(target)
        active = self._active_target()
        if active == target:
            if target not in self._walkers:
                await self._ensure_message_view(target)
            walker = self._walkers[target]
            bulk = self._bulk_reactions(target, items)
            for p in items:
                row = self._build_row_from_wire("post", target, p, bulk=bulk)
                self._mount_row_obj(target, walker, row, append=True)
            self._ui._client._store.mark_channel_read(cid)  # type: ignore[attr-defined]
        else:
            self._unread[target] = self._unread.get(target, 0) + len(items)
            self._refresh_target_label(target)
            self._refresh_tab_labels()

    def _build_row_from_wire(
        self,
        kind: str,
        target: TargetKey,
        wire: dict,
        *,
        bulk: dict | None = None,
    ) -> dict:
        """Convert a wire-form m/cp dict into the store-row shape that
        ``_build_row`` expects.

        WpsClient persists the row before emitting the event (see
        ``_dispatch`` in wps/client.py: handler runs before
        ``_on_event``), so look the canonical row up by its natural key
        first — that pulls in the SQLite rowid as ``lid`` so verbose
        render shows a real id instead of ``None``. Fall back to a
        wire-only dict if the lookup misses (defensive; shouldn't happen
        in practice)."""
        store = self._ui._client._store  # type: ignore[attr-defined]
        if kind == "dm":
            msg_id = wire.get("_id") or (
                f"{wire.get('ts')}-{wire.get('fc')}"
                if wire.get("ts") and wire.get("fc")
                else None
            )
            if msg_id:
                try:
                    row = store.lookup_message_by_id(msg_id)
                except Exception:
                    row = None
                if row is not None:
                    return row
        else:  # "post"
            _, target_key = target
            ts = wire.get("ts")
            if isinstance(ts, int):
                try:
                    cid = int(target_key)
                except ValueError:
                    cid = None
                if cid is not None:
                    try:
                        row = store.lookup_post(cid, ts)
                    except Exception:
                        row = None
                    if row is not None:
                        return row
        # Wire `at` is a list; mirror the persisted JSON-string shape so
        # at_calls_from_row decodes uniformly when this fallback row
        # flows into _build_row.
        at_wire = wire.get("at")
        at_serialised = None
        if isinstance(at_wire, list) and at_wire:
            import json as _json

            at_serialised = _json.dumps([str(c).upper() for c in at_wire])
        return {
            "id": wire.get("_id"),
            "ts": wire.get("ts"),
            "from_call": wire.get("fc"),
            "body": wire.get("m") or wire.get("p"),
            "edit_ts": wire.get("edts"),
            "delivered_ts": wire.get("dts"),
            "received_ts": int(time.time() * 1000) if wire.get("fc", "").upper() != self._ui._my_call else None,
            "realtime": 1,
            "lid": None,
            "reply_id": wire.get("r"),
            "reply_ts": wire.get("rts"),
            "reply_from": wire.get("rfc"),
            "at_calls": at_serialised,
        }

    def _mount_row_obj(
        self,
        target: TargetKey,
        walker: urwid.SimpleFocusListWalker,
        row_dict: dict,
        *,
        append: bool,
    ) -> None:
        self._mount_row(target, walker, row_dict, append=append)

    # --- Acks ---

    def _handle_dm_ack(self, obj: dict) -> None:
        msg_id = obj.get("_id")
        if not isinstance(msg_id, str):
            return
        try:
            store_row = self._ui._client._store.lookup_message_by_id(msg_id)  # type: ignore[attr-defined]
        except Exception:
            store_row = None
        if store_row is None:
            return
        peer = store_row.get("to_call") or store_row.get("from_call") or ""
        target: TargetKey = ("dm", str(peer))
        for (kind, tkey, nat), msg_row in self._rows.items():
            if kind == "dm" and nat == msg_id:
                msg_row.delivered_ts = (
                    store_row.get("delivered_ts") or int(time.time() * 1000)
                )
                self._refresh_row_label(msg_row)
                break
        if self._status_visible:
            ts_ms = ts_to_ms(store_row.get("ts"))
            now = int(time.time() * 1000)
            duration = _fmt_duration_ms(now - ts_ms) if ts_ms else "?"
            label = self._fmt_target_label(target)
            self._status_write(
                ("ack_line", f"[ack] {label} msg {store_row.get('lid')} delivered in {duration}")
            )

    def _handle_post_ack(self, obj: dict) -> None:
        ts = obj.get("ts")
        dts = obj.get("dts")
        if not isinstance(ts, int):
            return
        try:
            store_row = self._ui._client._store.lookup_post_by_from_ts(  # type: ignore[attr-defined]
                self._ui._my_call, ts
            )
        except Exception:
            store_row = None
        if store_row is None:
            return
        cid = store_row.get("channel_id")
        target: TargetKey = ("ch", str(cid))
        nat = str(int(ts))
        for (kind, tkey, n), msg_row in self._rows.items():
            if kind == "ch" and tkey == str(cid) and n == nat:
                msg_row.delivered_ts = (
                    int(dts) if isinstance(dts, int) else int(time.time() * 1000)
                )
                self._refresh_row_label(msg_row)
                break
        if self._status_visible:
            ts_ms = ts_to_ms(ts)
            end = ts_to_ms(dts) if isinstance(dts, int) else int(time.time() * 1000)
            duration = _fmt_duration_ms(end - ts_ms) if ts_ms else "?"
            label = self._fmt_target_label(target)
            self._status_write(
                ("ack_line", f"[ack] {label} post {store_row.get('lid')} delivered in {duration}")
            )

    # --- Edits ---

    def _handle_dm_edit(self, obj: dict, *, clear_delivered: bool = False) -> None:
        msg_id = obj.get("_id")
        if not isinstance(msg_id, str):
            return
        try:
            store_row = self._ui._client._store.lookup_message_by_id(msg_id)  # type: ignore[attr-defined]
        except Exception:
            store_row = None
        if store_row is None:
            return
        peer = store_row.get("to_call") or store_row.get("from_call") or ""
        target: TargetKey = ("dm", str(peer))
        body = obj.get("m") or store_row.get("body") or ""
        edts = obj.get("edts") or store_row.get("edit_ts") or int(time.time() * 1000)
        for (kind, tkey, nat), msg_row in self._rows.items():
            if kind == "dm" and nat == msg_id:
                msg_row.body = body
                msg_row.edit_ts = int(edts)
                if clear_delivered:
                    msg_row.delivered_ts = None
                self._refresh_row_label(msg_row)
                break
        if self._status_visible:
            self._status_write(
                ("yellow", f"[edit] {self._fmt_target_label(target)} msg {store_row.get('lid')} edited")
            )

    def _handle_post_edit(self, obj: dict, *, clear_delivered: bool = False) -> None:
        cid = obj.get("cid")
        ts = obj.get("ts")
        if not isinstance(cid, int) or not isinstance(ts, int):
            return
        try:
            store_row = self._ui._client._store.lookup_post(int(cid), int(ts))  # type: ignore[attr-defined]
        except Exception:
            store_row = None
        if store_row is None:
            return
        target: TargetKey = ("ch", str(int(cid)))
        nat = str(int(ts))
        body = obj.get("p") or store_row.get("body") or ""
        edts = obj.get("edts") or store_row.get("edit_ts") or int(time.time() * 1000)
        for (kind, tkey, n), msg_row in self._rows.items():
            if kind == "ch" and tkey == target[1] and n == nat:
                msg_row.body = body
                msg_row.edit_ts = int(edts)
                if clear_delivered:
                    msg_row.delivered_ts = None
                self._refresh_row_label(msg_row)
                break
        if self._status_visible:
            self._status_write(
                ("yellow", f"[edit] {self._fmt_target_label(target)} post {store_row.get('lid')} edited")
            )

    # --- Reactions ---

    def _handle_dm_reaction(self, obj: dict) -> None:
        msg_id = obj.get("_id")
        if not msg_id:
            return
        for (kind, tkey, nat), row in self._rows.items():
            if kind == "dm" and nat == msg_id:
                row.reactions = self._lookup_reactions("dm", tkey, nat)
                self._refresh_row_label(row)
                break

    def _handle_post_reaction(self, obj: dict) -> None:
        cid = obj.get("cid")
        ts = obj.get("ts")
        if cid is None or ts is None:
            return
        target_key = str(int(cid))
        nat = str(int(ts))
        for (kind, tkey, n), row in self._rows.items():
            if kind == "ch" and tkey == target_key and n == nat:
                row.reactions = self._lookup_reactions("ch", tkey, nat)
                self._refresh_row_label(row)
                break

    def _handle_post_reaction_group(self, group: dict) -> None:
        # ``cpemb`` group shape: ``{cid, ts, ets, e: [{e, c[]}, ...]}``.
        # Each group is a single post identified by (cid, ts). The
        # underlying store has already been updated by ``WpsClient``
        # via ``apply_post_emoji_batch``; here we just refresh the
        # corresponding mounted row.
        cid = group.get("cid")
        ts = group.get("ts")
        if cid is None or ts is None:
            return
        target_key = str(int(cid))
        nat = str(int(ts))
        for (kind, tkey, n), row in self._rows.items():
            if kind == "ch" and tkey == target_key and n == nat:
                row.reactions = self._lookup_reactions("ch", tkey, nat)
                self._refresh_row_label(row)
                break

    # --- Channel state ---

    async def _handle_cs(self, obj: dict) -> None:
        cid = obj.get("cid")
        subscribed = bool(obj.get("s"))
        pc = obj.get("pc")
        self._invalidate_subscribed_cids()
        if cid is not None:
            target: TargetKey = ("ch", str(int(cid)))
            self._refresh_target_label(target)
            name = self._channel_name(int(cid))
            display = f"{int(cid)} #{name}" if name else str(int(cid))
        else:
            display = "channel"
        verb = "Subscribed to" if subscribed else "Unsubscribed from"
        if subscribed and isinstance(pc, int) and pc > 0:
            self._status_write(f"{verb} {display} ({pc} historic posts on server)")
        else:
            self._status_write(f"{verb} {display}")
        self._refresh_footer()

    def _handle_pch(self, obj: dict) -> None:
        # Wire shape: ``{t:pch, ch:[{cid, pt}, ...]}`` — one entry per
        # paused channel. ``pt`` is the count of pending posts. An
        # earlier version read ``obj.get("cid")`` / ``obj.get("pc")``
        # directly which was wrong: ``pch`` is a list, not a single row.
        for entry in obj.get("ch") or []:
            cid = entry.get("cid")
            pt = entry.get("pt")
            if not isinstance(cid, int) or not isinstance(pt, int):
                continue
            ref = self._channel_ref(cid)
            self._refresh_target_label(("ch", str(cid)))
            self._write_to_active(
                [
                    ("yellow", f"[paused {ref}]"),
                    f" {pt} pending posts — /unpause {cid} [N] to download",
                ]
            )

    def _handle_user_connect(self, obj: dict) -> None:
        # WPS wire format: ``uc`` carries the callsign in field ``c``,
        # NOT ``call``. An earlier version of this method read the
        # wrong key and silently no-op'd, so the online pane never
        # updated when someone joined live. Fall back to ``call`` for
        # synthetic / test frames.
        call = (obj.get("c") or obj.get("call") or "").upper()
        if not call:
            return
        users = list(self._online_items.keys())
        if call not in users:
            users.append(call)
        self._refresh_online_pane(users)
        if self._status_visible:
            self._status_write(
                f"[user] {_fmt_user(call, self._ui._client.ham_name)} connected"
            )

    def _handle_user_disconnect(self, obj: dict) -> None:
        # Same wire shape: field is ``c``. See ``_handle_user_connect``.
        call = (obj.get("c") or obj.get("call") or "").upper()
        if not call:
            return
        users = [u for u in self._online_items.keys() if u != call]
        self._refresh_online_pane(users)
        if self._status_visible:
            self._status_write(
                f"[user] {_fmt_user(call, self._ui._client.ham_name)} disconnected"
            )

    def _handle_delivery_timeout(self, obj: dict) -> None:
        kind = obj.get("kind")
        lid = obj.get("lid")
        ts_str = _fmt_ts_str(obj.get("ts")) if obj.get("ts") is not None else "[--]"
        edit_tag = " (edit)" if obj.get("is_edit") else ""
        if kind == "post":
            cid = obj.get("cid")
            ref = self._channel_ref(int(cid)) if isinstance(cid, int) else f"ch:{cid}"
            line = (
                "red",
                f"[timeout] [{ref}] post {lid}{edit_tag} at {ts_str}. "
                f"To resend: /retrypost {lid}",
            )
        else:
            peer = obj.get("peer")
            line = (
                "red",
                f"[timeout] [dm:{peer}] msg {lid}{edit_tag} at {ts_str}. "
                f"To resend: /retrydm {lid}",
            )
        self._status_error(line)

    # ------------------------------------------------------------------
    # Top-level key handling.
    # ------------------------------------------------------------------

    def _on_unhandled_input(self, key: str) -> bool:
        # When a modal is active, ``_ModalShell.keypress`` already
        # dispatched the key to ``modal.keypress`` and the body widget;
        # if it bubbled here, the modal didn't consume it and the body
        # didn't either. Swallow it so global Ctrl-bindings don't fire
        # underneath an open modal.
        if self._modal_stack:
            return True
        # Bindings deliberately avoid the keys the terminal/tty layer
        # can intercept. ``ctrl q`` / ``ctrl s`` are XON/XOFF flow
        # control on most terminals (and on tmux / screen / ssh
        # session managers above the terminal). ``ctrl h`` is
        # backspace on most terminals. F1 covers help reliably; we
        # rely on Ctrl-X for quit and Ctrl-L (mnemonic: "log") for
        # the status pane.
        if key in ("ctrl c", "ctrl x"):
            self.action_quit_app()
            return True
        if key == "f1":
            self._handle_help([])
            return True
        if key == "ctrl l":
            self.action_toggle_status()
            return True
        if key == "ctrl d":
            self.action_toggle_verbose()
            return True
        if key == "ctrl e":
            self.action_insert_emoji()
            return True
        if key == "ctrl o":
            self._open_settings_modal()
            return True
        if key == "ctrl u":
            self.action_unsub_channel()
            return True
        if key == "esc":
            # Esc cancels an in-progress reply or edit before
            # refocusing the input. The pre-filled row body stays in
            # the prompt while editing, so cancelling clears it too.
            self._cancel_pending_reply()
            self._cancel_pending_edit()
            self._focus_input()
            return True
        if key == "tab":
            self._focus_step(1)
            return True
        if key == "shift tab":
            self._focus_step(-1)
            return True
        if key == "enter":
            # If the user just scrolled the message list with
            # arrows-from-input (the fall-through handler below),
            # ``Frame.focus_position`` is still on the footer (input),
            # so a naive submit-on-enter would send blank text instead
            # of opening the Edit/Resend/React menu the user expects.
            # When the active listbox has a focused ``_MessageRow``
            # AND the input is empty, treat Enter as "activate the
            # focused row" instead of "submit input".
            if self._input is not None and not self._input.edit_text:
                target = self._active_target()
                if target is not None:
                    lv = self._views.get(target)
                    if lv is not None and lv.body and lv.focus is not None:
                        try:
                            w = lv.body[lv.focus_position]
                        except Exception:
                            w = None
                        if isinstance(w, _MessageRow):
                            self._open_action_menu(w)
                            return True
            asyncio.create_task(self._handle_input_submit())
            return True
        # Up / Down / PgUp / PgDn always scroll the active message
        # list, regardless of which pane has focus. urwid's Edit
        # widget doesn't consume Up/Down (single-line) and the rest
        # of the chain doesn't either, so they bubble here. Forward
        # them to the active ListBox so arrow scrolling Just Works
        # whether the user reached the messages via Tab or clicked
        # a row to open the menu (which leaves Frame.focus_position
        # on the input).
        if key in ("up", "down", "page up", "page down", "home", "end"):
            target = self._active_target()
            if target is not None:
                lv = self._views.get(target)
                if lv is not None:
                    # Use a representative size if we don't have a
                    # real one — urwid ListBox.keypress only needs
                    # (cols, rows) for box-mode, and our rendering
                    # already has the actual size cached.
                    try:
                        lv.keypress((50, 20), key)
                    except Exception:
                        pass
                    return True
        return False

    def _focus_input(self) -> None:
        """Move focus to the input ``Edit``."""
        if self._frame is None:
            return
        try:
            self._frame.focus_position = "footer"
            # The footer is a Pile of [input, footer_text]; the input is
            # the only selectable item, so Pile already focuses it.
        except (IndexError, KeyError):
            pass

    def action_quit_app(self) -> None:
        modal = QuitConfirmModal()

        async def _wait() -> None:
            ok = await self._show_modal(modal)
            if ok:
                # Close client cleanly, then exit.
                try:
                    await self._ui._client.close()
                except Exception:
                    pass
                if self._exit_future is not None and not self._exit_future.done():
                    self._exit_future.set_result(None)

        asyncio.create_task(_wait())

    def action_toggle_verbose(self) -> None:
        self._ui._options.verbose_history = not self._ui._options.verbose_history
        active = self._active_target()
        self._verbose_dirty = {t for t in self._views if t != active}
        self._refresh_active_rows()

    def action_insert_emoji(self) -> None:
        modal = EmojiPrompt()

        async def _wait() -> None:
            picked = await self._show_modal(modal)
            if not picked:
                return
            ch = picked
            if re.fullmatch(r"[0-9a-fA-F]{4,6}", ch):
                try:
                    ch = chr(int(ch, 16))
                except (ValueError, OverflowError):
                    pass
            if self._input is None:
                return
            text = self._input.edit_text
            pos = self._input.edit_pos
            self._input.set_edit_text(text[:pos] + ch + text[pos:])
            self._input.set_edit_pos(pos + len(ch))
            if self._frame is not None:
                self._frame.focus_position = "footer"

        asyncio.create_task(_wait())

    def action_unsub_channel(self) -> None:
        cid = self._active_subscribed_channel_cid()
        if cid is None:
            return
        self._open_unsubscribe_modal(cid)

    # ---- Focus cycling ----

    # Tab cycle stops. ``input`` lives in the Frame footer; ``tabs``
    # and ``targets`` are positions 0 and 1 of the left Pile; the
    # centre column holds ``log`` (status pane, top) above
    # ``messages`` — cycle order matches the visual top-to-bottom
    # layout of the centre column. ``log`` is only included in the
    # cycle when it's visible (Ctrl-L toggles it). The online users
    # list is deliberately left OUT of the cycle — there are no
    # actions on online rows (they're informational), so a Tab stop
    # there would just be an extra hop without payoff.
    _FOCUS_ORDER = ("input", "tabs", "targets", "log", "messages")

    def _focus_step(self, delta: int) -> None:
        if self._frame is None:
            return
        cur = self._current_focus_step()
        try:
            idx = self._FOCUS_ORDER.index(cur)
        except ValueError:
            idx = 0
        # Skip stops that aren't presently usable (e.g. the Log pane
        # is hidden via Ctrl-L). Bound the search by the cycle length
        # so a fully-empty cycle can't loop forever.
        for offset in range(1, len(self._FOCUS_ORDER) + 1):
            new = self._FOCUS_ORDER[(idx + delta * offset) % len(self._FOCUS_ORDER)]
            if new == "log" and not self._status_visible:
                continue
            self._set_focus_step(new)
            return

    def _current_focus_step(self) -> str:
        """Best-effort detection of which Tab-stop currently has focus."""
        if self._frame is None:
            return "input"
        if self._frame.focus_position == "footer":
            return "input"
        # Body is a Pile [body_columns, bottom Divider]. Unwrap to the
        # Columns the rest of this method reasons about.
        try:
            body = self._frame.contents["body"][0]
        except Exception:
            return "input"
        cols = body.contents[0][0] if isinstance(body, urwid.Pile) else body
        if not isinstance(cols, urwid.Columns):
            return "input"
        # Body Columns: 0=left pane, 1=vertical separator, 2=centre pane.
        if cols.focus_position == 2:
            # Inside the centre Pile, position 0 is the status holder
            # *when visible* (the Pile drops it from contents while
            # hidden so the message box reclaims the space).
            if self._centre_pane is not None and self._status_visible:
                try:
                    focused = self._centre_pane.focus
                    if focused is self._status_holder:
                        return "log"
                except (IndexError, AttributeError):
                    pass
            return "messages"
        # Left column (the Pile). Inspect Pile.focus_position to narrow
        # down to tabs / targets.
        try:
            left = cols.contents[0][0]
            # Unwrap AttrMap → Pile.
            if isinstance(left, urwid.AttrMap):
                left = left.original_widget
            if isinstance(left, urwid.Pile):
                pos = left.focus_position
                # See _build_widgets: 0=tabs, 1=targets, 2=divider,
                # 3=online_count, 4=online_listbox.
                if pos == 0:
                    return "tabs"
                if pos == 1:
                    return "targets"
        except Exception:
            pass
        return "input"

    def _set_focus_step(self, step: str) -> None:
        """Move focus to the named Tab-stop."""
        if self._frame is None:
            return
        if step == "input":
            self._focus_input()
            return
        # Body stops. ``body`` is a Pile [body_columns, bottom Divider];
        # unwrap to the inner Columns and pin the Pile's focus to the
        # Columns so keys reach it.
        try:
            self._frame.focus_position = "body"
            body = self._frame.contents["body"][0]
        except Exception:
            return
        if isinstance(body, urwid.Pile):
            try:
                body.focus_position = 0
            except (IndexError, ValueError):
                pass
            cols = body.contents[0][0]
        else:
            cols = body
        if not isinstance(cols, urwid.Columns):
            return
        if step == "messages":
            try:
                cols.focus_position = 2
                # The centre Pile defaults focus to position 0, which
                # is either the status holder (non-selectable) or the
                # thread header. Force focus onto the message-list
                # placeholder so Enter reaches the row. Look it up by
                # identity since its index shifts when the status pane
                # is hidden.
                if self._centre_pane is not None:
                    try:
                        for i, (w, _) in enumerate(self._centre_pane.contents):
                            if w is self._messages_box:
                                self._centre_pane.focus_position = i
                                break
                    except (IndexError, ValueError):
                        pass
            except (IndexError, ValueError):
                pass
            return
        if step == "log":
            # Same shape as ``messages`` but pin focus on the status
            # holder. Skipped from the cycle when hidden, but a direct
            # set call from elsewhere is a no-op rather than an error
            # if the pane has been collapsed in the meantime.
            if not self._status_visible:
                return
            try:
                cols.focus_position = 2
                if self._centre_pane is not None:
                    for i, (w, _) in enumerate(self._centre_pane.contents):
                        if w is self._status_holder:
                            self._centre_pane.focus_position = i
                            break
            except (IndexError, ValueError):
                pass
            return
        # Left-column stops.
        try:
            cols.focus_position = 0
            left = cols.contents[0][0]
            if isinstance(left, urwid.AttrMap):
                left = left.original_widget
            if not isinstance(left, urwid.Pile):
                return
            if step == "tabs":
                left.focus_position = 0
            elif step == "targets":
                left.focus_position = 1
        except (IndexError, ValueError):
            pass

# ---------------------------------------------------------------------
# Custom ListBox that hooks Enter on rows + up-at-top → load older.
# ---------------------------------------------------------------------


class _MessageListBox(urwid.ListBox):
    """ListBox that opens the ActionMenu on Enter and pages older
    messages on up-at-top.

    Subclassing keeps the keypress override local — we don't want this
    behaviour on the target list / online list.
    """

    def __init__(
        self,
        body: urwid.SimpleFocusListWalker,
        *,
        app: _UrwidApp,
        target: TargetKey,
    ) -> None:
        super().__init__(body)
        self._app = app
        self._target = target
        # One-shot: flips on the first render that has a real (maxcol,
        # maxrow) so we top up to the visible height exactly once.
        self._topup_scheduled = False

    def render(self, size, focus=False):  # type: ignore[override]
        canvas = super().render(size, focus)
        if not self._topup_scheduled and len(size) >= 2 and size[1] > 0:
            self._topup_scheduled = True
            asyncio.create_task(
                self._app._fill_pane_initial(self._target, size[1])
            )
        return canvas

    def keypress(self, size, key):  # type: ignore[override]
        if key == "enter":
            if self.body and self.focus is not None:
                w = self.body[self.focus_position] if 0 <= self.focus_position < len(self.body) else None
                if isinstance(w, _MessageRow):
                    self._app._open_action_menu(w)
                    return None
        if key == "up" and self.focus_position == 0:
            asyncio.create_task(self._app._load_older(self._target))
            return None
        return super().keypress(size, key)


# ---------------------------------------------------------------------
# UrwidUI — public wrapper, parallel to TextualUI.
# ---------------------------------------------------------------------


class UrwidUI:
    """Public urwid UI shell. Mirrors ``TextualUI`` so cli.py is a
    one-line dispatch difference between the two.

    Two construction modes:

    * **Session-driven** — ``connection_opener`` + ``available_profiles``
      are provided and ``client`` is ``None``. The UI itself shows the
      profile picker (when ``initial_profile is None``) and the
      connect-progress modal, drives the connect, and handles the
      reconnect / giveup modal on link drop.
    * **Legacy / offline** — ``client`` is provided directly. The UI
      skips the bootstrap modals entirely and runs against the given
      client. Used for the offline-only path and the line UI shape.

    ``offline=True`` puts the UI in read-only mode: any send / edit /
    react / sub / unsub is refused with a banner. Note the ctor
    signature does **not** take Textual-only knobs like
    ``cursor_blink`` — urwid has no compositor and no cursor-blink
    redraws.
    """

    def __init__(
        self,
        client: WpsClient | None = None,
        *,
        my_call: str,
        channels: list[ChannelInfo] | None = None,
        history_backfill: int = 3,
        options: SessionOptions | None = None,
        offline: bool = False,
        connection_opener: Callable[..., Any] | None = None,
        available_profiles: list[ConnectProfile] | None = None,
        initial_profile: ConnectProfile | None = None,
        default_profile_name: str | None = None,
        is_offline_profile: Callable[[ConnectProfile], bool] | None = None,
    ) -> None:
        self._client = client
        self._my_call = my_call.upper()
        self._channels = list(channels or [])
        self._history_backfill = max(0, int(history_backfill))
        self._options = options or SessionOptions()
        self._offline = offline
        self._target: TargetKey | None = None
        self._pending: list[dict] = []
        self._app: _UrwidApp | None = None
        self.exit_reason: str | None = None

        # Session-driven mode plumbing. Only set when ``connection_opener``
        # is provided; otherwise the UI runs in legacy mode against
        # whatever ``client`` it was constructed with.
        self._connection_opener = connection_opener
        self._available_profiles = list(available_profiles or [])
        self._initial_profile = initial_profile
        self._default_profile_name = default_profile_name
        self._is_offline_profile_fn = is_offline_profile or (lambda p: False)

    @property
    def session_driven(self) -> bool:
        return self._connection_opener is not None

    def render_event(self, obj: dict) -> None:
        if self._app is None:
            self._pending.append(obj)
            return
        self._app.render_event(obj)

    async def run(self) -> None:
        self._app = _UrwidApp(self)
        await self._app.run_async()


def launch(
    client: WpsClient,
    *,
    my_call: str,
    channels: list[ChannelInfo] | None = None,
    history_backfill: int = 3,
    options: SessionOptions | None = None,
) -> UrwidUI:
    return UrwidUI(
        client,
        my_call=my_call,
        channels=channels,
        history_backfill=history_backfill,
        options=options,
    )
