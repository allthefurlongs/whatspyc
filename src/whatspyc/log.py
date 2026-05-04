"""Tiny logging helper. Stays out of the way of the prompt_toolkit UI by
honouring whatever stream config the caller sets up."""

from __future__ import annotations

import logging
import os


def setup(level: str | None = None) -> None:
    lvl = level or os.environ.get("WHATSPYC_LOG", "WARNING")
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s %(name)-18s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
