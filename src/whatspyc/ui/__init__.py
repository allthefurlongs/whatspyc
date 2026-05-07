"""UI package — shared helpers used by both the line and textual UIs."""

from __future__ import annotations

import re
from typing import Any


_REPLY_SNIPPET_LEN = 10


def reply_natural_key(kind: str, row: dict | Any) -> str | None:
    """Return the natural key of the parent for a reply row, or ``None``.

    For DMs this is the parent's ``_id`` (taken straight from the wire
    ``r`` field, which itself encodes ``{ts}-{fc}``). For posts it's
    the parent's ``ts`` as a string — posts are keyed locally on
    ``(channel_id, ts)`` and replies are presumed to live in the same
    channel as their parent.
    """
    get = row.get if hasattr(row, "get") else lambda k, d=None: d
    if kind == "dm":
        rid = get("reply_id") or get("r")
        return str(rid) if rid else None
    if kind == "ch":
        rts = get("reply_ts") or get("rts")
        return str(int(rts)) if rts is not None else None
    return None


def reply_call_for(kind: str, row: dict | Any) -> str | None:
    """Best-effort sender callsign of the parent post/DM being replied to.

    Posts: ``rfc`` is on the wire alongside ``rts``. DMs: ``r`` is the
    parent's ``_id`` of the form ``{ts}-{fc}``, so the callsign is
    encoded in the same field — parse it back out. Returns ``None``
    when no reply field is present, or when a DM's ``r`` is malformed.
    """
    get = row.get if hasattr(row, "get") else lambda k, d=None: d
    if kind == "dm":
        rid = get("reply_id") or get("r")
        if not rid or not isinstance(rid, str):
            return None
        _, _, fc = rid.partition("-")
        return fc.upper() or None
    if kind == "ch":
        rfc = get("reply_from") or get("rfc")
        return rfc.upper() if isinstance(rfc, str) and rfc else None
    return None


def resolve_reply_meta(store, kind: str, target_key: str, row: dict | Any) -> dict | None:
    """Resolve a row's reply metadata for rendering.

    Returns ``None`` if the row is not a reply. Otherwise returns a dict
    with ``call`` (str | None), ``snippet`` (str | None), ``in_db``
    (bool) and ``parent`` (dict | None) — the full parent row from the
    store, populated only when ``in_db`` is True so callers like the
    "View Full Reply-To" modal can avoid a second lookup.

    The parent lookup is done fresh on every call so a row that
    initially rendered "<msg not in db>" picks up the real preview as
    soon as the parent arrives and a refresh happens.
    """
    nk = reply_natural_key(kind, row)
    if nk is None:
        return None
    call = reply_call_for(kind, row)
    parent: dict | None = None
    if kind == "dm":
        try:
            parent = store.lookup_message_by_id(nk)
        except Exception:
            parent = None
    elif kind == "ch":
        try:
            parent = store.lookup_post(int(target_key), int(nk))
        except (TypeError, ValueError, Exception):
            parent = None
    snippet: str | None = None
    if parent is not None:
        body = parent.get("body") or ""
        # Only suffix with "..." when we actually truncated; a short
        # parent body renders verbatim so the user isn't misled into
        # thinking there's more text.
        if len(body) > _REPLY_SNIPPET_LEN:
            snippet = body[:_REPLY_SNIPPET_LEN] + "..."
        else:
            snippet = body or "..."
        if not call:
            call = (parent.get("from_call") or "").upper() or None
    return {
        "call": call,
        "snippet": snippet,
        "in_db": parent is not None,
        "parent": parent,
    }


def reply_prefix_text(meta: dict | None) -> str:
    """Plain-text reply prefix — used by the line UI.

    Returns ``""`` when there's no reply.
    """
    if not meta:
        return ""
    call = meta.get("call")
    if meta.get("in_db") and meta.get("snippet"):
        body = meta["snippet"]
        return f"[Reply To {call}: {body}] " if call else f"[Reply To: {body}] "
    if call:
        return f"[Reply To {call}: <msg not in db>] "
    return "[Reply To: <msg not in db>] "


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
_HEX_SEQUENCE = re.compile(r"\A[0-9a-fA-F]{4,6}(?:-[0-9a-fA-F]{4,6})+\Z")


def emoji_for_display(s: str) -> str:
    """Render a wire-format emoji string as a literal character.

    The WhatsPac protocol carries reaction emoji as hex-codepoint
    strings (e.g. ``"1f622"`` for 😢) — see MESSAGES.md type ``mem``.
    Multi-codepoint sequences (variation selectors like
    ``"2764-fe0f"`` for ❤️, ZWJ joins like ``"1f469-200d-1f4bb"`` for
    👩‍💻) arrive as hyphen-separated hex on the wire. Strings that
    don't match either form pass through unchanged, so legacy rows or
    terminal-typed literals render as-is.
    """
    if _HEX_CODEPOINT.match(s):
        try:
            return chr(int(s, 16))
        except (ValueError, OverflowError):
            pass
    elif _HEX_SEQUENCE.match(s):
        try:
            return "".join(chr(int(p, 16)) for p in s.split("-"))
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
