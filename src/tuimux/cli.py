"""Entry point: no args → launch the dashboard; subcommands → the bash engine."""

import os
import shutil
import subprocess
import sys
from importlib.resources import files


def engine_path() -> str:
    return str(files("tuimux").joinpath("engine.sh"))


def _require_tmux() -> None:
    """tuimux is a front-end for tmux — without it nothing here works. Fail
    loudly with an actionable message rather than letting a downstream command
    error out cryptically."""
    if shutil.which("tmux"):
        return
    sys.exit(
        "tuimux: 'tmux' is not installed or not on PATH.\n"
        "Install it first, e.g.:\n"
        "  macOS:         brew install tmux\n"
        "  Debian/Ubuntu: sudo apt install tmux\n"
        "  Fedora:        sudo dnf install tmux"
    )


def _require_python(version=sys.version_info) -> None:
    """The dashboard needs Python 3.11+ (routines read TOML with `tomllib`).
    Say so plainly instead of crashing on the import. Subcommands go straight
    to the bash engine and keep working on older Pythons."""
    if version >= (3, 11):
        return
    sys.exit(
        f"tuimux: the dashboard needs Python 3.11 or newer "
        f"(this is {version[0]}.{version[1]}: {sys.executable}).\n"
        "Install tuimux into a newer Python, e.g.:\n"
        "  conda create -n tuimux python=3.12 && conda activate tuimux\n"
        "  pip install git+https://github.com/ersilia-os/tuimux"
    )


def _maybe_first_run() -> None:
    """On the very first run, let the engine enable the default features
    (autostart + mouse scroll). A marker file makes it a one-time thing; we check
    it here (cheap) so we only spawn the engine on that first invocation."""
    state = os.path.expanduser(os.environ.get("TUIMUX_STATE_DIR") or "~/.config/tuimux")
    if os.path.exists(os.path.join(state, "initialized")):
        return
    try:
        env = {**os.environ, "TUIMUX_BIN": tuimux_bin()}
        subprocess.run(["bash", engine_path(), "__firstrun"], env=env, timeout=30)
    except Exception:
        pass  # never let setup hiccups block launching


def tuimux_bin() -> str:
    """Absolute path to this tuimux executable.

    Engine-spawned terminals re-invoke tuimux (``exec $TUIMUX_BIN __attach …``)
    in a fresh login shell. If tuimux lives in a venv/conda env that isn't on
    the default PATH, the bare name ``tuimux`` won't resolve there and opening a
    session silently fails. Resolving to an absolute path makes it work anywhere.
    """
    cand = sys.argv[0] or ""
    if (
        os.path.basename(cand) == "tuimux"
        and os.path.isabs(cand)
        and os.path.exists(cand)
    ):
        return cand
    # Console scripts sit next to the interpreter that runs them.
    guess = os.path.join(os.path.dirname(sys.executable), "tuimux")
    if os.path.exists(guess):
        return guess
    return shutil.which("tuimux") or "tuimux"


def main() -> None:
    args = sys.argv[1:]
    _require_tmux()  # nothing tuimux does makes sense without tmux
    _maybe_first_run()  # enable autostart + mouse scroll on the very first run
    if not args:
        _require_python()  # before importing the dashboard, which needs 3.11
        from .app import run

        run()
        return
    # Forward everything else (new/awake/init/doctor + internal __ commands) to
    # the bash engine. TUIMUX_BIN tells the engine its own absolute path, so
    # sessions it spawns into new terminal tabs re-invoke tuimux correctly.
    env = {**os.environ, "TUIMUX_BIN": tuimux_bin()}
    os.execvpe("bash", ["bash", engine_path(), *args], env)
