"""Routines: Claude prompts you run on a schedule, each on one fixed computer.

The registry is a hand-editable TOML file of ``[[routine]]`` tables (name,
host, login, folder, schedule, enabled, prompt). Run history lives apart from
it, in a small JSON state file, so the registry stays purely yours. Per
routine: ``since`` (when it was added), ``last`` (when its current run
started), ``opened`` (whether you've joined that run), ``done`` (when it was
finished, or null while it isn't) and ``runs`` — the last `HISTORY` runs. A routine is due relative to its last run, or
to when it was added if it never ran — so adding one never fires it at once. Schedules
are short phrases in local time — see `parse`.
"""

import calendar
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

REGISTRY = os.path.expanduser("~/.config/tuimux/routines.toml")
STATE = os.path.expanduser("~/.cache/tuimux/routines.json")
SESSION_PREFIX = "routine-"
FIELDS = (
    "name",
    "host",
    "login",
    "folder",
    "schedule",
    "enabled",
    "ask_done",
    "prompt",
)

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HISTORY = 8  # runs remembered per routine (the RUNS strip in the panel)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Schedule:
    """A parsed schedule phrase.

    Attributes
    ----------
    kind : str
        ``"days"`` (fires at ``hh:mm`` on the given weekdays), ``"monthly"``
        or ``"every"``.
    hh, mm : int
        Time of day for ``days`` and ``monthly``.
    days : frozenset of int
        Weekdays, Monday = 0, for ``days``.
    dom : int
        Day of month for ``monthly`` (clamped to the month's last day).
    every : int
        Interval in seconds for ``every``.
    """

    kind: str
    hh: int = 0
    mm: int = 0
    days: frozenset = field(default_factory=frozenset)
    dom: int = 1
    every: int = 0


def _hhmm(text):
    m = _HHMM.match(text)
    if not m:
        raise ValueError(f"expected a time like 09:00, got {text!r}")
    return int(m.group(1)), int(m.group(2))


def parse(text):
    """Parse a schedule phrase.

    Parameters
    ----------
    text : str
        One of ``daily HH:MM``, ``weekdays HH:MM``, ``weekly mon[,thu…] HH:MM``,
        ``monthly D HH:MM``, ``every Nh`` or ``every Nd``.

    Returns
    -------
    Schedule

    Raises
    ------
    ValueError
        With a message fit to show the user.
    """
    words = (text or "").lower().split()
    if not words:
        raise ValueError("empty schedule")
    head, rest = words[0], words[1:]
    if head == "daily" and len(rest) == 1:
        return Schedule("days", *_hhmm(rest[0]), days=frozenset(range(7)))
    if head == "weekdays" and len(rest) == 1:
        return Schedule("days", *_hhmm(rest[0]), days=frozenset(range(5)))
    if head == "weekly" and len(rest) == 2:
        names = [d for d in rest[0].split(",") if d]
        bad = [d for d in names if d[:3] not in _DAYS]
        if not names or bad:
            raise ValueError("weekly needs days like mon or mon,thu")
        days = frozenset(_DAYS.index(d[:3]) for d in names)
        return Schedule("days", *_hhmm(rest[1]), days=days)
    if head == "monthly" and len(rest) == 2:
        if not rest[0].isdigit() or not 1 <= int(rest[0]) <= 31:
            raise ValueError("monthly needs a day of month from 1 to 31")
        return Schedule("monthly", *_hhmm(rest[1]), dom=int(rest[0]))
    if head == "every" and len(rest) == 1:
        m = re.match(r"^(\d+)([hd])$", rest[0])
        if not m or int(m.group(1)) == 0:
            raise ValueError("every needs an interval like 6h or 3d")
        unit = 3600 if m.group(2) == "h" else 86400
        return Schedule("every", every=int(m.group(1)) * unit)
    raise ValueError(
        "use daily 09:00, weekdays 09:00, weekly mon,thu 09:00, "
        "monthly 1 09:00, every 6h or every 3d"
    )


def _month_slot(year, month, s):
    day = min(s.dom, calendar.monthrange(year, month)[1])
    return datetime(year, month, day, s.hh, s.mm)


def last_slot(s, now):
    """The most recent scheduled time at or before `now` (None for ``every``).

    Parameters
    ----------
    s : Schedule
    now : datetime
        Local, naive.

    Returns
    -------
    datetime or None
    """
    if s.kind == "days":
        for back in range(8):
            d = now - timedelta(days=back)
            t = d.replace(hour=s.hh, minute=s.mm, second=0, microsecond=0)
            if d.weekday() in s.days and t <= now:
                return t
    if s.kind == "monthly":
        y, m = now.year, now.month
        t = _month_slot(y, m, s)
        if t > now:
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
            t = _month_slot(y, m, s)
        return t
    return None


def next_slot(s, now, last_run):
    """The next time this schedule fires after `now`.

    Parameters
    ----------
    s : Schedule
    now : datetime
        Local, naive.
    last_run : float
        Epoch seconds of the last run (``every`` counts from it).

    Returns
    -------
    datetime
    """
    if s.kind == "every":
        return datetime.fromtimestamp(last_run + s.every)
    if s.kind == "days":
        for ahead in range(8):
            d = now + timedelta(days=ahead)
            t = d.replace(hour=s.hh, minute=s.mm, second=0, microsecond=0)
            if d.weekday() in s.days and t > now:
                return t
    y, m = now.year, now.month
    t = _month_slot(y, m, s)
    if t <= now:
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
        t = _month_slot(y, m, s)
    return t


def is_due(s, last_run, now):
    """Whether a run is owed. However many slots were missed, it's one run.

    Parameters
    ----------
    s : Schedule
    last_run : float
        Epoch seconds of the last run (or of creation, for a new routine).
    now : float
        Epoch seconds.

    Returns
    -------
    bool
    """
    if s.kind == "every":
        return now - last_run >= s.every
    slot = last_slot(s, datetime.fromtimestamp(now))
    return slot is not None and last_run < slot.timestamp()


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def describe(text):
    """A schedule phrase as people say it: "weekly mon 08:00" → "Mondays · 08:00".

    Returns the phrase unchanged if it doesn't parse.
    """
    try:
        s = parse(text)
    except ValueError:
        return text
    at = f"{s.hh:02d}:{s.mm:02d}"
    if s.kind == "every":
        n, unit = (
            (s.every // 86400, "day")
            if s.every % 86400 == 0
            else (s.every // 3600, "hour")
        )
        return f"Every {unit}" if n == 1 else f"Every {n} {unit}s"
    if s.kind == "monthly":
        return f"Monthly · {_ordinal(s.dom)} · {at}"
    if s.days == frozenset(range(7)):
        return f"Daily · {at}"
    if s.days == frozenset(range(5)):
        return f"Weekdays · {at}"
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if len(s.days) == 1:
        (d,) = s.days
        full = [
            "Mondays",
            "Tuesdays",
            "Wednesdays",
            "Thursdays",
            "Fridays",
            "Saturdays",
            "Sundays",
        ][d]
        return f"{full} · {at}"
    days = [names[d] for d in sorted(s.days)]
    joined = ", ".join(days[:-1]) + " & " + days[-1]
    return f"{joined} · {at}"


def session_name(name):
    """The tmux session a routine runs in."""
    return SESSION_PREFIX + name


def check_name(name):
    """Raise ValueError unless `name` is a valid routine name (a lowercase slug)."""
    if not _SLUG.match(name or ""):
        raise ValueError("name: lowercase letters, digits and dashes, e.g. lit-digest")


# ---- registry (TOML) -----------------------------------------------------------
def load_registry(path=None):
    """Read the routines registry.

    Parameters
    ----------
    path : str

    Returns
    -------
    list of dict
        One dict per routine with every key in `FIELDS`. Empty if the file is
        missing.

    Raises
    ------
    ValueError
        If the file isn't valid TOML.
    """
    path = path or REGISTRY  # read at call time, so it can be redirected
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return []
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: {e}") from e
    out = []
    for r in data.get("routine", []):
        out.append(
            {
                "name": str(r.get("name", "")),
                "host": str(r.get("host", "")),
                "login": str(r.get("login", "")),
                "folder": str(r.get("folder", "~")) or "~",
                "schedule": str(r.get("schedule", "")),
                "enabled": bool(r.get("enabled", True)),
                # Claude asks "done?" when finished; yes closes the run
                "ask_done": bool(r.get("ask_done", True)),
                "prompt": str(r.get("prompt", "")).strip("\n"),
            }
        )
    return out


def _toml_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_multiline(s):
    # A basic multi-line string: escape backslashes and every quote so `"""` can
    # never appear; keep real newlines so the prompt stays readable.
    body = s.replace("\\", "\\\\").replace('"', '\\"')
    # other control characters aren't allowed raw in TOML strings
    body = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", lambda m: f"\\u{ord(m[0]):04x}", body)
    return '"""\n' + body + '\n"""'


def dump_registry(routines):
    """Render routines as TOML (the inverse of `load_registry`).

    Parameters
    ----------
    routines : list of dict

    Returns
    -------
    str
    """
    blocks = []
    for r in routines:
        lines = ["[[routine]]"]
        for k in FIELDS[:5]:
            lines.append(f"{k} = {_toml_str(r.get(k, ''))}")
        for k in ("enabled", "ask_done"):
            lines.append(f"{k} = {'true' if r.get(k, True) else 'false'}")
        lines.append(f"prompt = {_toml_multiline(r.get('prompt', ''))}")
        blocks.append("\n".join(lines))
    head = "# tuimux routines — edit here or from the dashboard (n / e).\n\n"
    return head + "\n\n".join(blocks) + ("\n" if blocks else "")


def save_registry(routines, path=None):
    """Write the registry atomically."""
    _write(path or REGISTRY, dump_registry(routines))


# ---- run state (JSON) ----------------------------------------------------------
def load_state(path=None):
    """Read ``{name: {"since", "last", "opened", "done", "runs"}}``.

    Returns an empty dict if the file is missing or unreadable. Entries from
    older versions load with ``opened=False``, ``done=None`` and ``runs=[]``.
    """
    try:
        with open(path or STATE) as f:
            data = json.load(f)
        out = {}
        for k, v in data.items():
            last, done = v.get("last"), v.get("done")
            out[str(k)] = {
                "since": float(v["since"]),
                "last": None if last is None else float(last),
                "opened": bool(v.get("opened", False)),
                "done": None if done is None else float(done),
                "runs": [_run(x) for x in v.get("runs", []) if isinstance(x, dict)],
            }
        return out
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        return {}


def _run(x):
    done = x.get("done")
    return {
        "start": float(x.get("start") or 0),
        "done": None if done is None else float(done),
        "late": bool(x.get("late", False)),
        "failed": bool(x.get("failed", False)),
        "outcome": x.get("outcome") or None,
    }


def add_run(entry, start, failed=False):
    """Record a new run (or a failed start) in `entry`, keeping the last HISTORY."""
    runs = entry.setdefault("runs", [])
    runs.append(_run({"start": start, "failed": failed}))
    del runs[:-HISTORY]


def current_run(entry):
    """The latest run that actually started (not a failed start), or None."""
    return next((r for r in reversed(entry.get("runs", [])) if not r["failed"]), None)


def base_time(entry):
    """The time a routine's schedule counts from: its last run, else its creation."""
    return entry["last"] if entry.get("last") is not None else entry["since"]


def new_entry(now):
    """State for a routine added at `now`: never run, so nothing to open or finish."""
    return {"since": now, "last": None, "opened": False, "done": None, "runs": []}


def save_state(state, path=None):
    """Write the run state atomically."""
    _write(path or STATE, json.dumps(state, indent=1, sort_keys=True))


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
