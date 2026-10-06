"""tuimux — a TUI for tmux sessions across your tailnet.

A Textual dashboard to open, reach, and keep-awake tmux sessions across your
Tailscale tailnet. The bash engine (engine.sh) does discovery, probing, and
actions; the Textual app (app.py) is the front-end.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tuimux")  # single source of truth: pyproject.toml
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"
