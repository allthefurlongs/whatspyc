"""log.setup handler matrix + pane handler install/remove."""

from __future__ import annotations

import logging
import sys

import pytest

from whatspyc import log as log_mod


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """Each test starts with a clean root logger and ends without leaking
    handlers into the next test."""
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    for h in list(root.handlers):
        root.removeHandler(h)
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved:
        root.addHandler(h)
    root.setLevel(saved_level)


def _kinds(handlers):
    return [type(h).__name__ for h in handlers]


def test_console_stderr_no_file() -> None:
    log_mod.setup(level="DEBUG", file=None, console="stderr")
    handlers = logging.getLogger().handlers
    assert _kinds(handlers) == ["StreamHandler"]
    assert handlers[0].stream is sys.stderr
    assert logging.getLogger().level == logging.DEBUG


def test_console_off_no_file() -> None:
    log_mod.setup(level="INFO", file=None, console="off")
    assert logging.getLogger().handlers == []


def test_console_stderr_plus_file(tmp_path) -> None:
    target = tmp_path / "logs" / "whatspyc.log"
    log_mod.setup(level="WARNING", file=target, console="stderr")
    handlers = logging.getLogger().handlers
    assert sorted(_kinds(handlers)) == ["FileHandler", "StreamHandler"]
    # Parent dir auto-created.
    assert target.parent.exists()


def test_console_off_plus_file(tmp_path) -> None:
    target = tmp_path / "whatspyc.log"
    log_mod.setup(level="INFO", file=target, console="off")
    handlers = logging.getLogger().handlers
    assert _kinds(handlers) == ["FileHandler"]


def test_console_pane_defers_handler_install() -> None:
    """``console='pane'`` adds no handler at setup time — the TUI mount
    is responsible for installing the bridge."""
    log_mod.setup(level="INFO", file=None, console="pane")
    assert logging.getLogger().handlers == []


def test_pane_handler_install_routes_by_level(tmp_path) -> None:
    log_mod.setup(level="DEBUG", file=None, console="pane")
    writes: list[str] = []
    errors: list[str] = []
    handler = log_mod.install_pane_handler(writes.append, errors.append)
    assert handler is not None
    assert handler in logging.getLogger().handlers
    try:
        logger = logging.getLogger("whatspyc.test")
        logger.info("hello")
        logger.warning("careful")
        logger.error("boom")
        logger.critical("oh no")
        assert any("hello" in m for m in writes)
        assert any("careful" in m for m in writes)
        assert any("boom" in m for m in errors)
        assert any("oh no" in m for m in errors)
        # warning gets a yellow tag, error a red tag.
        warn_msg = next(m for m in writes if "careful" in m)
        assert "[yellow]" in warn_msg
        err_msg = next(m for m in errors if "boom" in m)
        assert "[red]" in err_msg
    finally:
        log_mod.remove_pane_handler(handler)
    assert handler not in logging.getLogger().handlers


def test_pane_handler_install_noop_when_not_armed() -> None:
    """If setup ran with console='stderr', install returns None and the
    root logger keeps just the StreamHandler."""
    log_mod.setup(level="INFO", file=None, console="stderr")
    h = log_mod.install_pane_handler(lambda _l: None, lambda _l: None)
    assert h is None
    # remove_pane_handler with None must be a no-op.
    log_mod.remove_pane_handler(None)


def test_setup_replaces_old_handlers() -> None:
    """Successive setup() calls don't accumulate handlers."""
    log_mod.setup(level="INFO", file=None, console="stderr")
    log_mod.setup(level="INFO", file=None, console="stderr")
    assert len(logging.getLogger().handlers) == 1


def test_level_falls_back_to_env(monkeypatch) -> None:
    monkeypatch.setenv("WHATSPYC_LOG", "ERROR")
    log_mod.setup(level=None, file=None, console="stderr")
    assert logging.getLogger().level == logging.ERROR


def test_level_default_when_no_env(monkeypatch) -> None:
    monkeypatch.delenv("WHATSPYC_LOG", raising=False)
    log_mod.setup(level=None, file=None, console="stderr")
    assert logging.getLogger().level == logging.WARNING
