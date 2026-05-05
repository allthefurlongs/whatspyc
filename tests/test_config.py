"""Config schema tests: connect_profiles array-of-tables, defaults, migration."""

from __future__ import annotations

import pytest
import tomllib

from whatspyc import config as cfg_mod
from whatspyc.config import ConnectProfile


_SAMPLE = """
my_call = "M0ABC"
name = "Tester"
ui = "tui"
default_profile = "via-mb7npw"

[[connect_profiles]]
name = "fake-server"
transport = "direct-tcp"
host = "127.0.0.1"
port = 63001

[[connect_profiles]]
name = "via-mb7npw"
transport = "rhp-ws"
host = "router.local"
engine = "xrouter"
radio_port = 1
ax_level = "L2"
remote = "MB7NPW"
digipeaters = ["RELAY1"]
ax25_modulo = 128
ax25_segmentation = true
connect_sequence = [
  { cmd = "C MB7NPW", val = "Connected to MB7NPW", timeout = 30 },
  { cmd = "C WPS",    val = "Connected to WPS" },
]
"""


def test_parse_full_sample() -> None:
    c = cfg_mod.parse(tomllib.loads(_SAMPLE))
    assert c.my_call == "M0ABC"
    assert c.ui == "tui"
    assert c.default_profile == "via-mb7npw"
    assert [p.name for p in c.connect_profiles] == ["fake-server", "via-mb7npw"]

    fake = c.resolve_profile("fake-server")
    assert fake.transport == "direct-tcp"
    assert fake.port == 63001
    assert fake.connect_script == []

    via = c.resolve_profile("via-mb7npw")
    assert via.engine == "xrouter"
    assert via.ax25_modulo == 128
    assert via.ax25_segmentation is True
    assert via.digipeaters == ["RELAY1"]
    assert len(via.connect_script) == 2
    assert via.connect_script[0].cmd == "C MB7NPW"
    assert via.connect_script[0].val == "Connected to MB7NPW"
    assert via.connect_script[0].timeout == 30.0
    assert via.connect_script[1].timeout is None


def test_resolve_unknown_profile_raises() -> None:
    c = cfg_mod.parse(tomllib.loads(_SAMPLE))
    with pytest.raises(KeyError, match="not found"):
        c.resolve_profile("nope")


def test_unknown_default_profile_rejected_at_parse() -> None:
    raw = tomllib.loads(
        """
        default_profile = "ghost"
        [[connect_profiles]]
        name = "fake"
        transport = "direct-tcp"
        """
    )
    with pytest.raises(ValueError, match="default_profile 'ghost'"):
        cfg_mod.parse(raw)


def test_legacy_top_level_connection_field_rejected() -> None:
    raw = tomllib.loads('transport = "rhp-ws"\nhost = "x"\n')
    with pytest.raises(ValueError, match="must live inside a \\[\\[connect_profiles\\]\\]"):
        cfg_mod.parse(raw)


def test_profile_without_name_rejected() -> None:
    raw = tomllib.loads(
        """
        [[connect_profiles]]
        transport = "direct-tcp"
        """
    )
    with pytest.raises(ValueError, match="must have a `name`"):
        cfg_mod.parse(raw)


def test_step_missing_required_keys_rejected() -> None:
    raw = tomllib.loads(
        """
        [[connect_profiles]]
        name = "x"
        transport = "rhp-ws"
        connect_sequence = [{ cmd = "C WPS" }]
        """
    )
    with pytest.raises(ValueError, match="missing required key"):
        cfg_mod.parse(raw)


def test_empty_config_yields_defaults() -> None:
    c = cfg_mod.parse({})
    assert isinstance(c, cfg_mod.Config)
    assert c.connect_profiles == []
    assert c.default_profile is None
    assert c.my_call is None
    assert c.name is None


def test_default_port_unchanged() -> None:
    """default_port is unchanged plumbing — sanity check it still resolves."""
    assert cfg_mod.default_port("xrouter", "rhp-ws") == 8086
    assert cfg_mod.default_port("bpq", "rhp-ws") == 8008
    assert cfg_mod.default_port(None, "rhp-tcp") == 9000
    assert cfg_mod.default_port(None, "rhp-ws") is None


def test_connect_profile_dataclass_defaults() -> None:
    p = ConnectProfile(name="x")
    assert p.transport == "rhp-ws"
    assert p.connect_script == []
    assert p.digipeaters == []


def test_engine_required_for_rhp_transports() -> None:
    raw = tomllib.loads(
        """
        [[connect_profiles]]
        name = "x"
        transport = "rhp-ws"
        host = "h"
        """
    )
    with pytest.raises(ValueError, match="`engine` is required"):
        cfg_mod.parse(raw)

    raw_tcp = tomllib.loads(
        """
        [[connect_profiles]]
        name = "x"
        transport = "rhp-tcp"
        host = "h"
        """
    )
    with pytest.raises(ValueError, match="`engine` is required"):
        cfg_mod.parse(raw_tcp)


def test_engine_invalid_value_rejected() -> None:
    raw = tomllib.loads(
        """
        [[connect_profiles]]
        name = "x"
        transport = "rhp-ws"
        engine = "bogus"
        """
    )
    with pytest.raises(ValueError, match="invalid engine 'bogus'"):
        cfg_mod.parse(raw)


def test_engine_xrouter_defaults_applied() -> None:
    raw = tomllib.loads(
        """
        [[connect_profiles]]
        name = "x"
        transport = "rhp-ws"
        host = "h"
        engine = "xrouter"
        """
    )
    p = cfg_mod.parse(raw).resolve_profile("x")
    assert p.radio_port == 1
    assert p.port == 8086
    assert p.remote == "WPS"  # dataclass default, untouched


def test_engine_bpq_defaults_applied_and_banner_preamble_prepended() -> None:
    raw = tomllib.loads(
        """
        [[connect_profiles]]
        name = "bpq"
        transport = "rhp-ws"
        host = "h"
        engine = "bpq"
        connect_sequence = [
          { cmd = "C MB7NPW-9", val = "Connected to MB7NPW-9" },
        ]
        """
    )
    p = cfg_mod.parse(raw).resolve_profile("bpq")
    assert p.remote == "SWITCH"
    assert p.radio_port == 1
    assert p.port == 8008
    assert len(p.connect_script) == 2
    # BPQ pushes its banner unprompted, so the preamble is wait-only
    # (empty cmd) and just consumes "Connected to RHP Server".
    assert p.connect_script[0].cmd == ""
    assert p.connect_script[0].val == "Connected to RHP Server"
    assert p.connect_script[1].cmd == "C MB7NPW-9"


def test_engine_bpq_normalizes_preamble_for_all_user_forms() -> None:
    """The user's first hop is normalized to a wait-only step that consumes
    the BPQ banner, regardless of which of the three idiomatic forms they
    wrote — explicit wait-only, the historic `SWITCH` form, or no hops at
    all. RHP-open with remote=SWITCH already lands at the switch prompt,
    so sending `SWITCH` again only earns an `Invalid command` reply."""
    # Form 1: explicit wait-only — user's val is preserved verbatim,
    # nothing prepended.
    raw_wait = tomllib.loads(
        """
        [[connect_profiles]]
        name = "bpq-wait"
        transport = "rhp-ws"
        host = "h"
        engine = "bpq"
        connect_sequence = [
          { cmd = "", val = "Connected to RHP Server v6.0" },
          { cmd = "C MB7NPW-9", val = "Connected to MB7NPW-9" },
        ]
        """
    )
    p = cfg_mod.parse(raw_wait).resolve_profile("bpq-wait")
    assert len(p.connect_script) == 2
    assert p.connect_script[0].cmd == ""
    assert p.connect_script[0].val == "Connected to RHP Server v6.0"

    # Form 2: historic `SWITCH` form — the redundant cmd is stripped,
    # but the user's val survives (they may have customized the banner
    # text). Effectively becomes form 1.
    raw_switch = tomllib.loads(
        """
        [[connect_profiles]]
        name = "bpq-switch"
        transport = "rhp-ws"
        host = "h"
        engine = "bpq"
        connect_sequence = [
          { cmd = "SWITCH", val = "Connected to RHP Server" },
          { cmd = "C MB7NPW-9", val = "Connected to MB7NPW-9" },
        ]
        """
    )
    p2 = cfg_mod.parse(raw_switch).resolve_profile("bpq-switch")
    assert len(p2.connect_script) == 2
    assert p2.connect_script[0].cmd == ""
    assert p2.connect_script[0].val == "Connected to RHP Server"
    assert p2.connect_script[1].cmd == "C MB7NPW-9"

    # Form 2 with leading whitespace / different case still recognised.
    raw_switch_loose = tomllib.loads(
        """
        [[connect_profiles]]
        name = "bpq-switch-loose"
        transport = "rhp-ws"
        host = "h"
        engine = "bpq"
        connect_sequence = [
          { cmd = " switch ", val = "Connected to RHP Server" },
        ]
        """
    )
    p3 = cfg_mod.parse(raw_switch_loose).resolve_profile("bpq-switch-loose")
    assert p3.connect_script[0].cmd == ""


def test_engine_custom_applies_no_defaults() -> None:
    raw = tomllib.loads(
        """
        [[connect_profiles]]
        name = "x"
        transport = "rhp-ws"
        host = "h"
        engine = "custom"
        port = 9999
        """
    )
    p = cfg_mod.parse(raw).resolve_profile("x")
    assert p.engine == "custom"
    assert p.port == 9999
    assert p.radio_port is None  # not defaulted
    assert p.remote == "WPS"  # dataclass default, not engine-defaulted
    assert p.connect_script == []  # no SWITCH auto-prepend


def test_explicit_user_values_override_engine_defaults() -> None:
    """The whole point: setting `port = 8080` with `engine = "bpq"` should
    keep the custom port AND get all the other BPQ smarts."""
    raw = tomllib.loads(
        """
        [[connect_profiles]]
        name = "bpq"
        transport = "rhp-ws"
        host = "raspberrypi"
        engine = "bpq"
        port = 8080
        remote = "MYSWITCH"
        """
    )
    p = cfg_mod.parse(raw).resolve_profile("bpq")
    assert p.port == 8080  # user value beats default 8008
    assert p.remote == "MYSWITCH"  # user value beats default "SWITCH"
    assert p.radio_port == 1  # BPQ default
    # Auto-injected wait-only banner preamble (user didn't supply hops)
    assert len(p.connect_script) == 1
    assert p.connect_script[0].cmd == ""
    assert p.connect_script[0].val == "Connected to RHP Server"


def test_engine_custom_rhp_ws_without_port_errors_at_build_time() -> None:
    """Custom mode means no defaults — rhp-ws with no port has nowhere to
    connect. The error is raised when the stream is built, not at parse."""
    raw = tomllib.loads(
        """
        [[connect_profiles]]
        name = "x"
        transport = "rhp-ws"
        host = "h"
        engine = "custom"
        """
    )
    p = cfg_mod.parse(raw).resolve_profile("x")
    assert p.port is None


def test_channels_in_main_config_rejected_with_migration_message() -> None:
    raw = tomllib.loads(
        """
        [[channels]]
        cid = 0
        name = "general"
        """
    )
    with pytest.raises(ValueError, match="now lives in its own file"):
        cfg_mod.parse(raw)


def test_parse_channels_happy_path() -> None:
    raw = tomllib.loads(
        """
        [[channels]]
        cid = 0
        name = "general"
        description = "Anything Ham Radio"

        [[channels]]
        cid = 2
        name = "tech"

        [[channels]]
        cid = 7
        """
    )
    chs = cfg_mod.parse_channels(raw)
    assert [(ch.cid, ch.name, ch.description) for ch in chs] == [
        (0, "general", "Anything Ham Radio"),
        (2, "tech", ""),
        (7, "", ""),
    ]


def test_parse_channels_missing_cid_rejected() -> None:
    raw = tomllib.loads(
        """
        [[channels]]
        name = "general"
        """
    )
    with pytest.raises(ValueError, match="must have a `cid`"):
        cfg_mod.parse_channels(raw)


def test_parse_channels_non_int_cid_rejected() -> None:
    raw = tomllib.loads(
        """
        [[channels]]
        cid = "0"
        name = "general"
        """
    )
    with pytest.raises(ValueError, match="cid must be an integer"):
        cfg_mod.parse_channels(raw)


def test_parse_channels_duplicate_cid_rejected() -> None:
    raw = tomllib.loads(
        """
        [[channels]]
        cid = 0
        name = "general"

        [[channels]]
        cid = 0
        name = "again"
        """
    )
    with pytest.raises(ValueError, match="listed more than once"):
        cfg_mod.parse_channels(raw)


def test_parse_channels_empty() -> None:
    assert cfg_mod.parse_channels({}) == []


def test_bundled_default_channels_file_parses() -> None:
    """The shipped channels.toml must parse to a non-empty directory.

    Catches typos in the package data file before they reach a user.
    """
    raw = tomllib.loads(cfg_mod._bundled_channels_toml())
    chs = cfg_mod.parse_channels(raw)
    assert len(chs) >= 13
    cids = [c.cid for c in chs]
    # Spot-check the canonical web-client cids.
    for required in (0, 1, 2, 3, 100):
        assert required in cids


def test_ensure_channels_file_seeds_then_idempotent(tmp_path) -> None:
    target = tmp_path / "subdir" / "channels.toml"
    cfg_mod.ensure_channels_file(target)
    assert target.exists()
    contents = target.read_text(encoding="utf-8")
    # Mutate so we can prove we don't overwrite on a second call.
    target.write_text("# user-edited\n", encoding="utf-8")
    cfg_mod.ensure_channels_file(target)
    assert target.read_text(encoding="utf-8") == "# user-edited\n"
    # Sanity: original seed contained the expected default content.
    assert "packet-network" in contents


def test_history_backfill_default_and_override() -> None:
    """Default is 3; integer overrides accepted; floats / negatives / bools rejected."""
    c = cfg_mod.parse({})
    assert c.history_backfill == 3

    c2 = cfg_mod.parse(tomllib.loads("history_backfill = 25\n"))
    assert c2.history_backfill == 25

    c3 = cfg_mod.parse(tomllib.loads("history_backfill = 0\n"))
    assert c3.history_backfill == 0

    with pytest.raises(ValueError, match="non-negative integer"):
        cfg_mod.parse(tomllib.loads("history_backfill = -1\n"))
    with pytest.raises(ValueError, match="non-negative integer"):
        cfg_mod.parse({"history_backfill": True})
    with pytest.raises(ValueError, match="non-negative integer"):
        cfg_mod.parse({"history_backfill": "ten"})


def test_auto_backfill_post_count_default_and_override() -> None:
    """Default is None (manual /unpause /fetch); 0 normalises to None;
    positive ints pass through; floats / negatives / bools rejected."""
    c = cfg_mod.parse({})
    assert c.auto_backfill_post_count is None

    c2 = cfg_mod.parse(tomllib.loads("auto_backfill_post_count = 50\n"))
    assert c2.auto_backfill_post_count == 50

    c3 = cfg_mod.parse(tomllib.loads("auto_backfill_post_count = 0\n"))
    assert c3.auto_backfill_post_count is None

    with pytest.raises(ValueError, match="non-negative integer"):
        cfg_mod.parse(tomllib.loads("auto_backfill_post_count = -1\n"))
    with pytest.raises(ValueError, match="non-negative integer"):
        cfg_mod.parse({"auto_backfill_post_count": True})
    with pytest.raises(ValueError, match="non-negative integer"):
        cfg_mod.parse({"auto_backfill_post_count": "fifty"})


def test_auto_reconnect_default_off_and_override() -> None:
    """Default is False (sessions don't auto-rebuild on link loss);
    booleans pass through; non-booleans rejected."""
    c = cfg_mod.parse({})
    assert c.auto_reconnect is False

    c2 = cfg_mod.parse(tomllib.loads("auto_reconnect = true\n"))
    assert c2.auto_reconnect is True

    with pytest.raises(ValueError, match="auto_reconnect"):
        cfg_mod.parse({"auto_reconnect": "yes"})
    with pytest.raises(ValueError, match="auto_reconnect"):
        cfg_mod.parse({"auto_reconnect": 1})


def test_reconnect_max_retries_default_and_override() -> None:
    """Default is 0 (unlimited); positive ints pass through; floats /
    negatives / bools rejected."""
    c = cfg_mod.parse({})
    assert c.reconnect_max_retries == 0

    c2 = cfg_mod.parse(tomllib.loads("reconnect_max_retries = 5\n"))
    assert c2.reconnect_max_retries == 5

    with pytest.raises(ValueError, match="non-negative integer"):
        cfg_mod.parse(tomllib.loads("reconnect_max_retries = -1\n"))
    with pytest.raises(ValueError, match="non-negative integer"):
        cfg_mod.parse({"reconnect_max_retries": True})
    with pytest.raises(ValueError, match="non-negative integer"):
        cfg_mod.parse({"reconnect_max_retries": "five"})


def test_show_acks_default_and_override() -> None:
    """Default is True (acks displayed); booleans pass through; non-bools rejected."""
    c = cfg_mod.parse({})
    assert c.show_acks is True

    c2 = cfg_mod.parse(tomllib.loads("show_acks = false\n"))
    assert c2.show_acks is False

    c3 = cfg_mod.parse(tomllib.loads("show_acks = true\n"))
    assert c3.show_acks is True

    with pytest.raises(ValueError, match="show_acks"):
        cfg_mod.parse({"show_acks": "yes"})
    with pytest.raises(ValueError, match="show_acks"):
        cfg_mod.parse({"show_acks": 1})


def test_show_edits_default_and_override() -> None:
    """Default is True (live edits rendered); booleans pass through; non-bools rejected."""
    c = cfg_mod.parse({})
    assert c.show_edits is True

    c2 = cfg_mod.parse(tomllib.loads("show_edits = false\n"))
    assert c2.show_edits is False

    c3 = cfg_mod.parse(tomllib.loads("show_edits = true\n"))
    assert c3.show_edits is True

    with pytest.raises(ValueError, match="show_edits"):
        cfg_mod.parse({"show_edits": "yes"})
    with pytest.raises(ValueError, match="show_edits"):
        cfg_mod.parse({"show_edits": 1})


def test_verbose_history_default_and_override() -> None:
    """Default is False (compact rendering); booleans pass through; non-bools rejected."""
    c = cfg_mod.parse({})
    assert c.verbose_history is False

    c2 = cfg_mod.parse(tomllib.loads("verbose_history = true\n"))
    assert c2.verbose_history is True

    c3 = cfg_mod.parse(tomllib.loads("verbose_history = false\n"))
    assert c3.verbose_history is False

    with pytest.raises(ValueError, match="verbose_history"):
        cfg_mod.parse({"verbose_history": "yes"})
    with pytest.raises(ValueError, match="verbose_history"):
        cfg_mod.parse({"verbose_history": 1})


def test_delivery_timeout_s_default_and_override() -> None:
    """Default is 60s; positive ints pass through; non-positive / non-int rejected."""
    c = cfg_mod.parse({})
    assert c.delivery_timeout_s == 60

    c2 = cfg_mod.parse(tomllib.loads("delivery_timeout_s = 120\n"))
    assert c2.delivery_timeout_s == 120

    with pytest.raises(ValueError, match="delivery_timeout_s"):
        cfg_mod.parse({"delivery_timeout_s": 0})
    with pytest.raises(ValueError, match="delivery_timeout_s"):
        cfg_mod.parse({"delivery_timeout_s": -1})
    with pytest.raises(ValueError, match="delivery_timeout_s"):
        cfg_mod.parse({"delivery_timeout_s": True})  # bool sneaks past int check
    with pytest.raises(ValueError, match="delivery_timeout_s"):
        cfg_mod.parse({"delivery_timeout_s": "60"})


def test_log_file_default_and_override(tmp_path) -> None:
    """Default is None (basicConfig stderr); strings expand to a Path."""
    c = cfg_mod.parse({})
    assert c.log_file is None

    target = tmp_path / "whatspyc.log"
    c2 = cfg_mod.parse({"log_file": str(target)})
    assert c2.log_file == target

    # ``~`` expansion
    c3 = cfg_mod.parse({"log_file": "~/whatspyc.log"})
    from pathlib import Path as _P
    assert c3.log_file == _P("~/whatspyc.log").expanduser()

    with pytest.raises(ValueError, match="log_file"):
        cfg_mod.parse({"log_file": ""})
    with pytest.raises(ValueError, match="log_file"):
        cfg_mod.parse({"log_file": 5})


def test_log_console_default_and_override() -> None:
    """Default is "auto" (resolved upstream); known values pass through;
    unknown values rejected at parse time."""
    c = cfg_mod.parse({})
    assert c.log_console == "auto"

    for v in ("auto", "stderr", "pane", "off"):
        c2 = cfg_mod.parse({"log_console": v})
        assert c2.log_console == v

    with pytest.raises(ValueError, match="log_console"):
        cfg_mod.parse({"log_console": "screen"})
    with pytest.raises(ValueError, match="log_console"):
        cfg_mod.parse({"log_console": 0})


def test_log_level_default_and_override() -> None:
    """Default is None (defer to env var / hardcoded WARNING); known
    level strings normalise to upper-case; unknown rejected."""
    c = cfg_mod.parse({})
    assert c.log_level is None

    c2 = cfg_mod.parse({"log_level": "info"})
    assert c2.log_level == "INFO"

    c3 = cfg_mod.parse({"log_level": "DEBUG"})
    assert c3.log_level == "DEBUG"

    with pytest.raises(ValueError, match="log_level"):
        cfg_mod.parse({"log_level": "verbose"})
    with pytest.raises(ValueError, match="log_level"):
        cfg_mod.parse({"log_level": 10})


def test_non_rhp_transport_does_not_require_engine() -> None:
    raw = tomllib.loads(
        """
        [[connect_profiles]]
        name = "fake"
        transport = "direct-tcp"
        host = "127.0.0.1"
        """
    )
    p = cfg_mod.parse(raw).resolve_profile("fake")
    assert p.engine is None
    assert p.port == 63001  # direct-tcp default still applied
