"""History backfill + /history slash-command coverage for LineUI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from whatspyc.config import ChannelInfo
from whatspyc.store.store import SqliteStore
from whatspyc.ui.line import LineUI
from whatspyc.ui.options import SessionOptions


def _make_ui(
    tmp_path: Path,
    *,
    history_backfill: int = 3,
    channels: list[ChannelInfo] | None = None,
    options: SessionOptions | None = None,
) -> tuple[LineUI, SqliteStore]:
    store = SqliteStore(tmp_path / "state.sqlite3")
    client = SimpleNamespace(
        _store=store,
        _paused_channels={},
        paused_channels=lambda: dict(client._paused_channels),
        auto_backfill_post_count=None,
        ham_name=lambda call: (
            (store.lookup_ham(call) or {}).get("name") or None
        ),
        # /set delivery_timeout_s forwards the new value into the client
        # — it's the timer-owning side. Real WpsClient exposes this as a
        # method; stub it as a no-op for the UI tests.
        set_delivery_timeout_s=lambda v: None,
    )
    ui = LineUI(
        client,
        my_call="M0ABC",
        history_backfill=history_backfill,
        channels=channels,
        options=options,
    )
    return ui, store


def test_show_history_prints_oldest_first_for_dm(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    # Insert three DM rows with M0FOO; ts in ascending order so we can
    # assert "oldest first" in the output even though the store query is
    # ORDER BY ts DESC.
    for ts, body in [(1_000, "first"), (2_000, "middle"), (3_000, "latest")]:
        store.upsert_message(
            {"_id": f"{ts}-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
             "m": body, "ts": ts}
        )

    ui._show_history(("dm", "M0FOO"), 3)
    captured = capsys.readouterr().out

    lines = [l for l in captured.splitlines() if l]
    assert "last 3 message(s) with M0FOO" in lines[0]
    assert lines[1].endswith("first")
    assert lines[2].endswith("middle")
    assert lines[3].endswith("latest")
    store.close()


def test_show_history_silent_when_store_empty(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    ui._show_history(("dm", "NOBODY"), 5)
    ui._show_history(("ch", "42"), 5)
    assert capsys.readouterr().out == ""
    store.close()


def test_show_history_zero_disables(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path, history_backfill=0)
    store.upsert_message(
        {"_id": "1-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
         "m": "hi", "ts": 1_000}
    )
    # Drive the auto-backfill via the /dm command.
    asyncio.run(ui._handle_command("/dm M0FOO"))
    out = capsys.readouterr().out
    assert "last" not in out  # no backfill banner printed
    store.close()


def test_target_command_triggers_backfill(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path, history_backfill=2)
    store.set_subscription(7, True)
    store.upsert_post(7, {"ts": 100, "fc": "M0FOO", "p": "alpha"})
    store.upsert_post(7, {"ts": 200, "fc": "M0FOO", "p": "beta"})
    store.upsert_post(7, {"ts": 300, "fc": "M0FOO", "p": "gamma"})

    asyncio.run(ui._handle_command("/ch 7"))
    out = capsys.readouterr().out
    # Default is 2 — so we should see beta and gamma but not alpha.
    assert "alpha" not in out
    assert "beta" in out
    assert "gamma" in out
    assert "last 2 post(s) in ch:7" in out
    store.close()


def test_history_command_overrides_default(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path, history_backfill=1)
    store.set_subscription(7, True)
    for ts in (100, 200, 300, 400):
        store.upsert_post(7, {"ts": ts, "fc": "M0FOO", "p": f"p{ts}"})

    asyncio.run(ui._handle_command("/ch 7"))
    capsys.readouterr()  # discard auto-backfill output

    asyncio.run(ui._handle_command("/history 3"))
    out = capsys.readouterr().out
    assert "p200" in out and "p300" in out and "p400" in out
    assert "p100" not in out
    store.close()


def test_history_without_target_warns(tmp_path: Path, capsys) -> None:
    ui, _ = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/history 5"))
    out = capsys.readouterr().out
    assert "no current target" in out


def test_history_rejects_non_int(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/dm M0FOO"))
    capsys.readouterr()
    asyncio.run(ui._handle_command("/history banana"))
    out = capsys.readouterr().out
    assert "integer" in out
    store.close()


def test_history_rejects_zero_or_negative(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/dm M0FOO"))
    capsys.readouterr()
    asyncio.run(ui._handle_command("/history 0"))
    out = capsys.readouterr().out
    assert "positive integer" in out
    store.close()


def test_ch_command_resolves_name_from_directory(tmp_path: Path, capsys) -> None:
    channels = [ChannelInfo(cid=5, name="lounge"), ChannelInfo(cid=11, name="wps-development")]
    ui, store = _make_ui(tmp_path, history_backfill=2, channels=channels)
    # Pre-subscribe so the resolver test isn't entangled with the
    # subscribe-prompt branch — that flow is covered separately.
    store.set_subscription(5, True)
    store.upsert_post(5, {"ts": 100, "fc": "M0FOO", "p": "alpha"})
    store.upsert_post(5, {"ts": 200, "fc": "M0FOO", "p": "beta"})

    asyncio.run(ui._handle_command("/ch #LOUNGE"))  # case-insensitive
    out = capsys.readouterr().out
    assert ui._target == ("ch", "5")
    assert "alpha" in out and "beta" in out
    assert "ch:5" in out
    store.close()


def test_ch_command_resolves_name_without_leading_hash(
    tmp_path: Path, capsys
) -> None:
    """``/ch lounge`` should be equivalent to ``/ch #lounge`` — a leading
    `#` is optional, the lookup falls back to the directory whenever the
    arg doesn't parse as an integer."""
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, history_backfill=1, channels=channels)
    store.set_subscription(5, True)
    store.upsert_post(5, {"ts": 100, "fc": "M0FOO", "p": "alpha"})

    asyncio.run(ui._handle_command("/ch LOUNGE"))  # bare name, case-insensitive
    assert ui._target == ("ch", "5")
    store.close()


def test_ch_command_rejects_unknown_name(tmp_path: Path, capsys) -> None:
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    asyncio.run(ui._handle_command("/ch #nope"))
    out = capsys.readouterr().out
    assert "unknown channel" in out
    assert ui._target is None
    store.close()


def test_ch_command_rejects_unknown_cid(tmp_path: Path, capsys) -> None:
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    asyncio.run(ui._handle_command("/ch 999"))
    out = capsys.readouterr().out
    assert "unknown channel" in out
    assert ui._target is None
    store.close()


def test_ch_command_accepts_directory_cid_without_subscription(
    tmp_path: Path, capsys
) -> None:
    """An unsubscribed directory entry is a valid /ch target — the UI
    previews local history and prompts to subscribe. Declining reverts
    the target so the prompt drops back where the user came from rather
    than stranding them in a read-only context."""
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    prompts: list[str] = []

    async def _no(prompt_text: str, *, default: bool = False) -> bool:
        prompts.append(prompt_text)
        return False

    ui._prompt_yes_no = _no  # type: ignore[assignment]
    asyncio.run(ui._handle_command("/ch 5"))
    assert ui._target is None
    assert len(prompts) == 1
    assert "Not subscribed" in prompts[0] and "ch 5 #lounge" in prompts[0]
    store.close()


def test_ch_unsubscribed_no_reverts_to_previous_target(
    tmp_path: Path, capsys
) -> None:
    """If the user already had a target before /ch'ing into an
    unsubscribed channel, declining the subscribe prompt restores that
    previous target rather than dropping them to no-target."""
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    store.set_subscription(7, True)  # so /ch 7 doesn't itself prompt

    asyncio.run(ui._handle_command("/ch 7"))
    capsys.readouterr()
    assert ui._target == ("ch", "7")

    async def _no(*a, **kw) -> bool:
        return False

    ui._prompt_yes_no = _no  # type: ignore[assignment]
    asyncio.run(ui._handle_command("/ch 5"))
    assert ui._target == ("ch", "7")
    store.close()


def test_ch_command_accepts_subscribed_cid_not_in_directory(tmp_path: Path) -> None:
    """No subscribe prompt when already subscribed — even if the channel
    isn't in the configured directory."""
    ui, store = _make_ui(tmp_path)
    store.set_subscription(42, True)

    async def _fail(*a, **kw) -> bool:
        raise AssertionError("subscribed channel must not prompt")

    ui._prompt_yes_no = _fail  # type: ignore[assignment]
    asyncio.run(ui._handle_command("/ch 42"))
    assert ui._target == ("ch", "42")
    store.close()


def test_ch_unsubscribed_yes_runs_subscribe_flow(tmp_path: Path, capsys) -> None:
    """Saying yes at the /ch prompt routes through the existing /sub flow,
    which fires `cs` and (for pc>0) prompts for the historic-post count."""
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    calls: list = []

    async def _yes(*a, **kw) -> bool:
        return True

    async def _sub_and_wait(cid: int, **kw) -> int:
        calls.append(("sub", cid))
        return 0  # nothing historic — skip count prompt entirely

    async def _req_post_batch(cid: int, n: int) -> None:
        calls.append(("cpb", cid, n))

    ui._prompt_yes_no = _yes  # type: ignore[assignment]
    ui._client.subscribe_and_wait = _sub_and_wait  # type: ignore[attr-defined]
    ui._client.request_post_batch = _req_post_batch  # type: ignore[attr-defined]

    asyncio.run(ui._handle_command("/ch 5"))
    assert calls == [("sub", 5)]
    store.close()


def test_ch_unsubscribed_history_prints_before_prompt(tmp_path: Path, capsys) -> None:
    """Local history must render before the subscribe prompt fires — the
    user reads top-down and the ask should sit just above the input."""
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, history_backfill=2, channels=channels)
    store.upsert_post(5, {"ts": 100, "fc": "M0FOO", "p": "old post one"})
    store.upsert_post(5, {"ts": 200, "fc": "G7BAR", "p": "old post two"})

    out_at_prompt: list[str] = []

    async def _no(*a, **kw) -> bool:
        # Snapshot what's already been printed at the moment the prompt
        # fires — history must already be on stdout by then.
        out_at_prompt.append(capsys.readouterr().out)
        return False

    ui._prompt_yes_no = _no  # type: ignore[assignment]
    asyncio.run(ui._handle_command("/ch 5"))
    assert len(out_at_prompt) == 1
    assert "old post one" in out_at_prompt[0]
    store.close()


def test_ch_paused_channel_skips_subscribe_prompt(tmp_path: Path, capsys) -> None:
    """Paused implies subscribed (server only flags channels you sub'd
    to). Switching to a paused channel must show the paused hint and
    NOT also offer to subscribe."""
    channels = [ChannelInfo(cid=7, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    store.set_subscription(7, True)
    ui._client._paused_channels[7] = 50  # type: ignore[attr-defined]

    async def _fail(*a, **kw) -> bool:
        raise AssertionError("paused channel must not show subscribe prompt")

    ui._prompt_yes_no = _fail  # type: ignore[assignment]
    asyncio.run(ui._handle_command("/ch 7"))
    out = capsys.readouterr().out
    assert "is paused" in out
    assert "not subscribed" not in out
    store.close()


def test_plain_text_to_unsubscribed_channel_is_blocked(tmp_path: Path, capsys) -> None:
    """A user can land on an unsubscribed channel (declined the prompt at
    /ch time, or the target was set programmatically), but the send path
    must refuse — posting works server-side but no replies ever arrive."""
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    ui._target = ("ch", "5")  # never subscribed
    posts: list = []

    async def _fake_post(cid: int, text: str) -> None:
        posts.append((cid, text))

    ui._client.post = _fake_post  # type: ignore[attr-defined]
    asyncio.run(ui._send_to_target("hello"))
    out = capsys.readouterr().out
    assert "not subscribed" in out
    assert posts == []

    # After subscribing, posting works.
    store.set_subscription(5, True)
    asyncio.run(ui._send_to_target("hello"))
    assert posts == [(5, "hello")]
    store.close()


def test_dm_command_uppercases_callsign(tmp_path: Path) -> None:
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/dm m0foo"))
    assert ui._target == ("dm", "M0FOO")
    store.close()


def _seed_list_fixtures(store: SqliteStore) -> None:
    store.set_subscription(7, True)
    store.upsert_post(7, {"ts": 100, "fc": "M0FOO", "p": "alpha"})
    store.upsert_message(
        {"_id": "1", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": 200}
    )
    store.upsert_message(
        {"_id": "2", "fc": "M0ABC", "tc": "G7BAR", "m": "hey", "ts": 300}
    )


def test_list_command_shows_channels_and_dms(tmp_path: Path, capsys) -> None:
    channels = [ChannelInfo(cid=7, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    _seed_list_fixtures(store)

    asyncio.run(ui._handle_command("/list"))
    out = capsys.readouterr().out

    assert "Channels: /ch <id> or /ch <name> to switch" in out
    assert " Subbed  ID   Name" in out
    assert "[*]   7    #lounge" in out
    assert "DM threads:  /dm <call> to switch" in out
    assert "M0FOO" in out
    assert "G7BAR" in out
    store.close()


def test_list_ch_only_shows_channels(tmp_path: Path, capsys) -> None:
    channels = [ChannelInfo(cid=7, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    _seed_list_fixtures(store)

    asyncio.run(ui._handle_command("/list ch"))
    out = capsys.readouterr().out

    assert "Channels:" in out
    assert "#lounge" in out
    assert "DM threads:" not in out
    assert "M0FOO" not in out
    store.close()


def test_list_dm_only_shows_dms(tmp_path: Path, capsys) -> None:
    channels = [ChannelInfo(cid=7, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    _seed_list_fixtures(store)

    asyncio.run(ui._handle_command("/list dm"))
    out = capsys.readouterr().out

    assert "DM threads:" in out
    assert "M0FOO" in out
    assert "Channels:" not in out
    assert "#lounge" not in out
    store.close()


def test_list_dm_with_no_threads_prints_placeholder(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/list dm"))
    out = capsys.readouterr().out
    assert "no DM threads yet" in out


def test_ch_command_warns_when_target_is_paused(tmp_path: Path, capsys) -> None:
    """Switching to a channel the server has paused must print a hint —
    otherwise the user sees an empty pane and assumes the channel is
    quiet, when in fact 700 posts are sitting in the server-side
    paused_channels backlog. The hint sits **after** the local-history
    backfill so it lands right above the prompt where it can't be missed.
    """
    channels = [ChannelInfo(cid=7, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    store.set_subscription(7, True)
    # Seed two old posts in the local store so we can assert they appear
    # *before* the paused hint in the output.
    store.upsert_post(7, {"ts": 100, "fc": "M0FOO", "p": "old post one"})
    store.upsert_post(7, {"ts": 200, "fc": "G7BAR", "p": "old post two"})
    ui._client._paused_channels[7] = 712  # type: ignore[attr-defined]

    asyncio.run(ui._handle_command("/ch 7"))
    out = capsys.readouterr().out

    assert "is paused" in out
    assert "712 posts" in out
    assert "/unpause 7" in out
    assert ui._target == ("ch", "7")
    # History first, paused hint last.
    history_pos = out.index("old post one")
    paused_pos = out.index("is paused")
    assert history_pos < paused_pos, (
        f"expected history before paused hint, got:\n{out}"
    )
    store.close()


def test_ch_command_no_hint_for_unpaused_channel(tmp_path: Path, capsys) -> None:
    channels = [ChannelInfo(cid=7, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    store.set_subscription(7, True)

    asyncio.run(ui._handle_command("/ch 7"))
    out = capsys.readouterr().out

    assert "is paused" not in out
    store.close()


def test_plain_text_to_announcements_channel_is_blocked(
    tmp_path: Path, capsys
) -> None:
    """The web client marks #announcements read-only and refuses posts
    client-side; the server doesn't enforce this so we mirror the
    block. Match by channel name (case-insensitive), not cid — any
    channel called ``announcements`` is read-only."""
    channels = [ChannelInfo(cid=100, name="announcements")]
    ui, store = _make_ui(tmp_path, channels=channels)
    store.set_subscription(100, True)
    ui._target = ("ch", "100")
    posts: list = []

    async def _fake_post(cid: int, text: str) -> None:
        posts.append((cid, text))

    ui._client.post = _fake_post  # type: ignore[attr-defined]
    asyncio.run(ui._send_to_target("hello"))
    out = capsys.readouterr().out
    assert "[Users cannot post to #announcements]" in out
    assert posts == []
    store.close()


def test_plain_text_to_paused_channel_is_blocked(tmp_path: Path, capsys) -> None:
    """Posting itself works server-side, but the user can't see what
    they'd be replying to — block at the UI to avoid talking past the
    backlog. The block is lifted automatically once /unpause clears the
    entry from client.paused_channels()."""
    channels = [ChannelInfo(cid=7, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    store.set_subscription(7, True)
    ui._target = ("ch", "7")
    ui._client._paused_channels[7] = 50  # type: ignore[attr-defined]
    posts: list = []

    async def _fake_post(cid: int, text: str) -> None:
        posts.append((cid, text))

    ui._client.post = _fake_post  # type: ignore[attr-defined]

    asyncio.run(ui._send_to_target("hello"))
    out = capsys.readouterr().out

    assert "is paused" in out
    assert "50 posts" in out
    assert posts == []  # nothing actually sent

    # Once the entry is cleared (e.g. after /unpause succeeds), posting works.
    ui._client._paused_channels.pop(7)  # type: ignore[attr-defined]
    asyncio.run(ui._send_to_target("hello"))
    assert posts == [(7, "hello")]
    store.close()


def test_sub_with_explicit_count_skips_prompt_and_fires_cpb(
    tmp_path: Path, capsys
) -> None:
    """``/sub 5 50`` should not prompt — it just subscribes and fetches 50."""
    ui, store = _make_ui(tmp_path)
    calls: list = []

    async def _sub_and_wait(cid: int, **kwargs) -> int:
        calls.append(("sub", cid))
        return 2500  # server has 2500 historic posts

    async def _req_post_batch(cid: int, n: int) -> None:
        calls.append(("cpb", cid, n))

    async def _prompt(*a, **kw) -> int:
        raise AssertionError("explicit count should not prompt")

    ui._client.subscribe_and_wait = _sub_and_wait  # type: ignore[attr-defined]
    ui._client.request_post_batch = _req_post_batch  # type: ignore[attr-defined]
    ui._prompt_for_count = _prompt  # type: ignore[assignment]

    asyncio.run(ui._handle_command("/sub 5 50"))

    assert calls == [("sub", 5), ("cpb", 5, 50)]
    store.close()


def test_sub_with_zero_count_subscribes_but_does_not_fetch(
    tmp_path: Path, capsys
) -> None:
    """``/sub 5 0`` subscribes (realtime-only) without pulling any history."""
    ui, store = _make_ui(tmp_path)
    calls: list = []

    async def _sub_and_wait(cid: int, **kwargs) -> int:
        calls.append(("sub", cid))
        return 2500

    async def _req_post_batch(cid: int, n: int) -> None:
        calls.append(("cpb", cid, n))

    ui._client.subscribe_and_wait = _sub_and_wait  # type: ignore[attr-defined]
    ui._client.request_post_batch = _req_post_batch  # type: ignore[attr-defined]

    asyncio.run(ui._handle_command("/sub 5 0"))

    assert calls == [("sub", 5)]  # no cpb
    store.close()


def test_sub_without_count_prompts_with_default_10(
    tmp_path: Path, capsys
) -> None:
    """``/sub 5`` (no count) → subscribe, await ack, prompt with default 10."""
    ui, store = _make_ui(tmp_path)
    calls: list = []
    prompts: list[tuple[str, int]] = []

    async def _sub_and_wait(cid: int, **kwargs) -> int:
        calls.append(("sub", cid))
        return 2500

    async def _req_post_batch(cid: int, n: int) -> None:
        calls.append(("cpb", cid, n))

    async def _prompt(prompt_text: str, *, default: int) -> int:
        prompts.append((prompt_text, default))
        return default  # user accepts default

    ui._client.subscribe_and_wait = _sub_and_wait  # type: ignore[attr-defined]
    ui._client.request_post_batch = _req_post_batch  # type: ignore[attr-defined]
    ui._prompt_for_count = _prompt  # type: ignore[assignment]

    asyncio.run(ui._handle_command("/sub 5"))

    assert calls == [("sub", 5), ("cpb", 5, 10)]
    assert len(prompts) == 1
    assert "Load how many historic posts?" in prompts[0][0]
    assert prompts[0][1] == 10  # default
    store.close()


def test_sub_default_uses_auto_backfill_when_set(
    tmp_path: Path, capsys
) -> None:
    """If the user has configured auto_backfill_post_count, the prompt's
    default reflects that instead of the bare 10."""
    ui, store = _make_ui(tmp_path)
    ui._client.auto_backfill_post_count = 50  # type: ignore[attr-defined]
    prompts: list[int] = []

    async def _sub_and_wait(cid: int, **kwargs) -> int:
        return 2500

    async def _req_post_batch(cid: int, n: int) -> None:
        pass

    async def _prompt(prompt_text: str, *, default: int) -> int:
        prompts.append(default)
        return default

    ui._client.subscribe_and_wait = _sub_and_wait  # type: ignore[attr-defined]
    ui._client.request_post_batch = _req_post_batch  # type: ignore[attr-defined]
    ui._prompt_for_count = _prompt  # type: ignore[assignment]

    asyncio.run(ui._handle_command("/sub 5"))
    assert prompts == [50]
    store.close()


def test_sub_default_capped_at_pc(tmp_path: Path, capsys) -> None:
    """If pc < the configured/default, the prompt default is the smaller —
    no point offering '50' when only 3 historic posts exist."""
    ui, store = _make_ui(tmp_path)
    prompts: list[int] = []

    async def _sub_and_wait(cid: int, **kwargs) -> int:
        return 3  # only 3 historic posts

    async def _req_post_batch(cid: int, n: int) -> None:
        pass

    async def _prompt(prompt_text: str, *, default: int) -> int:
        prompts.append(default)
        return default

    ui._client.subscribe_and_wait = _sub_and_wait  # type: ignore[attr-defined]
    ui._client.request_post_batch = _req_post_batch  # type: ignore[attr-defined]
    ui._prompt_for_count = _prompt  # type: ignore[assignment]

    asyncio.run(ui._handle_command("/sub 5"))
    assert prompts == [3]  # capped from 10 down to pc
    store.close()


def test_sub_skips_prompt_and_fetch_when_no_history(
    tmp_path: Path, capsys
) -> None:
    """Server reports pc=0 → nothing to prompt about, nothing to fetch."""
    ui, store = _make_ui(tmp_path)
    calls: list = []

    async def _sub_and_wait(cid: int, **kwargs) -> int:
        calls.append(("sub", cid))
        return 0

    async def _req_post_batch(cid: int, n: int) -> None:
        calls.append(("cpb", cid, n))

    async def _prompt(*a, **kw) -> int:
        raise AssertionError("should not prompt when pc=0")

    ui._client.subscribe_and_wait = _sub_and_wait  # type: ignore[attr-defined]
    ui._client.request_post_batch = _req_post_batch  # type: ignore[attr-defined]
    ui._prompt_for_count = _prompt  # type: ignore[assignment]

    asyncio.run(ui._handle_command("/sub 5"))
    assert calls == [("sub", 5)]
    store.close()


def test_sub_accepts_channel_name(tmp_path: Path, capsys) -> None:
    """``/sub lounge`` and ``/sub #lounge`` should resolve to the cid via
    the channel directory just like ``/ch`` does — names with or without
    the leading `#`, case-insensitive."""
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    calls: list = []

    async def _sub_and_wait(cid: int, **kwargs) -> int:
        calls.append(("sub", cid))
        return 0

    ui._client.subscribe_and_wait = _sub_and_wait  # type: ignore[attr-defined]

    asyncio.run(ui._handle_command("/sub LOUNGE"))
    asyncio.run(ui._handle_command("/sub #lounge"))

    assert calls == [("sub", 5), ("sub", 5)]
    store.close()


def test_sub_rejects_unknown_name(tmp_path: Path, capsys) -> None:
    """A bare-name argument that isn't in the channel directory is an
    error — there's no cid to send."""
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)

    async def _sub_and_wait(cid: int, **kwargs) -> int:
        raise AssertionError("should not send cs for unknown name")

    ui._client.subscribe_and_wait = _sub_and_wait  # type: ignore[attr-defined]

    asyncio.run(ui._handle_command("/sub nope"))
    out = capsys.readouterr().out
    assert "unknown channel" in out
    store.close()


def test_sub_accepts_unknown_cid(tmp_path: Path, capsys) -> None:
    """``/sub`` is the discovery path for cids that aren't in the
    directory yet — an integer arg is passed through verbatim, even if
    it isn't ``_known_cids()``."""
    ui, store = _make_ui(tmp_path)  # empty directory
    calls: list = []

    async def _sub_and_wait(cid: int, **kwargs) -> int:
        calls.append(cid)
        return 0

    ui._client.subscribe_and_wait = _sub_and_wait  # type: ignore[attr-defined]

    asyncio.run(ui._handle_command("/sub 999"))
    assert calls == [999]
    store.close()


def test_unsub_accepts_channel_name(tmp_path: Path, capsys) -> None:
    """``/unsub lounge`` resolves the name to the cid via the channel
    directory, same as ``/ch`` and ``/sub``."""
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    calls: list = []

    async def _unsubscribe(cid: int) -> None:
        calls.append(cid)

    ui._client.unsubscribe = _unsubscribe  # type: ignore[attr-defined]

    asyncio.run(ui._handle_command("/unsub lounge"))
    asyncio.run(ui._handle_command("/unsub #LOUNGE"))

    assert calls == [5, 5]
    store.close()


def test_unsub_rejects_unknown_name(tmp_path: Path, capsys) -> None:
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)

    async def _unsubscribe(cid: int) -> None:
        raise AssertionError("should not send for unknown name")

    ui._client.unsubscribe = _unsubscribe  # type: ignore[attr-defined]

    asyncio.run(ui._handle_command("/unsub nope"))
    out = capsys.readouterr().out
    assert "unknown channel" in out
    store.close()


def test_unpause_accepts_bare_name(tmp_path: Path, capsys) -> None:
    """``/unpause lounge`` (no leading `#`) resolves through the directory."""
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    ui._client._paused_channels[5] = 12  # type: ignore[attr-defined]
    calls: list = []

    async def _unpause(cid: int, *, post_count: int) -> None:
        calls.append((cid, post_count))

    ui._client.unpause_channel = _unpause  # type: ignore[attr-defined]

    asyncio.run(ui._handle_command("/unpause lounge"))
    assert calls == [(5, 12)]
    store.close()


def test_list_ch_annotates_paused_channels(tmp_path: Path, capsys) -> None:
    """A channel flagged as paused (via the client's pch state) shows up
    in /list with a "(N paused)" suffix so the user doesn't have to scroll
    back to the connect-time hint."""
    channels = [ChannelInfo(cid=7, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    store.set_subscription(7, True)
    # Fake the client's paused-channels state directly — easier than
    # plumbing a pch frame through an in-memory client for this UI test.
    ui._client._paused_channels[7] = 712  # type: ignore[attr-defined]

    asyncio.run(ui._handle_command("/list ch"))
    out = capsys.readouterr().out

    assert "#lounge" in out
    assert "(712 paused)" in out
    store.close()
    store.close()


def test_list_rejects_unknown_filter(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/list bogus"))
    out = capsys.readouterr().out
    assert "ch" in out and "dm" in out
    store.close()


def test_users_command_prints_cached_roster(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    ui._client.online_users = lambda: ["G7BAR", "M0FOO"]  # type: ignore[assignment]
    asyncio.run(ui._handle_command("/users"))
    out = capsys.readouterr().out
    assert "online (2)" in out
    assert "G7BAR" in out and "M0FOO" in out
    store.close()


def test_users_command_when_empty(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    ui._client.online_users = lambda: []  # type: ignore[assignment]
    asyncio.run(ui._handle_command("/users"))
    out = capsys.readouterr().out
    assert "no users online" in out
    store.close()


def test_users_command_prefixes_known_names(tmp_path: Path, capsys) -> None:
    """``/users`` shows ``Name, CALL`` for callsigns the local hams table
    knows, and falls back to bare ``CALL`` otherwise."""
    ui, store = _make_ui(tmp_path)
    store.upsert_ham("G7BAR", "Bob", 1_000)
    ui._client.online_users = lambda: ["G7BAR", "M0FOO"]  # type: ignore[assignment]
    asyncio.run(ui._handle_command("/users"))
    out = capsys.readouterr().out
    assert "Bob, G7BAR" in out
    # M0FOO has no ham row — bare callsign, no comma prefix.
    assert "M0FOO" in out
    assert "Bob, M0FOO" not in out
    store.close()


def test_render_event_uc_includes_name_when_known(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    store.upsert_ham("G7BAR", "Bob", 1_000)
    ui.render_event({"t": "uc", "c": "G7BAR"})
    ui.render_event({"t": "ud", "c": "M0FOO"})
    out = capsys.readouterr().out
    assert "Bob, G7BAR connected" in out
    assert "M0FOO disconnected" in out
    store.close()


def test_render_event_dm_includes_name_and_call(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    store.upsert_ham("M0FOO", "Alice", 1_000)
    ui.render_event(
        {"t": "m", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": 1}
    )
    out = capsys.readouterr().out
    # peer is the other side (M0FOO); ts=1ms renders to a 1970 timestamp,
    # but we only assert the bracket structure.
    assert "dm M0FOO>" in out
    assert "<Alice, M0FOO>: hi" in out
    assert "[1970-01-01" in out
    store.close()


def test_render_event_post_includes_name_and_call(tmp_path: Path, capsys) -> None:
    channels = [ChannelInfo(cid=7, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    store.upsert_ham("M0FOO", "Alice", 1_000)
    ui.render_event({"t": "cp", "cid": 7, "fc": "M0FOO", "p": "hello", "ts": 1})
    out = capsys.readouterr().out
    assert "7 #lounge>" in out
    assert "<Alice, M0FOO>: hello" in out
    assert "[1970-01-01" in out
    store.close()


def test_render_event_acks_visible_by_default(tmp_path: Path, capsys) -> None:
    """show_acks defaults to True — both `mr` (DM ack) and `cpr` (post ack)
    surface as `[ack]` lines tagged with the local lid + target label so
    the user can see which row was just acked."""
    channels = [ChannelInfo(cid=5, name="lounge")]
    ui, store = _make_ui(tmp_path, channels=channels)
    # Seed an outbound DM and post so the ack handlers can resolve back
    # to the local rows (lid + target) via the store lookups.
    store.upsert_message(
        {"_id": "1000-M0ABC", "fc": "M0ABC", "tc": "M6HKD",
         "m": "hi", "ts": 1000}
    )
    store.upsert_post(5, {"ts": 1000, "fc": "M0ABC", "p": "hello"})
    ui.render_event({"t": "mr", "_id": "1000-M0ABC"})
    ui.render_event({"t": "cpr", "ts": 1000, "dts": 2000})
    out = capsys.readouterr().out
    msg_lid = store.lookup_message_by_id("1000-M0ABC")["lid"]
    post_lid = store.lookup_post(5, 1000)["lid"]
    assert f"[ack] [dm:M6HKD] msg {msg_lid} at " in out
    assert "delivered in" in out
    assert f"[ack] [ch:5 #lounge] post {post_lid} at " in out
    store.close()


def test_render_event_acks_hidden_when_show_acks_off(tmp_path: Path, capsys) -> None:
    """show_acks=False suppresses both `mr` and `cpr` ack lines without
    affecting other event types — used to silence the noise on a fast
    link, or in environments where the ack itself is uninteresting."""
    ui, store = _make_ui(tmp_path, options=SessionOptions(show_acks=False))
    ui.render_event({"t": "mr", "_id": "1000-M0ABC"})
    ui.render_event({"t": "cpr", "ts": 1000, "dts": 2000})
    out = capsys.readouterr().out
    assert "[ack]" not in out
    store.close()


def test_set_no_args_lists_settings(tmp_path: Path, capsys) -> None:
    """`/set` (no args) prints every option's current value with its
    description so the user can discover what's tunable without running
    the binary with --help."""
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/set"))
    out = capsys.readouterr().out
    assert "Session settings" in out
    assert "show_acks = on" in out
    assert "Show [ack]" in out
    store.close()


def test_set_single_arg_shows_value(tmp_path: Path, capsys) -> None:
    """`/set NAME` with no value just prints the current setting."""
    ui, store = _make_ui(tmp_path, options=SessionOptions(show_acks=False))
    asyncio.run(ui._handle_command("/set show_acks"))
    out = capsys.readouterr().out
    assert "show_acks = off" in out
    store.close()


def test_set_show_acks_off_then_on_toggles_rendering(
    tmp_path: Path, capsys
) -> None:
    """End-to-end: `/set show_acks off` makes subsequent `mr` events
    silent; `/set show_acks on` restores them. The change applies to the
    same UI instance — i.e. the live session — without any reload."""
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/set show_acks off"))
    capsys.readouterr()
    ui.render_event({"t": "mr", "_id": "1000-M0ABC"})
    assert "[ack]" not in capsys.readouterr().out

    asyncio.run(ui._handle_command("/set show_acks on"))
    capsys.readouterr()
    ui.render_event({"t": "mr", "_id": "1000-M0ABC"})
    assert "[ack]" in capsys.readouterr().out
    store.close()


def test_set_change_reports_old_value(tmp_path: Path, capsys) -> None:
    """A successful change prints `name = new (was old)` so the user
    sees both states — handy when re-running `/set` after a typo to
    confirm what actually flipped."""
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/set show_acks off"))
    out = capsys.readouterr().out
    assert "show_acks = off" in out and "was on" in out
    store.close()


def test_set_unchanged_value_reports_no_change(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/set show_acks on"))
    out = capsys.readouterr().out
    assert "show_acks = on" in out and "unchanged" in out
    store.close()


def test_set_unknown_option_warns(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/set bogus 1"))
    out = capsys.readouterr().out
    assert "unknown option" in out
    assert "show_acks" in out  # known-options hint
    store.close()


def test_set_invalid_value_warns_and_keeps_old(tmp_path: Path, capsys) -> None:
    """A parse failure surfaces a hint and leaves the option untouched —
    so a typo doesn't silently change behaviour."""
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/set show_acks bogus"))
    out = capsys.readouterr().out
    assert "expected on/off" in out
    assert ui._options.show_acks is True
    store.close()


def test_set_accepts_synonyms_for_booleans(tmp_path: Path) -> None:
    """The boolean parser accepts the usual on/off synonyms so muscle
    memory from other tools (true/false, yes/no, 1/0) still works."""
    ui, _ = _make_ui(tmp_path)
    for raw, expected in [
        ("off", False), ("on", True),
        ("false", False), ("true", True),
        ("no", False), ("yes", True),
        ("0", False), ("1", True),
    ]:
        asyncio.run(ui._handle_command(f"/set show_acks {raw}"))
        assert ui._options.show_acks is expected, raw


def test_help_no_args_lists_every_command(tmp_path: Path, capsys) -> None:
    """`/h` with no arguments prints a one-line summary for every slash
    command — the entry point users discover everything else from."""
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/h"))
    out = capsys.readouterr().out
    assert "Slash commands" in out
    # Spot-check that a sampling of commands appear with their usage.
    for fragment in ("/h [command]", "/dm CALL", "/ch ID", "/sub ID", "/quit"):
        assert fragment in out, fragment
    store.close()


def test_help_with_command_shows_detail(tmp_path: Path, capsys) -> None:
    """`/h /ch` shows the detailed help block for /ch — usage line plus
    the multi-paragraph description."""
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/h /ch"))
    out = capsys.readouterr().out
    assert "/ch — Set the current target to a channel" in out
    assert "usage: /ch ID|NAME" in out
    assert "history_backfill" in out  # detail body present
    store.close()


def test_help_accepts_command_without_leading_slash(tmp_path: Path, capsys) -> None:
    """`/h ch` is equivalent to `/h /ch` — saves the user a keystroke and
    matches the convention most chat apps use."""
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/h ch"))
    out = capsys.readouterr().out
    assert "usage: /ch ID|NAME" in out
    store.close()


def test_help_unknown_command_warns(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/h bogus"))
    out = capsys.readouterr().out
    assert "unknown command" in out
    assert "bogus" in out
    store.close()


def _make_ui_with_react_capture(tmp_path: Path) -> tuple[LineUI, SqliteStore, list]:
    """Variant of _make_ui that captures react_message / react_post calls
    so /react dispatch can be asserted without a real WpsClient."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    calls: list = []

    async def _react_message(msg_id: str, emoji: str) -> None:
        calls.append(("msg", msg_id, emoji))

    async def _react_post(cid: int, ts: int, emoji: str) -> None:
        calls.append(("post", cid, ts, emoji))

    client = SimpleNamespace(
        _store=store,
        _paused_channels={},
        paused_channels=lambda: dict(client._paused_channels),
        auto_backfill_post_count=None,
        ham_name=lambda call: (store.lookup_ham(call) or {}).get("name") or None,
        react_message=_react_message,
        react_post=_react_post,
    )
    ui = LineUI(client, my_call="M0ABC", history_backfill=3)
    return ui, store, calls


def test_react_in_dm_target_resolves_lid_to_message_id(tmp_path: Path) -> None:
    """/react in a DM target looks up the lid in the messages table and
    sends a `mem` (via react_message) keyed on the server's `_id`."""
    ui, store, calls = _make_ui_with_react_capture(tmp_path)
    store.upsert_message(
        {"_id": "1700000000000-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
         "m": "hi", "ts": 1_700_000_000_000}
    )
    lid = store.recent_messages("M0FOO")[0]["lid"]
    ui._target = ("dm", "M0FOO")
    asyncio.run(ui._handle_command(f"/react {lid} 1f44d"))
    assert calls == [("msg", "1700000000000-M0FOO", "1f44d")]
    store.close()


def test_react_in_channel_target_resolves_lid_to_post_cid_ts(tmp_path: Path) -> None:
    """/react in a channel target looks up the lid in the posts table and
    sends a `cpem` (via react_post) keyed on (cid, ts)."""
    ui, store, calls = _make_ui_with_react_capture(tmp_path)
    store.set_subscription(7, True)
    store.upsert_post(7, {"ts": 1_777_821_179_422, "fc": "M0ABC", "p": "test"})
    lid = store.recent_posts(7)[0]["lid"]
    ui._target = ("ch", "7")
    asyncio.run(ui._handle_command(f"/react {lid} 1f44d"))
    assert calls == [("post", 7, 1_777_821_179_422, "1f44d")]
    store.close()


def test_react_without_target_warns(tmp_path: Path, capsys) -> None:
    ui, store, calls = _make_ui_with_react_capture(tmp_path)
    asyncio.run(ui._handle_command("/react 1 1f44d"))
    assert calls == []
    assert "no current target" in capsys.readouterr().out
    store.close()


def test_react_unknown_lid_warns(tmp_path: Path, capsys) -> None:
    ui, store, calls = _make_ui_with_react_capture(tmp_path)
    ui._target = ("dm", "M0FOO")
    asyncio.run(ui._handle_command("/react 999 1f44d"))
    assert calls == []
    assert "no local message with lid 999" in capsys.readouterr().out
    store.close()


def _make_ui_with_resend_capture(tmp_path: Path) -> tuple[LineUI, SqliteStore, list]:
    """Variant of _make_ui that captures resend_message / resend_post
    calls so /retrydm and /retrypost dispatch can be asserted without a
    real WpsClient."""
    store = SqliteStore(tmp_path / "state.sqlite3")
    calls: list = []

    async def _resend_message(msg_id: str) -> None:
        calls.append(("dm", msg_id))

    async def _resend_post(cid: int, ts: int) -> None:
        calls.append(("post", cid, ts))

    client = SimpleNamespace(
        _store=store,
        _paused_channels={},
        paused_channels=lambda: dict(client._paused_channels),
        auto_backfill_post_count=None,
        ham_name=lambda call: (store.lookup_ham(call) or {}).get("name") or None,
        resend_message=_resend_message,
        resend_post=_resend_post,
    )
    ui = LineUI(client, my_call="M0ABC", history_backfill=3)
    return ui, store, calls


def test_retrydm_dispatches_resend_with_server_id(tmp_path: Path) -> None:
    """/retrydm LID looks the local row up by lid and calls
    resend_message with the server-side `_id` so the wire frame matches
    the original send."""
    ui, store, calls = _make_ui_with_resend_capture(tmp_path)
    store.upsert_message(
        {"_id": "1700000000000-M0ABC", "fc": "M0ABC", "tc": "M0FOO",
         "m": "hello", "ts": 1_700_000_000_000, "ms": 0}
    )
    lid = store.recent_messages("M0FOO")[0]["lid"]
    asyncio.run(ui._handle_command(f"/retrydm {lid}"))
    assert calls == [("dm", "1700000000000-M0ABC")]
    store.close()


def test_retrydm_unknown_lid_warns(tmp_path: Path, capsys) -> None:
    ui, store, calls = _make_ui_with_resend_capture(tmp_path)
    asyncio.run(ui._handle_command("/retrydm 999"))
    assert calls == []
    assert "no local message with lid 999" in capsys.readouterr().out
    store.close()


def test_retrydm_non_int_warns(tmp_path: Path, capsys) -> None:
    ui, store, calls = _make_ui_with_resend_capture(tmp_path)
    asyncio.run(ui._handle_command("/retrydm banana"))
    assert calls == []
    assert "must be an integer" in capsys.readouterr().out
    store.close()


def test_retrydm_surfaces_client_value_error(tmp_path: Path, capsys) -> None:
    """If the client refuses (e.g. row isn't ours), the UI prints the
    error message rather than crashing."""
    ui, store, calls = _make_ui_with_resend_capture(tmp_path)
    store.upsert_message(
        {"_id": "1-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
         "m": "from peer", "ts": 1_000}
    )
    lid = store.recent_messages("M0FOO")[0]["lid"]

    async def _refuse(msg_id: str) -> None:
        raise ValueError("not us")

    ui._client.resend_message = _refuse  # type: ignore[attr-defined]
    asyncio.run(ui._handle_command(f"/retrydm {lid}"))
    out = capsys.readouterr().out
    assert "not us" in out
    store.close()


def test_retrypost_dispatches_resend_with_cid_ts(tmp_path: Path) -> None:
    """/retrypost LID looks the local row up by lid and calls
    resend_post with (cid, ts) — that's what the cp wire frame is
    keyed on."""
    ui, store, calls = _make_ui_with_resend_capture(tmp_path)
    store.set_subscription(7, True)
    store.upsert_post(7, {"ts": 1_777_821_179_422, "fc": "M0ABC", "p": "test"})
    lid = store.recent_posts(7)[0]["lid"]
    asyncio.run(ui._handle_command(f"/retrypost {lid}"))
    assert calls == [("post", 7, 1_777_821_179_422)]
    store.close()


def test_retrypost_unknown_lid_warns(tmp_path: Path, capsys) -> None:
    ui, store, calls = _make_ui_with_resend_capture(tmp_path)
    asyncio.run(ui._handle_command("/retrypost 999"))
    assert calls == []
    assert "no local post with lid 999" in capsys.readouterr().out
    store.close()


def test_help_self_documents(tmp_path: Path, capsys) -> None:
    """`/h h` (or `/h /h`) shows /h's own help — important so users who
    type /h on a whim discover the [command] argument form."""
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/h h"))
    out = capsys.readouterr().out
    assert "/h — Show help for slash commands." in out
    assert "usage: /h [command]" in out
    store.close()


# ---------------------------------------------------------------------------
# Verbose rendering (LineUI)
# ---------------------------------------------------------------------------


def test_verbose_history_inbound_realtime_shows_received_in(
    tmp_path: Path, capsys
) -> None:
    """A row stored as ``realtime=1`` with a ``received_ts`` renders the
    'Received real-time in Xs' suffix and the local id prefix."""
    ui, store = _make_ui(
        tmp_path, options=SessionOptions(verbose_history=True)
    )
    # DM `ts` is wire-seconds; `received_ts` is local-clock ms. The
    # verbose-status path normalises both to ms before subtracting.
    ts_s = 1_700_000_000
    store.upsert_message(
        {"_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
         "m": "test 1", "ts": ts_s},
        realtime=True,
        received_ts=ts_s * 1000 + 7_000,  # 7s after ts
    )
    ui._show_history(("dm", "M0FOO"), 1)
    out = capsys.readouterr().out
    assert "ID:" in out
    assert "Received real-time in 7s" in out
    assert "test 1" in out
    store.close()


def test_verbose_history_inbound_batch_omits_received_in(
    tmp_path: Path, capsys
) -> None:
    """A row stored as ``realtime=0`` (or NULL) gets no 'Received…' suffix
    — clean line, no 'received via backfill' noise."""
    ui, store = _make_ui(
        tmp_path, options=SessionOptions(verbose_history=True)
    )
    store.upsert_message(
        {"_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
         "m": "old msg", "ts": 1_000},
        realtime=False,
        received_ts=8_000,
    )
    ui._show_history(("dm", "M0FOO"), 1)
    out = capsys.readouterr().out
    assert "ID:" in out  # verbose prefix still present
    assert "Received real-time" not in out
    assert "old msg" in out
    store.close()


def test_verbose_history_outbound_delivered(tmp_path: Path, capsys) -> None:
    """Outbound row with ``delivered_ts`` set → 'Delivered to server in Xs'."""
    ui, store = _make_ui(
        tmp_path, options=SessionOptions(verbose_history=True)
    )
    # DM `ts` is wire-seconds; `delivered_ts` is local-clock ms.
    ts_s = 1_700_000_000
    store.upsert_message(
        {"_id": "1-M0ABC", "fc": "M0ABC", "tc": "M0FOO",
         "m": "test 1", "ts": ts_s, "ms": 1}
    )
    store.mark_message_delivered("1-M0ABC", ts_s * 1000 + 23_000)  # 23s after ts
    ui._show_history(("dm", "M0FOO"), 1)
    out = capsys.readouterr().out
    assert "Delivered to server in 23s" in out
    assert "Delivering" not in out
    assert "NOT DELIVERED" not in out
    store.close()


def test_verbose_history_outbound_undelivered_within_timeout(
    tmp_path: Path, capsys
) -> None:
    """Outbound row, no ack yet, age < timeout → 'Delivering...'."""
    import time as _time

    ui, store = _make_ui(
        tmp_path,
        options=SessionOptions(verbose_history=True, delivery_timeout_s=600),
    )
    now_ms = int(_time.time() * 1000)
    store.upsert_message(
        {"_id": f"{now_ms}-M0ABC", "fc": "M0ABC", "tc": "M0FOO",
         "m": "fresh send", "ts": now_ms, "ms": 0}
    )
    ui._show_history(("dm", "M0FOO"), 1)
    out = capsys.readouterr().out
    assert "Delivering..." in out
    assert "NOT DELIVERED" not in out
    store.close()


def test_verbose_history_outbound_undelivered_past_timeout(
    tmp_path: Path, capsys
) -> None:
    """Outbound row, no ack, age >= timeout → 'NOT DELIVERED'."""
    ui, store = _make_ui(
        tmp_path,
        options=SessionOptions(verbose_history=True, delivery_timeout_s=60),
    )
    # ts well in the past — far older than the 60s timeout.
    store.upsert_message(
        {"_id": "1-M0ABC", "fc": "M0ABC", "tc": "M0FOO",
         "m": "stale", "ts": 1_000, "ms": 0}
    )
    ui._show_history(("dm", "M0FOO"), 1)
    out = capsys.readouterr().out
    assert "NOT DELIVERED" in out
    assert "Delivering" not in out
    store.close()


def test_verbose_history_post_format_shows_channel_prefix(
    tmp_path: Path, capsys
) -> None:
    """Channel-target verbose render uses the ``CID #name>`` prefix."""
    channels = [ChannelInfo(cid=100, name="announcements")]
    ui, store = _make_ui(
        tmp_path,
        channels=channels,
        options=SessionOptions(verbose_history=True),
    )
    store.set_subscription(100, True)
    # Channel-post `ts` is wire-ms; `received_ts` is local-clock ms.
    ts_ms = 1_700_000_000_000
    store.upsert_post(
        100,
        {"ts": ts_ms, "fc": "M6HKD", "p": "test 1"},
        realtime=True,
        received_ts=ts_ms + 7_000,
    )
    store.upsert_ham("M6HKD", "Bob", 1_000)
    ui._show_history(("ch", "100"), 1)
    out = capsys.readouterr().out
    assert "100 #announcements>" in out
    assert "Received real-time in 7s" in out
    assert "<Bob, M6HKD>" in out
    assert "test 1" in out
    store.close()


def test_history_command_compact_by_default(tmp_path: Path, capsys) -> None:
    """The `/history` command (and target-switch backfill) follows the
    session option — compact by default."""
    ui, store = _make_ui(tmp_path)  # verbose_history=False default
    store.upsert_message(
        {"_id": "1-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": 1_000},
        realtime=True,
        received_ts=8_000,
    )
    asyncio.run(ui._handle_command("/dm M0FOO"))
    asyncio.run(ui._handle_command("/history 1"))
    out = capsys.readouterr().out
    assert "ID:" not in out  # no verbose prefix
    assert "Received real-time" not in out
    assert "hi" in out
    store.close()


def test_history_command_verbose_when_session_option_on(
    tmp_path: Path, capsys
) -> None:
    """With `verbose_history=True` the session option flips `/history`
    output to verbose without needing /vhistory."""
    ui, store = _make_ui(
        tmp_path, options=SessionOptions(verbose_history=True)
    )
    ts_s = 1_700_000_000
    store.upsert_message(
        {"_id": "1-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": ts_s},
        realtime=True,
        received_ts=ts_s * 1000 + 7_000,
    )
    asyncio.run(ui._handle_command("/dm M0FOO"))
    capsys.readouterr()  # discard the auto-backfill output
    asyncio.run(ui._handle_command("/history 1"))
    out = capsys.readouterr().out
    assert "ID:" in out
    assert "Received real-time in 7s" in out
    store.close()


def test_vhistory_forces_verbose_regardless_of_session(
    tmp_path: Path, capsys
) -> None:
    """`/vhistory` is the one-shot verbose replay — always verbose, even
    when the session option is off, and it does NOT change the option."""
    ui, store = _make_ui(tmp_path)  # compact session
    ts_s = 1_700_000_000
    store.upsert_message(
        {"_id": "1-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": ts_s},
        realtime=True,
        received_ts=ts_s * 1000 + 7_000,
    )
    asyncio.run(ui._handle_command("/dm M0FOO"))
    capsys.readouterr()
    asyncio.run(ui._handle_command("/vhistory 1"))
    out = capsys.readouterr().out
    assert "ID:" in out
    assert "Received real-time in 7s" in out
    # Session option not flipped by /vhistory.
    assert ui._options.verbose_history is False
    store.close()


def test_vhistory_without_target_warns(tmp_path: Path, capsys) -> None:
    ui, _ = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/vhistory 5"))
    out = capsys.readouterr().out
    assert "no current target" in out


def test_vhistory_rejects_non_int(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/dm M0FOO"))
    capsys.readouterr()
    asyncio.run(ui._handle_command("/vhistory banana"))
    out = capsys.readouterr().out
    assert "integer" in out
    store.close()


def test_set_verbose_history_toggle_flips_render_shape(
    tmp_path: Path, capsys
) -> None:
    """`/set verbose_history on/off` toggles render shape live, without
    reload."""
    ui, store = _make_ui(tmp_path)
    store.upsert_message(
        {"_id": "1-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": 1_000},
        realtime=True,
        received_ts=8_000,
    )
    asyncio.run(ui._handle_command("/dm M0FOO"))
    capsys.readouterr()

    # Compact by default.
    asyncio.run(ui._handle_command("/history 1"))
    assert "ID:" not in capsys.readouterr().out

    # Flip the option and try again.
    asyncio.run(ui._handle_command("/set verbose_history on"))
    capsys.readouterr()
    asyncio.run(ui._handle_command("/history 1"))
    assert "ID:" in capsys.readouterr().out
    store.close()


def test_render_event_verbose_realtime_dm_uses_persisted_row(
    tmp_path: Path, capsys
) -> None:
    """Live `m` arrival in verbose mode looks up the freshly-persisted
    row by `_id` to recover the lid + receipt-time columns. Mirrors the
    real WpsClient default handler running before the on_event hook."""
    ui, store = _make_ui(
        tmp_path, options=SessionOptions(verbose_history=True)
    )
    # Simulate the default handler having already persisted the row.
    ts_s = 1_700_000_000
    store.upsert_message(
        {"_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
         "m": "live", "ts": ts_s},
        realtime=True,
        received_ts=ts_s * 1000 + 7_000,
    )
    ui.render_event(
        {"t": "m", "_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
         "m": "live", "ts": ts_s}
    )
    out = capsys.readouterr().out
    assert "ID:" in out
    assert "Received real-time in 7s" in out
    assert "live" in out
    store.close()


def test_render_event_compact_does_not_use_lid(tmp_path: Path, capsys) -> None:
    """In compact mode (default) live `m` arrival skips the verbose
    lookup entirely and renders the historic single-line form."""
    ui, store = _make_ui(tmp_path)  # verbose_history=False
    ui.render_event(
        {"t": "m", "_id": "100-M0FOO", "fc": "M0FOO", "tc": "M0ABC",
         "m": "live", "ts": 1_000}
    )
    out = capsys.readouterr().out
    assert "ID:" not in out
    assert "live" in out
    store.close()


def test_set_delivery_timeout_s_validation(tmp_path: Path, capsys) -> None:
    """`delivery_timeout_s` accepts positive ints; rejects non-positive
    and non-integer."""
    ui, store = _make_ui(tmp_path)
    asyncio.run(ui._handle_command("/set delivery_timeout_s 120"))
    assert ui._options.delivery_timeout_s == 120

    asyncio.run(ui._handle_command("/set delivery_timeout_s 0"))
    out = capsys.readouterr().out
    assert "positive integer" in out
    assert ui._options.delivery_timeout_s == 120  # unchanged

    asyncio.run(ui._handle_command("/set delivery_timeout_s banana"))
    out = capsys.readouterr().out
    assert "positive integer" in out
    assert ui._options.delivery_timeout_s == 120
    store.close()


def test_delivery_timeout_render_dm(tmp_path: Path, capsys) -> None:
    """``_delivery_timeout`` renders the [timeout] line with the dm peer,
    lid, ts, and the /retrydm hint, regardless of show_acks."""
    ui, store = _make_ui(tmp_path, options=SessionOptions(show_acks=False))
    ui.render_event(
        {
            "t": "_delivery_timeout",
            "kind": "dm",
            "msg_id": "1751553902000-M0ABC",
            "peer": "M6HKD",
            "lid": 12,
            "ts": 1_751_553_902_000,
        }
    )
    out = capsys.readouterr().out
    assert "[timeout]" in out
    assert "[dm:M6HKD]" in out
    assert "msg 12" in out
    assert "/retrydm 12" in out
    store.close()


def test_delivery_timeout_render_post_with_channel_name(tmp_path: Path, capsys) -> None:
    """A post timeout renders ``[ch:5 #lounge]`` when the directory has
    a name for the cid, and ``[ch:5]`` when it doesn't."""
    ui, store = _make_ui(
        tmp_path,
        channels=[ChannelInfo(cid=5, name="lounge")],
    )
    ui.render_event(
        {
            "t": "_delivery_timeout",
            "kind": "post",
            "cid": 5,
            "lid": 6,
            "ts": 1_751_553_902_000,
        }
    )
    out = capsys.readouterr().out
    assert "[timeout]" in out
    assert "[ch:5 #lounge]" in out
    assert "post 6" in out
    assert "/retrypost 6" in out
    store.close()


def test_delivery_timeout_render_post_unknown_channel(tmp_path: Path, capsys) -> None:
    ui, store = _make_ui(tmp_path)
    ui.render_event(
        {
            "t": "_delivery_timeout",
            "kind": "post",
            "cid": 42,
            "lid": 3,
            "ts": 1_751_553_902_000,
        }
    )
    out = capsys.readouterr().out
    assert "[ch:42]" in out
    assert "#" not in out.split("[ch:42]")[1].split(".")[0]
    store.close()


def test_delivery_timeout_render_ignores_show_acks(tmp_path: Path, capsys) -> None:
    """The user explicitly asked: if acks are off, the timeout still
    fires. Two renders with show_acks toggled should both print."""
    ui_off, store_off = _make_ui(
        tmp_path / "off", options=SessionOptions(show_acks=False)
    )
    ui_on, store_on = _make_ui(
        tmp_path / "on", options=SessionOptions(show_acks=True)
    )
    obj = {
        "t": "_delivery_timeout",
        "kind": "dm",
        "msg_id": "1-M0ABC",
        "peer": "M6HKD",
        "lid": 1,
        "ts": 1_000,
    }
    ui_off.render_event(obj)
    out_off = capsys.readouterr().out
    ui_on.render_event(obj)
    out_on = capsys.readouterr().out
    assert "[timeout]" in out_off
    assert "[timeout]" in out_on
    store_off.close()
    store_on.close()
