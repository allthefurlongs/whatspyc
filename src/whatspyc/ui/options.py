"""Session-level UI options, mutable via ``/set NAME VALUE``.

Each option has a registry entry (``_OptionSpec``) describing how to parse
a string value and how to render the current value. ``SessionOptions``
stores the live state and is shared by ``LineUI``, ``TextualUI`` and
``UrwidUI`` via the constructor — so a ``/set`` from any UI updates the
same object.

The defaults come from the user's config file (top-level keys), so the
same name doubles as a config key. ``/set`` only changes the running
session — restarting the client picks up whatever is in config again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


def _parse_bool(raw: str) -> bool:
    s = raw.strip().lower()
    if s in ("true", "on", "yes", "y", "1"):
        return True
    if s in ("false", "off", "no", "n", "0"):
        return False
    raise ValueError(
        f"expected on/off (or true/false, yes/no, 1/0), got {raw!r}"
    )


def _format_bool(v: bool) -> str:
    return "on" if v else "off"


def _parse_positive_int(raw: str) -> int:
    s = raw.strip()
    try:
        v = int(s)
    except ValueError:
        raise ValueError(f"expected a positive integer, got {raw!r}") from None
    if v <= 0:
        raise ValueError(f"expected a positive integer, got {raw!r}")
    return v


def _parse_nonneg_int(raw: str) -> int:
    s = raw.strip()
    try:
        v = int(s)
    except ValueError:
        raise ValueError(f"expected a non-negative integer, got {raw!r}") from None
    if v < 0:
        raise ValueError(f"expected a non-negative integer, got {raw!r}")
    return v


def _format_int(v: int) -> str:
    return str(v)


@dataclass(frozen=True)
class _OptionSpec:
    name: str
    description: str
    parse: Callable[[str], Any]
    format: Callable[[Any], str]


_SPECS: dict[str, _OptionSpec] = {
    "show_acks": _OptionSpec(
        name="show_acks",
        description="Show [ack] line when a DM/post is delivered to the server.",
        parse=_parse_bool,
        format=_format_bool,
    ),
    "show_edits": _OptionSpec(
        name="show_edits",
        description=(
            "Mark edited DMs/posts with a grey [Edited <ts>] suffix "
            "(textual/urwid in-place; line UI emits a separate [EDITED] "
            "line on real-time med/cped). Off → row updates silently."
        ),
        parse=_parse_bool,
        format=_format_bool,
    ),
    "verbose_history": _OptionSpec(
        name="verbose_history",
        description=(
            "Render messages/posts in verbose form (id, timestamp, "
            "delivery state). /vhistory is always verbose regardless."
        ),
        parse=_parse_bool,
        format=_format_bool,
    ),
    "delivery_timeout_s": _OptionSpec(
        name="delivery_timeout_s",
        description=(
            "Seconds before an outbound DM/post with no ack flips from "
            "'Delivering...' to 'NOT DELIVERED' in verbose render."
        ),
        parse=_parse_positive_int,
        format=_format_int,
    ),
    "emoji_search_debounce_ms": _OptionSpec(
        name="emoji_search_debounce_ms",
        description=(
            "Milliseconds to wait after the last keystroke before "
            "rebuilding the EmojiPrompt grid (textual + urwid backends). "
            "0 = rebuild on every keystroke (historic behaviour). Higher "
            "values smooth typing on slow hardware."
        ),
        parse=_parse_nonneg_int,
        format=_format_int,
    ),
    "bell_on_activity": _OptionSpec(
        name="bell_on_activity",
        description=(
            "Ring the terminal bell on every real-time DM (m) or "
            "channel post (cp). Batch arrivals (mb/cpb) do not ring."
        ),
        parse=_parse_bool,
        format=_format_bool,
    ),
}


class SessionOptions:
    """Live, mutable view of session-tunable options.

    Each known option is exposed as a regular attribute. The class-level
    ``_SPECS`` registry drives ``/set`` so adding a new option means
    adding a spec entry and a default in ``__init__``.
    """

    def __init__(
        self,
        *,
        show_acks: bool = True,
        show_edits: bool = True,
        verbose_history: bool = False,
        delivery_timeout_s: int = 60,
        emoji_search_debounce_ms: int = 200,
        bell_on_activity: bool = True,
    ) -> None:
        self.show_acks = show_acks
        self.show_edits = show_edits
        self.verbose_history = verbose_history
        self.delivery_timeout_s = delivery_timeout_s
        self.emoji_search_debounce_ms = emoji_search_debounce_ms
        self.bell_on_activity = bell_on_activity

    @classmethod
    def names(cls) -> list[str]:
        return list(_SPECS.keys())

    @classmethod
    def describe(cls, name: str) -> str:
        return _SPECS[name].description

    def get(self, name: str) -> Any:
        if name not in _SPECS:
            raise KeyError(name)
        return getattr(self, name)

    def format(self, name: str) -> str:
        return _SPECS[name].format(self.get(name))

    @classmethod
    def format_value(cls, name: str, value: Any) -> str:
        return _SPECS[name].format(value)

    def set(self, name: str, raw: str) -> tuple[Any, Any]:
        """Parse ``raw`` and assign. Returns ``(old, new)``.

        Raises ``KeyError`` for unknown names and ``ValueError`` for
        unparseable values — callers surface those as user-facing hints.
        """
        if name not in _SPECS:
            raise KeyError(name)
        spec = _SPECS[name]
        new = spec.parse(raw)
        old = getattr(self, name)
        setattr(self, name, new)
        return old, new
