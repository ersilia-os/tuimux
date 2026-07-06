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


def relaunch_argv() -> list[str]:
    """A concrete argv that re-launches the dashboard and is guaranteed to run
    regardless of how tuimux was started.

    Used by the in-tmux handoff (``detach-client -E``), whose command runs in a
    fresh *non-interactive* shell: that shell sources almost nothing, so its PATH
    may not include the venv/conda env where tuimux lives. The bare name
    ``tuimux`` would fail there with ``command not found`` — and since the client
    has already detached, the user is left staring at a blank, frozen terminal.

    So prefer the resolved console script when it's a real file on disk;
    otherwise fall back to running the module with the current interpreter
    (``python -m tuimux``), which always works because we are, right now, that
    very module under that interpreter."""
    cand = tuimux_bin()
    if os.path.isabs(cand) and os.path.exists(cand):
        return [cand]
    return [sys.executable, "-m", "tuimux"]


def main() -> None:
    args = sys.argv[1:]
    _require_tmux()  # nothing tuimux does makes sense without tmux
    _maybe_first_run()  # enable autostart + mouse scroll on the very first run
    if not args:
        from .app import run

        run()
        return
    # Forward everything else (new/awake/init/doctor + internal __ commands) to
    # the bash engine. TUIMUX_BIN tells the engine its own absolute path, so
    # sessions it spawns into new terminal tabs re-invoke tuimux correctly.
    env = {**os.environ, "TUIMUX_BIN": tuimux_bin()}
    os.execvpe("bash", ["bash", engine_path(), *args], env)
