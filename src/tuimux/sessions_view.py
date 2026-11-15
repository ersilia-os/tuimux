"""The sessions table's view-model: one row per machine and session."""

import time
from functools import partial

from .fmt import (
    _folder,
    _title_parts,
)
from .hosts import (
    ACTION_TIMEOUT,
    PROBE_TIMEOUT,
    RECONCILE_TTL,
)
from .theme import (
    _PENDING_LABEL,
    _STATE_STYLE,
    AGENT,
    AMBER,
    MUTED,
    SESSION_COLUMNS,
    _marker,
    _row,
)


def _header_name(host, primary, marker_state, style):
    """NAME cell for a machine header. Primary → dot + bold host name.

    A secondary login → an indented, de-bolded "↳ <host>" sub-row that groups it
    under the machine; the login itself shows in SSH USER."""
    if primary:
        return (_marker(marker_state), (host, style))
    return (("    ↳ ", "dim"), (host, style.replace("bold ", "") or "dim"))


def _device_row(dead, hdr, status, lastseen, ident):
    """A phone or tablet: never SSH'd, so only whether it's online.

    A "device", not something you log into — calm gray (a computer that can't
    be reached is amber; a phone that can't be reached is just what a phone is)."""
    if status == "offline":
        return _row(
            dead,
            name=hdr("offline", "dim"),
            status=(("offline", "dim italic"),),
            state=((lastseen, "dim"),) if lastseen else (),
            **ident,
        )
    return _row(dead, name=hdr("device", "dim"), status=(("device", MUTED),), **ident)


def _warn_row(dead, hdr, word, hint, ident):
    """An online machine you can't use right now: an amber word and a hint."""
    return _row(
        dead,
        name=hdr("warn", f"bold {AMBER}"),
        status=((word, AMBER),),
        folder=((hint, "dim"),),
        **ident,
    )


class SessionsView:
    """The sessions table's rows, built as plain data (mixed into Tuimux)."""

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
        window (no count prefix). Tint the command blue when the active window is
        an AI agent (its command contains "claude")."""
        head, sep, cmd = tabs.partition("  ")
        if not sep:  # single window → the whole cell is the active command
            return ((tabs, AGENT),) if "claude" in tabs.lower() else ((tabs, "dim"),)
        if "claude" in cmd.lower():
            return ((head + sep, "dim"), (cmd, AGENT))
        return ((tabs, "dim"),)

    # Build the table as plain data — a list of (cells, meta) rows — so we can
    # diff cheaply and only repaint when something actually changed.
    #
    # Colours follow the one convention in theme.py, shared with the routines panel.
    def _view(self):
        # Two buckets: the machines you can use right now (local + reachable, any
        # owner) go on TOP; everything else (your own phones/offline boxes, and —
        # only under "o" — other people's unreachable stuff) goes BELOW a thin line.
        top, bottom = [], []
        # Your tailnet owner, from the local row — decides what counts as "yours".
        my_owner = next((h[5] for h in self._hosts if h[1] and len(h) > 5), "")
        for h in self._hosts:
            placed = self._host_rows(h, my_owner)
            if placed is not None:
                shown, rows = placed
                (top if shown else bottom).extend(rows)
        # usable machines on top; a thin line; then the rest (your own not-usable
        # devices always, plus — under "o" — everyone else's). The line only shows
        # when there's content on BOTH sides.
        rows = list(top)
        if top and bottom:
            rows.append(self._separator_row())
        rows.extend(bottom)
        return rows

    def _host_rows(self, h, my_owner):
        """One machine's rows: ``(shown, rows)``, or None when it's hidden.

        `shown` puts the rows above the separator; hidden machines (other
        people's boxes you can't use) only appear under "o", below it."""
        # Tolerate short host tuples (owner/mapping/probe/color/login are newer
        # columns): default to no owner/login and "probe it" (pre-fleet behavior).
        host, is_local, status, lastseen, kind = h[0], h[1], h[2], h[3], h[4]
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
        usable = is_local or bool(info and (info.get("reachable") or info.get("busy")))
        # Shown by default: everything that's yours (incl. your phone, offline
        # boxes) plus any box you can actually reach. Everything else — other
        # people's machines you can't use — appears only under "o" (org view),
        # below the separator.
        shown = mine or usable
        if not shown and self._scope != "org":
            return None
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
        ident = {
            "owner": self._owner_cell(owner, primary),
            "user": self._login_cell(login, usable and not consumer, mapping),
        }
        hdr = partial(_header_name, host, primary)
        if consumer:
            return shown, [_device_row(dead, hdr, status, lastseen, ident)]
        if status == "offline":
            return shown, self._offline_rows(dead, hdr, key, lastseen, ident)
        if info is None:
            # not probed yet this refresh — neutral placeholder while we check
            checking = (("checking…", "dim italic"),)
            return shown, [
                _row(machine, name=hdr("checking", "dim"), status=checking, **ident)
            ]
        if info.get("busy"):
            # SSH works, the host is just too loaded to answer in time.
            return shown, [_warn_row(dead, hdr, "busy", "high load", ident)]
        if not info["reachable"]:
            # online on the tailnet, but no working SSH login. Your own box →
            # Tailscale SSH probably isn't enabled there; a teammate's box → you
            # have no account/permission on it. The hint says which to fix.
            hint = "tailscale up --ssh" if mine else "no account · press u"
            return shown, [_warn_row(dead, hdr, "no ssh", hint, ident)]
        rows = self._reachable_rows(machine, hdr, key, info, accent, is_local, ident)
        return shown, rows

    def _offline_rows(self, dead, hdr, key, lastseen, ident):
        """A machine that's down: its header, then what was last running on it."""
        # Down (asleep / off the network / shut down): a hollow dot + last
        # seen. Decided by tailscale status, not by a probe — offline hosts
        # aren't probed, so info stays None for them.
        rows = [
            _row(
                dead,
                name=hdr("offline", "dim"),
                status=(("offline", "dim italic"),),
                state=((lastseen, "dim"),) if lastseen else (),
                **ident,
            )
        ]
        # Its tmux sessions almost certainly aren't gone — just out of reach
        # until it's back. Show what was last running, dimmed, so you know
        # what's "paused" (truth only becomes knowable at reconnect; see
        # _reconcile_sessions). Non-interactive: there's nothing to attach.
        host, login = key
        for s in self._snap.get(key, []):
            meta = {"host": host, "login": login, "session": s["name"]}
            rows.append(
                _row(
                    {**meta, "action": "none"},
                    name=(("  ", ""), (s["auto"], "dim")),
                    state=((s["state"], "dim"),),
                    folder=((_folder(s["dir"]), "dim"),),
                    tabs=((s["tabs"], "dim"),),
                    open_in=(("unreachable", "dim italic"),),
                )
            )
        return rows

    def _reachable_rows(self, machine, hdr, key, info, accent, is_local, ident):
        """A machine you can use: its header, its sessions, and "＋ new session"."""
        # reachable — the NAME carries the machine's colour (matching its tmux
        # bar); the green dot says it's usable, so the STATUS word stays dim.
        host, login = key
        # remembered from the last run, not yet re-probed: same rows, dimmed
        stale = info.get("stale", False)
        word = ("local" if is_local else "online", "dim")  # the green dot says it
        awake_c = (("☕ awake", ""),) if info["awake"] else ()
        if self._pending_for(host, login, None):
            awake_c = ((_PENDING_LABEL["awake"], "dim italic"),)
        rows = [
            _row(
                machine,
                name=hdr("checking", "dim") if stale else hdr("up", f"bold {accent}"),
                status=(("checking…", "dim italic"),) if stale else (word,),
                state=awake_c,
                **ident,
            )
        ]
        # Just-reconnected? Show for RECONCILE_TTL which sessions are the
        # same ones as before (resumed) and which are gone (lost).
        rec = self._reconcile.get(key)
        verdicts = rec[1] if rec and time.monotonic() - rec[0] < RECONCILE_TTL else {}
        for s in info["sessions"]:  # its sessions, indented beneath it
            rows.append(self._session_row(key, s, stale, verdicts.get(s["name"])))
        # Sessions that were here before the host dropped but didn't come
        # back — the host was shut down / tmux restarted, so they're gone.
        live = {s["name"] for s in info["sessions"]}
        meta = {"host": host, "login": login}
        for name, verdict in verdicts.items():
            if verdict == "lost" and name not in live:
                rows.append(
                    _row(
                        {**meta, "session": name, "action": "none"},
                        name=(("  ", ""), (name, "dim")),
                        open_in=(("lost — not restored", "dim italic"),),
                    )
                )
        rows.append(
            _row(
                {**meta, "session": "__NEW__", "action": "new"},
                name=(("  ＋ new session", "dim italic"),),
            )
        )
        return rows

    def _session_row(self, key, s, stale, verdict):
        """One live session's row, under its machine."""
        host, login = key
        # `open` means "we have a confirmed local tab to jump to" — the same
        # signal that drives the OPEN IN cell. If we can't pin down the tab,
        # the menu honestly offers a new tab instead of promising focus the
        # engine can't deliver.
        locs = self._window_locs(s["name"])
        # Session names are plain (only the machine's name carries its colour),
        # dim when it's just an idle, unattached shell.
        nm_style = (
            "dim" if stale or (s["state"] == "idle" and not s["attached"]) else ""
        )
        # "running" gets a live ellipsis (running / running. / running.. /
        # running…) that cycles on the animation tick — a quiet sign of
        # activity. Other states are static.
        st = s["state"]
        st_word = st + "." * (self._tick % 4) if st == "running" else st
        state_c = ((st_word, "dim" if stale else _STATE_STYLE.get(st, "")),)
        name_c = (("  ", ""), (s["auto"], nm_style))
        pend = self._pending_for(host, login, s["name"])
        if pend:
            state_c = ((_PENDING_LABEL[pend[0]], "dim italic"),)
            if pend[1]:  # rename: show the new name right away
                name_c = (("  ", ""), (pend[1], "dim italic"))
        resumed = (("  resumed", ""),) if verdict == "resumed" else ()
        return _row(
            {
                "host": host,
                "login": login,
                "session": s["name"],
                "action": "attach",
                "open": bool(locs),
            },
            name=name_c,
            state=state_c,
            uptime=((s["uptime"], "dim"),),
            folder=((_folder(s["dir"]), "dim"),),
            tabs=self._tabs_cell(s["tabs"]),
            open_in=tuple(self._open_in_cell(s, locs)) + resumed,
        )

    def _pending_for(self, host, login, session):
        """The in-flight action on a row, or None (also once it's long overdue)."""
        p = self._pending.get((host, login, session))
        if p and time.monotonic() - p[2] < ACTION_TIMEOUT + PROBE_TIMEOUT:
            return p
        return None

    @staticmethod
    def _separator_row():
        """An inert, end-to-end divider between your devices and everyone else's
        (only ever shown under "o"). A thin rule across every column; not a real
        row you can act on (every action gates on action in (attach|new))."""
        # dashes sized to each column so the line reads as continuous across the
        # whole table (the row's "open_in" cell is the OPEN IN column, "open")
        widths = {
            ("open_in" if key == "open" else key): w for key, _, w in SESSION_COLUMNS
        }
        meta = {"host": None, "login": "", "session": None, "action": "separator"}
        cells = {col: (("─" * w, "dim"),) for col, w in widths.items()}
        return _row(meta, **cells)
