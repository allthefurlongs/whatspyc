"""Offline-mode coverage: CLI picker sentinel, LineUI guards, config collision."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import tomllib
except ImportError:
    import tomli as tomllib

import click

from whatspyc import cli, config as cfg_mod
from whatspyc.config import ChannelInfo, Config
from whatspyc.store.store import SqliteStore
from whatspyc.ui.line import LineUI
from whatspyc.ui.options import SessionOptions


# ----------------------------------------------------------------------
# CLI sentinel + picker
# ----------------------------------------------------------------------


def test_list_profiles_includes_offline_at_position_zero(capsys) -> None:
    c = Config()
    cli._list_profiles(c, verbose=False)
    out = capsys.readouterr().out
    # Position 0 means literally "0." in the listing — user profiles
    # would start at 1.
    assert f"0. {cli.OFFLINE_PROFILE_NAME}" in out
    assert "browse local database" in out


def test_interactive_pick_returns_offline_for_zero(monkeypatch, capsys) -> None:
    c = Config()
    # No configured profiles, so the only valid pick is 0 (offline).
    monkeypatch.setattr(click, "prompt", lambda *a, **kw: "0")
    p = cli._interactive_pick(c)
    assert p.name == cli.OFFLINE_PROFILE_NAME
    assert cli._is_offline_profile(p)


def test_pick_profile_accepts_offline_name() -> None:
    c = Config()
    p = cli._pick_profile(
        c,
        profile_name=cli.OFFLINE_PROFILE_NAME,
        no_prompt=False,
        hops=[],
        adhoc_args={},
    )
    assert cli._is_offline_profile(p)


# ----------------------------------------------------------------------
# Config: collision name rejected
# ----------------------------------------------------------------------


def test_config_rejects_offline_profile_name() -> None:
    raw = tomllib.loads(
        """
        [[connect_profiles]]
        name = "<offline>"
        transport = "direct-tcp"
        """
    )
    with pytest.raises(ValueError, match="reserved"):
        cfg_mod.parse(raw)


# ----------------------------------------------------------------------
# LineUI offline=True: read paths work, write/network paths refuse
# ----------------------------------------------------------------------


def _make_offline_ui(
    tmp_path: Path,
    *,
    channels: list[ChannelInfo] | None = None,
) -> tuple[LineUI, SqliteStore, SimpleNamespace]:
    store = SqliteStore(tmp_path / "state.sqlite3")
    # The offline UI should never call any send / network methods on the
    # client. Stub them to record-and-fail so the tests assert the guard
    # fires before the call site.
    calls: list[str] = []

    def _record(name):
        async def _f(*a, **kw):
            calls.append(name)
        return _f

    client = SimpleNamespace(
        _store=store,
        _paused_channels={},
        paused_channels=lambda: dict(client._paused_channels),
        auto_backfill_post_count=None,
        ham_name=lambda call: None,
        online_users=lambda: [],
        is_auto_reconnect=False,
        set_delivery_timeout_s=lambda v: None,
        send_message=_record("send_message"),
        post=_record("post"),
        subscribe_and_wait=_record("subscribe_and_wait"),
        request_post_batch=_record("request_post_batch"),
        unsubscribe=_record("unsubscribe"),
        unpause_channel=_record("unpause_channel"),
        edit_message=_record("edit_message"),
        edit_post=_record("edit_post"),
        resend_message=_record("resend_message"),
        resend_post=_record("resend_post"),
        react_message=_record("react_message"),
        react_post=_record("react_post"),
        close=_record("close"),
        calls=calls,
    )
    ui = LineUI(
        client,
        my_call="M0ABC",
        history_backfill=3,
        channels=channels,
        options=SessionOptions(),
        offline=True,
    )
    return ui, store, client


def test_offline_send_to_target_refuses(tmp_path: Path, capsys) -> None:
    ui, store, client = _make_offline_ui(tmp_path)
    ui._target = ("dm", "M0FOO")
    asyncio.run(ui._send_to_target("hello"))
    out = capsys.readouterr().out
    assert "[offline]" in out
    assert client.calls == []
    store.close()


@pytest.mark.parametrize(
    "command",
    [
        "/sub 5",
        "/unsub 5",
        "/unpause 5",
        "/editdm 1 new body",
        "/editpost 1 new body",
        "/retrydm 1",
        "/retrypost 1",
        "/react 1 \U0001f44d",
    ],
)
def test_offline_network_commands_refuse(
    tmp_path: Path, capsys, command: str
) -> None:
    ui, store, client = _make_offline_ui(tmp_path)
    # /react needs a target to even reach the offline guard; set one.
    ui._target = ("dm", "M0FOO")
    asyncio.run(ui._handle_command(command))
    out = capsys.readouterr().out
    assert "[offline]" in out, f"expected refusal hint for {command!r}, got: {out!r}"
    assert client.calls == [], (
        f"command {command!r} should not have invoked the client; got {client.calls}"
    )
    store.close()


def test_offline_history_command_works(tmp_path: Path, capsys) -> None:
    """Read-only paths (/dm + auto-backfill, /history) keep working."""
    ui, store, _ = _make_offline_ui(tmp_path)
    store.upsert_message(
        {"_id": "1000-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
         "m": "hi", "ts": 1_000}
    )
    asyncio.run(ui._handle_command("/dm M0FOO"))
    out = capsys.readouterr().out
    assert "hi" in out
    assert "[offline]" not in out
    store.close()


def test_offline_ch_to_unsubscribed_skips_prompt(tmp_path: Path, capsys) -> None:
    """Offline /ch to a channel we're not subscribed to should just switch
    + show history (if any), never prompt to subscribe."""
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store, client = _make_offline_ui(tmp_path, channels=channels)
    store.upsert_post(5, {"ts": 100, "fc": "M0FOO", "p": "alpha"})
    # Sentinel: if the subscribe-prompt branch fires, _prompt_yes_no
    # would be awaited — replace it with a marker so we'd notice.
    async def _explode(*a, **kw):
        raise AssertionError("subscribe prompt should be skipped offline")
    ui._prompt_yes_no = _explode  # type: ignore[method-assign]
    asyncio.run(ui._handle_command("/ch 5"))
    out = capsys.readouterr().out
    assert "alpha" in out
    assert ui._target == ("ch", "5")
    assert client.calls == []
    store.close()


def test_offline_prompt_label_indicates_mode(tmp_path: Path) -> None:
    ui, store, _ = _make_offline_ui(tmp_path)
    assert ui._prompt_label().startswith("(offline) ")
    ui._target = ("dm", "M0FOO")
    assert ui._prompt_label().startswith("(offline) ")
    store.close()
