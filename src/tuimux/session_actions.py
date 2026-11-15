"""What you can do to sessions and machines: open, detach, close, rename."""

import subprocess
import time

from textual import work

from .hosts import (
    _ENV,
    ACTION_TIMEOUT,
    ENGINE,
    HOSTS_TIMEOUT,
    _run,
    host_action,
)
from .screens import (
    Ask,
    Confirm,
    Menu,
)


class SessionActions:
    """The sessions table's key actions (mixed into Tuimux)."""

    def _cur(self):
        t = self._table
        if t is not None and 0 <= t.cursor_row < len(self.row_meta):
            return self.row_meta[t.cursor_row]
        return None

    def _spawn(self, args):
        subprocess.Popen(["bash", ENGINE, *args], env=_ENV)

    def _act(self, kind, host, login, *args):
        # Show the action on its row at once ("closing…", the new name, …); the
        # re-probe after it finishes confirms the real outcome.
        session = None if kind == "awake" else args[0]
        new_name = args[1] if kind == "rename" else None
        self._pending[(host, login, session)] = (kind, new_name, time.monotonic())
        self._request_render()
        self._act_worker(kind, host, login, *args)

    @work(thread=True)
    def _act_worker(self, kind, host, login, *args):
        # Run a mutating action (detach/kill/rename/awake) off the UI thread so a
        # slow host can't freeze the TUI, then re-probe that host at once. Tabs
        # close/retitle a moment later, so re-read the window titles after that.
        host_action(kind, host, login, *args)
        self.call_from_thread(self._reprobe, host, login)
        self.call_from_thread(self.set_timer, 1.0, self._rescan_windows)

    def _after_open(self):
        # A new tab takes a moment to appear and retitle (ssh + tmux attach);
        # look twice so OPEN IN catches up without waiting for the slow cadence.
        self.set_timer(2.0, self._rescan_windows)
        self.set_timer(5.0, self._rescan_windows)

    # ---- actions ----
    def on_data_table_row_selected(self, event):
        """Enter: open the session, or run the routine."""
        if event.data_table is self._rtable:
            self.action_routine_run()
        else:
            self.action_open()

    def action_menu(self):
        """Show the actions for the highlighted session."""
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
        """Open the highlighted session in a tab (focus it if already open)."""
        m = self._cur()
        if m and m["action"] in ("attach", "new"):
            # "auto" first looks for an existing tab to focus (an AppleScript walk);
            # skip it when we already know the session isn't open here.
            self._spawn(
                [
                    "__open",
                    "auto" if m.get("open") else "tab",
                    m["host"],
                    m.get("login", ""),
                    m["session"],
                    m["action"],
                ]
            )
            self._after_open()

    def action_window(self):
        """Open the highlighted session in a new window."""
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
            self._after_open()

    def action_detach(self):
        """Detach every client from the highlighted session."""
        m = self._cur()
        if m and m["action"] == "attach":
            self._act("detach", m["host"], m.get("login", ""), m["session"])

    def action_close(self):
        """Close the highlighted session, after asking."""
        m = self._cur()
        if not m or m["action"] != "attach":
            return

        def done(ok):
            if ok:
                self._act("kill", m["host"], m.get("login", ""), m["session"])

        self.push_screen(
            Confirm(
                f"Shut down “{m['session']}” on {m['host']}?\nEverything running in it is killed."
            ),
            done,
        )

    def action_rename(self):
        """Rename the highlighted session (or name a new one)."""
        m = self._cur()
        if not m or m["action"] not in ("attach", "new"):
            return
        if m["action"] == "new":

            def made(name):
                if name:
                    self._spawn(
                        ["__open", "tab", m["host"], m.get("login", ""), name, "new"]
                    )
                    self._after_open()

            self.push_screen(Ask("Name for the new session:"), made)
        else:

            def renamed(name):
                if name:
                    self._act(
                        "rename", m["host"], m.get("login", ""), m["session"], name
                    )

            self.push_screen(Ask(f"Rename “{m['session']}” to:", m["session"]), renamed)

    def action_console(self):
        """Open the Tailscale admin console in the browser."""
        # open the Tailscale admin console in the browser — no row needed
        self._spawn(["__console"])

    def action_awake(self):
        """Toggle keep-awake on the highlighted machine."""
        m = self._cur()
        if m and m["host"]:
            self._act("awake", m["host"], m.get("login", ""))

    def action_orgview(self):
        """Switch between your machines and the whole tailnet."""
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
        """Set the SSH user(s) tuimux uses for the highlighted machine."""
        # Set/clear the SSH username(s) tuimux uses for the highlighted host. The
        # login map is per host (not per login sub-row), so this acts on the host
        # regardless of which login row the cursor is on. Multiple comma-separated
        # users are allowed — each is probed as its own tmux server; the first is
        # the connect-as default.
        m = self._cur()
        if not m or not m.get("host") or m.get("consumer"):
            return
        self._ask_login(m["host"])

    @work(thread=True)
    def _ask_login(self, host):
        # the raw comma-list currently mapped for the host (empty if none)
        current = _run(["__loginsfor", host], timeout=HOSTS_TIMEOUT).stdout.strip()
        self.call_from_thread(self._push_login_dialog, host, current)

    def _push_login_dialog(self, host, current):
        def chosen(users):
            # Empty clears the mapping (engine falls back to the default login);
            # otherwise set the whole comma-list. Reload either way.
            self._set_login(host, users)

        self.push_screen(
            Ask(
                f"SSH login(s) for {host} — comma-separated, blank = default:",
                current,
            ),
            chosen,
        )

    @work(thread=True)
    def _set_login(self, host, users):
        _run(["__setlogin", host, users], timeout=ACTION_TIMEOUT)
        # The host list itself changes (new login rows), so refresh everything now.
        self.call_from_thread(self.reload, True)
