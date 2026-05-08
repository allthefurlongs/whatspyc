"""--nodecmd: rejection paths for incompatible flags / missing config / EOF."""

from __future__ import annotations

import io
from pathlib import Path

import click
import pytest

from whatspyc import cli
from whatspyc.config import Config


def _cfg(node_state_dir: Path | None) -> Config:
    return Config(node_state_dir=node_state_dir)


def test_nodecmd_rejects_textual_ui(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("M0ABC\n"))
    with pytest.raises(click.UsageError, match="forces --ui line"):
        cli._apply_nodecmd_mode(
            _cfg(tmp_path),
            ui_mode="textual",
            my_call_cli=None,
            state_dir_cli=None,
        )


def test_nodecmd_rejects_urwid_ui(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("M0ABC\n"))
    with pytest.raises(click.UsageError, match="forces --ui line"):
        cli._apply_nodecmd_mode(
            _cfg(tmp_path),
            ui_mode="urwid",
            my_call_cli=None,
            state_dir_cli=None,
        )


def test_nodecmd_accepts_explicit_line_ui(tmp_path: Path, monkeypatch) -> None:
    """``--nodecmd --ui line`` is the redundant-but-fine form."""
    monkeypatch.setattr("sys.stdin", io.StringIO("M0ABC\nMatt\n"))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    c = _cfg(tmp_path)
    cli._apply_nodecmd_mode(
        c, ui_mode="line", my_call_cli=None, state_dir_cli=None
    )
    assert c.my_call == "M0ABC"
    assert c.ui == "line"


def test_nodecmd_rejects_my_call_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("M0ABC\n"))
    with pytest.raises(click.UsageError, match="--my-call is not allowed"):
        cli._apply_nodecmd_mode(
            _cfg(tmp_path),
            ui_mode=None,
            my_call_cli="MM7AAA",
            state_dir_cli=None,
        )


def test_nodecmd_rejects_state_dir_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("M0ABC\n"))
    with pytest.raises(click.UsageError, match="--state-dir is not allowed"):
        cli._apply_nodecmd_mode(
            _cfg(tmp_path),
            ui_mode=None,
            my_call_cli=None,
            state_dir_cli=Path("/tmp/foo"),
        )


def test_nodecmd_requires_node_state_dir_in_config(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("M0ABC\n"))
    with pytest.raises(click.UsageError, match="node_state_dir"):
        cli._apply_nodecmd_mode(
            _cfg(None),
            ui_mode=None,
            my_call_cli=None,
            state_dir_cli=None,
        )


def test_nodecmd_eof_before_callsign(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(click.UsageError, match="no callsign on stdin"):
        cli._apply_nodecmd_mode(
            _cfg(tmp_path),
            ui_mode=None,
            my_call_cli=None,
            state_dir_cli=None,
        )


def test_nodecmd_blank_callsign_line(tmp_path: Path, monkeypatch) -> None:
    """Whitespace-only callsign line is treated as empty — same as EOF."""
    monkeypatch.setattr("sys.stdin", io.StringIO("   \n"))
    with pytest.raises(click.UsageError, match="no callsign on stdin"):
        cli._apply_nodecmd_mode(
            _cfg(tmp_path),
            ui_mode=None,
            my_call_cli=None,
            state_dir_cli=None,
        )


def test_nodecmd_eof_before_name_on_first_use(tmp_path: Path, monkeypatch) -> None:
    """Callsign read OK, but stdin closes before the name is supplied —
    only fires on the very first run for a given callsign (no name.txt yet)."""
    monkeypatch.setattr("sys.stdin", io.StringIO("M0ABC\n"))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    with pytest.raises(click.UsageError, match="no name on stdin"):
        cli._apply_nodecmd_mode(
            _cfg(tmp_path),
            ui_mode=None,
            my_call_cli=None,
            state_dir_cli=None,
        )
