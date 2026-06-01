"""
Shared runtime state — holds the Telegram app reference so tools can
make Telegram API calls without circular imports through bot.py.
Set by bot.py on startup via set_app().
"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application

_app: "Application | None" = None
group_id: str = ""


def set_app(app: "Application", gid: str):
    global _app, group_id
    _app = app
    group_id = gid


def get_app() -> "Application":
    if _app is None:
        raise RuntimeError("App not initialised — set_app() must be called before using state.")
    return _app
