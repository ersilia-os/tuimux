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
    """ "1 waiting · 2 unopened" from ``{word: count}``; empty if nothing."""
    return "  ·  ".join(f"{n} {word}" for word, n in pending.items())


def _routine_summary(total, pending):
    """The routines panel's border note: how many, and which open runs wait on
    you (``{word: count}``, e.g. ``{"waiting": 1}``)."""
    if not total:
        return ""
    note = f"{total} routine{'' if total == 1 else 's'}"
    if pending:
        note += f":  [bold {AMBER}]{_pending_note(pending)}[/]"
    return note


def _routine_status(
    *,
    sched_ok,
    enabled,
    sess,
    due,
    live,
    probed,
    done_ago=None,
    exit_code=None,
    unattended=False,
    failed=False,
    launching=False,
    opened=False,
    tick=0,
):
    """Where the run for the current period stands (the THIS RUN column).

    `done_ago` is how long ago the latest run finished ("2d ago"), or None if
    it hasn't. When nothing is due, a finished latest run is this period's.
    An `unattended` run never waits on you: while its session is open it is
    running, and a non-zero `exit_code` marks the finished run as failed.

    Returns
    -------
    tuple
        ``(word, color, pending)``. An open run uses the sessions table's
        words (running, waiting, idle), plus "unopened" until you first join
        it. `pending` means that open run waits on you (to answer, open or
        close it): it blinks, and is counted on the panels' borders.
    """
    if sess is not None and unattended:  # claude -p, until its session closes
        return ("running" + "." * (tick % 4), GREEN, False)
    if sess is not None:  # a run is open on its computer
        if due:  # the next run is owed but this one was never finished
            return ("overdue", AMBER, True)
        if not sess["agent"]:
            return ("no claude", RED, True)  # claude didn't start in it
        st = sess["state"]
        if st in ("running", "working"):
            return ("running" + "." * (tick % 4), GREEN, False)
        if st == "waiting":  # claude asked you something
            return ("waiting", AMBER, True)
        if not opened:  # started, and you haven't looked at it yet
            return ("unopened", AMBER, True)
        return ("idle", "", False)
    if not sched_ok:
        return ("bad schedule", RED, False)
    if not enabled:
        return ("paused", "dim", False)
    if failed:
        return ("failed", RED, False)  # retried; the card says why
    if launching or (due and live):
        return ("starting", GREEN, False)
    if due:
        # its computer is off (amber), or still being checked (dim)
        return ("not run yet", AMBER if probed else "dim", False)
    if done_ago and exit_code:
        return (f"failed {done_ago}", RED, False)  # the card says how
    if done_ago:
        return (f"done {done_ago}", GREEN, False)
    return ("not yet", "dim", False)
