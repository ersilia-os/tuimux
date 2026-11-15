"""The dashboard's look: palette, Textual theme, stylesheet and table columns."""

from textual.theme import Theme

# A graphite palette: greys everywhere, colour only where it means something.
AGENT = "#8fa8d0"  # an AI agent (claude) in a session's tabs
AMBER = "#e5c07b"  # awake / waiting / no-ssh
GREEN = "#98c379"  # active session / running / working
MUTED = "#5c6370"  # gray (offline status dot)
RED = "#e06c75"  # a routine that failed / can't run
STEEL = "#6c8ebf"  # focus: the focused panel's frame, the cursor

THEME = Theme(
    name="tuimux",
    primary=STEEL,
    secondary="#8b929e",
    accent=STEEL,
    foreground="#d7dae0",
    background="#16181c",
    surface="#1b1e23",
    panel="#23272e",
    success=GREEN,
    warning=AMBER,
    error=RED,
    dark=True,
)

# The sessions table's columns (key, header, width). Fixed widths, sized to
# the widest realistic content in each column so the table reads as evenly
# distributed (and the separator rule lines up):
#   NAME    dot + hostname; longest real name ~23, sub-login rows a bit more
#   STATUS  fixed words; longest is "checking…" (9)
#   STATE   session state / "☕ awake" / last-seen ("2h ago")
#   UPTIME  values are tiny ("2d"); the header sets the floor
#   FOLDER  a short basename on session rows, doubling as the machine hint
#           ("tailscale up --ssh", "no account · press u")
#   TABS    "N  <active-cmd>"
#   OPEN IN longest is "lost — not restored" (19)
#   OWNER   "miquel@" / "tag:dev"        SSH USER  "mduranfrigola"
SESSION_COLUMNS = (
    ("name", "NAME", 26),
    ("status", "STATUS", 9),
    ("state", "STATE", 10),  # fits the animated "running…"
    ("uptime", "UPTIME", 6),
    ("folder", "FOLDER", 20),
    ("tabs", "TABS", 13),
    ("open", "OPEN IN", 20),
    ("owner", "OWNER", 10),
    ("user", "SSH USER", 14),
)


def _span_width(keys):
    """The width of a column that spans these sessions columns exactly (each
    DataTable cell has 1 column of padding on both sides)."""
    widths = dict((k, w) for k, _, w in SESSION_COLUMNS)
    return sum(widths[k] for k in keys) + 2 * (len(keys) - 1)


# The routines table's columns, each spanning whole sessions columns, so every
# routines column starts exactly where a sessions column does.
ROUTINE_COLUMNS = (
    ("name", "ROUTINE", ("name",)),
    ("schedule", "SCHEDULE", ("status", "state")),
    ("status", "THIS RUN", ("uptime", "folder")),
    ("next", "NEXT", ("tabs", "open")),
    ("mode", "MODE", ("owner", "user")),
)


# Colour convention, the same in both panels (keep to it when adding styles):
#   machine colour → a machine's NAME on its header row, in both tables (it
#                    matches that machine's tmux status bar). Nothing under the
#                    header is coloured: session and routine names are plain.
#   green → only the status dot of a machine you can use (see _marker).
#   amber → waits on you: Claude asked something, a run to read or finish, a
#           box you can't SSH into yet. Only a question blinks.
#   red   → broken: a failed run, no claude, a bad schedule.
#   blue  → "claude" in the TABS column (AGENT).
#   steel → keys and focus ($primary), e.g. a key named in a hint.
#   plain → active (running, working, starting); dim → everything else:
#           metadata, idle or offline, paused, done, scheduled.
#   bold  → machine names, their dots, and the item a hint is about.


def _marker(state):
    """The bold status dot in front of a machine's NAME — a traffic light for how
    reachable it is: ● green = usable (local / online over SSH), ● amber = online
    but no working SSH (or too busy to answer), ● gray = a non-SSH device (phone),
    ● dim = still checking, ○ gray = offline."""
    if state == "up":
        return ("● ", f"bold {GREEN}")
    if state == "warn":
        return ("● ", f"bold {AMBER}")
    if state == "device":
        return ("● ", f"bold {MUTED}")
    if state == "checking":
        return ("● ", "dim")
    return ("○ ", f"bold {MUTED}")  # offline


# Per-session STATE word → style. A Claude session is one of three states:
#   running — actively generating (things happening)
#   waiting — expecting a response FROM YOU: the one thing that wants you, so it
#             blinks amber (the only blink in the UI, kept rare so it truly pops)
#   idle    — up but nothing happening (finished, sitting at the input box)
# Non-agent sessions are "running" (a live command) or "idle" (a shell), dim.
# "working" is a legacy alias for running (the engine no longer emits it).
_STATE_STYLE = {
    "waiting": f"blink bold {AMBER}",
    "running": "",
    "working": "",
    "idle": "dim",
}

# What a row says while an action on it is in flight (until the host is re-probed).
_PENDING_LABEL = {
    "kill": "closing…",
    "detach": "detaching…",
    "rename": "renaming…",
    "awake": "☕ …",
}


# Table columns in display order. A "cell" is a tuple of (text, style) segments
# so one cell can mix styles (e.g. a dim marker + a bold host name); an empty
# tuple renders blank. _row() builds a (cells, meta) pair, filling only the
# columns you name and leaving the rest empty — keeps _view readable.
_COLS = (
    "name",
    "status",
    "state",
    "uptime",
    "folder",
    "tabs",
    "open_in",
    "owner",
    "user",
)


def _row(meta, **cells):
    return (tuple(cells.get(c, ()) for c in _COLS), meta)


# The dashboard's stylesheet (Tuimux.CSS).
APP_CSS = """
    Screen { background: $surface; }
    /* Every panel sits on one grid: the same margins, padding, frame and title
       placement, so their columns line up. Frames are quiet grey; only the
       focused panel's frame takes the accent. */
    #table-wrap, #preview, #routines-wrap {
        border: round $foreground 12%;
        border-title-color: $foreground 55%;
        border-title-style: bold;
        border-title-align: left;
        border-subtitle-color: $text-muted;
        border-subtitle-align: right;
        padding: 1 2 0 2;
        margin: 0 2 1 2;
    }
    #table-wrap { height: 3fr; margin: 1 2 1 2; }
    #preview, #routines-wrap { height: 2fr; }
    #preview { background: $surface; color: $text; overflow: hidden; }
    #table-wrap:focus-within, #routines-wrap:focus-within {
        border: round $primary 70%;
        border-title-color: $foreground;
    }
    /* Tables sit flat on their panel: no fill, no focus tint, no header band,
       no hover band. Focus shows on the panel's frame instead (focus-within),
       and only the focused table shows its cursor — a soft band that leaves
       the row's own colours alone (cursor_foreground_priority="renderable"). */
    DataTable { height: 1fr; background: transparent; }
    DataTable:focus { background-tint: $foreground 0%; }
    DataTable > .datatable--header,
    DataTable:focus > .datatable--header {
        color: $foreground 45%; text-style: bold;
        background: transparent; background-tint: $foreground 0%;
    }
    DataTable > .datatable--cursor { background: transparent; text-style: none; }
    DataTable:focus > .datatable--cursor { background: $primary 18%; text-style: none; }
    DataTable > .datatable--hover { background: transparent; }
    #routine-detail {
        height: 4; padding: 1 1 0 1; color: $text-muted;  /* rule, gap, 2 lines */
        border-top: solid $foreground 8%;
    }
    #form {
        width: 92; height: auto; padding: 1 3; margin: 1;
        background: $panel; border: round $foreground 20%;
    }
    #form-title { color: $foreground; text-style: bold; padding: 0 0 1 0; }
    #form .row { height: auto; }
    #form .col { width: 1fr; height: auto; }
    #form .col:first-of-type { margin-right: 2; }
    #form .field { color: $text-muted; margin-top: 1; }
    #form .hint { height: 1; padding: 0 1; }
    #form TextArea { height: 6; }
    #f-error { margin-top: 1; height: auto; }
    #form-keys { color: $text-muted; margin-top: 1; }
    #dialog {
        width: 56; height: auto; padding: 1 2; margin: 1;
        background: $panel; border: round $foreground 20%;
    }
    #dialog-msg { width: 1fr; padding: 0 0 1 0; content-align: center middle; }
    #dialog-input { margin-top: 1; }
    #menu-list {
        height: auto; max-height: 14; border: none;
        background: $panel; padding: 0;
    }
    #menu-list > .option-list--option { padding: 0 1; }
    #menu-list > .option-list--option-highlighted { background: $primary 20%; }
    ModalScreen { align: center middle; background: $background 70%; }
"""
