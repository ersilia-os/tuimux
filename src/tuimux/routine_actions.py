"""What you can do to routines: run, mark done, add, edit, copy, delete, pause."""

import time

from textual import work

from . import routines
from .hosts import (
    _ENV,
    _unattended,
    host_read,
)
from .routine_cells import _routine_options
from .screens import (
    Confirm,
    Menu,
    RoutineForm,
    edit_in_editor,
)


class RoutineActions:
    """The routines panel's key actions (mixed into Tuimux)."""

    def action_routine_done(self):
        """Mark the current run of the highlighted routine done."""
        r = self._cur_routine()
        if r is None:
            return
        login, info, sess = self._routine_info(r)
        entry = self._rstate.get(r["name"]) or {}
        if sess is None:
            if entry.get("last") and not entry.get("done"):
                self._finish_run(r, login, time.time())
                self._request_render()
            return

        def ok(yes):
            if yes:
                self._finish_run(r, login, time.time())
                self._act("kill", r["host"], login, sess["name"])

        self.push_screen(
            Confirm(
                f"Mark “{r['name']}” done?\nIts session on {r['host']} is closed.",
                ok_label="✓   yes, it's done",
            ),
            ok,
        )

    def action_routine_menu(self):
        """Show the actions for the highlighted routine (space)."""
        r = self._cur_routine()
        if r is None:
            return
        open_run = self._routine_info(r)[2] is not None
        handlers = {
            "run": self.action_routine_run,
            "done": self.action_routine_done,
            "edit": self.action_routine_edit,
            "copy": self.action_routine_copy,
            "toggle": self.action_routine_toggle,
            "delete": self.action_routine_delete,
        }

        def chosen(act):
            if act in handlers:
                handlers[act]()

        self.push_screen(Menu(r["name"], _routine_options(r, open_run)), chosen)

    def action_routine_run(self):
        """Run the highlighted routine now, or open its running session."""
        r = self._cur_routine()
        if r is None or r["name"] in self._launching:
            return
        login, info, sess = self._routine_info(r)
        if sess is not None:  # already running: just go to it
            self._mark_routines(r["name"], opened=True)
            self._spawn(["__open", "auto", r["host"], login, sess["name"], "attach"])
            self._after_open()
            return
        self._launching.add(r["name"])
        self._request_render()
        self._launch(r, login, not _unattended(r))  # unattended: no tab to open

    def _compute_hosts(self, also=""):
        names = []
        for h in self._hosts:
            if h[4] != "consumer" and h[0] not in names:
                names.append(h[0])
        if also and also not in names:
            names.insert(0, also)
        return names or ["?"]

    def action_routine_new(self):
        """Add a routine on the highlighted computer."""
        # on a computer's header row, or one of its routines: that computer;
        # otherwise this one
        here = self._cur_rmeta()
        host = _ENV.get("TUIMUX_SELF_HOST", "")
        if here and here[0] == "host":
            host = here[1]
        elif self._cur_routine():
            host = self._cur_routine()["host"]
        blank = {
            "name": "",
            "host": host,
            "login": "",
            "folder": "~",
            "schedule": "weekly mon 09:00",
            "enabled": True,
            "ask_done": True,
            "mode": "interactive",
            "allow": "",
            "prompt": "",
        }

        def done(r):
            if r:
                self._routines.append(r)
                self._rstate[r["name"]] = routines.new_entry(time.time())
                self._save_state()
                self._save_routines()

        taken = {x["name"] for x in self._routines}
        self.push_screen(
            RoutineForm(
                blank,
                self._compute_hosts(also=host),
                taken,
                check_claude=self._check_claude,
            ),
            done,
        )

    def action_routine_copy(self):
        """Duplicate the highlighted routine under a free name."""
        src = self._cur_routine()
        if src is None:
            return
        taken = {x["name"] for x in self._routines}
        name, n = f"{src['name']}-copy", 2
        while name in taken:
            name, n = f"{src['name']}-copy-{n}", n + 1
        # paused, so the copy can't fire before you've adjusted it
        copy = {**src, "name": name, "enabled": False}

        def done(r):
            if r:
                self._routines.append(r)
                self._rstate[r["name"]] = routines.new_entry(time.time())
                self._save_state()
                self._save_routines()
                self.notify(f"{r['name']} added, paused — space resumes it", timeout=4)

        self.push_screen(
            RoutineForm(
                copy,
                self._compute_hosts(also=src["host"]),
                taken,
                title=f"duplicate {src['name']}",
                check_claude=self._check_claude,
            ),
            done,
        )

    def action_routine_editor(self):
        """Edit the highlighted routine's prompt in $EDITOR."""
        r = self._cur_routine()
        if r is None:
            return
        text = edit_in_editor(self, r["prompt"])
        if text is not None and text.strip() and text.strip("\n") != r["prompt"]:
            r["prompt"] = text.strip("\n")
            self._save_routines()
            self.notify(f"{r['name']}: prompt saved", timeout=2)

    def _check_claude(self, host, callback):
        # Where claude lives on `host` — remembered for the session once known.
        if host in self._claude_where:
            callback(host, self._claude_where[host])
            return
        self._claude_check(host, callback)

    @work(thread=True, group="claude-check")
    def _claude_check(self, host, callback):
        out = host_read("claude_check", host, self._host_login(host))
        if out is None:
            where = None  # couldn't reach it: say so, and check again next time
        else:
            last = (out.strip().splitlines() or [""])[-1]
            where = "" if last in ("", "__NONE__") else last
        self.call_from_thread(self._claude_checked, host, where, callback)

    def _claude_checked(self, host, where, callback):
        if where is not None:
            self._claude_where[host] = where
        callback(host, where)

    def action_routine_edit(self):
        """Edit the highlighted routine in the form."""
        old = self._cur_routine()
        if old is None:
            return

        def done(r):
            if not r:
                return
            i = self._routines.index(old)
            self._routines[i] = r
            if r["name"] != old["name"]:  # carry its run history over
                self._rstate[r["name"]] = self._rstate.pop(
                    old["name"], routines.new_entry(time.time())
                )
                self._save_state()
            self._save_routines()

        taken = {x["name"] for x in self._routines} - {old["name"]}
        hosts = self._compute_hosts(also=old["host"])
        self.push_screen(
            RoutineForm(old, hosts, taken, check_claude=self._check_claude), done
        )

    def action_routine_delete(self):
        """Delete the highlighted routine, after asking."""
        r = self._cur_routine()
        if r is None:
            return

        def done(ok):
            if ok and r in self._routines:
                self._routines.remove(r)
                self._rstate.pop(r["name"], None)
                self._save_state()
                self._save_routines()

        self.push_screen(
            Confirm(
                f"Delete routine “{r['name']}”?\n"
                "Its session, if one is open, keeps running.",
                ok_label="✕   yes, delete it",
            ),
            done,
        )

    def action_routine_toggle(self):
        """Pause or resume the highlighted routine."""
        r = self._cur_routine()
        if r is None:
            return
        r["enabled"] = not r["enabled"]
        self._save_routines()
        self.notify(f"{r['name']} {'resumed' if r['enabled'] else 'paused'}", timeout=4)
