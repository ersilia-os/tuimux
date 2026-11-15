"""The routines panel's pure helpers: status words, groups and the detail card."""

import time
from datetime import datetime

from rich.text import Text

from . import routines
from .fmt import (
    _ago,
    _took,
)
from .hosts import (
    EARLIER_RUNS,
    _unattended,
)
from .theme import (
    AMBER,
    GREEN,
    RED,
)


def _routine_detail(r, entry=None, failed="", now=None):
    """The card under the routines table: two quiet lines.

    What it runs (schedule, folder, the prompt on one line), then how the
    latest run went and the dates of the ones before it.
    """
    now = now or time.time()
    entry = entry or {}
    body = Text(no_wrap=True, overflow="ellipsis")
    body.append(f"{routines.describe(r['schedule'])}  ·  {r['folder']}  ·  ", "dim")
    body.append(
        "  ↵  ".join(ln.strip() for ln in r["prompt"].splitlines() if ln.strip())
    )
    if _unattended(r):
        body.append("  ·  unattended", "dim")
    if not r["enabled"]:
        body.append("  ·  paused", "dim")
    body.append("\n")
    _append_latest(body, r, routines.current_run(entry), failed, now)
    _append_earlier(body, entry.get("runs", []))
    return body


def _append_latest(body, r, run, failed, now):
    """Line 2 of the card: how the latest run went."""
    if failed:
        body.append(f"couldn't start: {failed}", AMBER)
        return
    if run is None:
        body.append("never run", "dim italic")
        return
    if run["done"] is None:
        body.append(f"started {_ago(now - run['start'])}, not done yet", "dim")
        return
    if run.get("exit"):
        body.append(f"failed (exit {run['exit']})", RED)
        if run["outcome"]:
            body.append(f": {run['outcome']}", "dim")
        body.append(f"  ·  log ~/.cache/tuimux/routines/{r['name']}.log", "dim")
        return
    body.append("last outcome: ", "dim")
    body.append(run["outcome"] or "none noted", "" if run["outcome"] else "dim italic")
    body.append(f"  ·  took {_took(run['done'] - run['start'])}", "dim")


def _append_earlier(body, runs):
    """The runs before the latest, newest first: a mark and a date each."""
    earlier = list(reversed(runs[:-1]))[:EARLIER_RUNS]
    if not earlier:
        return
    body.append("  ·  earlier: ", "dim")
    for i, x in enumerate(earlier):
        if i:
            body.append(" ", "dim")
        if x["failed"]:
            body.append("✕", RED)
        elif x["done"] is not None:
            body.append("✓", GREEN)
        else:
            body.append("●", "dim")
        body.append(datetime.fromtimestamp(x["start"]).strftime(" %-d %b"), "dim")


def _routine_groups(rlist, hosts):
    """Routines grouped by computer, in display order.

    Parameters
    ----------
    rlist : list of dict
        The routines, in registry order (kept within each group).
    hosts : list of tuple
        The discovered host rows; computers follow their order here, and
        computers not in the tailnet come last.

    Returns
    -------
    list of tuple
        ``[(host, [routine, ...]), ...]``, only computers that have routines.
    """
    order = list(dict.fromkeys(h[0] for h in hosts))
    groups = {}
    for r in rlist:
        groups.setdefault(r["host"], []).append(r)
    known = [h for h in order if h in groups]
    unknown = [h for h in groups if h not in order]
    return [(h, groups[h]) for h in known + unknown]


def _parse_outcome(out):
    """Split the engine's outcome read into ``(line, exit_code)``: the first
    non-empty line (cut at 200 characters), and the code from an
    ``__EXIT__ <n>`` line, which only unattended runs write (else None)."""
    code, line = None, ""
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("__EXIT__"):
            tail = ln[len("__EXIT__") :].strip()
            code = int(tail) if tail.lstrip("-").isdigit() else None
        elif ln and not line:
            line = ln[:200]
    return line, code


def _keep_cursor(old_meta, meta, prev):
    """Where the routines cursor goes after a redraw: the same row if it's
    still there, else the first routine (never parked on a header if avoidable)."""
    if 0 <= prev < len(old_meta) and old_meta[prev] in meta:
        return meta.index(old_meta[prev])
    return next((i for i, m in enumerate(meta) if m[0] == "routine"), 0)


def _pending_note(pending):
    """ "1 to answer · 2 to read" from ``{todo: count}``; empty if nothing."""
    return "  ·  ".join(f"{n} {word}" for word, n in pending.items())


def _routine_summary(total, pending):
    """The routines panel's border note: how many, and which open runs wait on
    you (``{todo: count}``, e.g. ``{"to answer": 1}``)."""
    if not total:
        return ""
    note = f"{total} routine{'' if total == 1 else 's'}"
    if pending:
        note += f":  [bold {AMBER}]{_pending_note(pending)}[/]"
    return note


def _day(t, now):
    """A short date for the THIS RUN column, relative to `now`.

    Parameters
    ----------
    t, now : float
        Epoch seconds.

    Returns
    -------
    str
        "09:00" for today, "Mon" within the last six days, else "9 Nov".
    """
    d, today = datetime.fromtimestamp(t), datetime.fromtimestamp(now)
    if d.date() == today.date():
        return d.strftime("%H:%M")
    if 0 < (today.date() - d.date()).days < 7:
        return d.strftime("%a")
    return d.strftime("%-d %b")


# What makes a routine row amber: an open run that waits on you. Only "answer it"
# blinks — Claude is stuck until you reply; the rest can wait for a glance.
_ASK = f"blink bold {AMBER}"


def _routine_status(
    *,
    sched_ok,
    enabled,
    sess,
    due,
    live,
    probed,
    since=None,
    done_at=None,
    exit_code=None,
    unattended=False,
    failed=False,
    launching=False,
    opened=False,
    now=None,
    tick=0,
):
    """Where the run for the current period stands (the THIS RUN column).

    Each word says what to do about the run, and since when. Amber means it
    waits on you, red means something broke, everything else is plain or dim.

    Parameters
    ----------
    sched_ok, enabled : bool
        Whether the schedule parses, and whether the routine is paused.
    sess : dict or None
        The run's open session (from the probe), or None.
    due : bool
        Whether a run is owed now.
    live, probed : bool
        Whether its computer answers now, and whether we know either way.
    since, done_at : float or None
        When the current run started, and when it finished (epoch seconds).
    exit_code : int or None
        An unattended run's exit code; non-zero means it failed.
    unattended, failed, launching, opened : bool
        Runs alone; couldn't start; is starting now; you have joined it.
    now : float
        Epoch seconds, for the dates.
    tick : int
        Animation frame, for the "working…" dots.

    Returns
    -------
    tuple
        ``(word, style, todo)``. `todo` names what an open run waits on you
        for ("to answer", "to read", …) and is counted on the panels' borders;
        None when it doesn't need you.
    """
    now = now or time.time()
    when = _day(since, now) if since else "—"
    if sess is not None:  # a run is open on its computer
        st = sess.get("state")
        if unattended or st in ("running", "working"):  # claude -p, or busy
            return (f"working{'.' * (tick % 4):<3} · since {when}", "", None)
        if due:  # the next run is owed but this one was never finished
            return (f"finish it · since {when}", AMBER, "to finish")
        if not sess["agent"]:
            return ("check it · no claude", RED, "to check")  # claude didn't start
        if st == "waiting":  # claude asked you something
            return (f"answer it · since {when}", _ASK, "to answer")
        if not opened:  # finished its turn, and you haven't looked yet
            return (f"read it · since {when}", AMBER, "to read")
        return (f"close it · since {when}", "dim", None)  # read; d marks it done
    if not sched_ok:
        return ("fix schedule", RED, None)
    if not enabled:
        return ("paused", "dim", None)
    if failed:
        return ("couldn't start", RED, None)  # retried; the card says why
    if launching or (due and live):
        return ("starting…", "", None)
    if due:  # its computer is off, or still being checked
        if probed:
            return ("missed · computer off", AMBER, None)
        return ("due · checking…", "dim", None)
    if done_at and exit_code:
        return (f"failed {_day(done_at, now)}", RED, None)  # the card says how
    if done_at:
        return (f"done {_day(done_at, now)} ✓", "dim", None)
    return ("scheduled", "dim", None)
