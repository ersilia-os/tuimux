"""Talking to the machines: the engine, SSH probes, host actions and snapshots."""

import atexit
import json
import os
import re
import subprocess
import threading
from importlib.resources import files

from . import remote, routines
from .cli import tuimux_bin
from .fmt import (
    SHELLS,
    _abbrev,
    _auto_name,
    _uptime,
)

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
# Keep one SSH shell open per remote host (remote.py) instead of paying the ~2s
# Tailscale SSH handshake on every probe/preview/action. "off" = old per-call ssh.
PERSIST = os.environ.get("TUIMUX_PERSIST", "on").lower() not in ("off", "0", "no")
# How often (seconds) to re-read the terminal's window/tab titles for OPEN IN. On
# Ghostty that's an accessibility walk that stalls Ghostty's own redraw (i.e. this
# dashboard), so it's kept off the refresh tick; it also runs on returning to the
# dashboard and shortly after you open/detach/close something.
WINDOWS_EVERY = float(os.environ.get("TUIMUX_WINDOWS_EVERY", "15") or 15)
# Wait this long after the cursor stops before capturing a preview, so holding an
# arrow key sends one capture instead of one per row passed.
PEEK_DEBOUNCE = 0.12
# After a routine fails to start (e.g. its computer's disk is full), wait this
# long before the scheduler tries again — rather than on every probe.
ROUTINE_RETRY = 300
EARLIER_RUNS = 4  # past runs listed in the card under the routines table
# What the bottom panel can show, in the order `v` cycles through them.
PANELS = ("preview", "routines", "none")
# Last-known hosts + sessions, saved on exit and shown (dimmed) at the next launch
# while the real probes run, so the table is useful from the first frame.
SNAPSHOT = os.path.expanduser("~/.cache/tuimux/snapshot.json")


def _run(args, timeout=None, input=None):
    try:
        return subprocess.run(
            ["bash", ENGINE, *args],
            capture_output=True,
            text=True,
            env=_ENV,
            timeout=timeout,
            input=input,
        )
    except subprocess.TimeoutExpired:
        # Distinct marker so callers can tell "too slow to answer" apart from a
        # genuine SSH failure (engine prints UNREACHABLE for the latter).
        return subprocess.CompletedProcess(args, 124, "__TIMEOUT__\n", "")


# Static remote scripts (probe, keep-awake, ssh options) fetched once from the
# engine; per-session ones (peek/detach/…) are cheap local engine calls.
_SCRIPTS = {}
_POOL = None
_POOL_LOCK = threading.Lock()  # probe threads race to create the pool at startup


def _script(kind, *args):
    key = (kind, *args)
    if key in _SCRIPTS:
        return _SCRIPTS[key]
    out = _run(["__remote", kind, *args], timeout=HOSTS_TIMEOUT).stdout
    if kind in ("probe", "awake", "sshopts") and out:
        _SCRIPTS[key] = out
    return out


def _pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            opts = _script("sshopts").split()
            _POOL = remote.ShellPool(lambda h, lg: remote.ssh_argv(h, lg, opts))
            atexit.register(_POOL.close_all)
        return _POOL


def _persistent(host):
    """True when calls to `host` go over its persistent shell: a remote host,
    once discovery has told us which host is this machine."""
    me = _ENV.get("TUIMUX_SELF_HOST")
    return PERSIST and bool(me) and bool(host) and host != me


def _shell(host, login, kind, *args, timeout):
    """Run the engine's `kind` script on a remote host's persistent shell."""
    return _pool().get(host, login).run(_script(kind, *args), timeout)


# Mutating per-host actions: dashboard kind -> engine command (same arg order).
_ACTIONS = {
    "detach": "__detach",
    "kill": "__killraw",
    "rename": "__renameto",
    "awake": "__awaketoggle",
}


def host_action(kind, host, login, *args):
    """Run a mutating action (detach/kill/rename/awake) on a session's host.

    Parameters
    ----------
    kind : str
        The action: a key of ``_ACTIONS``.
    host, login : str
        The machine, and the SSH user to act as.
    *args : str
        The action's arguments (the session name, a new name, …).
    """
    if _persistent(host):
        _shell(host, login, kind, *args, timeout=ACTION_TIMEOUT)
    else:
        _run([_ACTIONS[kind], host, login, *args], timeout=ACTION_TIMEOUT)


def _unattended(r):
    """Whether a routine runs on its own: claude in print mode, no questions."""
    return r.get("mode") == "unattended"


def launch_routine(host, login, r):
    """Start a routine's session on its host.

    Parameters
    ----------
    host, login : str
        Where to start it, and as whom.
    r : dict
        The routine (see `routines.load_registry`).

    Returns
    -------
    str
        The engine's output: contains ``__LAUNCHED__``, ``__EXISTS__`` or
        ``__FAIL__``; anything else means the host couldn't be reached.
    """
    ask = "1" if r.get("ask_done", True) else "0"
    alone = "1" if _unattended(r) else "0"
    args = [
        routines.session_name(r["name"]),
        r["folder"],
        r["name"],
        host,
        login,
        ask,
        alone,
        r.get("allow", ""),
    ]
    if not _persistent(host):
        return _run(
            ["__launch", *args], timeout=ACTION_TIMEOUT, input=r["prompt"]
        ).stdout
    script = _run(
        ["__remote", "launch", *args], timeout=HOSTS_TIMEOUT, input=r["prompt"]
    )
    rc, out = _pool().get(host, login).run(script.stdout, ACTION_TIMEOUT)
    return out if rc is not None else ""


def host_read(kind, host, login, *args):
    """Run a read-only remote script on a host and return its output.

    Parameters
    ----------
    kind : str
        The engine's ``__script`` kind to run.
    host, login : str
        The machine, and the SSH user to read as.
    *args : str
        The script's arguments.

    Returns
    -------
    str or None
        The script's stdout, or None if the host couldn't be reached.
    """
    if _persistent(host):
        rc, out = _shell(host, login, kind, *args, timeout=ACTION_TIMEOUT)
        return out if rc is not None else None
    res = _run(["__read", kind, host, login, *args], timeout=ACTION_TIMEOUT)
    return res.stdout if res.returncode == 0 else None


def peek(host, login, session):
    """Capture a session's active pane (with colour escapes) for the preview.

    Parameters
    ----------
    host, login : str
        The machine, and the SSH user to read as.
    session : str
        The tmux session.

    Returns
    -------
    str
        The captured pane ("" if the host couldn't be reached).
    """
    if not _persistent(host):
        return _run(["__peek", host, login, session], timeout=PEEK_TIMEOUT).stdout
    rc, out = _shell(host, login, "peek", session, timeout=PEEK_TIMEOUT)
    return out if rc is not None else ""


def _probe_stdout(host, login):
    if not _persistent(host):
        return _run(["__probe", host, login], timeout=PROBE_TIMEOUT).stdout
    rc, out = _shell(host, login, "probe", timeout=PROBE_TIMEOUT)
    if out == remote.TIMEOUT:
        return "__TIMEOUT__\n"
    return f"OK\n{out}" if rc == 0 else "UNREACHABLE\n"


def fetch_hosts(scope="mine"):
    """List the tailnet's machines, as the engine sees them.

    Parameters
    ----------
    scope : str
        "mine" for your own machines plus any you've mapped a login for, "org"
        for the whole tailnet.

    Returns
    -------
    list of tuple
        One row per (machine, login): name, is_local, status, last seen, kind,
        owner, mapping, probe, colour, login and primary. Your own machines come
        first, phones and tablets last.
    """
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


def _parse_probe_lines(lines):
    """Collect the probe's ``KIND|field|…`` records by kind.

    Parameters
    ----------
    lines : list of str
        The probe output after its leading ``OK`` line.

    Returns
    -------
    tuple
        ``(S, W, L, A, C, notmux)``: sessions, windows, attached clients, agent
        states and agent session names, plus whether tmux is missing there.
    """
    S, W, L, A, C, notmux = {}, {}, {}, {}, set(), False
    for ln in lines:
        if ln == "__NOTMUX__":
            notmux = True
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
    return S, W, L, A, C, notmux


def _session_info(name, s, wins, clients, agent_state, is_agent):
    """One session's row data, from its probe records."""
    dird = _abbrev(s["dir"]) or "~"
    count = len(wins) or s["windows"] or 1
    active = next(
        (w[1] for w in wins if w[2]), wins[0][1] if wins else (s["cmd"] or "?")
    )
    if is_agent and re.match(r"^[0-9][0-9.]*$", active or ""):
        active = "claude"
    state = (
        (agent_state or "idle")
        if is_agent
        else ("idle" if s["cmd"] in SHELLS else "running")
    )
    return {
        "name": name,
        "auto": _auto_name(name, dird, is_agent, s["cmd"]),
        "attached": s["attached"],
        # how many tmux clients hold this session on its host — drives the
        # "on host"/"N clients"/"detached" half of the OPEN IN cell. This is
        # independent of whether a tab is open on *this* Mac (see _window_locs).
        "nclients": clients,
        "dir": dird,
        # window count only when >1 (a lone window's "1" is just noise);
        # a single-window session shows only its active command.
        "tabs": (f"{count}  {active}" if count > 1 else active),
        "state": state,
        "uptime": _uptime(s["created"]),
        "created": s["created"],  # raw epoch — identity key for reconnect
        "agent": is_agent,
    }


def probe(host, login):
    """Probe one machine over SSH for its tmux sessions.

    Parameters
    ----------
    host : str
        The tailnet host name.
    login : str
        The SSH user to log in as ("" for the default).

    Returns
    -------
    dict
        ``reachable``, ``busy``, ``notmux``, ``awake`` and ``sessions`` (a list
        of session dicts, sorted by display name).
    """
    info = {
        "reachable": False,
        "busy": False,
        "notmux": False,
        "awake": False,
        "sessions": [],
    }
    lines = _probe_stdout(host, login).splitlines()
    if lines and lines[0].strip() == "__TIMEOUT__":
        # SSH is fine, the host is just too loaded to answer within PROBE_TIMEOUT.
        info["busy"] = True
        return info
    if not lines or lines[0].strip() != "OK":
        return info
    info["reachable"] = True
    S, W, L, A, C, info["notmux"] = _parse_probe_lines(lines[1:])
    info["awake"] = AWAKE in S
    info["sessions"] = sorted(
        (
            _session_info(
                name, s, W.get(name, []), len(L.get(name, [])), A.get(name), name in C
            )
            for name, s in S.items()
            if name != AWAKE
        ),
        key=lambda x: x["auto"].lower(),
    )
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


def save_snapshot(path, hosts, snap, panel="preview"):
    """Save the hosts, last-known sessions and bottom panel for the next launch.

    Parameters
    ----------
    path : str
        The snapshot file; its folder is created if needed.
    hosts : list of tuple
        The host rows from `fetch_hosts`.
    snap : dict
        ``{(host, login): [session, …]}``, the last sessions seen on each.
    panel : str
        What the bottom panel shows: one of ``PANELS``.
    """
    data = {
        "hosts": [list(h) for h in hosts],
        "snap": {f"{h}\t{lg}": v for (h, lg), v in snap.items()},
        "panel": panel,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def load_snapshot(path):
    """Read a snapshot written by `save_snapshot`.

    Parameters
    ----------
    path : str
        The snapshot file.

    Returns
    -------
    tuple
        ``(hosts, snap, panel)``; empty (and ``"preview"``) if the file is
        missing or unreadable.
    """
    try:
        with open(path) as f:
            data = json.load(f)
        hosts = [tuple(h) for h in data["hosts"] if len(h) == 11]
        snap = {}
        for k, sessions in data["snap"].items():
            host, _, login = k.partition("\t")
            for x in sessions:  # uptime was frozen at save time; recompute it
                x["uptime"] = _uptime(x.get("created", ""))
            snap[(host, login)] = sessions
        panel = data.get("panel")
        return hosts, snap, panel if panel in PANELS else "preview"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return [], {}, "preview"
