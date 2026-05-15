"""--nodecmd: per-call state dir layout, name.txt persistence, isolation."""

from __future__ import annotations

import io
from pathlib import Path

from whatspyc import cli
from whatspyc.config import Config


def test_first_run_prompts_for_name_and_persists_it(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "node-state"
    monkeypatch.setattr("sys.stdin", io.StringIO("M0ABC\nMatt Tester\n"))
    fake_stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", fake_stdout)

    c = Config(node_state_dir=root)
    cli._apply_nodecmd_mode(
        c, ui_mode=None, my_call_cli=None, state_dir_cli=None
    )

    assert c.my_call == "M0ABC"
    assert c.name == "Matt Tester"
    assert c.ui == "line"
    assert c.state_dir == root / "M0ABC"
    assert c.state_dir.is_dir()

    name_file = c.state_dir / "name.txt"
    assert name_file.exists()
    # Persisted with the user's exact name (a trailing newline is fine
    # but the strip() on read-back must yield the original).
    assert name_file.read_text(encoding="utf-8").strip() == "Matt Tester"

    # The prompt was shown — operators rely on this to know the program
    # is asking for input.
    assert "Please enter your name:" in fake_stdout.getvalue()


def test_callsign_uppercased_and_stripped(tmp_path: Path, monkeypatch) -> None:
    """Callsign normalises like everywhere else in the client — upper +
    surrounding whitespace stripped — so the per-call dir is canonical."""
    root = tmp_path / "node-state"
    monkeypatch.setattr("sys.stdin", io.StringIO("  m0abc  \nMatt\n"))
    monkeypatch.setattr("sys.stdout", io.StringIO())

    c = Config(node_state_dir=root)
    cli._apply_nodecmd_mode(
        c, ui_mode=None, my_call_cli=None, state_dir_cli=None
    )

    assert c.my_call == "M0ABC"
    assert c.state_dir == root / "M0ABC"


def test_state_dir_strips_ssid(tmp_path: Path, monkeypatch) -> None:
    """The state dir is keyed on the bare callsign so one operator gets one
    store regardless of which SSID their node hands us. ``my_call`` keeps
    the SSID for AX.25 addressing (gotcha 14)."""
    root = tmp_path / "node-state"
    monkeypatch.setattr("sys.stdin", io.StringIO("2E0HKD-2\nMatt\n"))
    monkeypatch.setattr("sys.stdout", io.StringIO())

    c = Config(node_state_dir=root)
    cli._apply_nodecmd_mode(
        c, ui_mode=None, my_call_cli=None, state_dir_cli=None
    )

    assert c.my_call == "2E0HKD-2"
    assert c.state_dir == root / "2E0HKD"


def test_state_dir_shared_across_ssids_for_same_operator(
    tmp_path: Path, monkeypatch
) -> None:
    """Same operator connecting once with SSID and once without lands in
    the same state dir — no duplicate stores, no re-prompt for name."""
    root = tmp_path / "node-state"

    monkeypatch.setattr("sys.stdin", io.StringIO("2E0HKD-2\nMatt\n"))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    c1 = Config(node_state_dir=root)
    cli._apply_nodecmd_mode(
        c1, ui_mode=None, my_call_cli=None, state_dir_cli=None
    )

    # Second run, bare call on stdin, no name line — name.txt from the
    # first run must already be in place.
    monkeypatch.setattr("sys.stdin", io.StringIO("2E0HKD\n"))
    fake_stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", fake_stdout)
    c2 = Config(node_state_dir=root)
    cli._apply_nodecmd_mode(
        c2, ui_mode=None, my_call_cli=None, state_dir_cli=None
    )

    assert c1.state_dir == c2.state_dir == root / "2E0HKD"
    assert c2.name == "Matt"
    assert "Please enter your name:" not in fake_stdout.getvalue()


def test_second_run_does_not_reprompt_when_name_txt_exists(
    tmp_path: Path, monkeypatch
) -> None:
    """Once the name is saved, a follow-up run with just the callsign on
    stdin (no name line) must connect through without asking again."""
    root = tmp_path / "node-state"

    # First run — supply name.
    monkeypatch.setattr("sys.stdin", io.StringIO("M0ABC\nMatt Tester\n"))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    c1 = Config(node_state_dir=root)
    cli._apply_nodecmd_mode(
        c1, ui_mode=None, my_call_cli=None, state_dir_cli=None
    )

    # Second run — only the callsign on stdin (no name line).
    monkeypatch.setattr("sys.stdin", io.StringIO("M0ABC\n"))
    fake_stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", fake_stdout)
    c2 = Config(node_state_dir=root)
    cli._apply_nodecmd_mode(
        c2, ui_mode=None, my_call_cli=None, state_dir_cli=None
    )

    assert c2.my_call == "M0ABC"
    assert c2.name == "Matt Tester"
    # No reprompt — operator's stdin (just the callsign) was enough.
    assert "Please enter your name:" not in fake_stdout.getvalue()


def test_per_call_isolation(tmp_path: Path, monkeypatch) -> None:
    """Two different callsigns get independent state dirs and name files."""
    root = tmp_path / "node-state"

    monkeypatch.setattr("sys.stdin", io.StringIO("M0ABC\nAlice\n"))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    c1 = Config(node_state_dir=root)
    cli._apply_nodecmd_mode(
        c1, ui_mode=None, my_call_cli=None, state_dir_cli=None
    )

    monkeypatch.setattr("sys.stdin", io.StringIO("G7BAR\nBob\n"))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    c2 = Config(node_state_dir=root)
    cli._apply_nodecmd_mode(
        c2, ui_mode=None, my_call_cli=None, state_dir_cli=None
    )

    assert c1.state_dir == root / "M0ABC"
    assert c2.state_dir == root / "G7BAR"
    assert c1.state_dir != c2.state_dir
    assert (c1.state_dir / "name.txt").read_text(encoding="utf-8").strip() == "Alice"
    assert (c2.state_dir / "name.txt").read_text(encoding="utf-8").strip() == "Bob"


def test_blank_name_txt_falls_back_to_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    """If name.txt exists but is empty/whitespace-only (truncated by some
    operator footgun), treat it as missing and reprompt — better than
    handing an empty string to the WPS connect record."""
    root = tmp_path / "node-state"
    state_dir = root / "M0ABC"
    state_dir.mkdir(parents=True)
    (state_dir / "name.txt").write_text("   \n", encoding="utf-8")

    monkeypatch.setattr("sys.stdin", io.StringIO("M0ABC\nFresh Name\n"))
    fake_stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", fake_stdout)

    c = Config(node_state_dir=root)
    cli._apply_nodecmd_mode(
        c, ui_mode=None, my_call_cli=None, state_dir_cli=None
    )

    assert c.name == "Fresh Name"
    assert "Please enter your name:" in fake_stdout.getvalue()
    assert (state_dir / "name.txt").read_text(encoding="utf-8").strip() == "Fresh Name"


def test_node_state_dir_parsed_from_toml(tmp_path: Path) -> None:
    """The new ``node_state_dir`` config key is read by ``parse()``;
    leading ``~`` expands like ``log_file``."""
    from whatspyc import config as cfg_mod

    c = cfg_mod.parse({"node_state_dir": str(tmp_path / "nsd")})
    assert c.node_state_dir == tmp_path / "nsd"

    c2 = cfg_mod.parse({"node_state_dir": "~/nsd"})
    assert c2.node_state_dir == Path("~/nsd").expanduser()


def test_node_state_dir_validation() -> None:
    """Empty string and non-string values are rejected at parse time."""
    import pytest

    from whatspyc import config as cfg_mod

    with pytest.raises(ValueError, match="node_state_dir"):
        cfg_mod.parse({"node_state_dir": ""})
    with pytest.raises(ValueError, match="node_state_dir"):
        cfg_mod.parse({"node_state_dir": 5})
