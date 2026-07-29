"""Unit tests for tuimux's pure logic — parsing, naming, and table rendering.

No network, SSH, or running Textual app required: the engine call is stubbed and
the dashboard's view-model (`_view`) is exercised directly. Run with `pytest`, or
standalone with `python tests/test_tuimux.py`.
"""

import contextlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tuimux import app  # noqa: E402
from tuimux.cli import tuimux_bin  # noqa: E402

# Hermetic env: tuimux exports several TUIMUX_* vars into the shells it spawns (e.g.
# TUIMUX_SELF_HOST when you run inside a tuimux session). If the test runner inherits
# them, they override the stubbed tailscale/identity these tests set up and cause
# spurious failures. Strip them once so the suite is independent of where it runs.
for _leak in (
    "TUIMUX_SELF_HOST",
    "TUIMUX_SCOPE",
    "TUIMUX_HOSTS",
    "TUIMUX_LOGINS",
    "TUIMUX_SELF_WINID",
):
    os.environ.pop(_leak, None)


@contextlib.contextmanager
def stub_engine(stdout):
    """Make app._run return canned engine output instead of spawning bash."""
    orig = app._run
    app._run = lambda args, timeout=None: SimpleNamespace(stdout=stdout)
    try:
        yield
    finally:
        app._run = orig


def _cell_text(cell):
    return "".join(text for text, _ in cell)


def _cell_styles(cell):
    return " ".join(style for _, style in cell if style)


# ---- small pure helpers ------------------------------------------------------
def test_abbrev_collapses_home():
    assert app._abbrev("/Users/alice/code") == "~/code"
    assert app._abbrev("/home/bob/x") == "~/x"
    assert app._abbrev("/etc/hosts") == "/etc/hosts"
    assert app._abbrev("") == ""


def test_uptime_buckets():
    assert app._uptime(None) == ""
    assert app._uptime("not-a-number") == ""
    now = int(app.time.time())
    assert app._uptime(now - 120).endswith("m")
    assert app._uptime(now - 7200).endswith("h")
    assert app._uptime(now - 200000).endswith("d")


def test_probe_counts_clients_per_session():
    # two clients attached to the same session → nclients == 2
    canned = "\n".join(
        [
            "OK",
            "S|main|1|1|/home/u|zsh|100",
            "L|main|xterm-ghostty",
            "L|main|xterm-256color",
        ]
    )
    with stub_engine(canned):
        info = app.probe("host", "u")
    main = info["sessions"][0]
    assert main["attached"] is True and main["nclients"] == 2


def test_auto_name():
    # a bare tmux number in a real folder → the folder's basename
    assert app._auto_name("0", "~/code/ersilia", False, "zsh") == "ersilia"
    # numeric session running an agent → claude
    assert app._auto_name("3", "~", True, "node") == "claude"
    # numeric session running a non-shell command → that command
    assert app._auto_name("3", "~", False, "vim") == "vim"
    # an explicitly named session is kept as-is
    assert app._auto_name("build", "~", False, "zsh") == "build"


def test_tuimux_bin_is_absolute_or_name():
    got = tuimux_bin()
    assert got == "tuimux" or got.endswith("tuimux")


# ---- CLI: attach / detach (replaced the old `here`) -------------------------
def test_here_command_is_gone_and_usage_advertises_attach_detach():
    r = subprocess.run(["bash", app.ENGINE, "here"], capture_output=True, text=True)
    assert r.returncode == 1  # unknown command → usage + non-zero
    out = r.stdout + r.stderr
    assert "tuimux attach" in out and "tuimux detach" in out
    assert "tuimux here" not in out


def test_detach_outside_tmux_is_friendly_noop():
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    r = subprocess.run(
        ["bash", app.ENGINE, "detach"], capture_output=True, text=True, env=env
    )
    assert r.returncode == 0  # not an error — just nothing to do
    assert "not inside a tmux session" in (r.stdout + r.stderr).lower()


# ---- CLI: autostart (auto-attach every new local terminal) ------------------
def _autostart(action, rc):
    r = subprocess.run(
        ["bash", app.ENGINE, "autostart", action],
        capture_output=True,
        text=True,
        env={**os.environ, "TUIMUX_RC": rc},
    )
    return r.stdout + r.stderr  # `note` reports on stderr


def test_autostart_on_off_status_and_idempotency():
    with tempfile.TemporaryDirectory() as d:
        rc = os.path.join(d, "rc")
        with open(rc, "w") as f:
            f.write("export FOO=1\nalias x=y\n")

        assert "off" in _autostart("status", rc)
        _autostart("on", rc)
        assert "on" in _autostart("status", rc)
        body = open(rc).read()
        assert "# >>> tuimux autostart >>>" in body and "export FOO=1" in body

        _autostart("on", rc)  # idempotent — exactly one block
        assert open(rc).read().count("# >>> tuimux autostart >>>") == 1

        _autostart("off", rc)
        after = open(rc).read()
        assert "tuimux autostart" not in after  # block removed
        assert "export FOO=1" in after and "alias x=y" in after  # rest untouched
        assert "off" in _autostart("status", rc)


# ---- CLI: mouse (tmux mouse-mode toggle) ------------------------------------
def _mouse(action, conf, tmpdir):
    # TMUX_TMPDIR points tmux at an empty socket dir, so the command's live
    # `tmux set -g mouse …` finds no server and can't touch a real one.
    r = subprocess.run(
        ["bash", app.ENGINE, "mouse", action],
        capture_output=True,
        text=True,
        env={**os.environ, "TUIMUX_TMUX_CONF": conf, "TMUX_TMPDIR": tmpdir},
    )
    return r.stdout + r.stderr


def test_mouse_on_off_status_and_idempotency():
    with tempfile.TemporaryDirectory() as d:
        conf = os.path.join(d, "tmux.conf")
        with open(conf, "w") as f:
            f.write("set -g status on\n")  # pre-existing config to preserve

        assert "off" in _mouse("status", conf, d)
        _mouse("on", conf, d)
        body = open(conf).read()
        assert "# >>> tuimux mouse >>>" in body and "set -g mouse on" in body
        assert "set -g status on" in body  # original config untouched
        assert "on" in _mouse("status", conf, d)

        _mouse("on", conf, d)  # idempotent — one block
        assert open(conf).read().count("# >>> tuimux mouse >>>") == 1

        _mouse("off", conf, d)
        after = open(conf).read()
        assert "tuimux mouse" not in after  # block removed
        assert "set -g status on" in after  # rest preserved
        assert "off" in _mouse("status", conf, d)


def test_mouse_state_command_reports_on_off():
    with tempfile.TemporaryDirectory() as d:
        conf = os.path.join(d, "tmux.conf")

        def state():
            return subprocess.run(
                ["bash", app.ENGINE, "__mouse"],
                capture_output=True,
                text=True,
                env={**os.environ, "TUIMUX_TMUX_CONF": conf, "TMUX_TMPDIR": d},
            ).stdout.strip()

        assert state() == "off"
        _mouse("on", conf, d)
        assert state() == "on"


def test_settings_command_batches_autostart_and_mouse():
    # The dashboard reads autostart + mouse in one engine spawn (__settings) at
    # startup instead of one per setting; it must emit parseable key=value lines.
    with tempfile.TemporaryDirectory() as d:
        conf = os.path.join(d, "tmux.conf")
        rc = os.path.join(d, "zshrc")
        env = {
            **os.environ,
            "TUIMUX_TMUX_CONF": conf,
            "TUIMUX_RC": rc,
            "TUIMUX_STATE_DIR": os.path.join(d, "state"),
            "TMUX_TMPDIR": d,
            "SHELL": "/bin/zsh",
        }
        out = subprocess.run(
            ["bash", app.ENGINE, "__settings"], capture_output=True, text=True, env=env
        ).stdout
        st = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
        assert set(st) == {"autostart", "mouse"}  # no tabcolor anymore
        assert st["autostart"] in ("on", "off") and st["mouse"] in ("on", "off")


# ---- first run: enable autostart + mouse by default, once -------------------
def test_first_run_enables_defaults_once_then_respects_off():
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "state")
        rc = os.path.join(d, "zshrc")
        conf = os.path.join(d, "tmux.conf")
        env = {
            **os.environ,
            "HOME": d,
            "TUIMUX_STATE_DIR": state,
            "TUIMUX_RC": rc,
            "TUIMUX_TMUX_CONF": conf,
            "TMUX_TMPDIR": d,  # isolate the live `tmux set` from any real server
            "TUIMUX_BIN": "/x/tuimux",
            "SHELL": "/bin/zsh",
        }

        def firstrun():
            subprocess.run(
                ["bash", app.ENGINE, "__firstrun"],
                env=env,
                capture_output=True,
                text=True,
            )

        firstrun()  # first run → both defaults on, marker written
        assert os.path.exists(os.path.join(state, "initialized"))
        assert "# >>> tuimux autostart >>>" in open(rc).read()
        assert "# >>> tuimux mouse >>>" in open(conf).read()

        # user turns autostart off; a later run must NOT re-enable it (marker guards)
        subprocess.run(
            ["bash", app.ENGINE, "autostart", "off"],
            env=env,
            capture_output=True,
            text=True,
        )
        firstrun()
        assert "tuimux autostart" not in open(rc).read()


def test_autostart_bash_links_login_profile():
    # bash login shells (macOS) read .bash_profile/.profile, not .bashrc — so
    # `autostart on` must also make the login profile source .bashrc, and `off`
    # must remove both blocks. Force SHELL=bash and point both files at temps.
    def run(action, bashrc, profile):
        return subprocess.run(
            ["bash", app.ENGINE, "autostart", action],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "SHELL": "/bin/bash",
                "TUIMUX_RC": bashrc,
                "TUIMUX_LOGIN_RC": profile,
            },
        )

    with tempfile.TemporaryDirectory() as d:
        bashrc = os.path.join(d, "bashrc")
        profile = os.path.join(d, "bash_profile")
        with open(profile, "w") as f:
            f.write("export PATH=/x\n")  # a login profile that does NOT source .bashrc

        run("on", bashrc, profile)
        assert "# >>> tuimux autostart >>>" in open(bashrc).read()
        prof = open(profile).read()
        assert "# >>> tuimux autostart (login) >>>" in prof  # linked to .bashrc
        assert '. "$HOME/.bashrc"' in prof

        run("on", bashrc, profile)  # idempotent — one login block
        assert open(profile).read().count("# >>> tuimux autostart (login) >>>") == 1

        run("off", bashrc, profile)
        assert "tuimux autostart" not in open(bashrc).read()
        after = open(profile).read()
        assert "tuimux autostart" not in after  # both blocks gone
        assert "export PATH=/x" in after  # original preserved


def test_autostart_bash_skips_link_when_profile_already_sources_bashrc():
    with tempfile.TemporaryDirectory() as d:
        bashrc = os.path.join(d, "bashrc")
        profile = os.path.join(d, "bash_profile")
        with open(profile, "w") as f:
            f.write("[ -f ~/.bashrc ] && . ~/.bashrc\n")  # already sources it
        subprocess.run(
            ["bash", app.ENGINE, "autostart", "on"],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "SHELL": "/bin/bash",
                "TUIMUX_RC": bashrc,
                "TUIMUX_LOGIN_RC": profile,
            },
        )
        # no duplicate source line added to the login profile
        assert "tuimux autostart (login)" not in open(profile).read()
        assert "# >>> tuimux autostart >>>" in open(bashrc).read()


def test_autostart_state_reports_on_off_for_the_panel():
    # `__autostart` is the machine-readable on/off the dashboard shows in its border.
    with tempfile.TemporaryDirectory() as d:
        rc = os.path.join(d, "rc")
        env = {**os.environ, "TUIMUX_RC": rc}

        def state():
            return subprocess.run(
                ["bash", app.ENGINE, "__autostart"],
                capture_output=True,
                text=True,
                env=env,
            ).stdout.strip()

        assert state() == "off"
        _autostart("on", rc)
        assert state() == "on"
        _autostart("off", rc)
        assert state() == "off"


def test_autostart_snippet_carries_every_guard():
    with tempfile.TemporaryDirectory() as d:
        rc = os.path.join(d, "rc")
        _autostart("on", rc)
        body = open(rc).read()
    for guard in (
        '[ -z "$TMUX" ]',  # not already in tmux
        '[ -z "$SSH_CONNECTION" ]',  # local only (remote is `tuimux init`)
        '[ -z "$TUIMUX_NO_AUTOTMUX" ]',  # per-shell opt-out
        "case $- in *i*",  # interactive shells only
        "skip-autostart",  # the tuimux-spawned-tab exemption marker
        "__autoname",  # auto-named (happy-curie), not a bare number
        "tmux new-session",  # each terminal → its own fresh session
    ):
        assert guard in body, guard


def test_autostart_names_session_portably_not_zsh_broken():
    # zsh doesn't word-split `${x:+-s "$x"}` the way bash does, which made tmux
    # create sessions named " name" (leading space) → unopenable. The snippet must
    # use an explicit, quoted `-s "$_tx_name"` instead.
    with tempfile.TemporaryDirectory() as d:
        rc = os.path.join(d, "rc")
        _autostart("on", rc)
        body = open(rc).read()
    assert "${_tx_name:+" not in body  # the zsh-broken splitting trick must be gone
    assert 'tmux new-session -s "$_tx_name"' in body


def test_autostart_bakes_absolute_tuimux_path():
    # A fresh terminal often hasn't activated the conda env tuimux lives in, so the
    # snippet must call tuimux by ABSOLUTE path (baked from TUIMUX_BIN), not bare.
    with tempfile.TemporaryDirectory() as d:
        rc = os.path.join(d, "rc")
        fake = "/opt/envs/tuimux/bin/tuimux"
        subprocess.run(
            ["bash", app.ENGINE, "autostart", "on"],
            capture_output=True,
            text=True,
            env={**os.environ, "TUIMUX_RC": rc, "TUIMUX_BIN": fake},
        )
        body = open(rc).read()
        assert f"'{fake}' __autoname" in body


def test_autoname_prints_a_two_word_name():
    r = subprocess.run(
        ["bash", app.ENGINE, "__autoname"], capture_output=True, text=True
    )
    # docker-style "<adjective>-<scientist>", same generator tuimux uses elsewhere
    assert r.returncode == 0
    assert re.fullmatch(r"[a-z]+-[a-z]+", r.stdout.strip()), r.stdout


def test_term_spawn_drops_skip_autostart_marker():
    # tuimux's own spawns mark the new tab so the rc snippet skips auto-attaching it.
    with tempfile.TemporaryDirectory() as d:
        mark = os.path.join(d, "skip")
        subprocess.run(
            [
                "bash",
                "-c",
                f"source {app.ENGINE} __login >/dev/null 2>&1; "
                "linux_spawn(){ :; }; ghostty_spawn(){ :; }; terminal_spawn(){ :; }; "
                "term_spawn tab bash x __attach h s attach",
            ],
            env={**os.environ, "TUIMUX_SKIP_AUTOSTART": mark},
            stdin=subprocess.DEVNULL,
        )
        assert os.path.exists(mark)


# ---- probe parsing -----------------------------------------------------------
CANNED = "\n".join(
    [
        "OK",
        "S|keep-awake|0|1|/|caffeinate|0",
        "S|main|1|1|/Users/u/code|zsh|0",
        "S|cl|0|1|/Users/u/agent|node|0",
        "W|main|0|zsh|1",
        "L|main|xterm-ghostty",
        "C|cl",
        "A|cl|waiting",
    ]
)


def test_probe_parses_sessions_awake_and_agent():
    with stub_engine(CANNED):
        info = app.probe("host", "u")
    assert info["reachable"] is True
    assert info["busy"] is False
    assert info["awake"] is True  # the keep-awake helper session is present
    # keep-awake is hidden; sessions are sorted by display name
    assert [s["auto"] for s in info["sessions"]] == ["cl", "main"]
    cl, main = info["sessions"]
    assert cl["agent"] is True and cl["state"] == "waiting"
    assert cl["attached"] is False and cl["nclients"] == 0  # not attached
    assert main["agent"] is False and main["state"] == "idle"  # zsh → idle
    assert main["attached"] is True and main["nclients"] == 1  # one client on host


def test_probe_timeout_marks_busy_not_failed():
    with stub_engine("__TIMEOUT__\n"):
        info = app.probe("host", "u")
    assert info["busy"] is True and info["reachable"] is False


def test_probe_unreachable():
    with stub_engine("UNREACHABLE\n"):
        info = app.probe("host", "u")
    assert info["reachable"] is False and info["busy"] is False


def test_probe_keeps_raw_created_for_reconnect_identity():
    with stub_engine(CANNED):
        info = app.probe("host", "u")
    # the raw session_created epoch is preserved (not just the formatted uptime),
    # so a reconnect can tell whether the very same session survived.
    assert all("created" in s for s in info["sessions"])
    assert {s["created"] for s in info["sessions"]} == {"0"}


# ---- reconnect reconciliation -----------------------------------------------
def _sess(name, created):
    return {"name": name, "created": created}


def test_reconcile_resumed_lost_and_quiet():
    old = [_sess("a", "100"), _sess("b", "200")]
    # a survived (same created), b is gone, c is brand new
    new = [_sess("a", "100"), _sess("c", "300")]
    assert app._reconcile_sessions(old, new) == {"a": "resumed", "b": "lost"}
    # same name but a new created → the old instance was lost (tmux restarted it)
    assert app._reconcile_sessions([_sess("a", "100")], [_sess("a", "999")]) == {
        "a": "lost"
    }
    # everything survived → all resumed (the happy reconnect case)
    assert app._reconcile_sessions(old, old) == {"a": "resumed", "b": "resumed"}
    assert app._reconcile_sessions([], []) == {}


# ---- view-model / rendering --------------------------------------------------
def _rekey(hosts, d):
    # Tests key results/snap/reconcile by host name; _view now looks them up by
    # (host, login). Translate using each host's login column (h[9]; "" for the
    # short tuples that predate per-host logins).
    if not d:
        return {}
    login_of = {h[0]: (h[9] if len(h) > 9 else "") for h in hosts}
    return {(k, login_of.get(k, "")): v for k, v in d.items()}


def _view_for(hosts, results, snap=None, reconcile=None):
    a = app.Tuimux()
    a._hosts = hosts
    a._results = _rekey(hosts, results)
    a._snap = _rekey(hosts, snap)
    a._reconcile = _rekey(hosts, reconcile)
    return a._view()


def test_view_status_words_per_state():
    hosts = [
        ("me", True, "online", "", "compute"),
        ("rem", False, "online", "", "compute"),
        ("busyh", False, "online", "", "compute"),
        ("noss", False, "online", "", "compute"),
        ("off", False, "offline", "2h ago", "compute"),
        ("pending", False, "online", "", "compute"),  # left unprobed → "checking…"
    ]
    base = {"reachable": False, "busy": False, "notmux": False, "awake": False}
    results = {
        "me": {**base, "reachable": True, "awake": True, "sessions": []},
        "rem": {**base, "reachable": True, "sessions": []},
        "busyh": {**base, "busy": True, "sessions": []},
        "noss": {**base, "sessions": []},
        "off": {**base, "sessions": [], "lastseen": "2h ago"},
    }
    rows = _view_for(hosts, results)
    status = {
        _cell_text(cells[0]).strip().lstrip("●○◐ "): _cell_text(cells[1])
        for cells, _ in rows
    }
    assert status["me"] == "local"
    assert status["rem"] == "online"
    assert status["busyh"] == "busy"
    assert status["noss"] == "no ssh"
    assert status["off"] == "offline"
    assert status["pending"] == "checking…"


def test_consumer_devices_show_status_only():
    # Phones/tablets are status-only: a "device" when up (never SSH'd, calm gray),
    # "offline" when down — never "no ssh" or "checking…".
    hosts = [
        ("phone-on", False, "online", "", "consumer"),
        ("phone-off", False, "offline", "1m ago", "consumer"),
    ]
    rows = _view_org(hosts, {})  # no probe results — must not show "checking…"
    status = {
        _cell_text(cells[0]).strip().lstrip("●○◐ "): _cell_text(cells[1])
        for cells, _ in rows
    }
    assert status["phone-on"] == "device"
    assert status["phone-off"] == "offline"
    # consumer rows are non-actionable
    assert all(m["action"] == "none" for _, m in rows)


def test_consumer_devices_sorted_last():
    raw = "comp\t0\toffline\t2h\tcompute\nphone\t0\tonline\t\tconsumer\nme\t1\tonline\t\tcompute\n"
    with stub_engine(raw):
        hosts = app.fetch_hosts()
    assert [h[0] for h in hosts] == ["comp", "me", "phone"]


def test_view_bold_only_on_identifiers():
    hosts = [
        ("me", True, "online", "", "compute"),
        ("off", False, "offline", "1h", "compute"),
    ]
    results = {
        "me": {
            "reachable": True,
            "busy": False,
            "notmux": False,
            "awake": False,
            "sessions": [
                {
                    "name": "a",
                    "auto": "att",
                    "attached": True,
                    "nclients": 1,
                    "dir": "~",
                    "tabs": "1",
                    "state": "running",
                    "uptime": "1h",
                    "agent": False,
                },
                {
                    "name": "b",
                    "auto": "det",
                    "attached": False,
                    "nclients": 0,
                    "dir": "~",
                    "tabs": "1",
                    "state": "idle",
                    "uptime": "1h",
                    "agent": False,
                },
            ],
        },
        "off": {
            "reachable": False,
            "busy": False,
            "notmux": False,
            "awake": False,
            "sessions": [],
            "lastseen": "1h",
        },
    }
    rows = _view_for(hosts, results)

    def find(label):
        return next(c for c, _ in rows if label in _cell_text(c[0]))

    def name_style(cells, text):
        # style of the segment in the NAME cell whose text is `text`
        return next(st for t, st in cells[0] if t.strip() == text)

    me, off, att, det = find("me"), find("off"), find("att"), find("det")
    # The machine NAME text is bold only when online; STATUS word/sessions never.
    assert "bold" in name_style(me, "me")
    assert "bold" not in name_style(off, "off")
    assert "bold" not in name_style(att, "att")
    assert "bold" not in name_style(det, "det")
    assert "bold" not in _cell_styles(me[1])  # STATUS word not bold
    # The status dot is ALWAYS bold (green=ssh / orange=online / hollow=offline).
    assert "bold" in name_style(me, "●")
    assert "bold" in name_style(off, "○")


def test_view_row_meta_actions():
    hosts = [("me", True, "online", "", "compute")]
    results = {
        "me": {
            "reachable": True,
            "busy": False,
            "notmux": False,
            "awake": False,
            "sessions": [
                {
                    "name": "s1",
                    "auto": "s1",
                    "attached": False,
                    "nclients": 0,
                    "dir": "~",
                    "tabs": "1",
                    "state": "idle",
                    "uptime": "1h",
                    "agent": False,
                },
            ],
        }
    }
    rows = _view_for(hosts, results)
    actions = [meta["action"] for _, meta in rows]
    assert actions == ["machine", "attach", "new"]
    assert rows[-1][1]["session"] == "__NEW__"


# ---- offline "paused" sessions + reconnect verdicts -------------------------
_OPEN_IN = app._COLS.index("open_in")


def _snap_session(name, created="100", state="running"):
    return {
        "name": name,
        "auto": name,
        "attached": False,
        "nclients": 0,
        "dir": "~/code",
        "tabs": f"1  {state}",
        "state": state,
        "created": created,
        "uptime": "1h",
        "agent": False,
    }


def test_offline_host_shows_remembered_sessions_as_unreachable():
    hosts = [("off", False, "offline", "2h ago", "compute")]
    base = {"reachable": False, "busy": False, "notmux": False, "awake": False}
    results = {"off": {**base, "sessions": [], "lastseen": "2h ago"}}
    snap = {"off": [_snap_session("build"), _snap_session("api")]}
    rows = _view_for(hosts, results, snap=snap)
    # machine row + its two remembered sessions (no "+ new" for an offline host)
    assert [m["action"] for _, m in rows] == ["none", "none", "none"]
    sess = rows[1:]
    assert {_cell_text(c[0]).strip() for c, _ in sess} == {"build", "api"}
    # each remembered session is flagged unreachable, and nothing is attachable
    assert all(_cell_text(c[_OPEN_IN]) == "unreachable" for c, _ in sess)
    assert all(m["session"] in ("build", "api") for _, m in sess)


def test_offline_host_without_snapshot_shows_only_the_machine_row():
    # no remembered sessions (e.g. never probed before going down) → unchanged
    hosts = [("off", False, "offline", "2h ago", "compute")]
    base = {"reachable": False, "busy": False, "notmux": False, "awake": False}
    results = {"off": {**base, "sessions": [], "lastseen": "2h ago"}}
    rows = _view_for(hosts, results)
    assert len(rows) == 1 and _cell_text(rows[0][0][1]) == "offline"


def test_reconnect_marks_resumed_and_lost():
    hosts = [("rem", False, "online", "", "compute")]
    live = _snap_session("build", created="100")
    live.update(auto="build", attached=False)
    results = {
        "rem": {
            "reachable": True,
            "busy": False,
            "notmux": False,
            "awake": False,
            "sessions": [live],
        }
    }
    # "build" survived (still live); "gone" did not come back
    reconcile = {"rem": (app.time.monotonic(), {"build": "resumed", "gone": "lost"})}
    rows = _view_for(hosts, results, reconcile=reconcile)
    texts = [_cell_text(c[0]).strip() for c, _ in rows]
    # the surviving session carries a "resumed" marker in its OPEN IN cell
    build_row = next(c for c, _ in rows if _cell_text(c[0]).strip() == "build")
    assert "resumed" in _cell_text(build_row[_OPEN_IN])
    # the lost session gets its own transient row that isn't attachable
    lost = next((c, m) for c, m in rows if _cell_text(c[0]).strip() == "gone")
    assert _cell_text(lost[0][_OPEN_IN]) == "lost — not restored"
    assert lost[1]["action"] == "none"
    assert "gone" in texts


def test_reconnect_verdicts_age_out_after_ttl():
    hosts = [("rem", False, "online", "", "compute")]
    live = _snap_session("build", created="100")
    results = {
        "rem": {
            "reachable": True,
            "busy": False,
            "notmux": False,
            "awake": False,
            "sessions": [live],
        }
    }
    stale = app.time.monotonic() - app.RECONCILE_TTL - 1
    reconcile = {"rem": (stale, {"build": "resumed", "gone": "lost"})}
    rows = _view_for(hosts, results, reconcile=reconcile)
    # past the TTL: no "resumed" badge, no transient "lost" row
    assert not any("resumed" in _cell_text(c[_OPEN_IN]) for c, _ in rows)
    assert not any(_cell_text(c[0]).strip() == "gone" for c, _ in rows)


# ---- window location ("OPEN IN") --------------------------------------------
def test_window_locs_this_vs_other():
    a = app.Tuimux()
    # tuimux + "main" share window 1; "build" is alone in window 2
    a._windows = [("1", "tuimux"), ("1", "main · zsh"), ("2", "build · vim")]
    a._self_win = "1"
    assert a._window_locs("main") == ["this window"]
    assert a._window_locs("build") == ["other window"]
    assert a._window_locs("missing") == []  # no matching tab


def test_window_locs_handles_terminal_app_decorations():
    # Terminal.app decorates the "#S · #W" title tmux sets into
    # "<login> — <session> · <window> — <proc> — <WxH>". _title_parts splits on the
    # em-dash so the decorations don't defeat the match. (Terminal.app opens each
    # session as its own window, so they read "other window", never "this window".)
    a = app.Tuimux()
    a._windows = [
        ("1", "miquel — tuimux — -zsh — 80×24"),  # the dashboard itself
        ("2", "miquel — main · zsh — tmux — 80×24"),
        ("3", "miquel — build · vim — tmux — 120×40"),
    ]
    a._self_win = "1"
    assert a._window_locs("main") == ["other window"]
    assert a._window_locs("build") == ["other window"]
    assert a._window_locs("missing") == []


def test_open_in_cell():
    a = app.Tuimux()
    a._windows = [("1", "tuimux"), ("1", "main · zsh"), ("2", "build · vim")]
    a._self_win = "1"

    def cell(s):
        # the word tokens, dropping the dim " · " separator
        segs = a._open_in_cell(s, a._window_locs(s["name"]))
        return [seg[0] for seg in segs if seg[0] != " · "]

    # a local tab → just that one token; the host attachment is implied
    assert cell({"name": "main", "attached": True, "nclients": 1}) == ["this window"]
    assert cell({"name": "build", "attached": True, "nclients": 1}) == ["other window"]
    # a local tab wins even if the host shows it detached (you can still jump there)
    assert cell({"name": "main", "attached": False, "nclients": 0}) == ["this window"]
    # no local tab → "— · <attachment on the owning host>"
    assert cell({"name": "x", "attached": False, "nclients": 0}) == ["—", "detached"]
    assert cell({"name": "zzz", "attached": True, "nclients": 1}) == ["—", "1 client"]
    assert cell({"name": "zzz", "attached": True, "nclients": 2}) == ["—", "2 clients"]
    # switch-client'd away: attached with no client line → still a held client
    assert cell({"name": "zzz", "attached": True, "nclients": 0}) == ["—", "1 client"]
    # we never say "on host" anymore (confusing for local sessions)
    for s in (
        {"name": "main", "attached": True, "nclients": 1},
        {"name": "zzz", "attached": True, "nclients": 3},
    ):
        words = " ".join(cell(s))
        assert "on host" not in words


def _session(name, **kw):
    return {
        "name": name,
        "auto": name,
        "attached": kw.get("attached", False),
        "nclients": kw.get("nclients", 0),
        "dir": kw.get("dir", "~/p"),
        "tabs": "1  zsh",
        "state": kw.get("state", "running"),
        "uptime": "1h",
        "created": "1",
        "agent": kw.get("agent", False),
    }


def test_session_rows_use_device_accent_and_dim_metadata():
    base = {"reachable": True, "busy": False, "notmux": False, "awake": False}
    hosts = [("rem", False, "online", "", "compute", "arnau", "", True, "#abcdef")]
    results = {
        "rem": {
            **base,
            "sessions": [
                _session("att", attached=True, state="running"),
                _session("idle1", attached=False, state="idle"),
                _session("run1", attached=False, state="running"),
            ],
        }
    }
    rows = _view_for(hosts, results)

    def cells_for(n):
        return next(c for c, _ in rows if n in _cell_text(c[0]))

    # accent (never bold); idle+unattached → dim; attached/running → plain accent
    att_name = _cell_styles(cells_for("att")[0])
    assert "#abcdef" in att_name and "bold" not in att_name
    assert "dim" in _cell_styles(cells_for("idle1")[0])
    run_name = _cell_styles(cells_for("run1")[0])
    assert "#abcdef" in run_name and "bold" not in run_name
    # folder (col 4) is dim metadata now, not a fixed cyan
    assert _cell_styles(cells_for("att")[4]) == "dim"


def test_waiting_agent_is_amber_working_is_not():
    base = {"reachable": True, "busy": False, "notmux": False, "awake": False}
    hosts = [("rem", False, "online", "", "compute", "arnau", "", True, "#abcdef")]
    results = {
        "rem": {
            **base,
            "sessions": [
                _session("w", agent=True, state="waiting"),
                _session("k", agent=True, state="working"),
            ],
        }
    }
    rows = _view_for(hosts, results)
    state = {}  # session name → STATE cell styles (col 2)
    for c, m in rows:
        if m.get("session") in ("w", "k"):
            state[m["session"]] = _cell_styles(c[2])
    assert app.AMBER in state["w"]  # waiting wants you → amber
    assert app.AMBER not in state["k"]  # working is calm


# ---- run(): tuimux must never nest inside tmux ------------------------------
@contextlib.contextmanager
def _patched_run(tmux):
    """Stub Tuimux + $TMUX so run() can be exercised offline. Yields `launched`:
    whether the dashboard was actually started in-process."""
    launched = []
    orig_tuimux = app.Tuimux
    had_tmux = "TMUX" in os.environ
    prev_tmux = os.environ.get("TMUX")
    app.Tuimux = lambda: SimpleNamespace(run=lambda: launched.append(True))
    if tmux is None:
        os.environ.pop("TMUX", None)
    else:
        os.environ["TMUX"] = tmux
    try:
        yield launched
    finally:
        app.Tuimux = orig_tuimux
        if had_tmux:
            os.environ["TMUX"] = prev_tmux
        else:
            os.environ.pop("TMUX", None)


def test_run_inside_tmux_refuses_and_recommends_detach():
    with _patched_run(tmux="/tmp/tmux-501/default,9,0") as launched:
        try:
            app.run()
        except SystemExit as e:
            msg = str(e)
        else:
            raise AssertionError("expected SystemExit when run inside tmux")
    assert launched == []  # never opens a dashboard nested in the session
    assert "tuimux detach" in msg  # the one recommended way out


def test_run_outside_tmux_launches_dashboard():
    with _patched_run(tmux=None) as launched:
        app.run()
    assert launched == [True]


# ---- Linux spawn command builder (engine.sh) --------------------------------
# These shell out to the engine forcing TUIMUX_OS=linux + TUIMUX_DRY_RUN=1, so they
# exercise the Linux launch-command construction on any platform (incl. this macOS
# box) without ever opening a GUI window or touching osascript.
def _engine_dry(args, env=None):
    e = {**os.environ, "TUIMUX_OS": "linux", "TUIMUX_DRY_RUN": "1"}
    if env:
        e.update(env)
    return subprocess.run(
        ["bash", app.ENGINE, *args], capture_output=True, text=True, env=e
    ).stdout


def test_linux_gnome_tab():
    out = _engine_dry(
        ["__open", "tab", "macmini", "mduran", "main", "attach"],
        {"TUIMUX_TERM": "gnome"},
    )
    assert (
        out.strip()
        == f"[dry-run] gnome-terminal --tab -- bash {app.ENGINE} __attach macmini mduran main attach"
    )


def test_linux_gnome_window_carries_identity_and_quotes_spaces():
    out = _engine_dry(
        ["__open", "window", "macmini", "mduran", "weird name", "attach"],
        {"TUIMUX_TERM": "gnome", "TUIMUX_SELF_HOST": "mybox"},
    )
    assert "gnome-terminal --window -- env TUIMUX_SELF_HOST=mybox bash" in out
    assert "weird\\ name attach" in out  # the spaced session name stays one token


def test_linux_custom_template_wraps_via_sh():
    out = _engine_dry(
        ["__open", "tab", "macmini", "mduran", "main", "attach"],
        {"TUIMUX_TERM_CMD": "kitty -e sh -c {cmd}"},
    )
    assert out.startswith("[dry-run] sh -c ")
    # {cmd} expanded to the engine invocation, wrapped for the user's terminal
    for piece in ("kitty", "__attach", "macmini", "mduran", "main"):
        assert piece in out


def test_linux_generic_terminal_window_only():
    out = _engine_dry(
        ["__open", "window", "macmini", "mduran", "main", "new"],
        {"TUIMUX_TERM": "generic", "TERMINAL": "alacritty"},
    )
    assert (
        out.strip()
        == f"[dry-run] alacritty -e bash {app.ENGINE} __attach macmini mduran main new"
    )


def test_linux_wayland_auto_falls_through_to_new_surface():
    # Wayland blocks jump-to-window, so `auto` can't focus → opens a fresh tab.
    out = _engine_dry(
        ["__open", "auto", "macmini", "mduran", "happy-curie", "attach"],
        {"TUIMUX_TERM": "gnome", "XDG_SESSION_TYPE": "wayland"},
    )
    assert "[dry-run] gnome-terminal --tab -- bash" in out
    assert "__attach macmini mduran happy-curie attach" in out


# ---- new-tab targeting (engine.sh) ------------------------------------------
# osascript can't run headless, so we source the engine and stub it. The macOS
# spawners feed their AppleScript to osascript on stdin (tab) or via -e (window);
# the stub appends stdin to a capture file so we can assert what each path builds.
def _engine_func(call, env=None, stubs=""):
    e = {**os.environ}
    if env:
        e.update(env)
    script = (
        f"source {app.ENGINE} __login >/dev/null 2>&1; "
        "osascript(){ return 0; }; "  # never drive the GUI in tests
        f"{stubs}{call}"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=e,
        stdin=subprocess.DEVNULL,
    ).stdout


def test_ghostty_new_tab_targets_dashboard_window():
    # Ghostty finds its window by the visible "tuimux" title: a new *tab* raises it
    # first; a new *window* does not.
    stubs = "ghostty_raise_self(){ echo RAISED_SELF; }; "
    tab = _engine_func("ghostty_spawn t bash x __attach h s attach", stubs=stubs)
    win = _engine_func("ghostty_spawn n bash x __attach h s attach", stubs=stubs)
    assert "RAISED_SELF" in tab  # new tab → opened into the dashboard window
    assert "RAISED_SELF" not in win  # new window → a brand-new window, left alone


def test_terminal_new_tab_pins_dashboard_window_id():
    # Terminal.app hides our title, so a new *tab* pins the dashboard by the window
    # id we captured at startup (set index … to 1) before Cmd-T; a *window* doesn't.
    with tempfile.TemporaryDirectory() as d:
        cap = os.path.join(d, "cap")
        stubs = (
            "build_exec_cmd(){ echo CMD; }; osa_escape(){ printf '%s' \"$1\"; }; "
            f"osascript(){{ cat >> {cap} 2>/dev/null; return 0; }}; "
        )
        env = {"TUIMUX_SELF_WINID": "4242"}
        open(cap, "w").close()
        _engine_func("terminal_spawn tab bash x", env=env, stubs=stubs)
        tab_script = open(cap).read()
        open(cap, "w").close()
        _engine_func("terminal_spawn window bash x", env=env, stubs=stubs)
        win_script = open(cap).read()
    assert "set index of window id 4242 to 1" in tab_script
    assert 'keystroke "t"' in tab_script  # the new tab itself
    assert "window id 4242" not in win_script  # a window is brand-new, not pinned


def test_terminal_new_tab_without_winid_does_not_pin():
    # No captured id (e.g. Ghostty/Linux, or capture failed) → no pin, just front
    # window — degrades to the old behaviour, never errors.
    with tempfile.TemporaryDirectory() as d:
        cap = os.path.join(d, "cap")
        stubs = (
            "build_exec_cmd(){ echo CMD; }; osa_escape(){ printf '%s' \"$1\"; }; "
            f"osascript(){{ cat >> {cap} 2>/dev/null; return 0; }}; "
        )
        open(cap, "w").close()
        _engine_func("terminal_spawn tab bash x", stubs=stubs)  # no TUIMUX_SELF_WINID
        tab_script = open(cap).read()
    assert "set index of window id" not in tab_script
    assert 'keystroke "t"' in tab_script


def test_linux_list_windows_x11_keeps_only_terminals():
    with tempfile.TemporaryDirectory() as d:
        stub = os.path.join(d, "wmctrl")
        with open(stub, "w") as f:
            f.write(
                "#!/bin/bash\n"
                'if [ "$1" = "-lx" ]; then\n'
                '  echo "0x01 0 gnome-terminal-server.Gnome-terminal h tuimux"\n'
                '  echo "0x02 0 gnome-terminal-server.Gnome-terminal h happy-curie · main"\n'
                '  echo "0x03 0 firefox.Firefox h Mozilla Firefox"\n'
                "fi\n"
            )
        os.chmod(stub, 0o755)
        out = _engine_dry(
            ["__windows"],
            {"XDG_SESSION_TYPE": "x11", "PATH": d + os.pathsep + os.environ["PATH"]},
        )
    # firefox dropped; terminals emitted as "<n>|<title>" for the OPEN IN column
    assert out.strip().splitlines() == ["1|tuimux", "2|happy-curie · main"]


def test_preview_tail_trims_trailing_blanks_and_keeps_newest():
    # A tmux pane pads empty rows below the prompt; the preview drops them so the
    # last real line lands at the bottom, and keeps only the newest `height`.
    raw = "line1\nline2\nline3\nprompt $ \n\n\n"
    assert app._preview_tail(raw, 10) == ["line1", "line2", "line3", "prompt $ "]
    assert app._preview_tail(raw, 2) == ["line3", "prompt $ "]


def test_preview_tail_empty_capture_is_empty_list():
    assert app._preview_tail("", 12) == []
    assert app._preview_tail("\n\n  \n", 12) == []


def test_peek_with_missing_args_is_an_empty_noop():
    # The engine's __peek must not error or capture anything when it has no
    # session to point at (e.g. cursor on a machine header) — it just returns
    # nothing, which the UI renders as an empty preview.
    r = subprocess.run(
        ["bash", app.ENGINE, "__peek", "somehost"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == ""


# ---- per-host logins + org fleet (engine.sh) --------------------------------
# A fake `tailscale` (function on PATH-less shell) emitting a canned multi-owner
# status, so discovery/hosts_data can be exercised without a real tailnet. Self is
# me@ at 100.0.0.1; herbert/curie are arnau@; phone is an iOS consumer.
_TS_STUB = r"""tailscale(){ case "$1" in
  ip) echo 100.0.0.1 ;;
  status) cat <<'EOF'
100.0.0.1  mybox     me@      linux  -
100.0.0.2  herbert   arnau@   linux  -
100.0.0.3  phone     me@      iOS    -
100.0.0.4  curie     arnau@   macOS  offline, last seen 2h ago
EOF
  ;; esac; }; """


def _engine_eval(call, env=None, scope=None):
    """Source the engine (with a stubbed tailscale) and run a shell `call`,
    returning its stdout. CONFIG points at /dev/null so a real user config can't
    leak into the test."""
    e = {**os.environ, "TUIMUX_CONFIG": "/dev/null"}
    e.pop("TUIMUX_LOGINS", None)
    if scope:
        e["TUIMUX_SCOPE"] = scope
    if env:
        e.update(env)
    script = f"source {app.ENGINE} __login >/dev/null 2>&1; {_TS_STUB} {call}"
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=e
    ).stdout


def _engine_out(args, env=None):
    e = {**os.environ, "TUIMUX_CONFIG": "/dev/null"}
    e.pop("TUIMUX_LOGINS", None)
    if env:
        e.update(env)
    return subprocess.run(
        ["bash", app.ENGINE, *args], capture_output=True, text=True, env=e
    ).stdout


def test_login_for_resolves_mapping_else_default():
    env = {"TUIMUX_LOGINS": "herbert=mduran nebula=arnaul", "USER": "miquel"}
    assert _engine_out(["__loginfor", "herbert"], env).strip() == "mduran"
    assert _engine_out(["__loginfor", "nebula"], env).strip() == "arnaul"
    assert _engine_out(["__loginfor", "macmini"], env).strip() == "miquel"  # fallback


def test_login_cli_set_list_rm_and_validation():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config")
        env = {"TUIMUX_CONFIG": cfg, "USER": "miquel"}
        _engine_out(["login", "herbert", "mduran"], env)
        _engine_out(["login", "nebula", "mduran"], env)
        txt = Path(cfg).read_text()
        assert txt.count("TUIMUX_LOGINS=") == 1  # one line, never duplicated
        assert "herbert=mduran" in txt and "nebula=mduran" in txt
        # replacing a host rewrites its token in place, still one line
        _engine_out(["login", "herbert", "mfrigola"], env)
        txt = Path(cfg).read_text()
        assert txt.count("TUIMUX_LOGINS=") == 1
        assert "herbert=mfrigola" in txt and "herbert=mduran" not in txt
        # list shows the current mappings
        listing = _engine_out(["login"], env)
        assert "herbert" in listing and "mfrigola" in listing
        # --rm drops just that one
        _engine_out(["login", "--rm", "herbert"], env)
        txt = Path(cfg).read_text()
        assert "herbert=" not in txt and "nebula=mduran" in txt
        # an invalid token is rejected and leaves the config untouched
        before = Path(cfg).read_text()
        r = subprocess.run(
            ["bash", app.ENGINE, "login", "foo", "bad user"],
            capture_output=True,
            text=True,
            env={**os.environ, **env},
        )
        assert r.returncode != 0
        assert Path(cfg).read_text() == before


def test_discover_returns_all_online_regardless_of_scope():
    # Discovery is now owner-agnostic: self + every ONLINE peer, any owner (the app
    # decides what to SHOW per scope). curie is offline → never discovered.
    mine = _engine_eval("discover_hosts", scope="mine").split()
    org = _engine_eval("discover_hosts", scope="org").split()
    assert set(mine) == {"mybox", "herbert", "phone"}
    assert set(org) == {"mybox", "herbert", "phone"}
    assert "curie" not in mine and "mybox" == mine[0]  # self leads


def test_discover_includes_mapped_foreign_host():
    # A teammate-owned host you've mapped a login for shows up even in mine scope.
    mine = _engine_eval(
        "discover_hosts", env={"TUIMUX_LOGINS": "herbert=mduran"}, scope="mine"
    ).split()
    assert "herbert" in mine


def test_hosts_data_columns_owner_mapping_probe():
    rows = [
        ln.split("\t") for ln in _engine_eval("hosts_data", scope="org").splitlines()
    ]
    by = {r[0]: r for r in rows}
    # name islocal status lastseen kind owner mapping probe
    assert by["mybox"][1] == "1" and by["mybox"][5] == "me" and by["mybox"][7] == "1"
    assert by["phone"][4] == "consumer" and by["phone"][7] == "0"  # never SSH'd
    # foreign, unmapped, but online + compute → now probeable (access is discovered
    # by trying); owner recorded, mapping still empty
    assert (
        by["herbert"][5] == "arnau"
        and by["herbert"][6] == ""
        and by["herbert"][7] == "1"
    )
    assert (
        by["curie"][2] == "offline" and by["curie"][7] == "0"
    )  # offline → never probed
    # mapping a foreign host flips it to probe=1 and records the login
    rows2 = [
        ln.split("\t")
        for ln in _engine_eval(
            "hosts_data", env={"TUIMUX_LOGINS": "herbert=mduran"}, scope="org"
        ).splitlines()
    ]
    h = {r[0]: r for r in rows2}["herbert"]
    assert h[6] == "mduran" and h[7] == "1"


def _view_org(hosts, results):
    a = app.Tuimux()
    a._scope = "org"
    a._hosts = hosts
    a._results = _rekey(hosts, results)
    return a._view()


def test_view_user_column_shows_owner_and_login():
    base = {"reachable": False, "busy": False, "notmux": False, "awake": False}
    # full host tuples incl color + resolved login (last two columns)
    hosts = [
        (
            "mybox",
            True,
            "online",
            "",
            "compute",
            "miquel",
            "",
            True,
            "#34d8b1",
            "mduranfrigola",
        ),
        (
            "herbert",
            False,
            "online",
            "",
            "compute",
            "arnau",
            "",
            False,
            "#73ccde",
            "mduranfrigola",
        ),
        (
            "pujarnol",
            False,
            "online",
            "",
            "compute",
            "gemma",
            "mduran",
            True,
            "#d573de",
            "mduran",
        ),
    ]
    results = {
        "mybox": {**base, "reachable": True, "sessions": []},
        "pujarnol": {**base, "reachable": True, "sessions": []},
    }
    rows = _view_org(hosts, results)
    owner_col = app._COLS.index("owner")
    user_col = app._COLS.index("user")

    def cells_of(label):
        return next(c for c, _ in rows if label in _cell_text(c[0]))

    def owner_of(label):
        return _cell_text(cells_of(label)[owner_col])

    def user_of(label):
        return _cell_text(cells_of(label)[user_col])

    # OWNER column = tailnet account (shown as "handle@"); SSH USER = login we use.
    # local: owner in one column, our login in the other
    assert owner_of("mybox") == "miquel@" and user_of("mybox") == "mduranfrigola"
    # unmapped foreign (no account): owner shown, SSH USER blank
    assert owner_of("herbert") == "arnau@" and user_of("herbert") == ""
    # mapped foreign: owner + the login we connect as, each in its own column
    assert owner_of("pujarnol") == "gemma@" and user_of("pujarnol") == "mduran"
    # herbert has no probe result here → shown as "checking…", SSH USER blank
    herbert = cells_of("herbert")
    assert "checking…" in _cell_text(herbert[1])


def test_owner_and_login_cells_split():
    owner_cell = app.Tuimux._owner_cell
    login_cell = app.Tuimux._login_cell
    # OWNER: "handle@" on the primary row (domain dropped whether given a bare
    # handle or a full email); blank on secondary sub-rows / when unknown
    assert owner_cell("miquel", True) == (("miquel@", "dim"),)
    assert owner_cell("miquel@ersilia.io", True) == (("miquel@", "dim"),)
    assert owner_cell("miquel", False) == ()
    assert owner_cell("", True) == ()
    # tag-owned nodes are not accounts: rendered bare, never "tag:dev@"
    assert owner_cell("tag:dev", True) == (("tag:dev", "dim"),)
    assert owner_cell("tagged-devices", True) == (
        ("tagged-devices", "dim"),
    )  # fallback
    assert owner_cell("tag:dev", False) == ()
    # SSH USER (login, confirmed, mapping): show only when it's a real account —
    # a box we reached (confirmed), or one you've explicitly mapped a login for.
    assert login_cell("mduranfrigola", True, "") == (
        ("mduranfrigola", "dim"),
    )  # reached
    assert login_cell("mduranfrigola", False, "mduranfrigola") == (
        ("mduranfrigola", "dim"),
    )  # explicitly mapped
    assert login_cell("mduranfrigola", False, "") == ()  # speculative default → hidden
    assert login_cell("", True, "") == ()


def test_tabs_cell_tints_claude():
    tc = app.Tuimux._tabs_cell
    assert tc("1  zsh") == (("1  zsh", "dim"),)
    agent = tc("2  claude")
    assert agent[-1] == ("claude", app.VIOLET)  # command tinted violet
    assert "dim" in agent[0][1]  # the count stays dim


def test_status_dot_color_tracks_connection():
    base = {"busy": False, "notmux": False, "awake": False}
    hosts = [
        ("ok", False, "online", "", "compute"),
        ("noss", False, "online", "", "compute"),
        ("off", False, "offline", "2h", "compute"),
    ]
    results = {
        "ok": {**base, "reachable": True, "sessions": []},
        "noss": {**base, "reachable": False, "sessions": []},
        "off": {**base, "reachable": False, "sessions": [], "lastseen": "2h"},
    }
    rows = _view_for(hosts, results)
    dot = {}
    for cells, m in rows:
        if m.get("host") and m.get("session") is None:
            dot[m["host"]] = next(
                (t.strip(), st) for t, st in cells[0] if t.strip() in ("●", "○")
            )
    assert dot["ok"] == ("●", f"bold {app.GREEN}")  # reachable over SSH → green
    assert dot["noss"] == ("●", f"bold {app.AMBER}")  # online, no SSH → orange
    assert dot["off"] == ("○", f"bold {app.MUTED}")  # offline → hollow gray


def test_offline_host_with_probe_zero_renders_offline():
    # Offline hosts come back with probe=0 and are never probed (info stays None),
    # so the offline branch must win over "no login"/"checking" — decided by status.
    hosts = [
        (
            "down",
            False,
            "offline",
            "3h ago",
            "compute",
            "gemma",
            "",
            False,
            "#b08cff",
            "me",
        )
    ]
    rows = _view_org(
        hosts, {}
    )  # org scope — a teammate's offline box is hidden by default
    cells, meta = rows[0]
    assert meta["action"] == "none"
    assert "offline" in _cell_text(cells[1]) and "3h ago" in _cell_text(cells[2])
    dot = next(t.strip() for t, _ in cells[0] if t.strip() in ("●", "○"))
    assert dot == "○"


def test_view_org_unmapped_row_is_login_actionable():
    hosts = [("herbert", False, "online", "", "compute", "arnau", "", False)]
    rows = _view_org(hosts, {})
    _cells, meta = rows[0]
    assert meta["host"] == "herbert" and meta["action"] == "machine"
    assert meta.get("consumer") is False  # u (set login) applies


# ---- absolute per-machine colours (engine.sh) -------------------------------
# Sorted fleet from the stub: curie(0) herbert(1) mybox(2) phone(3).
def test_fleet_index_follows_sorted_order():
    assert _engine_eval("fleet_index curie").strip() == "0"
    assert _engine_eval("fleet_index herbert").strip() == "1"
    assert _engine_eval("fleet_index phone").strip() == "3"


def test_host_color_local_teal_remotes_distinct_and_stable():
    # mybox is self in the stub → always teal; others get distinct fleet-index hues.
    assert _engine_eval("host_color mybox").strip() == "#34d8b1"
    herb = _engine_eval("host_color herbert").strip()
    phone = _engine_eval("host_color phone").strip()
    assert re.match(r"^#[0-9a-f]{6}$", herb) and re.match(r"^#[0-9a-f]{6}$", phone)
    assert herb != phone  # different index → different colour
    assert _engine_eval("host_color herbert").strip() == herb  # deterministic


def test_fleet_palette_excludes_local_teal_band():
    # The golden-angle palette carves out a band around the local machine's fixed
    # teal (~166°) so no remote host ever reads as a near-teal look-alike.
    import colorsys

    def hue(hexs):
        r, g, b = (int(hexs[i : i + 2], 16) / 255 for i in (1, 3, 5))
        return colorsys.rgb_to_hls(r, g, b)[0] * 360

    teal = hue("#34d8b1")
    for i in range(24):
        c = _engine_eval(f"color_for_index {i}").strip()
        assert re.match(r"^#[0-9a-f]{6}$", c)
        d = abs(hue(c) - teal)
        assert min(d, 360 - d) > 20, f"index {i} → {c} is too close to teal"
    # the two code paths (color_for_index and hosts_data's inline hsl) must agree
    rows = [
        ln.split("\t") for ln in _engine_eval("hosts_data", scope="org").splitlines()
    ]
    by = {r[0]: r for r in rows}
    idx = int(_engine_eval("fleet_index herbert").strip())
    assert by["herbert"][8] == _engine_eval(f"color_for_index {idx}").strip()


def test_hosts_data_emits_color_column():
    rows = [
        ln.split("\t") for ln in _engine_eval("hosts_data", scope="org").splitlines()
    ]
    by = {r[0]: r for r in rows}
    assert by["mybox"][8] == "#34d8b1"  # local → teal
    assert re.match(r"^#[0-9a-f]{6}$", by["herbert"][8])
    assert by["herbert"][8] != by["phone"][8]


def test_view_uses_engine_color_for_machine_header():
    # The dashboard must paint the header with the engine-supplied colour (so it
    # matches the tmux bar), not recompute its own.
    hosts = [("rem", False, "online", "", "compute", "arnau", "", True, "#abcdef")]
    base = {"reachable": True, "busy": False, "notmux": False, "awake": False}
    rows = _view_for(hosts, {"rem": {**base, "sessions": []}})
    header = rows[0][0]
    styles = _cell_styles(header[0]) + " " + _cell_styles(header[1])
    assert "#abcdef" in styles


# ---- multiple logins per host ----------------------------------------------
def test_login_cli_comma_list_add_and_rm_one_user():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config")
        env = {"TUIMUX_CONFIG": cfg, "USER": "miquel"}
        # set a two-user list; first is the primary
        _engine_out(["login", "splunk", "mduranfrigola,gturon"], env)
        assert "splunk=mduranfrigola,gturon" in Path(cfg).read_text()
        assert _engine_out(["__loginfor", "splunk"], env).strip() == "mduranfrigola"
        assert (
            _engine_out(["__loginsfor", "splunk"], env).strip()
            == "mduranfrigola,gturon"
        )
        # --add appends (and dedups)
        _engine_out(["login", "--add", "splunk", "marina"], env)
        _engine_out(["login", "--add", "splunk", "gturon"], env)  # dup → no-op
        assert "splunk=mduranfrigola,gturon,marina" in Path(cfg).read_text()
        # --rm one user keeps the rest; removing the last drops the token entirely
        _engine_out(["login", "--rm", "splunk", "gturon"], env)
        assert "splunk=mduranfrigola,marina" in Path(cfg).read_text()
        _engine_out(["login", "--rm", "splunk", "mduranfrigola"], env)
        _engine_out(["login", "--rm", "splunk", "marina"], env)
        assert "splunk=" not in Path(cfg).read_text()


def test_login_cli_rejects_duplicate_user_list():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config")
        before = Path(cfg).read_text() if os.path.exists(cfg) else ""
        r = subprocess.run(
            ["bash", app.ENGINE, "login", "splunk", "a,a"],
            capture_output=True,
            text=True,
            env={**os.environ, "TUIMUX_CONFIG": cfg, "USER": "miquel"},
        )
        assert r.returncode != 0
        assert (Path(cfg).read_text() if os.path.exists(cfg) else "") == before


def test_hosts_data_fans_out_multiple_logins():
    rows = [
        ln.split("\t")
        for ln in _engine_eval(
            "hosts_data",
            env={"TUIMUX_LOGINS": "herbert=mduran,gturon", "USER": "me"},
            scope="org",
        ).splitlines()
    ]
    herbert = [r for r in rows if r[0] == "herbert"]
    assert len(herbert) == 2  # one row per mapped login
    # login (col 10) + mapping (col 7) + primary (col 11) per row
    assert [r[9] for r in herbert] == ["mduran", "gturon"]
    assert [r[6] for r in herbert] == ["mduran", "gturon"]
    assert [r[10] for r in herbert] == ["1", "0"]  # first is primary
    assert all(r[7] == "1" for r in herbert)  # both probed


def test_hosts_data_offline_and_consumer_do_not_fan_out():
    # curie is offline, phone is a consumer — even if mapped to several users they
    # stay a single row (never SSH'd as the extras).
    rows = [
        ln.split("\t")
        for ln in _engine_eval(
            "hosts_data",
            env={"TUIMUX_LOGINS": "curie=a,b phone=a,b", "USER": "me"},
            scope="org",
        ).splitlines()
    ]
    assert len([r for r in rows if r[0] == "curie"]) == 1
    assert len([r for r in rows if r[0] == "phone"]) == 1


def test_view_secondary_login_is_a_subrow_with_its_own_sessions():
    base = {"reachable": True, "busy": False, "notmux": False, "awake": False}
    # same host, two logins: primary mduran + secondary gturon (11-col tuples)
    hosts = [
        (
            "herbert",
            False,
            "online",
            "",
            "compute",
            "arnau@ersilia.io",
            "mduran",
            True,
            "#abcdef",
            "mduran",
            True,
        ),
        (
            "herbert",
            False,
            "online",
            "",
            "compute",
            "arnau@ersilia.io",
            "gturon",
            True,
            "#abcdef",
            "gturon",
            False,
        ),
    ]
    a = app.Tuimux()
    a._scope = "org"
    a._hosts = hosts
    a._results = {
        ("herbert", "mduran"): {**base, "sessions": [_session("m1")]},
        ("herbert", "gturon"): {**base, "sessions": [_session("g1")]},
    }
    rows = a._view()
    metas = [m for _, m in rows]
    # each session row carries the login it belongs to
    m1 = next(m for m in metas if m.get("session") == "m1")
    g1 = next(m for m in metas if m.get("session") == "g1")
    assert m1["login"] == "mduran" and g1["login"] == "gturon"
    # the secondary header is a sub-row of the machine (not marked primary): it
    # groups under the host in NAME and carries its login in the SSH USER column.
    user_col = app._COLS.index("user")
    sub = next(
        c for c, m in rows if m.get("action") == "machine" and not m.get("primary")
    )
    assert "herbert" in _cell_text(sub[0])
    assert _cell_text(sub[user_col]) == "gturon"


def test_folder_cell_shows_last_component():
    assert app._folder("~/Documents/GitHub/tuimux") == "tuimux"
    assert app._folder("/var/log/") == "log"
    assert app._folder("~") == "~"
    assert app._folder("/") == "/"
    assert app._folder("") == "~"


def test_tabs_cell_drops_lone_window_count():
    # single window → just the command, no "1" prefix; violet when it's claude
    assert app.Tuimux._tabs_cell("zsh") == (("zsh", "dim"),)
    assert app.Tuimux._tabs_cell("claude") == (("claude", app.VIOLET),)
    # multi-window keeps the count and still tints a claude command
    cell = app.Tuimux._tabs_cell("3  claude")
    assert cell[0] == ("3  ", "dim") and cell[1] == ("claude", app.VIOLET)


def test_waiting_state_blinks_running_and_idle_do_not():
    assert "blink" in app._STATE_STYLE["waiting"]
    assert app.AMBER in app._STATE_STYLE["waiting"]
    assert "blink" not in app._STATE_STYLE["running"]
    assert "blink" not in app._STATE_STYLE["idle"]


# ---- access-based default view (scope filter) ------------------------------
def _acc_hosts():
    # local + reachable-teammate + unreachable-teammate, all 11-col tuples
    return [
        ("mybox", True, "online", "", "compute", "me", "", True, "#111", "me", True),
        (
            "reach",
            False,
            "online",
            "",
            "compute",
            "arnau",
            "",
            True,
            "#222",
            "me",
            True,
        ),
        (
            "noacc",
            False,
            "online",
            "",
            "compute",
            "gemma",
            "",
            True,
            "#333",
            "me",
            True,
        ),
    ]


def _hdr_hosts(rows):
    # the set of hosts that produced a machine-header row (session None); the
    # separator divider has host None and is skipped.
    return {m["host"] for _, m in rows if m.get("session") is None and m.get("host")}


def test_default_view_shows_reachable_hides_unreachable_teammate():
    base = {"busy": False, "notmux": False, "awake": False}
    results = {
        "mybox": {**base, "reachable": True, "sessions": []},
        "reach": {**base, "reachable": True, "sessions": []},
        "noacc": {**base, "reachable": False, "sessions": []},
    }
    # default (mine): local + the teammate box you can reach; the one you can't is hidden
    assert _hdr_hosts(_view_for(_acc_hosts(), results)) == {"mybox", "reach"}
    # org (o): the whole fleet, including the box you can't log into
    assert _hdr_hosts(_view_org(_acc_hosts(), results)) == {"mybox", "reach", "noacc"}


def test_default_view_keeps_own_offline_and_no_ssh():
    base = {"busy": False, "notmux": False, "awake": False}
    hosts = [
        ("mybox", True, "online", "", "compute", "me", "", True, "#111", "me", True),
        (
            "mine-off",
            False,
            "offline",
            "2h",
            "compute",
            "me",
            "",
            False,
            "#222",
            "me",
            True,
        ),
        (
            "mine-nossh",
            False,
            "online",
            "",
            "compute",
            "me",
            "",
            True,
            "#333",
            "me",
            True,
        ),
    ]
    results = {
        "mybox": {**base, "reachable": True, "sessions": []},
        "mine-nossh": {**base, "reachable": False, "sessions": []},  # SSH not working
    }
    # your own machines stay visible whatever their SSH state (offline / no-ssh)
    assert {"mybox", "mine-off", "mine-nossh"} <= _hdr_hosts(_view_for(hosts, results))


def test_own_phone_shown_teammate_phone_hidden_until_o():
    base = {"reachable": True, "busy": False, "notmux": False, "awake": False}
    hosts = [
        ("mybox", True, "online", "", "compute", "me", "", True, "#111", "me", True),
        # your own phone: not SSH-able, but yours → always shown (below the line)
        (
            "myphone",
            False,
            "online",
            "",
            "consumer",
            "me",
            "",
            False,
            "#222",
            "me",
            True,
        ),
        # a teammate's phone: hidden by default, only under "o"
        (
            "herphone",
            False,
            "online",
            "",
            "consumer",
            "gemma",
            "",
            False,
            "#333",
            "me",
            True,
        ),
    ]
    results = {"mybox": {**base, "sessions": []}}
    mine = _hdr_hosts(_view_for(hosts, results))
    assert "mybox" in mine and "myphone" in mine  # your own devices always show
    assert "herphone" not in mine  # someone else's phone is hidden by default
    assert "herphone" in _hdr_hosts(_view_org(hosts, results))  # revealed under "o"


def test_separator_only_under_o_divides_yours_from_others():
    base = {"busy": False, "notmux": False, "awake": False}
    hosts = [
        ("mybox", True, "online", "", "compute", "me", "", True, "#111", "me", True),
        # a teammate box you can't reach — hidden by default, shown only under "o"
        (
            "theirs",
            False,
            "online",
            "",
            "compute",
            "gemma",
            "",
            True,
            "#222",
            "me",
            True,
        ),
    ]
    results = {
        "mybox": {**base, "reachable": True, "sessions": []}
    }  # theirs unreachable
    # default: just your box, no divider (nothing below the line)
    default = _view_for(hosts, results)
    assert _hdr_hosts(default) == {"mybox"}
    assert not any(m.get("action") == "separator" for _, m in default)
    # under "o": the teammate box appears below a single inert divider
    org = _view_org(hosts, results)
    seps = [m for _, m in org if m.get("action") == "separator"]
    assert len(seps) == 1 and seps[0]["host"] is None
    assert _hdr_hosts(org) == {"mybox", "theirs"}


def test_no_ssh_hint_adapts_to_owner():
    base = {"reachable": False, "busy": False, "notmux": False, "awake": False}
    folder = app._COLS.index("folder")
    hosts = [
        ("mybox", True, "online", "", "compute", "me", "", True, "#111", "me", True),
        ("mine-x", False, "online", "", "compute", "me", "", True, "#222", "me", True),
        (
            "their-x",
            False,
            "online",
            "",
            "compute",
            "gemma",
            "",
            True,
            "#333",
            "me",
            True,
        ),
    ]
    results = {
        "mybox": {**base, "reachable": True, "sessions": []},
        "mine-x": {**base, "sessions": []},  # my box, SSH not working
        "their-x": {**base, "sessions": []},  # teammate box, no account for me
    }
    rows = _view_org(hosts, results)
    hint = {
        m["host"]: _cell_text(c[folder])
        for c, m in rows
        if m.get("session") is None and m.get("host")
    }
    assert hint["mine-x"] == "tailscale up --ssh"  # your own box → enable SSH there
    assert "press u" in hint["their-x"]  # teammate box → map a login


def test_running_state_animates_ellipsis():
    base = {"reachable": True, "busy": False, "notmux": False, "awake": False}
    hosts = [
        ("mybox", True, "online", "", "compute", "me", "", True, "#111", "me", True)
    ]
    state_col = app._COLS.index("state")

    def state_word(sess_state, tick):
        a = app.Tuimux()
        a._hosts = hosts
        a._results = _rekey(
            hosts, {"mybox": {**base, "sessions": [_session("job", state=sess_state)]}}
        )
        a._tick = tick
        c = next(c for c, m in a._view() if m.get("session") == "job")
        return _cell_text(c[state_col])

    # "running" grows a cycling ellipsis with the animation tick
    assert state_word("running", 0) == "running"
    assert state_word("running", 1) == "running."
    assert state_word("running", 3) == "running..."
    assert state_word("running", 4) == "running"  # wraps
    # other states are static regardless of tick
    assert state_word("idle", 2) == "idle"


def test_kick_rechecks_failed_hosts_sparingly():
    # A box that refused SSH is retried only every RECHECK; reachable boxes every tick.
    a = app.Tuimux()
    probed = []
    a._probe_one = lambda h, key: probed.append(key)
    a._render = lambda: None
    a._scan_windows = lambda: None
    host = (
        "box",
        False,
        "online",
        "",
        "compute",
        "arnau",
        "",
        True,
        "#fff",
        "me",
        True,
    )
    key = ("box", "me")

    # known-unreachable + just probed → skipped
    a._results[key] = {"reachable": False, "busy": False}
    a._last_probe[key] = app.time.monotonic()
    a._kick([host])
    assert key not in probed

    # known-unreachable but stale → rechecked
    a._last_probe[key] = app.time.monotonic() - app.RECHECK - 1
    a._probing.clear()
    a._kick([host])
    assert key in probed

    # reachable → always re-probed, even right after a probe (sessions change)
    probed.clear()
    a._probing.clear()
    a._results[key] = {"reachable": True, "busy": False}
    a._last_probe[key] = app.time.monotonic()
    a._kick([host])
    assert key in probed


if __name__ == "__main__":
    import inspect

    tests = [
        fn
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and inspect.isfunction(fn)
    ]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} tests passed")
