"""Config file + CLI flag overrides.

The config lives at ``$XDG_CONFIG_HOME/whatspyc/config.toml`` (default
``~/.config/whatspyc/config.toml``).

Connection parameters live inside ``[[connect_profiles]]`` arrays-of-tables.
Each profile is a complete description of one path to a WPS service:
transport + endpoint + (optional) per-hop node-prompt script.
``default_profile`` names the one preselected on startup; the user can pick
another from a CLI prompt.

Top-level keys are reserved for *global* (non-connection) preferences:
``my_call``, ``name``, ``state_dir``, ``ui``, ``default_profile``. Connection
fields at the top level are no longer supported — load() raises so the user
notices and migrates rather than silently ignoring stale values.

The channel directory lives in its own file at
``$XDG_CONFIG_HOME/whatspyc/channels.toml``. The WPS protocol does not
advertise available channels (the server only sends per-channel data
for channels you're already subscribed to), so the web client hardcodes
its directory in the JS bundle. whatspyc ships an equivalent default
list as package data and copies it to the user's config dir on first
run, so a fresh install gets the standard channels and the user can
freely edit/extend the file from there.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from whatspyc.wps.hop_script import HopStep

# Connection-specific keys that must live inside a [[connect_profiles]] block.
# Used by load() to flag legacy configs and refuse to silently misbehave.
_PROFILE_KEYS = frozenset(
    {
        "engine",
        "transport",
        "host",
        "port",
        "radio_port",
        "ax_level",
        "remote",
        "rhp_auth_user",
        "rhp_auth_pass",
        "kiss_device",
        "kiss_baud",
        "kiss_port",
        "kiss_ackmode",
        "digipeaters",
        "ax25_modulo",
        "ax25_segmentation",
        "connect_sequence",
    }
)


VALID_ENGINES = ("xrouter", "bpq", "custom")


@dataclass
class ChannelInfo:
    """A WPS channel known to the client.

    The WPS protocol has no "list available channels" RPC — the server
    only sends per-channel data for channels the user is already
    subscribed to. The web client hardcodes its channel directory in the
    JS bundle; whatspyc lets the user supply theirs in
    ``[[channels]]`` config blocks instead.
    """

    cid: int
    name: str = ""
    description: str = ""


@dataclass
class ConnectProfile:
    """One named path to a WPS service."""

    name: str
    transport: str = "rhp-ws"  # "rhp-ws", "rhp-tcp", "kiss-serial", "kiss-tcp", "direct-tcp"
    host: str = "localhost"
    port: int | None = None
    engine: str | None = None  # "xrouter", "bpq", or "custom" — required for rhp-*
    radio_port: int | None = None
    ax_level: str = "L2"  # "L2" or "L4"
    remote: str = "WPS"
    rhp_auth_user: str | None = None
    rhp_auth_pass: str | None = None
    kiss_device: str | None = None
    kiss_baud: int = 9600
    kiss_port: int = 0
    kiss_ackmode: bool = False
    digipeaters: list[str] = field(default_factory=list)
    ax25_modulo: int = 8
    ax25_segmentation: bool = False
    connect_script: list[HopStep] = field(default_factory=list)


@dataclass
class Config:
    # `my_call` and `name` are both required — the CLI refuses to run if
    # neither the config file nor the CLI flag supplies them. They live as
    # ``Optional`` on the dataclass so that ``Config()`` and a fresh
    # config-less install can still be constructed; the validation gate is
    # in ``cli.main`` after CLI overrides have been applied.
    my_call: str | None = None
    name: str | None = None
    state_dir: Path = field(default_factory=lambda: _default_state_dir())
    ui: str = "line"  # "line" or "tui"
    default_profile: str | None = None
    # How many historic messages/posts to replay from the local store when
    # the user switches target. Live arrivals only ever show up after
    # connect, so without a backfill the pane looks empty for a peer or
    # channel you haven't talked in this session. `/history N` overrides
    # this on demand.
    history_backfill: int = 3
    # Auto-fire ``cu`` / ``cpb`` to pull at most this many historic posts
    # whenever the server reports paused channels (``pch``) or a fresh
    # subscribe-ack with a non-zero ``pc``. ``None`` (default) leaves the
    # decision to the user via /unpause and /fetch.
    auto_backfill_post_count: int | None = None
    # Rebuild the link with exponential backoff after an unexpected drop.
    # Off by default — opt in via config / CLI when you want unattended
    # link recovery (sessions keep running across temporary node /
    # transport hiccups). Backoff doubles from 2 s up to 60 s.
    auto_reconnect: bool = False
    # Cap on reconnect attempts when ``auto_reconnect`` is on. ``0`` means
    # retry forever (matching the historical behaviour). Anything > 0
    # gives up after that many failed attempts and emits a
    # ``_reconnect_giveup`` event.
    reconnect_max_retries: int = 0
    # Display the ``[ack]`` line each time the server confirms delivery of
    # a DM (`mr`) or a post (`cpr`). Useful confirmation on a slow link;
    # noisy on a fast one. Toggleable per session via ``/set show_acks``.
    show_acks: bool = True
    # Render an ``[EDITED]`` notice in the message log when a real-time
    # ``med`` (DM edit) or ``cped`` (channel post edit) arrives. Connect-
    # batch edits (``medb`` / ``cpedb``) always update the local store
    # silently — the toggle only affects the live notification path.
    # Toggleable per session via ``/set show_edits``.
    show_edits: bool = True
    # Default rendering mode for history replay (``/history``, target
    # switch backfill) and live arrivals. ``False`` keeps the historic
    # compact form (`prefix [ts] <Name, CALL>: body`); ``True`` renders
    # the verbose form with id, delivery state, and realtime-receipt
    # latency. ``/vhistory`` always uses verbose regardless.
    verbose_history: bool = False
    # Seconds an outbound DM / post can sit unacknowledged before
    # verbose render flips it from "Delivering..." to "NOT DELIVERED".
    # The web client has no equivalent automatic timeout (its "resend"
    # is a manual button); this is a whatspyc-specific knob.
    delivery_timeout_s: int = 60
    connect_profiles: list[ConnectProfile] = field(default_factory=list)
    channels: list[ChannelInfo] = field(default_factory=list)

    def resolve_profile(self, name: str) -> ConnectProfile:
        for p in self.connect_profiles:
            if p.name == name:
                return p
        raise KeyError(
            f"connect profile {name!r} not found. Available: "
            f"{[p.name for p in self.connect_profiles] or 'none'}"
        )


def _default_state_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "whatspyc"


def default_port(engine: str | None, transport: str) -> int | None:
    """Best-effort default port for the given transport.

    Returns ``None`` when the default cannot be determined without more
    input — most importantly, ``rhp-ws`` without an ``engine`` (since
    XRouter and BPQ use different web-server ports).
    """
    if transport == "rhp-tcp":
        return 9000
    if transport == "rhp-ws":
        if engine == "xrouter":
            return 8086
        if engine == "bpq":
            return 8008
        return None
    if transport == "kiss-tcp":
        return 8001
    if transport == "direct-tcp":
        return 63001  # WPS native TCP port (matches tests/fake_wps.py)
    return None


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "whatspyc" / "config.toml"


def channels_path() -> Path:
    """Location of the user's channel directory file.

    Lives next to ``config.toml`` so it's discoverable, but kept
    separate so users can edit it without churning the connection
    config — and so we can ship a sensible default list out of the box.
    """
    return config_path().parent / "channels.toml"


def _bundled_channels_toml() -> str:
    """The default channels.toml shipped with the package."""
    return (
        resources.files("whatspyc.data")
        .joinpath("channels.toml")
        .read_text(encoding="utf-8")
    )


def ensure_channels_file(path: Path | None = None) -> Path:
    """Create the user's channels.toml from the bundled defaults if missing.

    Returns the path. Idempotent — once the file exists we never overwrite
    it, so user edits survive upgrades.
    """
    p = path if path is not None else channels_path()
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_bundled_channels_toml(), encoding="utf-8")
    return p


def load() -> Config:
    cfg = Config()
    p = config_path()
    if p.exists():
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
        cfg = parse(raw)
    # Channels live in their own file. Seed it from package data on
    # first run so a fresh install ships with the standard directory.
    ch_path = ensure_channels_file()
    cfg.channels = parse_channels(
        tomllib.loads(ch_path.read_text(encoding="utf-8"))
    )
    return cfg


def parse(raw: dict) -> Config:
    """Build a ``Config`` from already-parsed TOML data. Split out so tests
    can exercise the schema without touching the filesystem."""
    cfg = Config()

    # Catch legacy top-level connection fields early — silent ignore would
    # be much worse than a clear error pointing at the config file.
    stray = sorted(_PROFILE_KEYS & raw.keys())
    if stray:
        raise ValueError(
            "config: keys "
            + ", ".join(repr(k) for k in stray)
            + " must live inside a [[connect_profiles]] block, not at the "
            "top level. Wrap them in a profile and set `default_profile = "
            "\"…\"` to pick it on startup."
        )

    for k in ("my_call", "name", "ui", "default_profile"):
        if k in raw:
            setattr(cfg, k, raw[k])
    if "state_dir" in raw:
        cfg.state_dir = Path(raw["state_dir"])
    if "history_backfill" in raw:
        v = raw["history_backfill"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(
                f"config: history_backfill must be a non-negative integer, got {v!r}"
            )
        cfg.history_backfill = v
    if "auto_backfill_post_count" in raw:
        v = raw["auto_backfill_post_count"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(
                "config: auto_backfill_post_count must be a non-negative "
                f"integer, got {v!r}"
            )
        cfg.auto_backfill_post_count = v if v > 0 else None
    if "auto_reconnect" in raw:
        v = raw["auto_reconnect"]
        if not isinstance(v, bool):
            raise ValueError(
                f"config: auto_reconnect must be a boolean, got {v!r}"
            )
        cfg.auto_reconnect = v
    if "reconnect_max_retries" in raw:
        v = raw["reconnect_max_retries"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(
                "config: reconnect_max_retries must be a non-negative "
                f"integer (0 = unlimited), got {v!r}"
            )
        cfg.reconnect_max_retries = v
    if "show_acks" in raw:
        v = raw["show_acks"]
        if not isinstance(v, bool):
            raise ValueError(
                f"config: show_acks must be a boolean, got {v!r}"
            )
        cfg.show_acks = v
    if "show_edits" in raw:
        v = raw["show_edits"]
        if not isinstance(v, bool):
            raise ValueError(
                f"config: show_edits must be a boolean, got {v!r}"
            )
        cfg.show_edits = v
    if "verbose_history" in raw:
        v = raw["verbose_history"]
        if not isinstance(v, bool):
            raise ValueError(
                f"config: verbose_history must be a boolean, got {v!r}"
            )
        cfg.verbose_history = v
    if "delivery_timeout_s" in raw:
        v = raw["delivery_timeout_s"]
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ValueError(
                "config: delivery_timeout_s must be a positive integer, "
                f"got {v!r}"
            )
        cfg.delivery_timeout_s = v

    if "channels" in raw:
        raise ValueError(
            "config: [[channels]] now lives in its own file at "
            f"{channels_path()} — move the entries there. The file is "
            "created automatically with the standard defaults on first "
            "run."
        )

    for entry in raw.get("connect_profiles", []):
        cfg.connect_profiles.append(_parse_profile(entry))

    if cfg.default_profile is not None and cfg.connect_profiles:
        # Surface a typo here instead of letting it bite at connect time.
        names = [p.name for p in cfg.connect_profiles]
        if cfg.default_profile not in names:
            raise ValueError(
                f"config: default_profile {cfg.default_profile!r} is not "
                f"one of the configured profiles ({names})"
            )
    return cfg


def _parse_profile(entry: dict) -> ConnectProfile:
    if "name" not in entry:
        raise ValueError(
            "config: every [[connect_profiles]] entry must have a `name` key"
        )
    p = ConnectProfile(name=entry["name"])
    user_supplied: set[str] = set()
    for k in (
        "transport",
        "host",
        "engine",
        "ax_level",
        "remote",
        "rhp_auth_user",
        "rhp_auth_pass",
        "kiss_device",
    ):
        if k in entry:
            setattr(p, k, entry[k])
            user_supplied.add(k)
    for k in ("port", "radio_port", "kiss_baud", "kiss_port", "ax25_modulo"):
        if k in entry:
            setattr(p, k, entry[k])
            user_supplied.add(k)
    for k in ("kiss_ackmode", "ax25_segmentation"):
        if k in entry:
            setattr(p, k, bool(entry[k]))
            user_supplied.add(k)
    if "digipeaters" in entry:
        p.digipeaters = list(entry["digipeaters"])
        user_supplied.add("digipeaters")
    if "connect_sequence" in entry:
        p.connect_script = [_parse_step(i, s) for i, s in enumerate(entry["connect_sequence"])]
        user_supplied.add("connect_script")
    resolve_engine_defaults(p, user_supplied)
    return p


def resolve_engine_defaults(p: ConnectProfile, user_supplied: set[str]) -> None:
    """Apply ``engine``-driven defaults and validation in place.

    ``engine`` is required for RHP transports and chooses sensible defaults
    for the related connection fields (``port``, ``radio_port``, ``remote``)
    and, for BPQ, prepends the SWITCH handshake to the connect script.
    Anything in ``user_supplied`` is treated as an explicit user choice and
    is **not** overwritten — that's how `port = 8080` survives alongside
    `engine = "bpq"`.

    ``engine = "custom"`` opts out of all defaulting (advanced mode); the
    user is responsible for every field.
    """
    transport = p.transport
    engine = p.engine

    if transport in ("rhp-ws", "rhp-tcp"):
        if engine is None:
            raise ValueError(
                f"profile {p.name!r}: `engine` is required for "
                f"transport={transport!r}. Use 'xrouter', 'bpq', or 'custom' "
                "(custom = no defaults applied; you set everything manually)."
            )
        if engine not in VALID_ENGINES:
            raise ValueError(
                f"profile {p.name!r}: invalid engine {engine!r}. "
                f"Use one of: {', '.join(repr(e) for e in VALID_ENGINES)}."
            )
    elif engine is not None and engine not in VALID_ENGINES:
        raise ValueError(
            f"profile {p.name!r}: invalid engine {engine!r}. "
            f"Use one of: {', '.join(repr(e) for e in VALID_ENGINES)}."
        )

    if engine == "xrouter":
        if "radio_port" not in user_supplied:
            p.radio_port = 1
    elif engine == "bpq":
        if "remote" not in user_supplied:
            p.remote = "SWITCH"
        if "radio_port" not in user_supplied:
            p.radio_port = 1
        p.connect_script = _normalize_bpq_preamble(p.connect_script)

    # Engine doesn't affect port for non-RHP transports, but default_port
    # still has useful answers for kiss-tcp and direct-tcp. For "custom",
    # leave port alone unless the transport has a non-engine-dependent
    # default (rhp-tcp returns 9000 regardless).
    if "port" not in user_supplied:
        port = default_port(engine, transport)
        if port is not None:
            p.port = port


def _normalize_bpq_preamble(script: list[HopStep]) -> list[HopStep]:
    """Ensure the script starts with a wait-only step that consumes
    BPQ's ``Connected to RHP Server`` banner.

    BPQ pushes that banner unprompted right after the RHP link comes up,
    so the script needs to absorb it before any later hop's
    ``val = "Connected"`` could false-match it. RHP-open with
    ``remote = "SWITCH"`` already drops the user at the switch prompt —
    no further command is needed to "enter" the switch.

    Three input forms are collapsed to the same wire behaviour:

    1. Empty / first hop unrelated to the banner → prepend a wait-only
       ``HopStep(cmd="", val="Connected to RHP Server")``.
    2. First hop is already wait-only (``cmd == ""``) → leave alone;
       the user is already doing the right thing (and may have customized
       the banner text).
    3. First hop is a literal ``SWITCH`` cmd whose val targets the banner
       — the historic explicit form. Strip the redundant SWITCH command
       (sending it gets ``Invalid command`` back from the switch
       interpreter) but keep the user's val so any custom banner wording
       still matches. Effectively turns case 3 into case 2.

    Anything else with a banner-matching ``val`` but a non-SWITCH cmd is
    left untouched — that's the user driving the chain themselves.
    """
    if script:
        first = script[0]
        if first.cmd == "":
            return script
        if "Connected to RHP Server" in first.val:
            if first.cmd.strip().upper() == "SWITCH":
                return [HopStep(cmd="", val=first.val, timeout=first.timeout)] + script[1:]
            return script
    return [HopStep(cmd="", val="Connected to RHP Server")] + script


def parse_channels(raw: dict) -> list[ChannelInfo]:
    """Parse a channels.toml document into a list of ``ChannelInfo``.

    Split out from :func:`load` so tests can drive the schema without
    touching the filesystem.
    """
    out: list[ChannelInfo] = []
    seen: set[int] = set()
    for entry in raw.get("channels", []):
        ch = _parse_channel(entry)
        if ch.cid in seen:
            raise ValueError(
                f"channels.toml: cid={ch.cid} is listed more than once"
            )
        seen.add(ch.cid)
        out.append(ch)
    return out


def _parse_channel(entry: dict) -> ChannelInfo:
    if "cid" not in entry:
        raise ValueError(
            "channels.toml: every [[channels]] entry must have a `cid` key"
        )
    if not isinstance(entry["cid"], int):
        raise ValueError(
            f"channels.toml: cid must be an integer, got {entry['cid']!r}"
        )
    return ChannelInfo(
        cid=entry["cid"],
        name=str(entry.get("name", "")),
        description=str(entry.get("description", "")),
    )


def _parse_step(idx: int, raw: dict) -> HopStep:
    missing = [k for k in ("cmd", "val") if k not in raw]
    if missing:
        raise ValueError(
            f"config: connect_sequence[{idx}] missing required key(s): "
            + ", ".join(repr(k) for k in missing)
        )
    timeout = raw.get("timeout")
    if timeout is not None:
        timeout = float(timeout)
    return HopStep(cmd=raw["cmd"], val=raw["val"], timeout=timeout)
