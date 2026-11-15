# tuimux — Developer Guide

`tuimux` is a terminal dashboard for tmux sessions across Ersilia's Tailscale tailnet. It follows the Ersilia Python package standard (`ersilia-os/eos-python-package`), with the deviations noted below.

## Working with the user

- **Ask, don't assume.** For any non-trivial decision (approach, naming, a new dependency, an ambiguous case), use `AskUserQuestion` before editing.
- **Plans are mandatory.** Anything beyond a one-line fix or pure read-only investigation must go through plan mode.
- **Surface uncertainty.** When you have multiple reasonable options or are unsure about intent, name them and ask. Don't pick silently.

## Architecture

- `src/tuimux/cli.py` — the `tuimux` entry point. No arguments launches the dashboard; any subcommand is forwarded to the bash engine.
- `src/tuimux/engine.sh` — the bash engine. It does discovery (`tailscale status`), probing over Tailscale SSH, tmux actions, and drives the local terminal emulator. It also implements every subcommand (`attach`, `detach`, `autostart`, `mouse`, `login`, `devices`, `doctor`) and the internal `__*` commands the dashboard calls.
- `src/tuimux/app.py` — the Textual dashboard: the `Tuimux` app, its lifecycle and rendering. It composes the panels below as mixins, and calls the engine through `hosts.py`.
  - `hosts.py` — talking to machines: engine calls, SSH probes, host actions, snapshots. `fmt.py` — display formatting (ages, folders, session names).
  - `theme.py` — palette, Textual theme, stylesheet, and the column grid both tables share.
  - `sessions_view.py` — `_view`, the sessions table's view-model the tests exercise directly. `fleet.py` keeps hosts and probes fresh; `preview.py` mirrors the highlighted pane; `session_actions.py` holds the session keys.
  - `routines_panel.py`, `routine_cells.py`, `routine_actions.py` — the routines panel: rendering and scheduling, its pure cell/status helpers, and its keys. `routines.py` is the registry and run state.
  - `screens.py` — dialogs, the routine form, and the two `DataTable`s.
- `config.example` — the authoritative list of `TUIMUX_*` settings. Keep it in sync when the engine gains or loses a setting.

## Deviations from the package standard

- **The CLI is bash, not Click.** The subcommand surface lives in `engine.sh` on purpose: it drives tmux, ssh and terminal emulators directly and is re-invoked inside spawned shells. Do not port it to Click without discussing it first.

## Code style and quality

- **Run ruff before every commit.** `ruff check` and `ruff format` must both pass. `ruff.toml` is the canonical Ersilia config; `pre-commit install` runs it for you, and CI (`.github/workflows/ci.yml`) runs it and `pytest` on every PR.
- **Keep modules under ~600 lines.** Add a mixin or a helper module rather than growing `app.py` back.
- **Docstrings: NumPy convention.** Write succinct NumPy-style docstrings for every public class, function, and method. For private helpers, only add a docstring when the intent isn't obvious from the name and signature.
- **Keep code, docstrings, and docs aligned.** When a subcommand or setting changes, update the engine's usage header, the README, and `config.example` in the same commit.

## Tests

- Run `pytest`. The suite is hermetic: the engine is stubbed and no network, SSH or running Textual app is needed. Keep it that way.
- Smoke-test the user-facing behaviour; skip exhaustive coverage of internals.

## Dependencies and packaging

- **Pin exact versions.** Use `==X.Y.Z` for every entry in `pyproject.toml`. No floors (`>=`), no ranges.
- **Evaluate every new dependency.** Prefer the standard library; the runtime dependencies are deliberately just `textual` and `rich`.
- **Keep `pyproject.toml` in sync with the package**, including `package-data` for `engine.sh`.

## README guidelines

- **Be brutally brief.** The README answers "what is this and how do I use it". Long-form content belongs in `docs/`.
- **No AI-style filler.** Skip generic Installation / Contributing / License / Acknowledgements boilerplate.
- Keep the About the Ersilia Open Source Initiative footer, with its logo, as the last section.

## Versioning and releases

- **Semantic versioning only.** Versions are `vMAJOR.MINOR.PATCH`.
- **PyPI releases via GitHub Actions.** `.github/workflows/python-publish.yml` publishes on GitHub release. The git tag, the release name, and `[project].version` in `pyproject.toml` must all match.

## Ersilia ecosystem

- Ersilia maintains a set of skills in [`ersilia-skills`](https://github.com/ersilia-os/ersilia-skills). Check it before writing logic a skill already covers.
