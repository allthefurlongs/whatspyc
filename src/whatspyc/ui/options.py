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


def _format_int(v: int) -> str:
    return str(v)


@dataclass(frozen=True)
class _OptionSpec:
    name: str
    description: str
    parse: Callable[[str], Any]
    format: Callable[[Any], str]
    # When True the option is only meaningful in the line UI and is
    # filtered out of the textual / urwid Settings modals via
    # ``SessionOptions.names(include_line_only=False)``. Stored on the
    # SessionOptions object regardless so callers can read it without
    # caring which UI is running.
    line_only: bool = False


_SPECS: dict[str, _OptionSpec] = {
    "show_acks": _OptionSpec(
        name="show_acks",
        description="Show [ack] when DM/post delivered to server.",
        parse=_parse_bool,
        format=_format_bool,
        line_only=True,
    ),
    "show_edits": _OptionSpec(
        name="show_edits",
        description="Display when a post/DM has been edited.",
        parse=_parse_bool,
        format=_format_bool,
    ),
    "verbose_history": _OptionSpec(
        name="verbose_history",
        description="Display messages with more metadata.",
        parse=_parse_bool,
        format=_format_bool,
    ),
    "delivery_timeout_s": _OptionSpec(
        name="delivery_timeout_s",
        description='Secs before unacked msg shows as "not delivered".',
        parse=_parse_positive_int,
        format=_format_int,
    ),
    "bell_on_activity": _OptionSpec(
        name="bell_on_activity",
        description="Ring terminal bell on real-time post or DM.",
        parse=_parse_bool,
        format=_format_bool,
    ),
    "notify_new_dms": _OptionSpec(
        name="notify_new_dms",
        description="Print when new DMs arrive outside your target.",
        parse=_parse_bool,
        format=_format_bool,
        line_only=True,
    ),
    "notify_new_posts": _OptionSpec(
        name="notify_new_posts",
        description="Print when new posts arrive outside your target.",
        parse=_parse_bool,
        format=_format_bool,
        line_only=True,
    ),
    "notify_user_conn": _OptionSpec(
        name="notify_user_conn",
        description="Print when a user connects or disconnects.",
        parse=_parse_bool,
        format=_format_bool,
        line_only=True,
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
        bell_on_activity: bool = True,
        notify_new_dms: bool = True,
        notify_new_posts: bool = True,
        notify_user_conn: bool = True,
    ) -> None:
        self.show_acks = show_acks
        self.show_edits = show_edits
        self.verbose_history = verbose_history
        self.delivery_timeout_s = delivery_timeout_s
        self.bell_on_activity = bell_on_activity
        self.notify_new_dms = notify_new_dms
        self.notify_new_posts = notify_new_posts
        self.notify_user_conn = notify_user_conn

    @classmethod
    def names(cls, *, include_line_only: bool = True) -> list[str]:
        """Names of every known option.

        ``include_line_only=False`` drops options flagged as line-UI-only
        from the listing — used by the textual / urwid Settings modals
        so options that have no effect there don't show up. The line UI
        keeps the default (``True``) and sees every option.
        """
        if include_line_only:
            return list(_SPECS.keys())
        return [n for n, s in _SPECS.items() if not s.line_only]

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
