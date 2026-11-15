"""Finding machines and their sessions: host scans, probes, window scans."""

import time

from textual import work

from .fmt import (
    _title_parts,
)
from .hosts import (
    _ENV,
    QUIET,
    RECHECK,
    REFRESH,
    WINDOWS_EVERY,
    WINDOWS_TIMEOUT,
    _probe_or_offline,
    _reconcile_sessions,
    _run,
    fetch_hosts,
)


class Fleet:
    """Keeps the hosts, probe results and terminal windows fresh (mixed into Tuimux)."""

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

    def reload(self, force=False):
        """Refresh hosts and probes on the heartbeat, when it makes sense.

        Parameters
        ----------
        force : bool
            Refresh now, ignoring the cadence and the typing debounce.
        """
        # Called on every heartbeat. Refresh only when it makes sense:
        #   • not before the table exists, and never while a dialog is up;
        #   • not while you're actively pressing keys (defer < QUIET since the
        #     last keystroke) — keeps navigation fluid;
        #   • no more often than REFRESH, so an idle dashboard still updates
        #     on its normal cadence rather than every heartbeat.
        if self._table is None or self._modal_open():
            return
        now = time.monotonic()
        if not force and (
            now - self._last_input < QUIET or now - self._last_refresh < REFRESH
        ):
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
        for h in hosts:  # a remembered host that went offline isn't "usable"
            if h[2] == "offline" and self._results.get((h[0], h[9]), {}).get("stale"):
                del self._results[(h[0], h[9])]
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
        if now - self._last_windows >= WINDOWS_EVERY:
            self._rescan_windows()  # where sessions are open (local, off-thread)

    def _rescan_windows(self):
        self._last_windows = time.monotonic()
        self._scan_windows()

    def _reprobe(self, host, login):
        # Re-probe one machine right away (after an action on it), bypassing the
        # refresh cadence so the change shows up in about one round trip.
        key = (host, login)
        h = next((h for h in self._hosts if (h[0], h[9]) == key), None)
        if h is None or key in self._probing:
            return
        self._last_probe[key] = time.monotonic()
        self._probing.add(key)
        self._probe_one(h, key)

    @work(thread=True, group="probe")
    def _probe_one(self, h, key):
        started, wall = time.monotonic(), time.time()
        _host, info = _probe_or_offline(h)
        self.call_from_thread(self._got, key, info, started, wall)

    def _got(self, key, info, started=None, wall=None):
        # A probe that began after an action reflects its outcome: drop the
        # pending label. An older probe still in flight must not, or the row
        # would flicker back to its pre-action state.
        if started is not None:
            for pk, (_k, _n, t0) in list(self._pending.items()):
                if pk[:2] == key and t0 <= started:
                    del self._pending[pk]
        if info.get("reachable"):
            live = {s["name"] for s in info["sessions"]}
            for ck in [k for k in self._peek_cache if k[:2] == key]:
                if ck[2] not in live:
                    del self._peek_cache[ck]
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
        if info.get("reachable") and wall is not None:
            self._track_routine_runs(key, info, wall)
        self._request_render()
        self._tick_routines()  # a host just answered: start its due routines now

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
