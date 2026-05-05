"""Logging helper.

Two sinks are independently controllable:

* ``log_file`` — append records to a rotating-free flat file.
* ``log_console`` — one of ``"stderr"`` (the historic default for the line
  UI), ``"pane"`` (route through the TUI's status pane via callbacks the
  TUI registers on mount), or ``"off"`` (no console sink at all).

``"auto"`` is resolved upstream in ``cli.main`` to ``"pane"`` for ``--ui
tui`` and ``"stderr"`` otherwise; ``setup`` only ever sees the resolved
value.

Pane-mode setup defers handler installation to TUI mount time — the App
instance doesn't exist yet at ``setup`` time. ``setup`` arms a flag;
``install_pane_handler`` consults the flag and either attaches a handler
that bridges to the App's ``_status_write`` / ``_status_error`` (with
ERROR+ records routed via ``_status_error`` so the pane auto-opens) or
no-ops, returning ``None`` for the symmetric ``remove_pane_handler``
call on unmount.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

_FORMAT = "%(asctime)s %(name)-18s %(levelname)-7s %(message)s"
_DATEFMT = "%H:%M:%S"

# Set by ``setup`` so ``install_pane_handler`` knows whether to actually
# attach a handler when the TUI mounts. Module-level state because the
# TUI build path doesn't otherwise see the resolved console choice.
_pane_install_armed = False


def setup(
    level: str | None = None,
    file: str | Path | None = None,
    console: str = "stderr",
) -> None:
    lvl = level or os.environ.get("WHATSPYC_LOG", "WARNING")
    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    root = logging.getLogger()
    # Wipe any pre-existing handlers so successive ``setup`` calls (e.g.
    # tests) don't accumulate sinks.
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    root.setLevel(lvl)

    if file:
        # Routing logging to a file unblocks the TUI's full-screen mode,
        # where any stderr write would corrupt the rendered surface.
        path = Path(file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(path))
        fh.setFormatter(fmt)
        root.addHandler(fh)

    if console == "stderr":
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    # "off" → nothing added here.
    # "pane" → handler attached by install_pane_handler at TUI mount.

    global _pane_install_armed
    _pane_install_armed = console == "pane"


def install_pane_handler(
    write: Callable[[str], None],
    error: Callable[[str], None],
) -> logging.Handler | None:
    """If ``setup`` was called with ``console="pane"``, attach a handler
    that bridges every log record to the supplied callbacks. Returns the
    handler so the caller can detach it later via ``remove_pane_handler``;
    returns ``None`` (a no-op for the remove call) when pane mode is not
    armed.

    ``write`` is invoked for records below ERROR; ``error`` is invoked
    for ERROR/CRITICAL — the convention being that ``error`` also opens
    the pane if it's currently hidden.
    """
    if not _pane_install_armed:
        return None
    handler = _PaneLogHandler(write, error)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    logging.getLogger().addHandler(handler)
    return handler


def remove_pane_handler(handler: logging.Handler | None) -> None:
    if handler is None:
        return
    logging.getLogger().removeHandler(handler)
    try:
        handler.close()
    except Exception:
        pass


class _PaneLogHandler(logging.Handler):
    """Bridges a logging record into the TUI status pane.

    The pane has ``markup=True`` so the colour prefix is rendered as
    Rich markup. ERROR / CRITICAL go through the error callback, which
    auto-shows the pane; everything else uses the plain write callback.
    """

    _LEVEL_MARKUP = {
        logging.WARNING: "[yellow]",
        logging.ERROR: "[red]",
        logging.CRITICAL: "[red]",
    }

    def __init__(
        self,
        write: Callable[[str], None],
        error: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._write = write
        self._error = error

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            tag = self._LEVEL_MARKUP.get(record.levelno)
            if tag is not None:
                msg = f"{tag}{msg}[/]"
            if record.levelno >= logging.ERROR:
                self._error(msg)
            else:
                self._write(msg)
        except Exception:  # pragma: no cover — never let logging crash
            self.handleError(record)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
