"""The one full-screen view: an interactive checklist for a single activity.

Deliberately built on rich.Live plus a hand-rolled raw-mode key reader rather
than a TUI framework. That keeps the dependency list at two packages and means
the same render code in render.py serves both the static and interactive views.
"""

from __future__ import annotations

import select
import sys
import termios
import tty
from contextlib import contextmanager

from rich.console import Group
from rich.live import Live
from rich.text import Text

from .model import Activity
from .render import ACCENT, MUTED, console, activity_view

HELP = "  ↑↓/jk move    space toggle    d delete    a all done    q save & quit"

# Escape sequences we care about, mapped to logical key names.
_SEQUENCES = {
    "[A": "up",
    "[B": "down",
    "OA": "up",
    "OB": "down",
}


@contextmanager
def raw_mode(fd: int):
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def read_key(fd: int) -> str:
    """Block for one keypress, resolving arrow-key escape sequences."""
    char = sys.stdin.read(1)
    if char != "\x1b":
        return char

    # An escape sequence arrives as several bytes in one burst. A bare Esc has
    # nothing following it, so poll briefly rather than blocking forever.
    if not select.select([fd], [], [], 0.05)[0]:
        return "esc"
    rest = sys.stdin.read(2)
    return _SEQUENCES.get(rest, "esc")


def _frame(activity: Activity, cursor: int, message: str) -> Group:
    parts: list = [activity_view(activity, cursor=cursor, show_detail=True), Text("")]
    if message:
        parts.append(Text(f"  {message}", style=ACCENT))
    parts.append(Text(HELP, style=MUTED))
    return Group(*parts)


def run_checklist(activity: Activity) -> bool:
    """Drive the interactive checklist. Returns True if anything changed.

    Falls back to a plain static render when stdin or stdout is not a terminal,
    so `boost show 3 | less` and CI runs still produce useful output instead of
    crashing in termios.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print(activity_view(activity, show_detail=True))
        return False

    if not activity.tasks:
        console.print(activity_view(activity))
        return False

    fd = sys.stdin.fileno()
    cursor = 0
    dirty = False
    message = ""

    with raw_mode(fd), Live(console=console, screen=True, auto_refresh=False) as live:
        while True:
            live.update(_frame(activity, cursor, message), refresh=True)
            message = ""
            key = read_key(fd)

            if key in ("q", "\x03", "esc"):  # q, Ctrl-C, Esc
                break

            elif key in ("up", "k"):
                cursor = (cursor - 1) % len(activity.tasks)

            elif key in ("down", "j"):
                cursor = (cursor + 1) % len(activity.tasks)

            elif key in (" ", "\r", "\n", "x"):
                task = activity.tasks[cursor]
                task.done = not task.done
                dirty = True
                # Advance so repeated presses walk down the list, which is how
                # people actually tick off a morning's work.
                if task.done and cursor < len(activity.tasks) - 1:
                    cursor += 1

            elif key == "d":
                removed = activity.tasks.pop(cursor)
                cursor = min(cursor, len(activity.tasks) - 1)
                dirty = True
                message = f"Deleted “{removed.title}”"
                if not activity.tasks:
                    break

            elif key == "a":
                target = not all(t.done for t in activity.tasks)
                for task in activity.tasks:
                    task.done = target
                dirty = True
                message = "Marked all done" if target else "Cleared all"

    return dirty
