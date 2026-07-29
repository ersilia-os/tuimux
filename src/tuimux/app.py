#!/usr/bin/env python3
"""tuimux — a Textual dashboard for tmux sessions across your tailnet.

The heavy lifting (Tailscale discovery, tmux/ssh probing, terminal tab spawning,
keep-awake, claude/state detection) lives in the bundled `engine.sh`; this is
purely the front-end, calling it for data and actions.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from importlib.resources import files

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import Header, Footer, DataTable, Label, Input, OptionList, Static
from textual.widgets.option_list import Option

from .cli import relaunch_argv, tuimux_bin

ENGINE = str(files("tuimux").joinpath("engine.sh"))
# TUIMUX_BIN tells the engine its own absolute path, so sessions it spawns into
# new terminal tabs re-invoke tuimux even when it isn't on the tab's PATH
# (e.g. installed in a conda env / venv that isn't activated by default).
_ENV = {**os.environ, "TUIMUX_BIN": tuimux_bin()}
REFRESH = float(os.environ.get("TUIMUX_REFRESH", "3") or 3)
# Hold off the periodic refresh until you've stopped pressing keys for this long,
# so navigating the list stays fluid (no probe/render churn mid-keystroke). The
# refresh still runs on every tick once you're idle, and immediately on returning
# from a dialog.
QUIET = float(os.environ.get("TUIMUX_QUIET", "1.2") or 1.2)
# How long a "resumed"/"lost" verdict stays on screen after a machine reconnects.
RECONCILE_TTL = float(os.environ.get("TUIMUX_RECONCILE_TTL", "30") or 30)
AWAKE = "keep-awake"
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

VIOLET = "#b08cff"  # agent
AMBER = "#e0af68"  # awake / waiting / no-ssh
GREEN = "#9ece6a"  # active session / running / working
MUTED = "#565f89"  # gray (offline status dot)


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

# A probe that can't finish in this many seconds is treated as unreachable, so
# one slow/contended host can't stall (or back up) the whole refresh.
PROBE_TIMEOUT = float(os.environ.get("TUIMUX_PROBE_TIMEOUT", "10") or 10)
# Mutating actions (detach/close/rename/awake) run off the UI thread; cap them
# too so a slow host can't leave a worker hanging forever.
ACTION_TIMEOUT = float(os.environ.get("TUIMUX_ACTION_TIMEOUT", "15") or 15)
# Host discovery (tailscale status) — bounded so a wedged tailscaled can't hang
# the host-scan worker indefinitely.
HOSTS_TIMEOUT = float(os.environ.get("TUIMUX_HOSTS_TIMEOUT", "8") or 8)
# Terminal window/tab scan (AppleScript) used to show where a session is open.
WINDOWS_TIMEOUT = float(os.environ.get("TUIMUX_WINDOWS_TIMEOUT", "5") or 5)
# Live-preview pane capture (tmux capture-pane, local or over SSH). Short, since
# it's a tiny read that fires on navigation and every heartbeat.
PEEK_TIMEOUT = float(os.environ.get("TUIMUX_PEEK_TIMEOUT", "6") or 6)
# How many of the pane's most-recent lines the preview shows when its on-screen
# height isn't known yet (before first layout). The tail is what matters — the
# prompt and the latest output sit at the bottom of a tmux pane.
PREVIEW_LINES = 12
# How often (seconds) to re-probe a host that refused SSH. The access-based default
# probes the whole online fleet to discover which boxes you can log into; this keeps
# steady-state light — reachable/own boxes refresh every tick, boxes you have no
# account on are retried only this often (enough to notice newly-granted access).
RECHECK = float(os.environ.get("TUIMUX_RECHECK", "60") or 60)


def _run(args, timeout=None):
    try:
        return subprocess.run(
            ["bash", ENGINE, *args],
            capture_output=True,
            text=True,
            env=_ENV,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # Distinct marker so callers can tell "too slow to answer" apart from a
        # genuine SSH failure (engine prints UNREACHABLE for the latter).
        return subprocess.CompletedProcess(args, 124, "__TIMEOUT__\n", "")


def fetch_hosts(scope="mine"):
    # scope "org" lists the whole tailnet fleet (every owner); "mine" is your own
    # machines plus any host you've mapped a login for.
    args = ["__hosts", "org"] if scope == "org" else ["__hosts"]
    res = []
    for ln in _run(args, timeout=HOSTS_TIMEOUT).stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        name = parts[0]
        is_local = parts[1].strip() == "1"
        status = parts[2].strip() if len(parts) > 2 else "online"
        lastseen = parts[3].strip() if len(parts) > 3 else ""
        kind = parts[4].strip() if len(parts) > 4 else "compute"
        owner = parts[5].strip() if len(parts) > 5 else ""
        mapping = parts[6].strip() if len(parts) > 6 else ""
        # probe defaults true when the engine doesn't say (older output / your own).
        probe = parts[7].strip() != "0" if len(parts) > 7 else True
        # accent colour computed by the engine (host_color) — kept there as the
        # single source of truth so the tmux status bar matches this row exactly.
        color = parts[8].strip() if len(parts) > 8 else ""
        # this row's SSH username (a multi-login host emits one row per user).
        login = parts[9].strip() if len(parts) > 9 else ""
        # 1 for the host's first/only login (full header), 0 for extra logins
        # (drawn as a sub-row). Default 1 for older engine output.
        primary = parts[10].strip() != "0" if len(parts) > 10 else True
        res.append(
            (
                name,
                is_local,
                status,
                lastseen,
                kind,
                owner,
                mapping,
                probe,
                color,
                login,
                primary,
            )
        )
    # Order: your own machines first (so the fleet view still opens on you), then
    # other owners grouped together, consumers (phones/tablets) always last. Stable
    # sort keeps the engine's self-leads order within each group.
    my_owner = next((r[5] for r in res if r[1]), "")
    res.sort(key=lambda r: (r[4] == "consumer", r[5] != my_owner, r[5]))
    return res


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


def probe(host, login):
    info = {
        "reachable": False,
        "busy": False,
        "notmux": False,
        "awake": False,
        "sessions": [],
    }
    lines = _run(["__probe", host, login], timeout=PROBE_TIMEOUT).stdout.splitlines()
    if lines and lines[0].strip() == "__TIMEOUT__":
        # SSH is fine, the host is just too loaded to answer within PROBE_TIMEOUT.
        info["busy"] = True
        return info
    if not lines or lines[0].strip() != "OK":
        return info
    info["reachable"] = True
    S, W, L, A, C = {}, {}, {}, {}, set()
    for ln in lines[1:]:
        if ln == "__NOTMUX__":
            info["notmux"] = True
            continue
        if "|" not in ln:
            continue
        p = ln.split("|")
        t = p[0]
        try:
            if t == "S":
                S[p[1]] = {
                    "attached": p[2] == "1",
                    "windows": int(p[3] or 0),
                    "dir": p[4] if len(p) > 4 else "",
                    "cmd": p[5] if len(p) > 5 else "",
                    "created": p[6] if len(p) > 6 else "",
                }
            elif t == "W":
                W.setdefault(p[1], []).append((p[2], p[3], p[4] == "1"))
            elif t == "L":
                L.setdefault(p[1], []).append(p[2])  # one entry per attached client
            elif t == "A":
                A[p[1]] = p[2]
            elif t == "C":
                C.add(p[1])
        except IndexError:
            continue
    info["awake"] = AWAKE in S
    for name, s in S.items():
        if name == AWAKE:
            continue
        is_agent = name in C
        dird = _abbrev(s["dir"]) or "~"
        wins = W.get(name, [])
        count = len(wins) or s["windows"] or 1
        active = next(
            (w[1] for w in wins if w[2]), wins[0][1] if wins else (s["cmd"] or "?")
        )
        if is_agent and re.match(r"^[0-9][0-9.]*$", active or ""):
            active = "claude"
        state = (
            (A.get(name) or "idle")
            if is_agent
            else ("idle" if s["cmd"] in SHELLS else "running")
        )
        info["sessions"].append(
            {
                "name": name,
                "auto": _auto_name(name, dird, is_agent, s["cmd"]),
                "attached": s["attached"],
                # how many tmux clients hold this session on its host — drives the
                # "on host"/"N clients"/"detached" half of the OPEN IN cell. This is
                # independent of whether a tab is open on *this* Mac (see _window_locs).
                "nclients": len(L.get(name, [])),
                "dir": dird,
                # window count only when >1 (a lone window's "1" is just noise);
                # a single-window session shows only its active command.
                "tabs": (f"{count}  {active}" if count > 1 else active),
                "state": state,
                "uptime": _uptime(s["created"]),
                "created": s["created"],  # raw epoch — identity key for reconnect
                "agent": is_agent,
            }
        )
    info["sessions"].sort(key=lambda x: x["auto"].lower())
    return info


def _reconcile_sessions(old_sessions, new_sessions):
    """Compare the last-known-good sessions of a host against what's there now,
    just after it has come back online. A session is identified by (name, created)
    — tmux's session_created epoch is stable for the life of a session, so a match
    means the very same session survived (its process kept running); a miss means
    the old one is gone (the host was shut down, or tmux/the session restarted).

    Returns {name: "resumed"|"lost"}. Only _got calls this, and only on an
    unreachable→reachable flip, so routine refreshes never flash badges."""
    old_ids = {(s["name"], s.get("created", "")) for s in old_sessions}
    new_ids = {(s["name"], s.get("created", "")) for s in new_sessions}
    verdicts = {}
    for s in new_sessions:
        if (s["name"], s.get("created", "")) in old_ids:
            verdicts[s["name"]] = "resumed"
    for s in old_sessions:
        if (s["name"], s.get("created", "")) not in new_ids:
            verdicts[s["name"]] = "lost"
    return verdicts


def _probe_or_offline(h):
    name, _is_local, status, lastseen = h[0], h[1], h[2], h[3]
    login = h[9] if len(h) > 9 else ""
    if status == "offline":
        # Tailscale already reports it down — skip the SSH probe (and its timeout).
        info = {
            "reachable": False,
            "busy": False,
            "notmux": False,
            "awake": False,
            "sessions": [],
            "lastseen": lastseen,
        }
        return (h, info)
    return (h, probe(name, login))


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


class Confirm(ModalScreen[bool]):
    BINDINGS = [("escape", "no"), ("n", "no"), ("y", "yes")]

    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.message, id="dialog-msg")
            yield OptionList(
                Option("↩   cancel", id="cancel"),
                Option("✕   yes, shut it down", id="ok"),
                id="menu-list",
            )

    def on_mount(self):
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, e: OptionList.OptionSelected) -> None:
        self.dismiss(e.option.id == "ok")

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


class Ask(ModalScreen[str]):
    BINDINGS = [("escape", "cancel")]

    def __init__(self, prompt, default=""):
        super().__init__()
        self.prompt = prompt
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt, id="dialog-msg")
            yield Input(value=self.default, id="dialog-input")

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(self, e: Input.Submitted) -> None:
        self.dismiss(e.value.strip())

    def action_cancel(self):
        self.dismiss("")


class Menu(ModalScreen[str]):
    BINDINGS = [("escape", "cancel")]

    def __init__(self, meta):
        super().__init__()
        self.meta = meta

    def compose(self) -> ComposeResult:
        attach = self.meta["action"] == "attach"
        title = self.meta["session"] if attach else "new session"
        if attach:
            opened = self.meta.get("open")
            opts = [
                ("↵  go to its tab" if opened else "↵  open in a new tab", "open"),
                ("⊕  open in a new window", "window"),
                ("✎  rename", "rename"),
            ]
            if opened:  # only meaningful when it's actually open
                opts.append(("⏏  detach (close its tab, keep running)", "detach"))
            opts.append(("✕  close (kill session)", "close"))
        else:
            opts = [
                ("↵  new tab", "open"),
                ("⊕  new window", "window"),
                ("✎  name & create", "rename"),
            ]
        with Vertical(id="dialog"):
            yield Label(title, id="dialog-msg")
            yield OptionList(*[Option(t, id=i) for t, i in opts], id="menu-list")

    def on_mount(self):
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, e: OptionList.OptionSelected) -> None:
        self.dismiss(e.option.id)

    def action_cancel(self):
        self.dismiss(None)


class SessionTable(DataTable):
    # DataTable already binds enter→select_cursor (hidden) which posts RowSelected;
    # we just make it visible on the footer as "open tab". Doing it here — on the
    # focused widget — instead of an app-level priority binding is crucial: a
    # priority binding fires app-wide and would hijack Enter inside the modal
    # menus (selecting a menu option, submitting a rename), breaking them.
    # `right` (drill into the row → actions menu) is also bound here because the
    # table's own column-nav binding would otherwise shadow an app-level one.
    BINDINGS = [
        Binding("enter", "select_cursor", "open tab", key_display="enter"),
        Binding("right", "app.menu", "menu", show=False),  # app.* → App.action_menu
    ]


class Tuimux(App):
    COMMAND_PALETTE_BINDING = "p"  # open the command palette with p (not ctrl+p)
    CSS = """
    Screen { background: $surface; }
    #table-wrap {
        border: round $primary 40%;
        border-title-color: $primary;
        border-title-style: bold;
        border-title-align: center;
        border-subtitle-color: $text-muted;
        border-subtitle-align: right;
        padding: 1 2;
        margin: 1 2;
        height: 3fr;
    }
    DataTable { height: 1fr; background: $surface; }
    #preview {
        height: 2fr;
        margin: 0 2 1 2;
        padding: 0 1;
        border: round $primary 40%;
        border-title-color: $primary;
        border-title-style: bold;
        border-title-align: left;
        background: $surface;
        color: $text;
        overflow: hidden;
    }
    DataTable > .datatable--cursor { background: $primary 20%; color: $text; }
    DataTable > .datatable--header {
        color: $text-muted; text-style: bold; background: $surface;
    }
    #dialog {
        width: 56; height: auto; padding: 1 2; margin: 1;
        background: $panel; border: round $primary;
    }
    #dialog-msg { width: 1fr; padding: 0 0 1 0; content-align: center middle; }
    #dialog-input { margin-top: 1; }
    #menu-list {
        height: auto; max-height: 14; border: none;
        background: $panel; padding: 0;
    }
    #menu-list > .option-list--option { padding: 0 1; }
    #menu-list > .option-list--option-highlighted { background: $primary 25%; }
    ModalScreen { align: center middle; background: $background 70%; }
    """

    # Footer order: enter (shown by SessionTable) space w n d x v o u c r a q
    BINDINGS = [
        Binding("space", "menu", "menu"),
        Binding("w", "window", "new window"),
        Binding("n", "rename", "rename"),
        Binding("d", "detach", "detach"),
        Binding("x", "close", "close"),
        Binding("v", "preview", "preview"),
        Binding("o", "orgview", "org fleet"),
        Binding("u", "login", "set login"),
        Binding("c", "console", "tailscale"),
        Binding("r", "reload", "refresh"),
        Binding("a", "awake", "keep-awake"),
        Binding("q", "quit", "quit"),
        # hidden alias: m also opens the menu (right is bound on SessionTable)
        Binding("m", "menu", "menu", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.row_meta = []
        # [(name, is_local, status, lastseen)] from the last host scan
        self._hosts = []
        # host -> probe info, filled in incrementally as probes land
        self._results = {}
        # hosts with a probe currently in flight (avoid double-probing)
        self._probing = set()
        # (host, login) -> monotonic time of the last probe attempt, so a box that
        # refused SSH is retried only every RECHECK instead of every refresh.
        self._last_probe = {}
        # host -> last-known-good session list, kept so an offline machine can
        # still show what was running. Written only from a reachable probe;
        # never cleared when a host drops off (in-memory only, lost on restart).
        self._snap = {}
        # host -> (monotonic_time, {name: "resumed"|"lost"}): transient verdicts
        # shown briefly after a machine reconnects, then aged out (RECONCILE_TTL).
        self._reconcile = {}
        self._table = None  # cached DataTable handle (avoids repeated DOM queries)
        # last paint, for cheap diffing: overall signature, per-row keys, cells
        self._last_sig = None
        self._last_keys = []
        self._last_cells = []
        self._render_pending = False  # a coalesced render is already scheduled
        # terminal layout: [(win_index, tab_title)] and the window holding us
        self._windows = []
        self._self_win = None
        self._last_input = 0.0  # monotonic time of the last keypress (idle debounce)
        self._last_refresh = 0.0  # monotonic time of the last actual refresh
        self._preview = None  # cached Static handle for the live-preview panel
        # (host, session) currently mirrored in the preview, or None when the
        # cursor isn't on a live session. Guards stale peeks and avoids re-peeking
        # the same row on every re-render — only a real selection change re-kicks.
        self._preview_key = None
        self._preview_on = True  # v toggles the panel (and its peeking) off/on
        # "mine" = your own machines + hosts you've mapped; "org" = whole tailnet
        # fleet (every owner). o toggles it; session-only, not persisted.
        self._scope = "mine"
        # frame counter for the animated "running…" ellipsis (ticked by a timer)
        self._tick = 0

    def on_key(self, event):
        # Record activity so the periodic refresh can hold off while you navigate.
        # We don't consume the event — bindings and cursor movement still happen.
        self._last_input = time.monotonic()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="table-wrap") as w:
            w.border_title = "tuimux by Ersilia"
            yield SessionTable(cursor_type="row", zebra_stripes=False)
        yield Static(id="preview")
        yield Footer()

    def on_mount(self):
        self.title = "tuimux"
        self.sub_title = os.environ.get("TERM_PROGRAM", "terminal").lower()
        self._load_subtitle()  # fill in the login name off the UI thread
        self._capture_self_window()  # learn our own window id, for "new tab" targeting
        self._load_status_note()  # show autostart + mouse state in the panel border
        # On Linux X11, jumping to an already-open tab and the OPEN IN column
        # need wmctrl or xdotool — without them the dashboard can't see local
        # tabs at all, so every "open" silently spawns a fresh surface. Surface
        # this once at startup so users don't have to run `tuimux doctor` to
        # find out.
        if (
            sys.platform.startswith("linux")
            and os.environ.get("XDG_SESSION_TYPE", "x11") == "x11"
            and not shutil.which("wmctrl")
            and not shutil.which("xdotool")
        ):
            self.notify(
                "Install wmctrl (or xdotool) to jump to existing tabs — "
                "without it, every open spawns a new tab.",
                severity="warning",
                timeout=12,
            )
        for theme in ("tokyo-night", "catppuccin-mocha", "nord"):
            try:
                self.theme = theme
                break
            except Exception:
                continue
        t = self._table = self.query_one(SessionTable)
        # Fixed widths, sized to the widest realistic content in each column so the
        # table reads as evenly distributed (and the separator rule lines up):
        #   NAME    dot + hostname; longest real name ~23, sub-login rows a bit more
        #   STATUS  fixed words; longest is "checking…" (9)
        #   STATE   session state / "☕ awake" / last-seen ("2h ago")
        #   UPTIME  values are tiny ("2d"); the header sets the floor
        #   FOLDER  a short basename on session rows, doubling as the machine hint
        #           ("tailscale up --ssh", "no account · press u")
        #   TABS    "N  <active-cmd>"
        #   OPEN IN longest is "lost — not restored" (19)
        #   OWNER   "miquel@" / "tag:dev"        SSH USER  "mduranfrigola"
        for col, key, w in (
            ("NAME", "name", 26),
            ("STATUS", "status", 9),
            ("STATE", "state", 10),  # fits the animated "running…"
            ("UPTIME", "uptime", 6),
            ("FOLDER", "folder", 20),
            ("TABS", "tabs", 13),
            ("OPEN IN", "open", 20),
            ("OWNER", "owner", 10),
            ("SSH USER", "user", 14),
        ):
            t.add_column(col, key=key, width=w)
        t.focus()
        self._preview = self.query_one("#preview", Static)
        self._set_preview_idle()
        self.reload()
        # Heartbeat faster than REFRESH so we notice you've gone quiet promptly;
        # reload() itself enforces the REFRESH cadence and the idle debounce.
        self.set_interval(min(1.0, REFRESH), self.reload)
        # Animate the "running…" ellipsis. Cheap: it re-renders, but the diff only
        # repaints the STATE cells that actually change — a no-op when nothing runs.
        self.set_interval(0.5, self._tick_anim)

    def _tick_anim(self):
        self._tick += 1
        self._request_render()

    @work(thread=True)
    def _load_subtitle(self):
        login = _run(["__login"], timeout=HOSTS_TIMEOUT).stdout.strip()
        login = login or os.environ.get("USER", "")
        term = os.environ.get("TERM_PROGRAM", "terminal").lower()
        self.call_from_thread(
            setattr, self, "sub_title", f"{login} · {term}" if login else term
        )

    @work(thread=True)
    def _load_status_note(self):
        # One engine spawn for both toggle states (key=value lines) instead of a
        # separate `bash engine.sh` invocation per setting at startup.
        out = _run(["__settings"], timeout=HOSTS_TIMEOUT).stdout
        st = {}
        for ln in out.splitlines():
            k, sep, v = ln.partition("=")
            if sep:
                st[k.strip()] = v.strip()
        self.call_from_thread(
            self._set_status_note, st.get("autostart", ""), st.get("mouse", "")
        )

    def _set_status_note(self, auto, mouse):
        # Note in the panel's bottom border whether new terminals auto-attach and
        # whether tmux mouse mode (wheel scrolls the pane) is on.
        try:
            wrap = self.query_one("#table-wrap")
        except Exception:
            return
        wrap.border_subtitle = (
            f"autostart: {auto or 'off'}  ·  mouse scroll: {mouse or 'off'}"
        )

    @work(thread=True)
    def _capture_self_window(self):
        # Some terminals (Terminal.app) don't expose our window's title to the
        # engine, so "new tab" can't find the dashboard's window by name the way it
        # can on Ghostty. We capture our window id now — while we're the frontmost
        # window, just after launch — and pass it on _ENV so every later spawn can
        # bring this exact window forward before opening the tab. Empty (and thus a
        # no-op) on terminals that don't need it.
        wid = _run(["__selfwin"], timeout=WINDOWS_TIMEOUT).stdout.strip()
        if wid:
            _ENV["TUIMUX_SELF_WINID"] = wid

    # ---- data ----
    # Refresh is incremental and per-host. We fetch the host list, paint every
    # machine immediately (from cached results, or a "checking…" placeholder),
    # then probe each host in its OWN background worker and re-render as each
    # result lands. A slow host (e.g. one pinned at high load) only delays its
    # own row — fast hosts and the whole UI never wait on it. A host already
    # being probed is never re-probed, so refreshes can't pile up.
    def _modal_open(self):
        # A dialog (Menu/Confirm/Ask) is on top. Pause background refresh while
        # it is, so probe/scan subprocesses and table repaints don't compete with
        # the dialog for the main loop — that churn is what makes menus feel laggy.
        return len(self.screen_stack) > 1

    def reload(self):
        # Called on every heartbeat. Refresh only when it makes sense:
        #   • not before the table exists, and never while a dialog is up;
        #   • not while you're actively pressing keys (defer < QUIET since the
        #     last keystroke) — keeps navigation fluid;
        #   • no more often than REFRESH, so an idle dashboard still updates
        #     on its normal cadence rather than every heartbeat.
        if self._table is None or self._modal_open():
            return
        now = time.monotonic()
        if now - self._last_input < QUIET or now - self._last_refresh < REFRESH:
            return
        self._last_refresh = now
        self._load_hosts()
        # Keep the preview live: re-capture the selected session each refresh so
        # its panel tracks the real pane, not just a snapshot from when you landed.
        if self._preview_on and self._preview_key is not None:
            self._peek_one(self._preview_key)

    @work(exclusive=True, thread=True, group="hosts")
    def _load_hosts(self):
        hosts = fetch_hosts(self._scope)
        self.call_from_thread(self._kick, hosts)

    def _kick(self, hosts):
        self._hosts = hosts
        # Tell the engine which host is us, so per-host probes skip the tailscale
        # lookups self_host would otherwise run on every call.
        for h in hosts:
            if h[1]:  # is_local
                _ENV["TUIMUX_SELF_HOST"] = h[0]
                break
        self._render()  # show machines right away, before any probe returns
        now = time.monotonic()
        for h in hosts:
            name, kind, want_probe, login = (
                h[0],
                h[4],
                h[7],
                (h[9] if len(h) > 9 else ""),
            )
            # Phones/tablets (status only) and offline hosts (probe flag 0) are never
            # SSH'd. A host with several mapped logins fans out to several rows — probe
            # each (host,login) separately, since each login has its own tmux server.
            if kind == "consumer" or not want_probe:
                continue
            key = (name, login)
            if key in self._probing:
                continue
            # The default view probes the whole fleet to find boxes you can log into.
            # A box that already refused SSH is rechecked only every RECHECK seconds,
            # so teammate machines you have no account on don't get hammered each tick;
            # reachable (and busy) boxes still refresh every tick to track sessions.
            info = self._results.get(key)
            if (
                info is not None
                and not info.get("reachable")
                and not info.get("busy")
                and now - self._last_probe.get(key, 0.0) < RECHECK
            ):
                continue
            self._last_probe[key] = now
            self._probing.add(key)
            self._probe_one(h, key)
        self._scan_windows()  # refresh where sessions are open (local, off-thread)

    @work(thread=True, group="probe")
    def _probe_one(self, h, key):
        _host, info = _probe_or_offline(h)
        self.call_from_thread(self._got, key, info)

    def _got(self, key, info):
        if info.get("reachable"):
            prev = self._results.get(key)
            # Only an actual unreachable→reachable flip reconciles, so routine
            # refreshes never flash verdicts; compare against the snapshot taken
            # before it went offline, then refresh the snapshot to the new state.
            if prev is not None and not prev.get("reachable"):
                verdicts = _reconcile_sessions(
                    self._snap.get(key, []), info["sessions"]
                )
                if verdicts:
                    self._reconcile[key] = (time.monotonic(), verdicts)
            self._snap[key] = info["sessions"]
        self._results[key] = info
        self._probing.discard(key)
        self._request_render()

    @work(exclusive=True, thread=True, group="windows")
    def _scan_windows(self):
        # Read the terminal's window/tab layout so sessions can show where they're
        # open. Local + read-only (no focus stealing); falls back to nothing.
        out = _run(["__windows"], timeout=WINDOWS_TIMEOUT).stdout
        wins, self_win = [], None
        for ln in out.splitlines():
            idx, sep, title = ln.partition("|")
            if not sep:
                continue
            idx, title = idx.strip(), title.strip()
            wins.append((idx, title))
            # the dashboard's own window — its title is "tuimux" (one of the
            # em-dash components on Terminal.app, the whole title on Ghostty)
            if any(p.lower() == "tuimux" for p in _title_parts(title)):
                self_win = idx
        self.call_from_thread(self._set_windows, wins, self_win)

    def _set_windows(self, wins, self_win):
        self._windows = wins
        self._self_win = self_win
        self._request_render()

    # ---- live preview ----
    # A panel under the table mirrors the highlighted session's pane — a glimpse
    # of what it's actually doing without attaching. It updates as you move the
    # cursor (only on a real selection change) and on every heartbeat, so the
    # selected session stays live. Capture is read-only (tmux capture-pane), so
    # it never steals focus or disturbs the session.
    def on_data_table_row_highlighted(self, _event):
        self._sync_preview_target()

    def _sync_preview_target(self):
        # Point the preview at the current row, if it's a live session. Re-kick a
        # capture only when the target actually changes — re-renders that keep the
        # cursor on the same session shouldn't spawn a peek every time.
        m = self._cur()
        key = (
            (m["host"], m.get("login", ""), m["session"])
            if m and m.get("action") == "attach"
            else None
        )
        if key == self._preview_key:
            return
        self._preview_key = key
        if not self._preview_on:
            return
        if key is None:
            self._set_preview_idle()
        else:
            # Title updates immediately; the body fills in when the capture lands.
            self._preview.border_title = f" {key[2]} · {key[0]} "
            self._peek_one(key)

    @work(thread=True, exclusive=True, group="peek")
    def _peek_one(self, key):
        host, login, session = key
        raw = _run(["__peek", host, login, session], timeout=PEEK_TIMEOUT).stdout
        self.call_from_thread(self._show_preview, key, raw)

    def _show_preview(self, key, raw):
        # Ignore a capture that finished after the cursor moved on (exclusive
        # workers cancel queued peeks, but one already mid-SSH still returns).
        if not self._preview_on or key != self._preview_key:
            return
        # Show the most-recent lines that fit; size.height is 0 until first layout.
        height = self._preview.size.height or PREVIEW_LINES
        lines = _preview_tail(raw, height)
        if not lines:
            self._preview.update(Text("  (pane is empty)", style="dim italic"))
            return
        self._preview.update(Text.from_ansi("\n".join(lines)))

    def _set_preview_idle(self):
        self._preview.border_title = " preview "
        self._preview.update(
            Text("  Move to a session to preview its output.", style="dim italic")
        )

    def action_preview(self):
        self._preview_on = not self._preview_on
        self._preview.display = self._preview_on
        if self._preview_on:
            # Force a re-target: the key may be stale from while it was hidden.
            self._preview_key = None
            self._sync_preview_target()

    def _window_locs(self, session_name):
        """Labels for every terminal window *on this Mac* showing this session.

        tuimux-opened surfaces are titled "<session> · <window>" (tmux set-titles),
        so we match the part before " · " against the session name — per title
        component, so Terminal.app's "<login> — …" decorations don't get in the way.
        Returns a list (a session can be open in more than one local window); empty
        if it isn't open anywhere on this Mac."""
        locs = []
        for idx, title in self._windows:
            if any(
                p.split(" · ", 1)[0].strip() == session_name
                for p in _title_parts(title)
            ):
                if self._self_win is not None and idx == self._self_win:
                    locs.append("this window")
                else:
                    locs.append("other window")
        return locs

    def _open_in_cell(self, s, locs):
        """OPEN IN — where you can reach this session.

        If it's open as a tab on THIS Mac, say just that ("this window" /
        "other window") and stop: you can jump to it, and the host-side attachment
        is implied. Otherwise ("—", no local tab) report its attachment on the host
        that runs it — "N clients" if something holds it (e.g. a teammate on a
        shared box), else "detached". We never say "on host": for a local session
        the host *is* this Mac, which only ever read as confusing."""
        if "this window" in locs:
            return [("this window", "")]
        if locs:
            return [("other window", "dim")]
        n = s.get("nclients", 0)
        # No local tab, but a client may still hold it from elsewhere. Trust
        # attached too (a switch-client'd session can show attached with no
        # list-clients line); n only splits one client from many.
        if n > 1:
            host = (f"{n} clients", "dim")
        elif n == 1 or s.get("attached"):
            host = ("1 client", "dim")
        else:
            host = ("detached", "dim")
        return [("—", "dim"), (" · ", "dim"), host]

    @staticmethod
    def _owner_cell(owner, primary):
        """The gray OWNER cell: the tailnet account that owns the box, shown as its
        login handle + "@" (e.g. "miquel@") — the domain adds nothing on a
        single-org tailnet. Primary header only; a host's extra-login sub-rows share
        the same owner, so repeating it there would just be noise. Tag-owned nodes
        carry a tag instead of an account (e.g. "tag:dev", or the literal
        "tagged-devices" if the tag couldn't be resolved); those render bare, since
        "@" would imply a login handle that doesn't exist."""
        handle = (owner or "").split("@")[0]
        if not (primary and handle):
            return ()
        is_tag = handle.startswith("tag:") or handle == "tagged-devices"
        return ((handle if is_tag else handle + "@", "dim"),)

    @staticmethod
    def _login_cell(login, confirmed, mapping):
        """The gray SSH USER cell: the account tuimux logs in as here — shown only
        when it's real, i.e. a box we actually reached (`confirmed`) or one you've
        explicitly mapped a login for. On a box you can't reach and haven't mapped,
        the login is just the speculative default ($USER); showing it there would
        wrongly imply you have an account (e.g. on a teammate's machine)."""
        if not login or not (confirmed or mapping):
            return ()
        return ((login, "dim"),)

    @staticmethod
    def _tabs_cell(tabs):
        """TABS reads "<count>  <active-cmd>", or just "<active-cmd>" for a lone
        window (no count prefix). Tint the command violet when the active window is
        an AI agent (its command contains "claude")."""
        head, sep, cmd = tabs.partition("  ")
        if not sep:  # single window → the whole cell is the active command
            return ((tabs, VIOLET),) if "claude" in tabs.lower() else ((tabs, "dim"),)
        if "claude" in cmd.lower():
            return ((head + sep, "dim"), (cmd, VIOLET))
        return ((tabs, "dim"),)

    # Build the table as plain data — a list of (cells, meta) rows — so we can
    # diff cheaply and only repaint when something actually changed.
    #
    # Colour & emphasis convention (keep consistent when editing styles here):
    #   accent → the device's own colour, used as the identity thread: its machine
    #            NAME/STATUS *and* its session NAMEs. Within a device block this is
    #            the only hue — so each device reads as one calm colour family.
    #   bold   → the machine (device) NAME and its status dot (the dot is always
    #            bold; green=ssh, orange=online/no-ssh, hollow=offline). Nothing
    #            else is bold — not the STATUS word, not session names.
    #   amber  → the one thing that wants you: an agent STATE of "waiting" (plus
    #            keep-awake / no-ssh markers). Kept rare so it actually pops.
    #   violet → "claude" inside the TABS column (the active window is an AI agent).
    #   dim    → everything else: metadata (folder, tabs, uptime, OPEN IN, USER),
    #            idle/transient sessions, offline/"checking…" machines, hints.
    def _view(self):
        # Two buckets: the machines you can use right now (local + reachable, any
        # owner) go on TOP; everything else (your own phones/offline boxes, and —
        # only under "o" — other people's unreachable stuff) goes BELOW a thin line.
        top, bottom = [], []
        # Your tailnet owner, from the local row — decides what counts as "yours".
        my_owner = next((h[5] for h in self._hosts if h[1] and len(h) > 5), "")
        for h in self._hosts:
            # Tolerate short host tuples (owner/mapping/probe/color/login are newer
            # columns): default to no owner/login and "probe it" (pre-fleet behavior).
            host, is_local, status, _lastseen, kind = h[0], h[1], h[2], h[3], h[4]
            owner = h[5] if len(h) > 5 else ""
            mapping = h[6] if len(h) > 6 else ""
            accent = h[8] if len(h) > 8 else ""  # engine-computed color (may be "")
            login = h[9] if len(h) > 9 else ""  # this row's SSH user
            # primary = the host's first/only login; extras (a multi-login host)
            # come as their own rows with primary False, drawn as light sub-headers.
            primary = h[10] if len(h) > 10 else True
            key = (host, login)
            info = self._results.get(key)
            consumer = kind == "consumer"
            mine = owner == my_owner
            # "usable" = you can attach right now: this machine, or a box whose SSH
            # probe came back OK (busy counts — it answered, just slowly).
            usable = is_local or bool(
                info and (info.get("reachable") or info.get("busy"))
            )
            # Shown by default: everything that's yours (incl. your phone, offline
            # boxes) plus any box you can actually reach. Everything else — other
            # people's machines you can't use — appears only under "o" (org view),
            # below the separator.
            shown = mine or usable
            if not shown and self._scope != "org":
                continue
            bucket = top if shown else bottom
            machine = {
                "host": host,
                "login": login,
                "session": None,
                "action": "machine",
                "consumer": consumer,
                "mapping": mapping,
                "primary": primary,
            }
            dead = {**machine, "action": "none"}
            # Two gray identity cells for the header: OWNER (tailnet handle, primary
            # row only) and SSH USER (the login — only where it's a real account:
            # a box we reached, or one you've explicitly mapped).
            owner_c = self._owner_cell(owner, primary)
            login_c = self._login_cell(login, usable and not consumer, mapping)

            def hdr(marker_state, style, _host=host, _login=login, _primary=primary):
                # NAME cell for a machine header. Primary → dot + bold host name.
                # Secondary login → an indented, de-bolded "↳ <host>" sub-row that
                # groups it under the machine; the login itself shows in SSH USER.
                if _primary:
                    return (_marker(marker_state), (_host, style))
                return (
                    ("    ↳ ", "dim"),
                    (_host, style.replace("bold ", "") or "dim"),
                )

            if consumer:
                # phones/tablets are never SSH'd — a "device", not something you log
                # into. Calm gray (a computer that can't be reached is amber; a phone
                # that can't be reached is just what a phone is).
                if status == "offline":
                    seen = _lastseen
                    bucket.append(
                        _row(
                            dead,
                            name=hdr("offline", "dim"),
                            status=(("offline", "dim italic"),),
                            state=((seen, "dim"),) if seen else (),
                            owner=owner_c,
                            user=login_c,
                        )
                    )
                else:
                    bucket.append(
                        _row(
                            dead,
                            name=hdr("device", "dim"),
                            status=(("device", MUTED),),
                            owner=owner_c,
                            user=login_c,
                        )
                    )
            elif status == "offline":
                # Down (asleep / off the network / shut down): a hollow dot + last
                # seen. Decided by tailscale status, not by a probe — offline hosts
                # aren't probed, so info stays None for them.
                bucket.append(
                    _row(
                        dead,
                        name=hdr("offline", "dim"),
                        status=(("offline", "dim italic"),),
                        state=((_lastseen, "dim"),) if _lastseen else (),
                        owner=owner_c,
                        user=login_c,
                    )
                )
                # Its tmux sessions almost certainly aren't gone — just out of reach
                # until it's back. Show what was last running, dimmed, so you know
                # what's "paused" (truth only becomes knowable at reconnect; see
                # _reconcile_sessions). Non-interactive: there's nothing to attach.
                for s in self._snap.get(key, []):
                    bucket.append(
                        _row(
                            {
                                "host": host,
                                "login": login,
                                "session": s["name"],
                                "action": "none",
                            },
                            name=(("  ", ""), (s["auto"], "dim")),
                            state=((s["state"], "dim"),),
                            folder=((_folder(s["dir"]), "dim"),),
                            tabs=((s["tabs"], "dim"),),
                            open_in=(("unreachable", "dim italic"),),
                        )
                    )
            elif info is None:
                # not probed yet this refresh — neutral placeholder while we check
                bucket.append(
                    _row(
                        machine,
                        name=hdr("checking", "dim"),
                        status=(("checking…", "dim italic"),),
                        owner=owner_c,
                        user=login_c,
                    )
                )
            elif info.get("busy"):
                # SSH works, the host is just too loaded to answer in time.
                bucket.append(
                    _row(
                        dead,
                        name=hdr("warn", f"bold {AMBER}"),
                        status=(("busy", AMBER),),
                        folder=(("high load", "dim"),),
                        owner=owner_c,
                        user=login_c,
                    )
                )
            elif not info["reachable"]:
                # online on the tailnet, but no working SSH login. Your own box →
                # Tailscale SSH probably isn't enabled there; a teammate's box → you
                # have no account/permission on it. The hint says which to fix.
                hint = "tailscale up --ssh" if mine else "no account · press u"
                bucket.append(
                    _row(
                        dead,
                        name=hdr("warn", f"bold {AMBER}"),
                        status=(("no ssh", AMBER),),
                        folder=((hint, "dim"),),
                        owner=owner_c,
                        user=login_c,
                    )
                )
            else:
                # reachable — the NAME carries the machine's accent (local = teal;
                # each remote its own colour, matching the tmux bar), but the STATUS
                # is a consistent green "usable" signal, not the per-host accent.
                color = accent
                word = ("local" if is_local else "online", GREEN)
                bucket.append(
                    _row(
                        machine,
                        name=hdr("up", f"bold {color}"),
                        status=(word,),
                        state=(("☕ awake", AMBER),) if info["awake"] else (),
                        owner=owner_c,
                        user=login_c,
                    )
                )
                # Just-reconnected? Show for RECONCILE_TTL which sessions are the
                # same ones as before (resumed) and which are gone (lost).
                rec = self._reconcile.get(key)
                verdicts = (
                    rec[1] if rec and time.monotonic() - rec[0] < RECONCILE_TTL else {}
                )
                for s in info["sessions"]:  # its sessions, indented beneath it
                    # `open` means "we have a confirmed local tab to jump to" —
                    # the same signal that drives the OPEN IN cell. If we can't
                    # pin down the tab, the menu honestly offers a new tab
                    # instead of promising focus the engine can't deliver.
                    locs = self._window_locs(s["name"])
                    # Session name carries the device accent (never bold — only the
                    # machine name is). Dim when it's just an idle, unattached shell.
                    if s["state"] == "idle" and not s["attached"]:
                        nm_style = "dim"
                    else:
                        nm_style = color
                    # "running" gets a live ellipsis (running / running. / running.. /
                    # running…) that cycles on the animation tick — a quiet sign of
                    # activity. Other states are static.
                    st = s["state"]
                    st_word = st + "." * (self._tick % 4) if st == "running" else st
                    bucket.append(
                        _row(
                            {
                                "host": host,
                                "login": login,
                                "session": s["name"],
                                "action": "attach",
                                "open": bool(locs),
                            },
                            name=(("  ", ""), (s["auto"], nm_style)),
                            state=((st_word, _STATE_STYLE.get(st, "")),),
                            uptime=((s["uptime"], "dim"),),
                            folder=((_folder(s["dir"]), "dim"),),
                            tabs=self._tabs_cell(s["tabs"]),
                            open_in=tuple(self._open_in_cell(s, locs))
                            + (
                                (("  resumed", GREEN),)
                                if verdicts.get(s["name"]) == "resumed"
                                else ()
                            ),
                        )
                    )
                # Sessions that were here before the host dropped but didn't come
                # back — the host was shut down / tmux restarted, so they're gone.
                live = {s["name"] for s in info["sessions"]}
                for name, verdict in verdicts.items():
                    if verdict == "lost" and name not in live:
                        bucket.append(
                            _row(
                                {
                                    "host": host,
                                    "login": login,
                                    "session": name,
                                    "action": "none",
                                },
                                name=(("  ", ""), (name, "dim")),
                                open_in=(("lost — not restored", "dim italic"),),
                            )
                        )
                bucket.append(
                    _row(
                        {
                            "host": host,
                            "login": login,
                            "session": "__NEW__",
                            "action": "new",
                        },
                        name=(("  ＋ new session", "dim italic"),),
                    )
                )
        # usable machines on top; a thin line; then the rest (your own not-usable
        # devices always, plus — under "o" — everyone else's). The line only shows
        # when there's content on BOTH sides.
        rows = list(top)
        if top and bottom:
            rows.append(self._separator_row())
        rows.extend(bottom)
        return rows

    @staticmethod
    def _separator_row():
        """An inert, end-to-end divider between your devices and everyone else's
        (only ever shown under "o"). A thin rule across every column; not a real
        row you can act on (every action gates on action in (attach|new))."""
        # dashes sized to each column (must match the add_column widths above) so
        # the line reads as continuous across the whole table
        widths = {
            "name": 26,
            "status": 9,
            "state": 10,
            "uptime": 6,
            "folder": 20,
            "tabs": 13,
            "open_in": 20,
            "owner": 10,
            "user": 14,
        }
        meta = {"host": None, "login": "", "session": None, "action": "separator"}
        cells = {col: (("─" * w, "dim"),) for col, w in widths.items()}
        return _row(meta, **cells)

    @staticmethod
    def _cell(segments):
        txt = Text()
        for text, style in segments:
            txt.append(text, style=style or None)
        return txt

    def _request_render(self):
        # Coalesce bursts of updates (each probe result, each window scan) into a
        # single _view() rebuild. Without this, N probes landing in a cycle trigger
        # N full row-model recomputes on the UI thread; here they collapse into one.
        if self._render_pending:
            return
        self._render_pending = True
        self.set_timer(0.05, self._render_now)

    def _render_now(self):
        self._render_pending = False
        self._render()

    def _render(self):
        # Skip painting the (hidden) table while a dialog is up — keeps menus
        # responsive; the table refreshes when the dialog closes.
        if self._modal_open():
            return
        # Three tiers, cheapest first:
        #   1. nothing changed            → do nothing
        #   2. same rows, cells differ    → patch only the changed cells in place
        #      (no clear, no cursor reset, no flicker)
        #   3. rows added/removed/moved   → rebuild once inside a batch
        rows = self._view()
        cells_only = [cells for cells, _ in rows]
        sig = tuple(cells_only)
        if sig == self._last_sig:
            return
        keys = [(m["host"], m.get("login", ""), m["session"]) for _, m in rows]
        t = self._table

        if keys == self._last_keys and t.row_count == len(rows):
            for r, cells in enumerate(cells_only):
                old = self._last_cells[r]
                for c, cell in enumerate(cells):
                    if cell != old[c]:
                        t.update_cell_at(Coordinate(r, c), self._cell(cell))
        else:
            prev = t.cursor_row
            sel = self._cur()
            sel_id = (
                (sel["host"], sel.get("login", ""), sel["session"]) if sel else None
            )
            with self.batch_update():
                t.clear()
                t.add_rows(tuple(self._cell(c) for c in cells) for cells in cells_only)
            if rows:
                # keep the cursor on the same logical row, not the same index —
                # rows shift as machines resolve and sessions come and go.
                idx = next((i for i, k in enumerate(keys) if k == sel_id), None)
                t.move_cursor(row=idx if idx is not None else min(prev, len(rows) - 1))

        self.row_meta = [meta for _, meta in rows]
        self._last_sig = sig
        self._last_keys = keys
        self._last_cells = cells_only

    def _cur(self):
        t = self._table
        if t is not None and 0 <= t.cursor_row < len(self.row_meta):
            return self.row_meta[t.cursor_row]
        return None

    def _spawn(self, args):
        subprocess.Popen(["bash", ENGINE, *args], env=_ENV)

    @work(thread=True)
    def _act(self, args):
        # Run a mutating engine command (detach/kill/rename/awake) off the UI
        # thread so a slow host can't freeze the TUI, then refresh once it's done.
        _run(args, timeout=ACTION_TIMEOUT)
        self.call_from_thread(self.reload)

    # ---- actions ----
    def on_data_table_row_selected(self, _):
        self.action_open()

    def action_menu(self):
        m = self._cur()
        if not m or m["action"] not in ("attach", "new"):
            return
        handlers = {
            "window": self.action_window,
            "rename": self.action_rename,
            "detach": self.action_detach,
            "close": self.action_close,
            "open": self.action_open,
        }

        def chosen(act):
            if act in handlers:
                handlers[act]()

        self.push_screen(Menu(m), chosen)

    def action_open(self):
        m = self._cur()
        if m and m["action"] in ("attach", "new"):
            self._spawn(
                [
                    "__open",
                    "auto",
                    m["host"],
                    m.get("login", ""),
                    m["session"],
                    m["action"],
                ]
            )

    def action_window(self):
        m = self._cur()
        if m and m["action"] in ("attach", "new"):
            self._spawn(
                [
                    "__open",
                    "window",
                    m["host"],
                    m.get("login", ""),
                    m["session"],
                    m["action"],
                ]
            )

    def action_detach(self):
        m = self._cur()
        if m and m["action"] == "attach":
            self._act(["__detach", m["host"], m.get("login", ""), m["session"]])

    def action_close(self):
        m = self._cur()
        if not m or m["action"] != "attach":
            return

        def done(ok):
            if ok:
                self._act(["__killraw", m["host"], m.get("login", ""), m["session"]])

        self.push_screen(
            Confirm(
                f"Shut down “{m['session']}” on {m['host']}?\nEverything running in it is killed."
            ),
            done,
        )

    def action_rename(self):
        m = self._cur()
        if not m or m["action"] not in ("attach", "new"):
            return
        if m["action"] == "new":

            def made(name):
                if name:
                    self._spawn(
                        ["__open", "tab", m["host"], m.get("login", ""), name, "new"]
                    )

            self.push_screen(Ask("Name for the new session:"), made)
        else:

            def renamed(name):
                if name:
                    self._act(
                        [
                            "__renameto",
                            m["host"],
                            m.get("login", ""),
                            m["session"],
                            name,
                        ]
                    )

            self.push_screen(Ask(f"Rename “{m['session']}” to:", m["session"]), renamed)

    def action_console(self):
        # open the Tailscale admin console in the browser — no row needed
        self._spawn(["__console"])

    def action_awake(self):
        m = self._cur()
        if m and m["host"]:
            self._act(["__awaketoggle", m["host"], m.get("login", "")])

    def action_orgview(self):
        # Flip between the access view (machines you can SSH into) and the whole
        # tailnet fleet. Scope is now purely a display filter over already-fetched,
        # already-probed data, so just re-render — no re-fetch/re-probe needed, which
        # makes the toggle instant.
        self._scope = "org" if self._scope == "mine" else "mine"
        self.notify(
            "showing all devices" if self._scope == "org" else "my machines",
            timeout=2,
        )
        self._request_render()

    def action_login(self):
        # Set/clear the SSH username(s) tuimux uses for the highlighted host. The
        # login map is per host (not per login sub-row), so this acts on the host
        # regardless of which login row the cursor is on. Multiple comma-separated
        # users are allowed — each is probed as its own tmux server; the first is
        # the connect-as default.
        m = self._cur()
        if not m or not m.get("host") or m.get("consumer"):
            return
        host = m["host"]
        # the raw comma-list currently mapped for the host (empty if none)
        current = _run(["__loginsfor", host], timeout=HOSTS_TIMEOUT).stdout.strip()

        def chosen(users):
            # Empty clears the mapping (engine falls back to the default login);
            # otherwise set the whole comma-list. Reload either way.
            self._act(["__setlogin", host, users])

        self.push_screen(
            Ask(
                f"SSH login(s) for {host} — comma-separated, blank = default:",
                current,
            ),
            chosen,
        )


def _disposable_tmux_session():
    """Name of the current tmux session iff it's a throwaway worth removing
    rather than leaving detached: a single-pane, single-window session with no
    other client attached — i.e. the kind `autostart` spins up for a fresh
    terminal. Anything with more structure (extra windows/panes, or a client
    attached elsewhere) returns None and is only ever detached, never killed, so
    real work is never destroyed."""
    fmt = "#{session_name}\t#{session_windows}\t#{window_panes}\t#{session_attached}"
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", fmt],
            capture_output=True,
            text=True,
        ).stdout.strip()
    except OSError:
        return None
    parts = out.split("\t")
    # 1 window, 1 pane, exactly this one client → safe to discard
    return parts[0] if len(parts) == 4 and parts[1:] == ["1", "1", "1"] else None


def run():
    """Entry point: launch the dashboard. tuimux must run *outside* tmux (it
    drives tmux; nesting it inside a session is meaningless). So if you type
    `tuimux` from within a tmux client, detach that client and relaunch the
    dashboard in the SAME terminal window: `detach-client -E` replaces the
    client with our command. If the session we were in is a throwaway (a lone
    autostart shell), the relaunch also kills it — running from the freed
    terminal, so it's safe — instead of leaving it cluttering the dashboard.
    Falls back to a plain message if the handoff can't be performed."""
    if os.environ.get("TMUX"):
        sess = _disposable_tmux_session()
        cleanup = (
            f"tmux kill-session -t {shlex.quote(sess)} 2>/dev/null; " if sess else ""
        )
        # Quote each argv token separately: relaunch_argv() may be a multi-word
        # `python -m tuimux`, which a single shlex.quote() would mangle into one
        # bogus "python -m tuimux" command name.
        cmd = " ".join(shlex.quote(a) for a in relaunch_argv())
        relaunch = f"{cleanup}TUIMUX_NO_AUTOTMUX=1 exec {cmd}"
        try:
            ok = (
                subprocess.run(
                    ["tmux", "detach-client", "-E", relaunch],
                    stderr=subprocess.DEVNULL,
                ).returncode
                == 0
            )
        except OSError:
            ok = False
        if ok:
            # detach-client returns 0 once the detach is queued — before the -E
            # command execs — so this confirms the handoff started, not that the
            # dashboard came up. A broken tuimux_bin() would leave a bare shell;
            # that's inherent to -E and the same failure you'd get launching by hand.
            return
        raise SystemExit(
            "Don't run tuimux inside tmux — open it in a plain terminal tab."
        )
    Tuimux().run()


if __name__ == "__main__":
    run()
