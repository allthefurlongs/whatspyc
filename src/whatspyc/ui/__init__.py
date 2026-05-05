"""UI package — shared helpers used by both the line and textual UIs."""

from __future__ import annotations

import re


# 1e12 ms = 2001-09-09 — well before WhatsPac existed. Anything below
# this threshold can only sensibly be a seconds-since-epoch value;
# anything at or above is milliseconds.
_MS_THRESHOLD = 1_000_000_000_000


def ts_to_ms(ts: int | float | None) -> int | None:
    """Normalise a wire timestamp to milliseconds.

    The WhatsPac protocol stores DM ``ts`` in **seconds** (the web
    client sends ``Math.round(Date.now()/1e3)``) and channel-post ``ts``
    in **milliseconds** (`Date.now()`). The local store also has
    legacy rows from earlier versions that wrote DM ``ts`` in ms.
    Display and duration code can't know the type from a bare value, so
    we use the magnitude as the discriminator.
    """
    if ts is None:
        return None
    n = int(ts)
    return n * 1000 if n < _MS_THRESHOLD else n


_HEX_CODEPOINT = re.compile(r"\A[0-9a-fA-F]{4,6}\Z")


def emoji_for_display(s: str) -> str:
    """Render a wire-format emoji string as a literal character.

    The WhatsPac protocol carries reaction emoji as hex-codepoint
    strings (e.g. ``"1f622"`` for 😢) — see MESSAGES.md type ``mem``.
    The web client renders them via ``String.fromCodePoint(parseInt(t,
    16))``; we mirror that. Strings that aren't 4-6 hex digits or that
    don't resolve to a valid Unicode codepoint pass through unchanged,
    so legacy rows or terminal-typed literals render as-is.
    """
    if _HEX_CODEPOINT.match(s):
        try:
            return chr(int(s, 16))
        except (ValueError, OverflowError):
            pass
    return s


def emoji_to_wire(s: str) -> str:
    """Normalise an emoji input to the protocol's hex-codepoint form.

    Pickers that return a literal character get converted to the
    lowercase hex codepoint (``"👍"`` → ``"1f44d"``); strings that are
    already in hex-codepoint form pass through unchanged. Multi-char
    sequences (skin-tone modifiers, ZWJ joins) are sent as-is — the
    server is permissive and the protocol docs only specify the
    single-codepoint case.
    """
    if _HEX_CODEPOINT.match(s):
        return s.lower()
    if len(s) == 1:
        return format(ord(s), "x")
    return s
