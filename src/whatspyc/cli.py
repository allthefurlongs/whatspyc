"""Command-line entry point.

WPS requires a stateful connect handshake before anything is possible, so
``whatspyc`` is a single command that drops the user into a prompt rather
than offering one-shot subcommands.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

import click

from whatspyc import __version__
from whatspyc import config as cfg_mod
from whatspyc import log
from whatspyc.config import ConnectProfile
from whatspyc.store.store import SqliteStore
from whatspyc.transport.base import AsyncByteStream
from whatspyc.transport.direct_tcp import DirectTcpStream
from whatspyc.transport.rhp_session import RhpConfig
from whatspyc.transport.rhp_tcp import RhpTcpStream
from whatspyc.transport.rhp_ws import RhpWebSocketStream
from whatspyc.ui.line import LineUI
from whatspyc.ui.options import SessionOptions
from whatspyc.ui.textual_ui import TextualUI
from whatspyc.ui.urwid_ui import UrwidUI
from whatspyc.wps.client import WpsClient
from whatspyc.wps.connect_seq import ConnectSequence, ConnectSummary
from whatspyc.wps.hop_script import HopScriptError, HopStep


def _apply_textual_perf_env(c: cfg_mod.Config) -> None:
    """Translate ``textual_*`` config keys into Textual env vars.

    Textual reads ``TEXTUAL_FPS`` / ``TEXTUAL_ANIMATIONS`` /
    ``TEXTUAL_SMOOTH_SCROLL`` once during ``App.__init__``, so we have
    to set them before any Textual code runs (i.e. before ``run_async``
    or even ``App()``). Caller is responsible for only invoking this
    when the effective UI is ``textual`` — these env vars are
    Textual-specific and ignored by the urwid backend.

    ``os.environ.setdefault`` so a power user's shell-set value still
    wins over the config — the config knob is the "if I haven't set it
    in my shell" default.
    """
    if c.textual_fps != 60:
        os.environ.setdefault("TEXTUAL_FPS", str(c.textual_fps))
    if not c.textual_animations:
        os.environ.setdefault("TEXTUAL_ANIMATIONS", "0")
    if not c.textual_smooth_scroll:
        os.environ.setdefault("TEXTUAL_SMOOTH_SCROLL", "0")


# Sentinel profile that means "don't connect, just browse the local store".
# Surfaced as picker entry 0 and recognised throughout the run path. Angle
# brackets keep the name from colliding with anything a user might write
# in their config; `config._parse_profile` rejects user profiles with this
# exact name as a belt-and-braces guard.
OFFLINE_PROFILE_NAME = "<offline>"
_OFFLINE_PROFILE = ConnectProfile(name=OFFLINE_PROFILE_NAME)


def _is_offline_profile(p: ConnectProfile) -> bool:
    return p.name == OFFLINE_PROFILE_NAME


def _apply_nodecmd_mode(
    c: cfg_mod.Config,
    *,
    ui_mode: str | None,
    my_call_cli: str | None,
    state_dir_cli: Path | None,
    conf_path: Path | None = None,
) -> None:
    """Apply ``--nodecmd`` mode in place: read callsign + (first-use) name
    from stdin, derive a per-call state dir, force ``ui = "line"``.

    Designed for packet-node deployment: the node hands the user's
    callsign to the program on stdin (a long-standing convention) and the
    program then drives a basic line UI over a packet terminal. Rejects
    every flag whose value would override the stdin / per-call data
    rather than silently ignoring them — operators should know if a
    flag is being lost.
    """
    if ui_mode is not None and ui_mode != "line":
        raise click.UsageError(
            f"--nodecmd forces --ui line; --ui {ui_mode!r} cannot be used with it"
        )
    if my_call_cli is not None:
        raise click.UsageError(
            "--nodecmd takes the callsign from stdin; --my-call is not allowed"
        )
    if state_dir_cli is not None:
        raise click.UsageError(
            "--nodecmd derives state-dir from node_state_dir/<CALL>; "
            "--state-dir is not allowed"
        )
    if c.node_state_dir is None:
        raise click.UsageError(
            "--nodecmd requires `node_state_dir` set in "
            f"{conf_path or cfg_mod.config_path()} — that's the root "
            "directory each per-call state dir is created under"
        )

    # When stdout is a pipe (the node case), Python defaults to block
    # buffering — `print()` output sits in a 4KB buffer until flushed,
    # so the user sees nothing until either the buffer fills or the
    # program writes a prompt that does its own flush. Force line
    # buffering so every newline-terminated line is delivered as it
    # would be on a TTY. Only TextIOWrapper has `reconfigure`; tests
    # may swap a StringIO into stdout.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    raw_call = sys.stdin.readline()
    call = raw_call.strip().upper()
    if not call:
        raise click.UsageError("--nodecmd: no callsign on stdin")

    state_dir = Path(c.node_state_dir) / call
    state_dir.mkdir(parents=True, exist_ok=True)

    name_path = state_dir / "name.txt"
    name: str | None = None
    if name_path.exists():
        existing = name_path.read_text(encoding="utf-8").strip()
        if existing:
            name = existing
    if name is None:
        sys.stdout.write("Please enter your name: ")
        sys.stdout.flush()
        raw_name = sys.stdin.readline()
        name = raw_name.strip()
        if not name:
            raise click.UsageError("--nodecmd: no name on stdin")
        name_path.write_text(name + "\n", encoding="utf-8")

    c.my_call = call
    c.name = name
    c.state_dir = state_dir
    c.ui = "line"


def _build_stream_for(profile: ConnectProfile, my_call: str) -> AsyncByteStream:
    rhp_cfg = RhpConfig(
        pfam="ax25" if profile.ax_level.upper() == "L2" else "netrom",
        port=profile.radio_port,
        local=my_call,
        remote=profile.remote,
        flags=0x80,
        auth_user=profile.rhp_auth_user,
        auth_pass=profile.rhp_auth_pass,
    )
    # `port` is resolved upstream by config.resolve_engine_defaults — engine
    # defaults fill it in unless the profile explicitly overrides it. The
    # only way it's still None here is `engine="custom"` + `transport="rhp-ws"`
    # with no explicit port (custom = no defaults applied).
    port = profile.port
    if profile.transport == "rhp-ws":
        if port is None:
            raise click.UsageError(
                f"profile {profile.name!r}: rhp-ws with engine=custom needs "
                "an explicit `port` — no default is applied in custom mode"
            )
        return RhpWebSocketStream(profile.host, port, rhp_cfg)
    if profile.transport == "rhp-tcp":
        return RhpTcpStream(profile.host, port, rhp_cfg)
    if profile.transport == "direct-tcp":
        return DirectTcpStream(profile.host, port)
    raise click.UsageError(f"transport {profile.transport!r} not recognised")


def _parse_hops(specs: tuple[str, ...]) -> list[HopStep]:
    """Parse ``--hop "cmd|val"`` specs into HopStep list."""
    out: list[HopStep] = []
    for s in specs:
        if "|" not in s:
            raise click.UsageError(
                f"--hop spec {s!r} must be of the form 'cmd|val' "
                "(e.g. --hop 'C WPS|*** Connected')"
            )
        cmd, val = s.split("|", 1)
        out.append(HopStep(cmd=cmd, val=val))
    return out


def _adhoc_profile(
    *,
    transport: str | None,
    host: str | None,
    port: int | None,
    engine: str | None,
    radio_port: int | None,
    ax_level: str | None,
    remote: str | None,
    hops: list[HopStep],
) -> ConnectProfile:
    """Build a one-shot profile from CLI flags + --hop entries."""
    if not transport:
        raise click.UsageError(
            "ad-hoc connection (no --profile) requires at least --transport "
            "(and --host/--port/--remote as appropriate)"
        )
    p = ConnectProfile(name="<ad-hoc>", transport=transport)
    user_supplied: set[str] = set()
    flags = {
        "host": host,
        "port": port,
        "engine": engine,
        "radio_port": radio_port,
        "ax_level": ax_level,
        "remote": remote,
    }
    for k, v in flags.items():
        if v is not None:
            setattr(p, k, v)
            user_supplied.add(k)
    if hops:
        p.connect_script = hops
        user_supplied.add("connect_script")
    try:
        cfg_mod.resolve_engine_defaults(p, user_supplied)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None
    return p


def _pick_profile(
    c: cfg_mod.Config,
    *,
    profile_name: str | None,
    no_prompt: bool,
    hops: list[HopStep],
    adhoc_args: dict,
    conf_path: Path | None = None,
) -> ConnectProfile:
    """Decide which ConnectProfile to use for this run.

    Order of precedence:
      1. ``--profile NAME`` — look up in config (mutually exclusive with --hop).
      2. Any ad-hoc connection flag (``--transport`` / ``--hop`` / etc.) —
         build an unnamed profile from CLI flags.
      3. ``--no-prompt`` — use ``default_profile`` from config (or fail).
      4. Configured profiles — interactive picker (default starred).
      5. No profiles, no flags — usage error pointing at the config file.
    """
    if profile_name and (hops or any(v is not None for v in adhoc_args.values())):
        raise click.UsageError(
            "--profile is mutually exclusive with --hop / --transport / "
            "--host / --remote etc."
        )
    if profile_name:
        if profile_name == OFFLINE_PROFILE_NAME:
            return _OFFLINE_PROFILE
        try:
            return c.resolve_profile(profile_name)
        except KeyError as exc:
            raise click.UsageError(str(exc)) from None

    if hops or any(v is not None for v in adhoc_args.values()):
        return _adhoc_profile(hops=hops, **adhoc_args)

    if not c.connect_profiles:
        raise click.UsageError(
            "no connection configured. Either define [[connect_profiles]] "
            f"in {conf_path or cfg_mod.config_path()}, or pass "
            "--transport/--host/... (plus --hop entries) to build an "
            "ad-hoc profile."
        )

    if no_prompt:
        if not c.default_profile:
            raise click.UsageError(
                "--no-prompt was given but no `default_profile` is set in config"
            )
        return c.resolve_profile(c.default_profile)

    return _interactive_pick(c)


def _list_profiles(c: cfg_mod.Config, *, verbose: bool) -> None:
    click.echo("Available connect profiles:")
    click.echo(f"  0. {OFFLINE_PROFILE_NAME}  browse local database (no connection)")
    for i, p in enumerate(c.connect_profiles, start=1):
        marker = " (default)" if p.name == c.default_profile else ""
        user_hops = [s for s in p.connect_script if s.cmd]
        suffix = f"  {len(user_hops)}-hop" if user_hops else "  direct"
        click.echo(f"  {i}. {p.name}{marker}{suffix}")
        if verbose:
            for j, step in enumerate(user_hops, start=1):
                click.echo(f"       {j}. Command: {step.cmd!r}, Wait for: {step.val!r}")


def _interactive_pick(c: cfg_mod.Config) -> ConnectProfile:
    _list_profiles(c, verbose=False)
    names = [p.name for p in c.connect_profiles]
    default_idx = next(
        (i for i, p in enumerate(c.connect_profiles, start=1) if p.name == c.default_profile),
        1,
    )
    while True:
        raw = click.prompt(
            "Profile num (v for profile details, q to quit)",
            default=str(default_idx),
            show_default=True,
        )
        s = str(raw).strip()
        if s.lower() == "q":
            raise click.exceptions.Exit(0)
        if s.lower() == "v":
            _list_profiles(c, verbose=True)
            continue
        if s == "0" or s == OFFLINE_PROFILE_NAME:
            return _OFFLINE_PROFILE
        if s.isdigit():
            idx = int(s) - 1
            if 0 <= idx < len(c.connect_profiles):
                return c.connect_profiles[idx]
        if s in names:
            return c.resolve_profile(s)
        click.echo(f"  not a recognised choice: {s!r}")


class _VersionedCommand(click.Command):
    def format_help(self, ctx, formatter):
        formatter.write(f"Version {__version__}\n")
        super().format_help(ctx, formatter)


@click.command(
    cls=_VersionedCommand,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Connect to a WhatsPac service and drop into an interactive prompt.",
)
@click.version_option(
    __version__,
    "-v",
    "--version",
    message="whatspyc version %(version)s",
)
@click.option("--profile", "profile_name", default=None, help="Named connect profile from config.")
@click.option("--no-prompt", is_flag=True, help="Skip the picker; use default_profile.")
@click.option(
    "--hop",
    "hops",
    multiple=True,
    help='Ad-hoc hop step "cmd|val", repeat for multi-hop. e.g. --hop "C MB7NPW|Connected".',
)
@click.option(
    "--engine",
    type=click.Choice(["xrouter", "bpq"]),
    default=None,
    help="Host node engine. Used by ad-hoc rhp-ws to pick the default port.",
)
@click.option(
    "--transport",
    type=click.Choice(["rhp-ws", "rhp-tcp", "direct-tcp"]),
    default=None,
)
@click.option("--host", default=None)
@click.option("--port", type=int, default=None)
@click.option("--radio-port", type=int, default=None, help="XRouter radio port number")
@click.option("--ax-level", type=click.Choice(["L2", "L4"]), default=None)
@click.option("--my-call", default=None, help="Your callsign, including SSID if any")
@click.option("--name", default=None)
@click.option("--remote", default=None, help="AX.25 service callsign (default WPS)")
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory for the SQLite state DB (default: "
    "$XDG_DATA_HOME/whatspyc, i.e. ~/.local/share/whatspyc).",
)
@click.option(
    "--ui",
    "ui_mode",
    type=click.Choice(["line", "textual", "urwid"]),
    default=None,
    help="UI backend. line = simple stdin/stdout REPL; textual = Textual full-"
    "screen multi-pane; urwid = urwid full-screen multi-pane (lighter on "
    "slow hardware).",
)
@click.option(
    "--nodecmd",
    is_flag=True,
    default=False,
    help="Packet-node mode: read callsign from stdin (unprompted), "
    "prompt for name on first use, store per-call state under "
    "node_state_dir/<CALL>. Forces --ui line; rejects --my-call "
    "and --state-dir.",
)
@click.option(
    "--log-level",
    default=None,
    help="Python logging level. Wins over config / WHATSPYC_LOG env var.",
)
@click.option(
    "--log-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Route log records to a file in addition to (not instead of) the "
    "console sink. Wins over the `log_file` config key.",
)
@click.option(
    "--log-console",
    type=click.Choice(["auto", "stderr", "pane", "off"]),
    default=None,
    help="Console log sink. 'auto' (default): textual/urwid UI → status "
    "pane, line UI → stderr. 'pane' is rejected with --ui line.",
)
@click.option(
    "--conf",
    "conf",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Alternative path to the config file. Overrides the default "
    "$XDG_CONFIG_HOME/whatspyc/config.toml location. Channels.toml stays "
    "at its default location.",
)
def main(profile_name, no_prompt, hops, engine, transport, host, port, radio_port, ax_level,
         my_call, name, remote, state_dir, ui_mode, nodecmd, log_level, log_file,
         log_console, conf) -> None:
    """Connect to a WhatsPac service and drop into an interactive prompt."""
    # `--nodecmd` reads the callsign from stdin with no prompt printed
    # first — that's the convention packet nodes follow. Skip the banner
    # in that mode so the program's first stdout write is either the
    # name prompt (first run for that call) or the connect output.
    if not nodecmd:
        click.echo(
            f"\nwhatspyc (v{__version__}) text-only WhatsPac client - WhatsPac is designed for "
            "GUI experience, try it at http://whatspac.oarc.uk/\n"
        )
    conf_path = conf if conf is not None else cfg_mod.config_path()
    try:
        c = cfg_mod.load(conf)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None
    if nodecmd:
        _apply_nodecmd_mode(
            c,
            ui_mode=ui_mode,
            my_call_cli=my_call,
            state_dir_cli=state_dir,
            conf_path=conf_path,
        )
    # Precedence: CLI flag > config key > env var (WHATSPYC_LOG, handled in
    # log.setup) > hardcoded WARNING. log_file has no env var.
    effective_ui = ui_mode or c.ui
    effective_console = log_console or c.log_console
    _is_full_screen_ui = effective_ui in ("textual", "urwid")
    if effective_console == "auto":
        effective_console = "pane" if _is_full_screen_ui else "stderr"
    elif effective_console == "pane" and not _is_full_screen_ui:
        raise click.UsageError(
            "log_console = 'pane' requires --ui textual or --ui urwid "
            "(the line UI has no status pane to write to)."
        )
    log.setup(
        level=log_level or c.log_level,
        file=log_file if log_file is not None else c.log_file,
        console=effective_console,
    )

    # Apply global (non-connection) overrides.
    if my_call is not None:
        c.my_call = my_call
    if name is not None:
        c.name = name
    if state_dir is not None:
        c.state_dir = state_dir
    if ui_mode is not None:
        c.ui = ui_mode
    if not c.my_call:
        raise click.UsageError("--my-call is required (or set in ~/.config/whatspyc/config.toml)")
    if not c.name:
        raise click.UsageError("--name is required (or set in ~/.config/whatspyc/config.toml)")
    # Translate TUI perf config keys into TEXTUAL_* env vars before any
    # Textual code runs. Skipped for line and urwid UIs — those env
    # vars only affect Textual's driver.
    if effective_ui == "textual":
        _apply_textual_perf_env(c)

    parsed_hops = _parse_hops(hops)
    adhoc_args = {
        "transport": transport,
        "host": host,
        "port": port,
        "engine": engine,
        "radio_port": radio_port,
        "ax_level": ax_level,
        "remote": remote,
    }

    if effective_ui in ("textual", "urwid"):
        # Session-driven flow: picker, connect-progress, and reconnect
        # all happen inside the UI itself. cli.py contributes the
        # connection_opener (the bit that actually builds + opens the
        # WpsClient) and resolves the initial profile.
        initial_profile = _resolve_initial_profile_for_session(
            c,
            profile_name=profile_name,
            no_prompt=no_prompt,
            parsed_hops=parsed_hops,
            adhoc_args=adhoc_args,
            conf_path=conf_path,
        )
        asyncio.run(_run_session_driven(c, initial_profile))
        return

    profile = _pick_profile(
        c,
        profile_name=profile_name,
        no_prompt=no_prompt,
        hops=parsed_hops,
        adhoc_args=adhoc_args,
        conf_path=conf_path,
    )
    # If the picker would have run on this invocation (no explicit profile,
    # no ad-hoc flags, no --no-prompt, and profiles are configured), use the
    # picker again on terminal link loss; otherwise offer a simple
    # reconnect-or-quit prompt against the same profile.
    picker_used = (
        not profile_name
        and not parsed_hops
        and not any(v is not None for v in adhoc_args.values())
        and not no_prompt
        and bool(c.connect_profiles)
    )
    on_terminal_disconnect = _make_reconnect_handler(c, profile, picker_used=picker_used)
    asyncio.run(_run(c, profile, on_terminal_disconnect))


def _resolve_initial_profile_for_session(
    c: cfg_mod.Config,
    *,
    profile_name: str | None,
    no_prompt: bool,
    parsed_hops: list[HopStep],
    adhoc_args: dict,
    conf_path: Path | None = None,
) -> ConnectProfile | None:
    """Mirror ``_pick_profile``'s precedence but never run the click
    prompt — for textual/urwid the in-UI picker handles that case.

    Returns the resolved profile, or ``None`` to mean "show the in-UI
    picker on startup".
    """
    if profile_name and (parsed_hops or any(v is not None for v in adhoc_args.values())):
        raise click.UsageError(
            "--profile is mutually exclusive with --hop / --transport / "
            "--host / --remote etc."
        )
    if profile_name:
        if profile_name == OFFLINE_PROFILE_NAME:
            return _OFFLINE_PROFILE
        try:
            return c.resolve_profile(profile_name)
        except KeyError as exc:
            raise click.UsageError(str(exc)) from None
    if parsed_hops or any(v is not None for v in adhoc_args.values()):
        return _adhoc_profile(hops=parsed_hops, **adhoc_args)
    if no_prompt:
        if not c.default_profile:
            raise click.UsageError(
                "--no-prompt was given but no `default_profile` is set in config"
            )
        return c.resolve_profile(c.default_profile)
    if not c.connect_profiles:
        raise click.UsageError(
            "no connection configured. Either define [[connect_profiles]] "
            f"in {conf_path or cfg_mod.config_path()}, or pass "
            "--transport/--host/... (plus --hop entries) to build an "
            "ad-hoc profile."
        )
    return None


def _make_connection_opener(c: cfg_mod.Config, store: SqliteStore):
    """Build the (opener, closer) pair the UI calls to open / close
    a WPS link for a given profile.

    ``opener(profile, progress, on_event, on_client_ready)`` is the
    "build a fresh WpsClient and open it" coroutine. It closes the
    previously-opened client (if any), registers the freshly-built
    client via ``on_client_ready`` *before* ``client.open()`` returns
    so the UI's render_event sees a real client when the type-`c`
    burst starts flowing, and returns ``(client, ConnectSummary)``.

    ``closer()`` shuts whatever client is currently held — used in
    the outer ``finally`` of ``_run_session_driven`` to clean up on
    normal exit.
    """

    state: dict[str, WpsClient | None] = {"client": None}

    async def _close_state_client() -> None:
        cur = state["client"]
        if cur is not None:
            try:
                await cur.close()
            except Exception:
                pass
            state["client"] = None

    async def opener(
        profile: ConnectProfile,
        *,
        progress: Callable[[Any], None],
        on_event: Callable[[dict], Awaitable[None]],
        on_client_ready: Callable[[WpsClient], None] | None = None,
    ) -> tuple[WpsClient, ConnectSummary]:
        await _close_state_client()
        if _is_offline_profile(profile):
            def _no_stream() -> AsyncByteStream:
                raise RuntimeError(
                    "offline mode: WpsClient stream factory called — "
                    "a UI guard is missing for some send / network path"
                )

            client = WpsClient(
                _no_stream,
                store,
                my_call=c.app_call,  # type: ignore[arg-type]
                name=c.name,
                on_event=on_event,
                connect_script=[],
                auto_backfill_post_count=c.auto_backfill_post_count,
                auto_reconnect=False,
                reconnect_max_retries=0,
                delivery_timeout_s=c.delivery_timeout_s,
            )
            state["client"] = client
            if on_client_ready is not None:
                on_client_ready(client)
            return client, ConnectSummary(paused_channels=[], online_users=[])

        seq = ConnectSequence(my_call=c.app_call)

        async def hook(obj: dict) -> None:
            await seq.on_event(obj)
            await on_event(obj)

        client = WpsClient(
            lambda: _build_stream_for(profile, c.my_call),  # type: ignore[arg-type]
            store,
            my_call=c.app_call,  # type: ignore[arg-type]
            name=c.name,
            on_event=hook,
            connect_script=profile.connect_script,
            hop_progress=progress,
            auto_backfill_post_count=c.auto_backfill_post_count,
            auto_reconnect=c.auto_reconnect,
            reconnect_max_retries=c.reconnect_max_retries,
            delivery_timeout_s=c.delivery_timeout_s,
        )
        state["client"] = client
        if on_client_ready is not None:
            on_client_ready(client)
        progress(f"Connecting with '{profile.name}' profile...")
        try:
            await client.open()
        except BaseException:
            # Cancel / handshake-error path. Clear state so the next
            # opener call doesn't try to close a half-open client.
            try:
                await client.close()
            except Exception:
                pass
            state["client"] = None
            raise
        progress("Sending connection details...")
        summary = await seq.wait()
        return client, summary

    return opener, _close_state_client


async def _run_session_driven(
    c: cfg_mod.Config,
    initial_profile: ConnectProfile | None,
) -> None:
    """textual/urwid entry point. Hands the picker + connect lifecycle
    to the UI; cli.py contributes only the connector closure.

    ``initial_profile`` is non-``None`` when the user gave an explicit
    ``--profile`` / ad-hoc flags / ``--no-prompt`` (so we know what to
    connect to up front). ``None`` means "ask the user via the in-UI
    picker."
    """
    store = SqliteStore(Path(c.state_dir) / "state.sqlite3")
    opener, closer = _make_connection_opener(c, store)
    options = SessionOptions(
        show_acks=c.show_acks,
        show_edits=c.show_edits,
        verbose_history=c.verbose_history,
        delivery_timeout_s=c.delivery_timeout_s,
        bell_on_activity=c.bell_on_activity,
        notify_new_dms=c.notify_new_dms,
        notify_new_posts=c.notify_new_posts,
        notify_user_conn=c.notify_user_conn,
    )
    # Picker entries: <offline> at index 0, then configured profiles.
    available = [_OFFLINE_PROFILE] + list(c.connect_profiles)
    if c.ui == "textual":
        ui = TextualUI(
            client=None,
            my_call=c.app_call,  # type: ignore[arg-type]
            channels=c.channels,
            history_backfill=c.history_backfill,
            options=options,
            cursor_blink=not c.low_power_mode,
            connection_opener=opener,
            available_profiles=available,
            initial_profile=initial_profile,
            default_profile_name=c.default_profile,
            is_offline_profile=_is_offline_profile,
        )
    else:
        ui = UrwidUI(
            client=None,
            my_call=c.app_call,  # type: ignore[arg-type]
            channels=c.channels,
            history_backfill=c.history_backfill,
            options=options,
            connection_opener=opener,
            available_profiles=available,
            initial_profile=initial_profile,
            default_profile_name=c.default_profile,
            is_offline_profile=_is_offline_profile,
        )
    try:
        await ui.run()
    finally:
        try:
            await closer()
        finally:
            store.close()


def _make_reconnect_handler(
    c: cfg_mod.Config,
    initial_profile: ConnectProfile,
    *,
    picker_used: bool,
) -> Callable[[], ConnectProfile | None]:
    """Build the strategy for the post-disconnect prompt.

    If the user originally landed on this profile via the interactive
    picker, re-show the picker on terminal disconnect (they may want to
    pick a different profile). Otherwise — explicit ``--profile``,
    ``--no-prompt``, or ad-hoc flags — there's nothing to pick between, so
    offer a single reconnect-or-quit prompt that defaults to quit.
    Returns ``None`` to mean "stop reconnecting, exit the program".
    """

    if picker_used:
        def _repick() -> ConnectProfile | None:
            try:
                return _interactive_pick(c)
            except click.exceptions.Exit:
                return None
        return _repick

    def _reconnect_or_quit() -> ConnectProfile | None:
        raw = click.prompt(
            "Reconnect (r), or Quit (q)?",
            default="q",
            show_default=False,
            prompt_suffix=" ",
        )
        return initial_profile if str(raw).strip().lower() == "r" else None

    return _reconnect_or_quit


def _format_connect_error(exc: BaseException) -> str:
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused — is the host/port correct and the service running?"
    if isinstance(exc, asyncio.IncompleteReadError):
        return "link closed unexpectedly during connect"
    if isinstance(exc, TimeoutError):
        return "connection timed out"
    if isinstance(exc, HopScriptError):
        return f"hop script failed: {exc}"
    if isinstance(exc, OSError):
        # gaierror (DNS), no route to host, network unreachable, etc.
        msg = str(exc) or exc.__class__.__name__
        return f"network error: {msg}"
    return f"{type(exc).__name__}: {exc}"


async def _watch_stdin_for_cancel() -> None:
    """Resolve when the user types 'q' + Enter on stdin.

    Used during the connect attempt so the user can bail out of a slow
    handshake (hop chain, RHP open, type-`c` settle) without ctrl+c. Runs
    until cancelled; the caller cancels it once the connect race resolves.
    """
    loop = asyncio.get_running_loop()
    fd = sys.stdin.fileno()
    fut: asyncio.Future[None] = loop.create_future()
    buf = bytearray()

    def _on_readable() -> None:
        try:
            chunk = os.read(fd, 1024)
        except (BlockingIOError, OSError):
            return
        if not chunk:
            # EOF on stdin — stop watching but don't trigger cancel.
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
            return
        buf.extend(chunk)
        while b"\n" in buf:
            line, _, rest = bytes(buf).partition(b"\n")
            buf[:] = rest
            if line.strip().lower() == b"q":
                if not fut.done():
                    fut.set_result(None)
                return

    try:
        loop.add_reader(fd, _on_readable)
    except (NotImplementedError, OSError):
        # add_reader unavailable (Windows ProactorEventLoop, non-fd stdin
        # under some test runners). Fall back to never resolving — ctrl+c
        # still works as the cancel path of last resort.
        await asyncio.Event().wait()
        return
    try:
        await fut
    finally:
        try:
            loop.remove_reader(fd)
        except Exception:
            pass


async def _run(
    c: cfg_mod.Config,
    profile: ConnectProfile,
    on_terminal_disconnect: Callable[[], ConnectProfile | None],
) -> None:
    store = SqliteStore(Path(c.state_dir) / "state.sqlite3")
    try:
        # Offline mode never produces a terminal link-loss event and has
        # no profile to "reconnect" to — `/quit` ends the session.
        if _is_offline_profile(profile):
            await _run_offline(c, store)
            return
        current_profile = profile
        while True:
            exit_reason = await _connect_and_run_ui(c, current_profile, store)
            if exit_reason != "terminal":
                return
            click.echo()
            click.echo("Disconnected from WPS.")
            click.echo()
            next_profile = on_terminal_disconnect()
            if next_profile is None:
                return
            current_profile = next_profile
    finally:
        store.close()


async def _run_offline(c: cfg_mod.Config, store: SqliteStore) -> None:
    """Run the UI against the local store with no WPS connection.

    Builds a ``WpsClient`` but never opens it — the read paths the UIs
    use (``_store.*``, ``ham_name``, ``paused_channels``,
    ``online_users``) all work without a live link, and the
    ``offline=True`` flag tells the UI to refuse the send / network
    paths up front rather than letting them fail with
    ``ConnectionError`` deeper in the stack.
    """
    def _no_stream() -> AsyncByteStream:
        # Offline mode never opens the link, so this factory should never
        # be invoked. If we ever land here it means a code path tried to
        # send through a not-opened client; the message names the cause.
        raise RuntimeError(
            "offline mode: WpsClient stream factory called — a UI guard "
            "is missing for some send / network path"
        )

    client = WpsClient(
        _no_stream,
        store,
        my_call=c.app_call,  # type: ignore[arg-type]
        name=c.name,
        on_event=None,
        connect_script=[],
        auto_backfill_post_count=c.auto_backfill_post_count,
        auto_reconnect=False,
        reconnect_max_retries=0,
        delivery_timeout_s=c.delivery_timeout_s,
    )
    options = SessionOptions(
        show_acks=c.show_acks,
        show_edits=c.show_edits,
        verbose_history=c.verbose_history,
        delivery_timeout_s=c.delivery_timeout_s,
        bell_on_activity=c.bell_on_activity,
        notify_new_dms=c.notify_new_dms,
        notify_new_posts=c.notify_new_posts,
        notify_user_conn=c.notify_user_conn,
    )
    if c.ui == "textual":
        ui = TextualUI(  # type: ignore[arg-type]
            client,
            my_call=c.app_call,
            channels=c.channels,
            history_backfill=c.history_backfill,
            options=options,
            offline=True,
            cursor_blink=not c.low_power_mode,
        )
    elif c.ui == "urwid":
        ui = UrwidUI(  # type: ignore[arg-type]
            client,
            my_call=c.app_call,
            channels=c.channels,
            history_backfill=c.history_backfill,
            options=options,
            offline=True,
        )
    else:
        ui = LineUI(  # type: ignore[arg-type]
            client,
            my_call=c.app_call,
            channels=c.channels,
            history_backfill=c.history_backfill,
            options=options,
            offline=True,
        )
    click.echo()
    click.echo(f"[{OFFLINE_PROFILE_NAME}] browsing local store, no connection.")
    await ui.run()


async def _connect_and_run_ui(
    c: cfg_mod.Config,
    profile: ConnectProfile,
    store: SqliteStore,
) -> str | None:
    """Run one connect-and-UI cycle. Returns ``"terminal"`` if the link
    dropped without recovery (cli should offer reconnect/quit), otherwise
    ``None`` (clean exit / cancelled connect — cli should stop)."""
    # Snapshot before client.open(): inbound DMs / subscriptions land
    # in the store as the connect burst arrives, so reading after open
    # would always look "non-fresh".
    is_fresh_connect = store.is_first_connect()
    seq = ConnectSequence(my_call=c.app_call)

    # Hold UI rendering until the connect sequence settles so the
    # "Sending connection details..." line stays put — otherwise the
    # roster (`o`) lands first and the user thinks they're online before
    # the rest of the handshake actually completes.
    pending_events: list[dict] = []
    connect_done = False

    async def event_hook(obj: dict) -> None:
        await seq.on_event(obj)
        if connect_done:
            ui.render_event(obj)
        else:
            pending_events.append(obj)

    client = WpsClient(
        lambda: _build_stream_for(profile, c.my_call),  # type: ignore[arg-type]
        store,
        my_call=c.app_call,  # type: ignore[arg-type]
        name=c.name,
        on_event=event_hook,
        connect_script=profile.connect_script,
        hop_progress=click.echo,
        auto_backfill_post_count=c.auto_backfill_post_count,
        auto_reconnect=c.auto_reconnect,
        reconnect_max_retries=c.reconnect_max_retries,
        delivery_timeout_s=c.delivery_timeout_s,
    )
    options = SessionOptions(
        show_acks=c.show_acks,
        show_edits=c.show_edits,
        verbose_history=c.verbose_history,
        delivery_timeout_s=c.delivery_timeout_s,
        bell_on_activity=c.bell_on_activity,
        notify_new_dms=c.notify_new_dms,
        notify_new_posts=c.notify_new_posts,
        notify_user_conn=c.notify_user_conn,
    )
    if c.ui == "textual":
        ui = TextualUI(  # type: ignore[arg-type]
            client,
            my_call=c.app_call,
            channels=c.channels,
            history_backfill=c.history_backfill,
            options=options,
            cursor_blink=not c.low_power_mode,
        )
    elif c.ui == "urwid":
        ui = UrwidUI(  # type: ignore[arg-type]
            client,
            my_call=c.app_call,
            channels=c.channels,
            history_backfill=c.history_backfill,
            options=options,
        )
    else:
        ui = LineUI(  # type: ignore[arg-type]
            client,
            my_call=c.app_call,
            channels=c.channels,
            history_backfill=c.history_backfill,
            options=options,
        )

    click.echo(
        f"Connecting with '{profile.name}' profile "
        "(send q to cancel connection attempt)..."
    )

    async def _connect_phase():
        await client.open()
        click.echo("Sending connection details...")
        return await seq.wait()

    cancel_task = asyncio.create_task(_watch_stdin_for_cancel())
    connect_task = asyncio.create_task(_connect_phase())
    try:
        await asyncio.wait(
            {cancel_task, connect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        cancel_task.cancel()
        connect_task.cancel()
        raise

    if not connect_task.done():
        # User typed 'q' — tear down anything the partial handshake
        # opened (entry-node link, hop chain, etc.) and bail.
        connect_task.cancel()
        try:
            await connect_task
        except (asyncio.CancelledError, Exception):
            pass
        click.echo("Cancelling — disconnecting...")
        await client.close()
        return None

    cancel_task.cancel()
    try:
        await cancel_task
    except (asyncio.CancelledError, Exception):
        pass
    exc = connect_task.exception()
    if exc is not None:
        click.echo()
        click.echo(f"Could not connect: {_format_connect_error(exc)}")
        await client.close()
        return None
    summary = connect_task.result()
    # Aggregate connect-window DMs (from `m` / `mb`) into one summary line.
    # Per-row prints would scroll a busy reconnect off the top before the
    # user can read it; non-DM buffered events still replay individually.
    dm_senders: dict[str, int] = {}
    my_upper = c.app_call.upper()  # type: ignore[union-attr]
    line_unread_dms = getattr(ui, "_unread_dms", None)
    for obj in pending_events:
        t = obj.get("t")
        if t == "m":
            fc = (obj.get("fc") or "").upper()
            if fc and fc != my_upper:
                dm_senders[fc] = dm_senders.get(fc, 0) + 1
                if line_unread_dms is not None:
                    line_unread_dms[fc] = line_unread_dms.get(fc, 0) + 1
        elif t == "mb":
            for m in obj.get("m", []):
                fc = (m.get("fc") or "").upper()
                if fc and fc != my_upper:
                    dm_senders[fc] = dm_senders.get(fc, 0) + 1
                    if line_unread_dms is not None:
                        line_unread_dms[fc] = line_unread_dms.get(fc, 0) + 1
        else:
            ui.render_event(obj)
    pending_events.clear()
    connect_done = True
    if is_fresh_connect:
        # Fresh-DB / first-ever connect: the server hardcodes mc=0 in
        # `first_time_connect_handler` and replays per-peer `mb` batches
        # whose arrival can straddle the connect-quiescence window on
        # slow packet links. A "0 new DMs" or under-counted line would
        # mislead the user — instead, tell them traffic is incoming.
        click.echo()
        click.echo("[Connected]")
        click.echo()
        click.echo("First connection, existing DMs will sync in the background.")
        click.echo()
    else:
        parts = [
            f"{summary.received_message_count} new DMs",
            f"{summary.server_post_count} new posts",
        ]
        if summary.paused_channels:
            n = len(summary.paused_channels)
            parts.append(
                f"{n} paused channel(s) — see /unpause hint(s) above"
            )
        click.echo()
        click.echo("[Connected] " + ", ".join(parts))
        if dm_senders:
            ordered = sorted(dm_senders.items(), key=lambda kv: (-kv[1], kv[0]))
            click.echo(
                "[New DMs from "
                + ", ".join(f"{call} ({n})" for call, n in ordered)
                + "]"
            )
    try:
        await ui.run()
    finally:
        await client.close()
    return ui.exit_reason


if __name__ == "__main__":
    main()
