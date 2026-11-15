#!/usr/bin/env python3
"""tuimux — a Textual dashboard for tmux sessions across your tailnet.

The heavy lifting (Tailscale discovery, tmux/ssh probing, terminal tab spawning,
keep-awake, claude/state detection) lives in the bundled `engine.sh`; this is
purely the front-end, calling it for data and actions.
"""

import os
import shutil
import sys
import time

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Static,
)

from . import hosts
from .fleet import (
    Fleet,
)
from .hosts import (
    _ENV,
    HOSTS_TIMEOUT,
    REFRESH,
    SNAPSHOT,
    WINDOWS_TIMEOUT,
    _run,
    load_snapshot,
    save_snapshot,
)
from .preview import (
    Preview,
)
from .routine_actions import (
    RoutineActions,
)
from .routine_cells import (
    _pending_note,
)
from .routines_panel import (
    RoutinesPanel,
)
from .screens import (
    RoutineTable,
    SessionTable,
)
from .session_actions import (
    SessionActions,
)
from .sessions_view import (
    SessionsView,
)
from .theme import (
    AMBER,
    APP_CSS,
    ROUTINE_COLUMNS,
    SESSION_COLUMNS,
    THEME,
    _span_width,
)

# Session-table actions, hidden from the footer while the routines panel has focus.
_SESSION_ACTIONS = {"menu", "window", "rename", "detach", "close", "awake", "login"}


class Tuimux(
    Fleet, Preview, SessionsView, SessionActions, RoutinesPanel, RoutineActions, App
):
    """The tuimux dashboard: sessions across the tailnet, and your routines."""

    COMMAND_PALETTE_BINDING = "p"  # open the command palette with p (not ctrl+p)
    CSS = APP_CSS

    # Footer order: enter (shown by SessionTable) space w n d x v o u c r a q
    BINDINGS = [
        Binding("space", "menu", "menu"),
        Binding("w", "window", "new window"),
        Binding("n", "rename", "rename"),
        Binding("d", "detach", "detach"),
        Binding("x", "close", "close"),
        Binding("v", "panel", "panel"),
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
        self._last_windows = 0.0  # monotonic time of the last window/tab scan
        self._peek_timer = None  # pending debounced preview capture
        # (host, login, session) -> last pane capture, shown instantly on return
        self._peek_cache = {}
        # (host, login, session|None) -> (kind, new_name, monotonic start) for an
        # action in flight; the row shows it until a later probe lands.
        self._pending = {}
        # routines: the registry, run state, names being launched right now, the
        # registry's mtime (to pick up hand edits), and the panel's last paint
        self._routines = []
        self._rmeta = []  # per routines-table row: ("host", name) or ("routine", name)
        self._rstate = {}
        self._launching = set()
        # name -> (monotonic time, reason) of a failed start, for the retry backoff
        self._failed = {}
        self._claude_where = {}  # host -> where claude lives there ("" = not found)
        self._routines_mtime = None
        self._rtable = None
        self._last_rsig = None
        self._preview = None  # cached Static handle for the live-preview panel
        # (host, session) currently mirrored in the preview, or None when the
        # cursor isn't on a live session. Guards stale peeks and avoids re-peeking
        # the same row on every re-render — only a real selection change re-kicks.
        self._preview_key = None
        # what the bottom panel shows — "preview", "routines" or "none" (v cycles)
        self._panel = "preview"
        self._status_note = ""  # autostart / mouse state for the sessions border
        # "mine" = your own machines + hosts you've mapped; "org" = whole tailnet
        # fleet (every owner). o toggles it; session-only, not persisted.
        self._scope = "mine"
        # frame counter for the animated "running…" ellipsis (ticked by a timer)
        self._tick = 0

    @property
    def _preview_on(self):
        # the live preview (and its pane captures) runs only while it's shown
        return self._panel == "preview"

    def check_action(self, action, parameters):
        """Hide the session keys while the routines panel has focus."""
        # While the routines panel has focus, the session keys don't apply (and
        # drop out of the footer) — its own n/e/x/space take over.
        if action in _SESSION_ACTIONS and self.focused is not None:
            if self.focused is self._rtable:
                return False
        return True

    def on_descendant_focus(self, event):
        """Point the preview and the footer at the table you moved into."""
        # The preview follows whichever table you're in.
        if self._rtable is None or not isinstance(event.widget, DataTable):
            return
        self.refresh_bindings()
        if event.widget is self._rtable:
            self._sync_routine_detail()
        else:
            self._preview_key = ("",)  # force a re-target
            self._sync_preview_target()

    def on_unmount(self):
        """Save the snapshot for the next launch and close the SSH shells."""
        save_snapshot(SNAPSHOT, self._hosts, self._snap, self._panel)
        if hosts._POOL is not None:  # read live: _pool() rebinds it on first use
            hosts._POOL.close_all()

    def _restore_snapshot(self):
        # Paint the last-known table before any probe: online hosts get their
        # remembered sessions as a "stale" result (dimmed, still actionable) that
        # the first real probe replaces.
        hosts, snap, panel = load_snapshot(SNAPSHOT)
        self._set_panel(panel, save=False)  # reopen in the view you left
        if not hosts:
            return
        self._hosts, self._snap = hosts, snap
        for h in hosts:
            key = (h[0], h[9])
            if h[2] != "offline" and key in snap:
                self._results[key] = {
                    "reachable": True,
                    "busy": False,
                    "notmux": False,
                    "awake": False,
                    "sessions": snap[key],
                    "stale": True,
                }
        self._render()

    def on_app_focus(self, _event: events.AppFocus):
        """Rescan terminal windows when you come back to the dashboard."""
        # Back from another tab/window: tabs may have opened or closed meanwhile.
        self._rescan_windows()

    def on_key(self, event):
        """Note the keypress, so the refresh holds off while you navigate."""
        # Record activity so the periodic refresh can hold off while you navigate.
        # We don't consume the event — bindings and cursor movement still happen.
        self._last_input = time.monotonic()

    def compose(self) -> ComposeResult:
        """Lay out the sessions table, the bottom panel and the footer."""
        yield Header(show_clock=True)
        with Vertical(id="table-wrap") as w:
            w.border_title = "tuimux by Ersilia"
            yield SessionTable(
                cursor_type="row",
                zebra_stripes=False,
                cursor_foreground_priority="renderable",
            )
        # the bottom panel: one of these is shown at a time (v cycles; see PANELS)
        yield Static(id="preview")
        with Vertical(id="routines-wrap") as w:
            w.border_title = "routines"
            yield RoutineTable(
                cursor_type="row",
                zebra_stripes=False,
                cursor_foreground_priority="renderable",
            )
            yield Static(id="routine-detail")
        yield Footer()

    def on_mount(self):
        """Set up the theme and tables, restore the snapshot and start the timers."""
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
        self.register_theme(THEME)
        self.theme = THEME.name
        t = self._table = self.query_one(SessionTable)
        for key, col, w in SESSION_COLUMNS:
            t.add_column(col, key=key, width=w)
        t.focus()
        r = self._rtable = self.query_one(RoutineTable)
        for key, col, spans in ROUTINE_COLUMNS:
            r.add_column(col, key=key, width=_span_width(spans))
        self._load_routines()
        self._preview = self.query_one("#preview", Static)
        self._set_preview_idle()
        self._restore_snapshot()
        self.reload()
        # Heartbeat faster than REFRESH so we notice you've gone quiet promptly;
        # reload() itself enforces the REFRESH cadence and the idle debounce.
        self.set_interval(min(1.0, REFRESH), self.reload)
        # Animate the "running…" ellipsis. Cheap: it re-renders, but the diff only
        # repaints the STATE cells that actually change — a no-op when nothing runs.
        self.set_interval(0.5, self._tick_anim)
        # Start due routines (also run as each host's probe lands, see _got).
        self.set_interval(30, self._tick_routines)

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
        self._status_note = (
            f"autostart: {auto or 'off'}  ·  mouse scroll: {mouse or 'off'}"
        )
        self._refresh_badge()

    def _refresh_badge(self, pending=None):
        # Routines that want you, flagged on the sessions box's border while the
        # routines view isn't showing — so they reach you from any view.
        try:
            wrap = self.query_one("#table-wrap")
        except Exception:
            return
        if pending is None:
            pending = self._pending_routines(time.time())
        note = self._status_note
        if pending and self._panel != "routines":
            badge = f"[blink bold {AMBER}]routines: {_pending_note(pending)} (v)[/]"
            note = f"{badge}  ·  {note}" if note else badge
        wrap.border_subtitle = note

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
        self._render_routines()

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


def run():
    """Entry point: launch the dashboard. tuimux must run *outside* tmux — it
    drives tmux, so nesting it inside a session is meaningless. Rather than try to
    escape the session on the user's behalf (fragile), refuse and point them at
    `tuimux detach`, which frees THIS terminal while the session keeps running in
    the background — so they can just reopen the dashboard right here."""
    if os.environ.get("TMUX"):
        raise SystemExit(
            "tuimux can't run inside tmux — it drives tmux, so a dashboard nested in\n"
            "a session can't work. Detach this terminal first, then reopen it:\n"
            "\n"
            "    tuimux detach     (your tmux session keeps running in the background)\n"
            "    tuimux"
        )
    Tuimux().run()


if __name__ == "__main__":
    run()
