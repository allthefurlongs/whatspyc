"""UI package — shared helpers used by both the line and textual UIs."""

from __future__ import annotations


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
