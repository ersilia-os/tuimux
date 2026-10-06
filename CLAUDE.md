# tuimux — Developer Guide

`tuimux` is a terminal dashboard for tmux sessions across Ersilia's Tailscale tailnet. It follows the Ersilia Python package standard (`ersilia-os/eos-python-package`), with the deviations noted below.

## Working with the user

- **Ask, don't assume.** For any non-trivial decision (approach, naming, a new dependency, an ambiguous case), use `AskUserQuestion` before editing.
- **Plans are mandatory.** Anything beyond a one-line fix or pure read-only investigation must go through plan mode.
- **Surface uncertainty.** When you have multiple reasonable options or are unsure about intent, name them and ask. Don't pick silently.

## Architecture

- `src/tuimux/cli.py` — the `tuimux` entry point. No arguments launches the dashboard; any subcommand is forwarded to the bash engine.
- `src/tuimux/engine.sh` — the bash engine. It does discovery (`tailscale status`), probing over Tailscale SSH, tmux actions, and drives the local terminal emulator. It also implements every subcommand (`attach`, `detach`, `autostart`, `mouse`, `login`, `devices`, `doctor`) and the internal `__*` commands the dashboard calls.
- `src/tuimux/app.py` — the Textual dashboard. It calls the engine and renders its output; `_view` is the view-model the tests exercise directly.
- `config.example` — the authoritative list of `TUIMUX_*` settings. Keep it in sync when the engine gains or loses a setting.

## Deviations from the package standard

- **The CLI is bash, not Click.** The subcommand surface lives in `engine.sh` on purpose: it drives tmux, ssh and terminal emulators directly and is re-invoked inside spawned shells. Do not port it to Click without discussing it first.

## Code style and quality

- **Run ruff before every commit.** `ruff check` and `ruff format` must both pass.
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
