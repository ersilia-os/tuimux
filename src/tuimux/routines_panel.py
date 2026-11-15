"""The routines panel: its rows, status words, detail card and actions."""

import os
import time
from datetime import datetime

from rich.text import Text
from textual import work
from textual.coordinate import Coordinate
from textual.widgets import (
    Static,
)

from . import routines
from .fmt import (
    _ago,
    _when,
)
from .hosts import (
    ROUTINE_RETRY,
    _unattended,
    host_read,
    launch_routine,
)
from .routine_cells import (
    _keep_cursor,
    _parse_outcome,
    _routine_detail,
    _routine_groups,
    _routine_status,
    _routine_summary,
)
from .theme import (
    AMBER,
    GREEN,
    _marker,
)


class RoutinesPanel:
    """The routines panel's state, rendering and actions (mixed into Tuimux)."""

    # ---- routines ----
    # A second panel of Claude prompts you run on a schedule, each on one fixed
    # computer. The dashboard is the scheduler: while it's open (and on opening
    # it) a due routine starts as a detached session on its computer, with
    # claude already running the prompt, and waits there for you. At most one
    # session per routine; however many runs were missed, it starts once.
    def _load_routines(self):
        try:
            self._routines = routines.load_registry()
        except ValueError as err:
            self._routines = []
            self.notify(f"routines: {err}", severity="error", timeout=10)
        self._routines_mtime = self._registry_mtime()
        self._rstate = routines.load_state()
        now = time.time()
        new = [r["name"] for r in self._routines if r["name"] not in self._rstate]
        for name in new:  # count a new routine from now, so adding it doesn't fire it
            self._rstate[name] = routines.new_entry(now)
        if new:
            self._save_state()
        self._last_rsig = None
        self._request_render()

    @staticmethod
    def _registry_mtime():
        try:
            return os.stat(routines.REGISTRY).st_mtime
        except OSError:
            return None

    def _save_routines(self):
        try:
            routines.save_registry(self._routines)
        except OSError as err:
            self.notify(f"couldn't save routines: {err}", severity="error")
        self._routines_mtime = self._registry_mtime()
        self._last_rsig = None
        self._request_render()

    def _save_state(self):
        try:
            routines.save_state(self._rstate)
        except OSError:
            pass

    def _host_login(self, host):
        # the connect-as login for a computer (its primary row), as the sessions use
        return next((h[9] for h in self._hosts if h[0] == host and h[10]), "")

    def _routine_info(self, r):
        """(login, probe info or None, its session dict or None) for a routine."""
        login = r["login"] or self._host_login(r["host"])
        info = self._results.get((r["host"], login))
        sess = None
        if info and info.get("reachable"):
            name = routines.session_name(r["name"])
            sess = next((s for s in info["sessions"] if s["name"] == name), None)
        return login, info, sess

    def _routine_state(self, r, now):
        """Everything a routine's row shows, worked out once (starts nothing)."""
        login, info, sess = self._routine_info(r)
        try:
            sched = routines.parse(r["schedule"])
        except ValueError:
            sched = None
        entry = self._rstate.get(r["name"]) or routines.new_entry(now)
        due = bool(sched) and routines.is_due(sched, routines.base_time(entry), now)
        run = routines.current_run(entry)
        status = _routine_status(
            sched_ok=sched is not None,
            enabled=r["enabled"],
            sess=sess,
            due=due and r["enabled"],
            live=bool(info and info.get("reachable") and not info.get("stale")),
            probed=bool(info and not info.get("stale")) or self._host_down(r["host"]),
            done_ago=_ago(now - run["done"]) if run and run["done"] else None,
            exit_code=run.get("exit") if run else None,
            unattended=_unattended(r),
            failed=r["name"] in self._failed,
            launching=r["name"] in self._launching,
            opened=entry.get("opened", False),
            tick=self._tick,
        )
        return sched, entry, due, status

    def _host_down(self, host):
        # Known to be unreachable without probing: Tailscale reports it offline
        # (offline hosts are never probed), or it isn't in the tailnet at all.
        if not self._hosts:
            return False  # discovery hasn't answered yet
        row = next((h for h in self._hosts if h[0] == host), None)
        return row is None or row[2] == "offline"

    def _host_accent(self, host):
        # the machine's colour, as on its sessions and its tmux status bar
        return next((h[8] for h in self._hosts if h[0] == host and len(h) > 8), "")

    def _routine_row(self, r, now):
        """The cells of one routine's row (pure: reads state, starts nothing)."""
        sched, entry, due, (word, color, pending) = self._routine_state(r, now)
        flash = f"blink bold {color}" if pending else color
        if sched is None or not r["enabled"]:
            nxt = ("—", "dim")
        elif due:
            nxt = ("now", f"bold {GREEN}")
        else:
            nowdt = datetime.fromtimestamp(now)
            t = routines.next_slot(sched, nowdt, routines.base_time(entry))
            soon = (t - nowdt).total_seconds() < 3600
            nxt = (_when(t), "" if soon else "dim")
        return (
            (("  ", ""), (r["name"], "" if r["enabled"] else "dim")),
            ((routines.short(r["schedule"]), "dim"),),
            ((word, flash),),
            (nxt,),
            (("unattended", "") if _unattended(r) else ("interactive", "dim"),),
        )

    def _host_state(self, host):
        """ "online", "offline" or "checking…" for a computer, from its probes."""
        infos = [v for (h, _), v in self._results.items() if h == host]
        if any(i.get("reachable") and not i.get("stale") for i in infos):
            return "online"
        if self._host_down(host) or any(
            not i.get("reachable") and not i.get("stale") for i in infos
        ):
            return "offline"
        return "checking…"

    def _host_header_row(self, host):
        """The cells of a computer's header row in the routines table."""
        state = self._host_state(host)
        accent = self._host_accent(host)
        dot = _marker({"online": "up", "offline": "offline"}.get(state, "checking"))
        return (
            (dot, (host, f"bold {accent}".strip())),
            ((state, AMBER if state == "offline" else "dim"),),
            (),
            (),
            (),
        )

    def _pending_routines(self, now):
        """Open runs that wait on you, by state: ``{"waiting": 1, ...}``."""
        out = {}
        for r in self._routines:
            word, _color, pending = self._routine_state(r, now)[3]
            if pending:
                out[word] = out.get(word, 0) + 1
        return out

    def _render_routines(self):
        t = self._rtable
        # not mounted yet, already torn down (a timer firing on exit), or hidden
        # behind a dialog: nothing to paint
        if t is None or t.parent is None or self._modal_open():
            return
        now = time.time()
        pending = self._pending_routines(now)
        self._refresh_badge(pending)
        # t.parent is the panel: a direct handle that works whichever screen is up
        t.parent.border_subtitle = _routine_summary(len(self._routines), pending)
        rows, meta = [], []
        for host, group in _routine_groups(self._routines, self._hosts):
            rows.append(self._host_header_row(host))
            meta.append(("host", host))
            for r in group:
                rows.append(self._routine_row(r, now))
                meta.append(("routine", r["name"]))
        sig = tuple(rows)
        old_meta = self._rmeta
        self._rmeta = meta
        if sig == self._last_rsig and meta == old_meta:
            return
        prev = t.cursor_row
        if (
            self._last_rsig is not None
            and len(self._last_rsig) == len(rows)
            and meta == old_meta
        ):
            for i, cells in enumerate(rows):
                for c, cell in enumerate(cells):
                    if cell != self._last_rsig[i][c]:
                        t.update_cell_at(Coordinate(i, c), self._cell(cell))
        else:
            t.clear()
            t.add_rows(tuple(self._cell(c) for c in cells) for cells in rows)
            if rows:
                t.move_cursor(row=_keep_cursor(old_meta, meta, prev))
        self._last_rsig = sig
        if self._panel == "routines":
            self._sync_routine_detail()  # edits, failures and renames show here too

    def _cur_rmeta(self):
        t = self._rtable
        if t is not None and 0 <= t.cursor_row < len(self._rmeta):
            return self._rmeta[t.cursor_row]
        return None

    def _cur_routine(self):
        """The routine under the cursor, or None (on a computer's header row)."""
        kind, name = self._cur_rmeta() or ("", "")
        if kind != "routine":
            return None
        return next((r for r in self._routines if r["name"] == name), None)

    def _tick_routines(self):
        if self._table is None or self._modal_open():
            return
        if self._registry_mtime() != self._routines_mtime:
            self._load_routines()  # edited by hand
        now = time.time()
        for r in self._routines:
            if not r["enabled"] or r["name"] in self._launching:
                continue
            failed = self._failed.get(r["name"])
            if failed and time.monotonic() - failed[0] < ROUTINE_RETRY:
                continue
            try:
                sched = routines.parse(r["schedule"])
            except ValueError:
                continue
            entry = self._rstate.get(r["name"])
            if not entry or not routines.is_due(sched, routines.base_time(entry), now):
                continue
            login, info, sess = self._routine_info(r)
            # Only on a fresh answer from its computer (not the startup snapshot),
            # and never a second session while the last one is still there.
            if not info or not info.get("reachable") or info.get("stale"):
                continue
            if sess is not None:
                continue
            self._launching.add(r["name"])
            self._launch(r, login, False)
        self._request_render()

    @work(thread=True, group="routine")
    def _launch(self, r, login, open_after):
        out = launch_routine(r["host"], login, r)
        self.call_from_thread(self._launched, r, login, out, open_after)

    def _launched(self, r, login, out, open_after):
        self._launching.discard(r["name"])
        if "__LAUNCHED__" in out:
            self._failed.pop(r["name"], None)
            entry = self._rstate.setdefault(r["name"], routines.new_entry(time.time()))
            entry["last"] = time.time()
            entry["opened"] = bool(open_after)  # run now opens a tab straight away
            entry["done"] = None
            routines.add_run(entry, entry["last"])
            self._save_state()
            self.notify(f"started {r['name']} on {r['host']}", timeout=4)
        elif "__EXISTS__" not in out:
            why = out.split("__FAIL__", 1)[1].strip() if "__FAIL__" in out else ""
            why = why or "couldn't reach it"
            # Say it once per failure streak (and whenever you asked for it),
            # not on every retry.
            if r["name"] not in self._failed:  # a new failure streak: remember it once
                entry = self._rstate.setdefault(
                    r["name"], routines.new_entry(time.time())
                )
                routines.add_run(entry, time.time(), failed=True)
                self._save_state()
            if open_after or r["name"] not in self._failed:
                self.notify(
                    f"couldn't start {r['name']} on {r['host']}: {why}",
                    severity="error",
                    timeout=10,
                )
            self._failed[r["name"]] = (time.monotonic(), why)
            self._request_render()
            return
        self._reprobe(r["host"], login)
        if open_after:
            name = routines.session_name(r["name"])
            self._spawn(["__open", "auto", r["host"], login, name, "attach"])
            self._after_open()
        self._request_render()

    def _sync_routine_detail(self):
        r = self._cur_routine()
        detail = self.query_one("#routine-detail", Static)
        here = self._cur_rmeta()
        if r is None and here and here[0] == "host":  # a computer's header row
            n = sum(1 for x in self._routines if x["host"] == here[1])
            detail.update(
                Text.assemble(
                    (here[1], "bold"),
                    (f"  ·  {n} routine{'' if n == 1 else 's'}", "dim"),
                    (f"  ·  {self._host_state(here[1])}\n", "dim"),
                    ("Press ", "dim"),
                    ("n", f"bold {AMBER}"),
                    (" to add one here.", "dim"),
                )
            )
            return
        if r is None:  # nothing yet: say what routines are, and how to start
            detail.update(
                Text.assemble(
                    ("No routines yet.", "bold"),
                    "  Claude prompts that run on a schedule, each on one computer.\n",
                    ("Press ", "dim"),
                    ("n", f"bold {AMBER}"),
                    (" to add one.", "dim"),
                )
            )
            return
        failed = self._failed.get(r["name"], (0, ""))[1]
        detail.update(_routine_detail(r, self._rstate.get(r["name"]), failed))

    def _mark_routines(self, name, **fields):
        entry = self._rstate.setdefault(name, routines.new_entry(time.time()))
        if any(entry.get(k) != v for k, v in fields.items()):
            entry.update(fields)
            self._save_state()

    def _track_routine_runs(self, key, info, wall):
        """Learn from a fresh probe whether each routine's run was joined or ended.

        Joined: its session has a client attached. Ended: its session is gone,
        and this probe started after the run did — then the run counts as done.
        """
        names = {s["name"]: s for s in info["sessions"]}
        for r in self._routines:
            login = r["login"] or self._host_login(r["host"])
            entry = self._rstate.get(r["name"])
            if (r["host"], login) != key or not entry or not entry.get("last"):
                continue
            sess = names.get(routines.session_name(r["name"]))
            if sess is not None:
                if sess["attached"] or sess["nclients"]:
                    self._mark_routines(r["name"], opened=True)
            elif not entry.get("done") and wall > entry["last"]:
                self._finish_run(r, login, wall)

    def _finish_run(self, r, login, when):
        """Record a run as done, then ask its computer what the run produced."""
        entry = self._rstate.setdefault(r["name"], routines.new_entry(when))
        entry["done"] = when
        run = routines.current_run(entry)
        if run is not None and run["done"] is None:
            run["done"] = when
        self._save_state()
        if run is not None:
            self._fetch_outcome(r, login, run["start"])

    @work(thread=True, group="outcome")
    def _fetch_outcome(self, r, login, start):
        out = host_read("outcome", r["host"], login, r["name"]) or ""
        line, code = _parse_outcome(out)
        if line or code is not None:
            self.call_from_thread(self._got_outcome, r["name"], start, line, code)

    def _got_outcome(self, name, start, line, code=None):
        entry = self._rstate.get(name) or {}
        run = next((x for x in entry.get("runs", []) if x["start"] == start), None)
        if run is not None:
            run["outcome"] = line or None
            run["exit"] = code
            self._save_state()
            self._last_rsig = None
            self._request_render()
