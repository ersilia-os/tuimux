"""Display formatting: ages, dates, folders, uptimes and session names."""

import os
import re
import time

# Commands that mean a session is just sitting at a shell prompt (idle).
SHELLS = {
    "zsh",
    "bash",
    "sh",
    "fish",
    "-zsh",
    "-bash",
    "dash",
    "ksh",
    "tcsh",
    "login",
    "",
}


def _ago(secs):
    """ "5m ago" / "3h ago" / "2d ago" for a duration in seconds."""
    secs = max(int(secs), 0)
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _when(t):
    """A routine's next run as a date and time: "Fri 13 Nov 08:00"."""
    return t.strftime("%a %-d %b %H:%M")


def _took(secs):
    """A run's length: "25m", "2h 10m", "3d"."""
    m = max(int(secs) // 60, 1)
    if m < 60:
        return f"{m}m"
    if m < 1440:
        return f"{m // 60}h {m % 60}m" if m % 60 else f"{m // 60}h"
    return f"{m // 1440}d"


def _abbrev(path):
    return re.sub(r"^/home/[^/]+", "~", re.sub(r"^/Users/[^/]+", "~", path or ""))


def _folder(dird):
    """FOLDER cell text: just the working dir's last component — the useful tail —
    instead of the whole path, which the narrow column would truncate from the
    front. Keeps "~" for home and "/" for root."""
    if dird in ("", "~", "/"):
        return dird or "~"
    return os.path.basename(dird.rstrip("/")) or dird


def _uptime(created):
    try:
        secs = int(time.time()) - int(created)
    except (TypeError, ValueError):
        return ""
    secs = max(secs, 0)
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _title_parts(title):
    """Split a window/tab title into comparable components.

    Ghostty titles are a single string ("<session> · <window>"); Terminal.app
    decorates them as "<login> — <session> · <window> — <process> — <WxH>".
    Splitting on the em-dash yields one component per terminal that we can match
    exactly, so Terminal's decorations don't defeat the lookup."""
    return [p.strip() for p in title.split("—")]


def _auto_name(name, dird, is_agent, cmd):
    if not name.isdigit():  # docker/manual name → keep it
        return name or "?"
    if dird not in ("~", "", "/"):  # bare tmux number in a real folder → folder
        return os.path.basename(dird)
    if is_agent:
        return "claude"
    if cmd and cmd not in SHELLS:
        return cmd
    return name


def _preview_tail(raw, height):
    """The lines a pane capture should show, newest at the bottom.

    Trims trailing blank lines (a tmux pane is usually padded with empties below
    the prompt) so the latest output sits on the panel's last row, then keeps the
    final `height` lines — the tail is what tells you what the session is doing.
    Returns [] for an all-blank/empty capture."""
    lines = raw.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-height:] if height > 0 else lines
